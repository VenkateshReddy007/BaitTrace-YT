import argparse
import logging
import os
import sys
import time
import random
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Any

from dotenv import load_dotenv
load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    print("FATAL: SUPABASE_URL and SUPABASE_KEY must be set in environment or .env file.")
    sys.exit(1)

from youtube_comment_downloader import YoutubeCommentDownloader
from supabase import create_client, Client

from discovery import DiscoveredVideo, generate_dynamic_queries, search_youtube
from parser import extract_indicators, normalize_text
from brain import (
    evaluate_comment, evaluate_batch, evaluate_handle_campaign,
    remaining_call_budget, explain_promoted_lead,
    reset_jev_sweep_cost, get_jev_sweep_cost, get_jev_sweep_calls,
)
from scoring import compute_campaign_score, author_burner_score
from enricher import enrich_channel
from pivot import pivot_on_handle
from heuristics import should_escalate
from config import (
    DEFAULT_LIMIT_PER_QUERY,
    DEFAULT_MAX_COMMENTS,
    DEFAULT_QUERY_SAMPLE_SIZE,
    DEFAULT_LLM_CALL_BUDGET,
    PIVOT_THRESHOLD,
    PIVOT_MAX_HANDLES_PER_RUN,
    DEFAULT_SWEEP_INTERVAL_MINUTES,
    CLASSIFIER_PROVIDER,
    JEV_LLM_CALL_BUDGET,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("BaitTrace-Pipeline")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
_BATCH_CHUNK_SIZE = 500


def format_supabase_error(e: Exception) -> str:
    """Extracts granular PostgREST error details (code, message, details, hint)
    from supabase-py exceptions for immediate diagnosability.
    """
    details = []
    for attr in ("code", "message", "details", "hint"):
        val = getattr(e, attr, None)
        if val:
            details.append(f"{attr}={val}")
    if hasattr(e, "response") and e.response is not None:
        try:
            details.append(f"status_code={getattr(e.response, 'status_code', None)}")
            details.append(f"response_text={getattr(e.response, 'text', None)}")
        except Exception:
            pass
    if hasattr(e, "args") and e.args:
        details.append(f"args={e.args}")
    if not details:
        details.append(str(e))
    return " | ".join(str(d) for d in details)


def build_sighting_record(
    target: str,
    indicator_type: str,
    raw_value: str,
    cid: str,
    author: str,
    author_channel: str,
    video_id: str,
    video_title: str,
    video_url: str,
    lane: str,
    is_reply: bool,
    raw_text: str,
    posted_time: str,
    llm_is_fraud: bool,
    llm_role: str,
    llm_confidence: float,
    llm_reason: str,
    heuristic_score: float,
    channel_meta: Dict[str, Any],
    llm_status: str = "PENDING_RETRY",
    is_creator_author: bool = False
) -> Dict[str, Any]:
    """Constructs a candidate_sightings payload matching the database schema."""
    return {
        "handle_norm": target,
        "indicator_type": indicator_type,
        "raw_value": raw_value,
        "comment_id": cid,
        "author": author,
        "author_channel_id": author_channel,
        "video_id": video_id,
        "video_title": video_title,
        "video_url": video_url,
        "lane": lane,
        "is_reply": is_reply,
        "comment_text": raw_text,
        "posted_time": posted_time,
        "llm_is_fraud": llm_is_fraud,
        "llm_role": llm_role,
        "llm_confidence": llm_confidence,
        "llm_reason": llm_reason,
        "heuristic_score": heuristic_score,
        "channel_meta": channel_meta,
        "llm_status": llm_status,
        "is_creator_author": is_creator_author,
    }


def build_handle_payload(
    handle_norm: str,
    indicator_type: str,
    campaign: Dict[str, Any],
    tier: str,
    rep_video_url: str
) -> Dict[str, Any]:
    """Constructs a handles table payload matching the database schema."""
    return {
        "handle_norm": handle_norm,
        "indicator_type": indicator_type,
        "distinct_video_count": campaign["distinct_video_count"],
        "distinct_author_count": campaign["distinct_author_count"],
        "campaign_score": campaign["campaign_score"],
        "tier": tier,
        "status": campaign["status"],
        "evidence": campaign["evidence"],
        "representative_video_url": rep_video_url,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }


def build_lead_payload(
    handle_norm: str,
    indicator_type: str,
    campaign: Dict[str, Any],
    tier: str,
    rep_video_url: str
) -> Dict[str, Any]:
    """Constructs an actionable_leads table payload matching the database schema."""
    return {
        "handle_norm": handle_norm,
        "indicator_type": indicator_type,
        "distinct_video_count": campaign["distinct_video_count"],
        "distinct_author_count": campaign["distinct_author_count"],
        "campaign_score": campaign["campaign_score"],
        "tier": tier,
        "status": campaign["status"],
        "evidence": campaign["evidence"],
        "representative_video_url": rep_video_url,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }


class PipelineTelemetry:
    def __init__(self):
        self.comments_scanned = 0
        self.indicators_extracted = 0
        self.indicators_rejected = 0
        self.indicators_rejected_heuristic = 0
        self.sightings_written = 0
        self.llm_calls_made = 0
        self.llm_evaluated = 0
        self.llm_pending_retry = 0
        self.handles_scored = 0
        self.promotions_by_tier = defaultdict(int)
        # Auto-pivot telemetry
        self.auto_pivot_handles_selected = 0
        self.auto_pivot_videos_discovered = 0
        self.auto_pivot_sightings_staged = 0
        # Jev cost tracking
        self.jev_spend = 0.0
        self.jev_calls = 0
        # Gemini explain calls
        self.gemini_explain_calls = 0
        # Dynamic query generation
        self.llm_queries_used = 0

    def print_summary(self):
        active_provider = os.environ.get("CLASSIFIER_PROVIDER", CLASSIFIER_PROVIDER)
        print("\n" + "=" * 65)
        print("         BAITTRACE v2 PIPELINE OBSERVABILITY MATRIX")
        print("=" * 65)
        print(f"  Classifier Provider:              {active_provider:>6}")
        print(f"  Total Comments Scanned:           {self.comments_scanned:>6}")
        print(f"  Indicators Extracted (Raw):       {self.indicators_extracted:>6}")
        print(f"  Indicators Rejected (Stopwords):  {self.indicators_rejected:>6}")
        print(f"  Indicators Rejected (Heuristic Gate): {self.indicators_rejected_heuristic:>3}")
        print(f"  Candidate Sightings Staged:       {self.sightings_written:>6}")
        print(f"  LLM Inference Invocations:        {self.llm_calls_made:>6}")
        print(f"  LLM Status: EVALUATED:            {self.llm_evaluated:>6}")
        print(f"  LLM Status: PENDING_RETRY:        {self.llm_pending_retry:>6}")
        print(f"  Campaign Handles Scored:          {self.handles_scored:>6}")
        if active_provider == "jev":
            print("-" * 65)
            print("  JEV COST TRACKING:")
            print(f"    Jev Spend This Sweep:           ${self.jev_spend:.4f}")
            print(f"    Jev Calls This Sweep:           {self.jev_calls:>6}")
            print(f"    Gemini Explain Calls:           {self.gemini_explain_calls:>6}")
        print("-" * 65)
        print("  PROMOTIONS BY CAMPAIGN TIER:")
        print(f"    - CONFIRMED (High Threat):      {self.promotions_by_tier['CONFIRMED']:>6}")
        print(f"    - PROBABLE (Needs Review):      {self.promotions_by_tier['PROBABLE']:>6}")
        print(f"    - WATCH (Staged Evidence):      {self.promotions_by_tier['WATCH']:>6}")
        print(f"    - DISCARD (Suppressed):         {self.promotions_by_tier['DISCARD']:>6}")
        print("-" * 65)
        print("  AUTO-PIVOT:")
        print(f"    Handles Selected for Pivot:     {self.auto_pivot_handles_selected:>6}")
        print(f"    New Videos Discovered:          {self.auto_pivot_videos_discovered:>6}")
        print(f"    New Sightings Staged:           {self.auto_pivot_sightings_staged:>6}")
        print("-" * 65)
        print("  DYNAMIC DISCOVERY:")
        print(f"    LLM-Generated Queries Used This Sweep: {self.llm_queries_used:>2}")
        print("=" * 65 + "\n")


class StateManager:
    def __init__(self):
        logger.info("Syncing state with Supabase...")
        try:
            res_v = supabase.table("seen_videos").select("video_id").execute()
            self.seen_videos = {r["video_id"] for r in res_v.data}
        except Exception:
            self.seen_videos = set()
        
        try:
            res_c = supabase.table("seen_comments").select("comment_id").execute()
            self.seen_comments = {r["comment_id"] for r in res_c.data}
        except Exception:
            self.seen_comments = set()

        self._comment_buffer: List[Dict[str, Any]] = []
        self._sightings_buffer: List[Dict[str, Any]] = []
        self._seen_sightings_keys: Set[str] = set()

    def is_video_seen(self, video_id: str) -> bool:
        return video_id in self.seen_videos

    def mark_video_seen(self, video_id: str):
        if video_id not in self.seen_videos:
            try:
                supabase.table("seen_videos").upsert({"video_id": video_id}).execute()
            except Exception as e:
                logger.error(f"Failed to upsert seen_video ({video_id}): {format_supabase_error(e)}")
            self.seen_videos.add(video_id)

    def is_comment_seen(self, comment_id: str) -> bool:
        return comment_id in self.seen_comments

    def mark_comment_seen(self, comment_id: str):
        if comment_id not in self.seen_comments:
            self.seen_comments.add(comment_id)
            self._comment_buffer.append({"comment_id": comment_id})

    def buffer_sighting(self, sighting: Dict[str, Any]):
        key = f"{sighting.get('handle_norm')}|{sighting.get('comment_id')}"
        if key not in self._seen_sightings_keys:
            self._seen_sightings_keys.add(key)
            self._sightings_buffer.append(sighting)

    def flush_comments(self):
        if not self._comment_buffer:
            return
        for i in range(0, len(self._comment_buffer), _BATCH_CHUNK_SIZE):
            chunk = self._comment_buffer[i:i + _BATCH_CHUNK_SIZE]
            try:
                supabase.table("seen_comments").upsert(chunk).execute()
            except Exception as e:
                logger.error(f"Failed to flush seen_comments batch ({len(chunk)} items): {format_supabase_error(e)}")
        self._comment_buffer.clear()

    def flush_sightings(self, telemetry: PipelineTelemetry):
        if not self._sightings_buffer:
            return
        for i in range(0, len(self._sightings_buffer), _BATCH_CHUNK_SIZE):
            chunk = self._sightings_buffer[i:i + _BATCH_CHUNK_SIZE]
            try:
                supabase.table("candidate_sightings").upsert(chunk).execute()
                telemetry.sightings_written += len(chunk)
            except Exception as e:
                err_detail = format_supabase_error(e)
                logger.error(f"CRITICAL: Failed to flush candidate_sightings batch ({len(chunk)} items): {err_detail}")
                raise RuntimeError(f"candidate_sightings write failed: {err_detail}") from e
        self._sightings_buffer.clear()


def reprocess_pending_sightings(budget: int = 15):
    """Fetch all PENDING_RETRY sightings from Supabase, evaluate them within the
    given LLM call budget, and upsert the updated verdicts back to the DB.
    """
    logger.info(f"♻️  reprocess_pending: fetching PENDING_RETRY sightings (budget={budget})...")
    try:
        res = supabase.table("candidate_sightings") \
            .select("id,handle_norm,comment_id,comment_text,video_title,heuristic_score") \
            .eq("llm_status", "PENDING_RETRY") \
            .order("heuristic_score", desc=True) \
            .limit(budget * 20) \
            .execute()
        rows = res.data or []
    except Exception as e:
        logger.error(f"reprocess_pending: failed to fetch rows: {format_supabase_error(e)}")
        return

    if not rows:
        logger.info("♻️  reprocess_pending: no PENDING_RETRY sightings found.")
        return

    logger.info(f"♻️  reprocess_pending: {len(rows)} PENDING_RETRY sightings queued.")

    # Reshape to evaluate_batch format
    items = [
        {
            "comment_id": row["comment_id"],
            "video_title": row.get("video_title", ""),
            "comment_text": row.get("comment_text", ""),
            "target": row["handle_norm"],
            "heuristic_score": row.get("heuristic_score", 0.0),
        }
        for row in rows
    ]

    batch_results = evaluate_batch(items, budget=budget)

    evaluated = 0
    still_pending = 0
    for result in batch_results:
        verdict = result["verdict"]
        status = verdict.get("llm_status", "PENDING_RETRY")
        update_payload = {
            "llm_is_fraud": verdict.get("is_fraud", False),
            "llm_role": verdict.get("role", "NEUTRAL"),
            "llm_confidence": verdict.get("confidence", 0.0),
            "llm_reason": verdict.get("reason", ""),
            "llm_status": status,
        }
        try:
            supabase.table("candidate_sightings") \
                .update(update_payload) \
                .eq("comment_id", result["comment_id"]) \
                .eq("handle_norm", result["target"]) \
                .execute()
            if status == "EVALUATED":
                evaluated += 1
            else:
                still_pending += 1
        except Exception as e:
            logger.error(f"reprocess_pending: upsert failed for ({result['comment_id']}, {result['target']}): {format_supabase_error(e)}")

    logger.info(f"♻️  reprocess_pending complete: {evaluated} EVALUATED, {still_pending} still PENDING_RETRY.")


def _fetch_historical_sightings(handle_norms: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """Batch-fetch all historical sightings from candidate_sightings for a set
    of handles in a single IN(...) query, returning {handle_norm: [rows]}.
    """
    if not handle_norms:
        return {}

    historical: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    try:
        res = supabase.table("candidate_sightings") \
            .select("*") \
            .in_("handle_norm", handle_norms) \
            .execute()
        for row in (res.data or []):
            historical[row["handle_norm"]].append(row)
    except Exception as e:
        logger.warning(f"Historical sightings fetch failed: {format_supabase_error(e)}")

    return historical


def run_pipeline(
    limit_per_query: int = DEFAULT_LIMIT_PER_QUERY,
    max_comments: int = DEFAULT_MAX_COMMENTS,
    query_type: str = "ALL",
    pivot_mode: bool = False,
    llm_call_budget: int = DEFAULT_LLM_CALL_BUDGET,
    query_sample_size: int = DEFAULT_QUERY_SAMPLE_SIZE,
    auto_pivot: bool = True,
):
    try:
        _run_pipeline_internal(
            limit_per_query=limit_per_query,
            max_comments=max_comments,
            query_type=query_type,
            pivot_mode=pivot_mode,
            llm_call_budget=llm_call_budget,
            query_sample_size=query_sample_size,
            auto_pivot=auto_pivot,
        )
    except RuntimeError as e:
        if "candidate_sightings" in str(e):
            logger.critical("STOPPING: candidate_sightings writes are failing, evidence is not being persisted.")
            return
        raise


def _run_pipeline_internal(
    limit_per_query: int = DEFAULT_LIMIT_PER_QUERY,
    max_comments: int = DEFAULT_MAX_COMMENTS,
    query_type: str = "ALL",
    pivot_mode: bool = False,
    llm_call_budget: int = DEFAULT_LLM_CALL_BUDGET,
    query_sample_size: int = DEFAULT_QUERY_SAMPLE_SIZE,
    auto_pivot: bool = True,
):
    state = StateManager()
    downloader = YoutubeCommentDownloader()
    telemetry = PipelineTelemetry()

    # Reset Jev per-sweep cost tracker
    reset_jev_sweep_cost()

    active_provider = os.environ.get("CLASSIFIER_PROVIDER", CLASSIFIER_PROVIDER)

    # If using Jev, override budget with the generous Jev budget
    if active_provider == "jev":
        llm_call_budget = max(llm_call_budget, JEV_LLM_CALL_BUDGET)

    # Print effective config at start of every run
    print(f"[CONFIG] classifier={active_provider} limit={limit_per_query} max_comments={max_comments} "
          f"query_sample={query_sample_size} llm_budget={llm_call_budget} "
          f"pivot_threshold={PIVOT_THRESHOLD} auto_pivot={auto_pivot}")

    fresh_videos: List[DiscoveredVideo] = []

    # --- Phase 1: Pivot Mode (Seed-to-Campaign Expansion) ---
    if pivot_mode:
        logger.info("⚡ Executing Priority Pivot Sweep on Confirmed & Probable Targets...")
        try:
            res_handles = supabase.table("handles").select("handle_norm, campaign_score").gte("campaign_score", 50).execute()
            handles_to_pivot = res_handles.data or []
            if not handles_to_pivot:
                res_all = supabase.table("handles").select("handle_norm, campaign_score").execute()
                handles_to_pivot = res_all.data or []
            for h in handles_to_pivot:
                p_videos = pivot_on_handle(h["handle_norm"])
                for pv in p_videos:
                    if not state.is_video_seen(pv.video_id):
                        fresh_videos.append(pv)
        except Exception as e:
            logger.warning(f"Pivot mode query failed: {e}")

    # --- Phase 2: Autonomous Multi-Lane Discovery ---
    logger.info(f"Initiating dynamic discovery sweep ({query_type}) across lanes...")
    
    recent_patterns = []
    try:
        res = supabase.table("candidate_sightings").select(
            "target, scam_type, raw_text, lane"
        ).eq("llm_is_fraud", True).order("created_at", desc=True).limit(20).execute()
        if res.data:
            recent_patterns = [
                {
                    "handle_norm": r["target"],
                    "scam_type": r["scam_type"],
                    "comment_text": r.get("raw_text", ""),
                    "lane": r.get("lane", "UNKNOWN")
                }
                for r in res.data
            ]
    except Exception as e:
        logger.warning(f"Failed to fetch recent patterns for LLM queries: {e}")

    dynamic_queries = generate_dynamic_queries(recent_patterns, supabase)

    for lane, query_dicts in dynamic_queries.items():
        if query_type != "ALL" and lane != query_type:
            continue

        # §2: Sample N queries per lane from the expanded pool
        if len(query_dicts) > query_sample_size:
            sampled_queries = random.sample(query_dicts, query_sample_size)
        else:
            sampled_queries = query_dicts

        for qd in sampled_queries:
            q = qd["query"]
            if qd.get("source") == "LLM_GENERATED":
                telemetry.llm_queries_used += 1
                
            sort_by_views = (lane == "VICTIM_RICH")
            found = search_youtube(q, fetch_depth=100, lane=lane, sort_by_views=sort_by_views)

            # §2: For LURE lane, prioritize candidates with recent year in title
            if lane == "LURE" and found:
                recent = [v for v in found if ("2026" in v.title or "2025" in v.title)]
                rest = [v for v in found if v not in recent]
                found = recent + rest

            fresh_for_this_query = 0
            for v in found:
                if not state.is_video_seen(v.video_id):
                    fresh_videos.append(v)
                    fresh_for_this_query += 1
                if fresh_for_this_query >= limit_per_query:
                    break

            logger.info(f"[{lane}] Found {fresh_for_this_query} net-new videos for query '{q}'")

    fresh_videos = fresh_videos[:50]
    logger.info(f"Target pool assembled: {len(fresh_videos)} unscanned videos.")

    # In-memory tracking of all sightings per handle during this sweep
    handle_sightings_map: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    # Accumulator for deferred batch LLM evaluation
    # Each entry: {comment_id, video_title, comment_text, target, heuristic_score, _sighting_key}
    pending_llm_items: List[Dict[str, Any]] = []
    # Map (handle_norm, comment_id) -> sighting_record for post-eval update
    sighting_key_map: Dict[str, Dict[str, Any]] = {}

    # --- Phase 3: Extraction (NO LLM calls — collect all indicators first) ---
    logger.info("📥 Phase 3: Extracting indicators from all videos (LLM deferred)...")
    for idx, video in enumerate(fresh_videos, 1):
        logger.info(f"[{idx}/{len(fresh_videos)}] ({video.lane}) Scanning: '{video.title[:45]}...'")

        try:
            comment_gen = downloader.get_comments_from_url(video.url, sort_by=1)
        except Exception as e:
            logger.warning(f"Comment fetch failed for {video.url}: {e}")
            continue

        parsed_count = 0
        try:
            for comment in comment_gen:
                if parsed_count >= max_comments:
                    break

                cid = comment.get("cid")
                if not cid or state.is_comment_seen(cid):
                    continue

                state.mark_comment_seen(cid)
                parsed_count += 1
                telemetry.comments_scanned += 1

                raw_text = comment.get("text", "")
                author = comment.get("author", "Unknown")
                author_channel = comment.get("channel", "")
                is_reply = comment.get("reply", False)
                posted_time = comment.get("time", "")

                clean_text = normalize_text(raw_text)
                indicators = extract_indicators(clean_text)

                if not indicators:
                    continue

                telemetry.indicators_extracted += len(indicators)

                # §1: Heuristic gate — filter indicators before LLM
                pre_filter_count = len(indicators)
                indicators = [ind for ind in indicators if should_escalate(ind, clean_text)]
                rejected_count = pre_filter_count - len(indicators)
                telemetry.indicators_rejected_heuristic += rejected_count

                if not indicators:
                    continue

                # Channel metadata enrichment (cached)
                channel_meta = enrich_channel(author_channel) if author_channel else {}

                # §6: Determine is_creator_author by comparing comment author
                # channel_id against the video's own channel_id
                is_creator = False
                if author_channel and video.channel_id:
                    is_creator = (author_channel == video.channel_id)

                for ind in indicators:
                    target = ind.normalized_value
                    # Stage a PENDING_RETRY sighting immediately (will be updated after batch eval)
                    sighting_record = build_sighting_record(
                        target=target,
                        indicator_type=ind.indicator_type,
                        raw_value=ind.raw_value,
                        cid=cid,
                        author=author,
                        author_channel=author_channel,
                        video_id=video.video_id,
                        video_title=video.title,
                        video_url=video.url,
                        lane=video.lane,
                        is_reply=is_reply,
                        raw_text=raw_text,
                        posted_time=posted_time,
                        llm_is_fraud=False,
                        llm_role="NEUTRAL",
                        llm_confidence=0.0,
                        llm_reason="Awaiting batch LLM evaluation",
                        heuristic_score=ind.confidence,
                        channel_meta=channel_meta,
                        llm_status="PENDING_RETRY",
                        is_creator_author=is_creator,
                    )

                    state.buffer_sighting(sighting_record)
                    sk = f"{target}|{cid}"
                    sighting_key_map[sk] = sighting_record

                    pending_llm_items.append({
                        "comment_id": cid,
                        "video_title": video.title,
                        "comment_text": clean_text,
                        "target": target,
                        "heuristic_score": ind.confidence,
                    })

        except Exception as e:
            logger.warning(f"Comment iteration error on {video.url}: {e}")
            continue

        state.mark_video_seen(video.video_id)
        state.flush_comments()

    # Flush all PENDING_RETRY sightings to DB before LLM evaluation
    state.flush_sightings(telemetry)

    # --- Phase 3.5: Deferred Batch LLM Evaluation ---
    logger.info(f"🧠 Phase 3.5: Batch LLM evaluation ({len(pending_llm_items)} indicators, budget={llm_call_budget} calls)...")
    batch_results = evaluate_batch(pending_llm_items, budget=llm_call_budget)
    telemetry.llm_calls_made = len({r["comment_id"] for r in batch_results})  # unique comment calls

    # Apply verdicts: update in-memory sightings and upsert back to DB
    updates_to_flush: List[Dict[str, Any]] = []
    for result in batch_results:
        verdict = result["verdict"]
        target = result["target"]
        cid = result["comment_id"]
        llm_status = verdict.get("llm_status", "PENDING_RETRY")
        llm_role = verdict.get("role", "NEUTRAL")
        llm_is_fraud = verdict.get("is_fraud", False)

        sk = f"{target}|{cid}"
        if sk in sighting_key_map:
            sighting_key_map[sk].update({
                "llm_is_fraud": llm_is_fraud,
                "llm_role": llm_role,
                "llm_confidence": verdict.get("confidence", 0.0),
                "llm_reason": verdict.get("reason", ""),
                "llm_status": llm_status,
            })
            updates_to_flush.append(sighting_key_map[sk])
            handle_sightings_map[target].append(sighting_key_map[sk])

        if llm_status == "EVALUATED":
            telemetry.llm_evaluated += 1
            if llm_role == "RECRUITER":
                logger.info(f"🚨 Sighted RECRUITER indicator: {target} ({verdict.get('reason')})")
        else:
            telemetry.llm_pending_retry += 1

    # Upsert evaluated sightings back to DB
    for i in range(0, len(updates_to_flush), _BATCH_CHUNK_SIZE):
        chunk = updates_to_flush[i:i + _BATCH_CHUNK_SIZE]
        try:
            supabase.table("candidate_sightings").upsert(
                chunk, 
                on_conflict="handle_norm,comment_id"
            ).execute()
        except Exception as e:
            err_detail = format_supabase_error(e)
            logger.error(f"CRITICAL: Failed to upsert evaluated sightings batch ({len(chunk)} items): {err_detail}")
            raise RuntimeError(f"candidate_sightings write failed: {err_detail}") from e

    # --- Phase 4: Campaign Scoring & Promotion (with historical merge) ---
    logger.info("⚖️ Evaluating campaign-level threat scores for all sighted handles...")

    # §4: Fetch historical sightings for ALL handles touched this run in a single batch query
    handles_this_run = list(handle_sightings_map.keys())
    historical_map = _fetch_historical_sightings(handles_this_run)

    for handle_norm, current_sightings in handle_sightings_map.items():
        telemetry.handles_scored += 1

        # §4: Merge historical sightings, deduplicating by comment_id
        current_comment_ids = {s.get("comment_id") for s in current_sightings}
        historical_rows = historical_map.get(handle_norm, [])
        for hist_row in historical_rows:
            if hist_row.get("comment_id") not in current_comment_ids:
                current_sightings.append(hist_row)
                current_comment_ids.add(hist_row.get("comment_id"))

        campaign = compute_campaign_score(handle_norm, current_sightings)

        # §7: For handles with >=3 total sightings, call evaluate_handle_campaign
        if len(current_sightings) >= 3:
            try:
                campaign_verdict = evaluate_handle_campaign(handle_norm, current_sightings[:10])
                if campaign_verdict.get("llm_status") == "EVALUATED":
                    # Fold LLM campaign verdict into evidence as a bounded component
                    llm_camp_conf = campaign_verdict.get("confidence", 0.0)
                    campaign_boost = min(10.0, llm_camp_conf * 10.0)
                    campaign["campaign_score"] = min(100.0, campaign["campaign_score"] + campaign_boost)
                    campaign["evidence"]["llm_campaign_verdict"] = {
                        "is_fraud": campaign_verdict.get("is_fraud", False),
                        "role": campaign_verdict.get("role", "NEUTRAL"),
                        "confidence": llm_camp_conf,
                        "reason": campaign_verdict.get("reason", ""),
                    }
                    # Recalculate tier after boosted score
                    from scoring import THRESHOLD_CONFIRMED, THRESHOLD_PROBABLE, THRESHOLD_WATCH
                    score = campaign["campaign_score"]
                    n_vids = campaign["distinct_video_count"]
                    n_auth = campaign["distinct_author_count"]
                    is_multi = (n_vids >= 2 and n_auth >= 2)
                    has_recruiter = campaign["evidence"].get("roles_summary", {}).get("RECRUITER", 0) >= 1
                    is_victim_maj = campaign["evidence"].get("roles_summary", {}).get("VICTIM_REPORT", 0) > (len(current_sightings) / 2.0)

                    if is_multi and not is_victim_maj and (score >= THRESHOLD_CONFIRMED or (is_multi and has_recruiter)):
                        campaign["tier"] = "CONFIRMED"
                        campaign["status"] = "CONFIRMED"
                    elif score >= THRESHOLD_PROBABLE:
                        campaign["tier"] = "PROBABLE"
                        campaign["status"] = "NEEDS_REVIEW"

                    logger.info(f"🧠 Campaign LLM verdict for {handle_norm}: fraud={campaign_verdict.get('is_fraud')}, boost=+{campaign_boost:.1f}")
            except Exception as e:
                logger.warning(f"evaluate_handle_campaign failed for {handle_norm}: {e}")

        tier = campaign["tier"]
        telemetry.promotions_by_tier[tier] += 1

        first_sighting = current_sightings[0]
        indicator_type = first_sighting.get("indicator_type", "TELEGRAM")
        rep_video_url = first_sighting.get("video_url", "")

        # 1. Update/Upsert handles table
        handle_payload = build_handle_payload(
            handle_norm=handle_norm,
            indicator_type=indicator_type,
            campaign=campaign,
            tier=tier,
            rep_video_url=rep_video_url
        )
        try:
            supabase.table("handles").upsert(handle_payload).execute()
        except Exception as e:
            logger.error(f"Failed to upsert handles row ({handle_norm}): {format_supabase_error(e)}")

        # 2. Promote to actionable_leads ONLY if CONFIRMED or PROBABLE
        if tier in ("CONFIRMED", "PROBABLE"):
            logger.info(f"⭐ PROMOTED TO LEADS: [{tier}] {handle_norm} (Score: {campaign['campaign_score']})")

            # §4: Generate Gemini explanation ONLY for newly promoted leads
            if active_provider == "jev" and current_sightings:
                rep_sighting = current_sightings[0]
                try:
                    reason_text = explain_promoted_lead(rep_sighting)
                    telemetry.gemini_explain_calls += 1
                    # Update the campaign evidence with the explanation
                    if reason_text:
                        campaign["evidence"]["gemini_explanation"] = reason_text
                except Exception as e:
                    logger.warning(f"explain_promoted_lead failed for {handle_norm}: {e} — promotion proceeds")
                    # A failed explanation must NEVER block or reverse a promotion

            lead_payload = build_lead_payload(
                handle_norm=handle_norm,
                indicator_type=indicator_type,
                campaign=campaign,
                tier=tier,
                rep_video_url=rep_video_url
            )
            try:
                supabase.table("actionable_leads").upsert(lead_payload).execute()
            except Exception as e:
                logger.error(f"Failed to upsert actionable_leads row ({handle_norm}): {format_supabase_error(e)}")

    # --- Phase 4.5: Auto-Pivot (§3) ---
    if auto_pivot and not pivot_mode:
        logger.info("🔄 Phase 4.5: Auto-Pivot on high-scoring handles...")
        try:
            res_pivot = supabase.table("handles") \
                .select("handle_norm, campaign_score") \
                .in_("tier", ["WATCH", "PROBABLE"]) \
                .gte("campaign_score", PIVOT_THRESHOLD) \
                .order("campaign_score", desc=True) \
                .limit(PIVOT_MAX_HANDLES_PER_RUN) \
                .execute()
            pivot_candidates = res_pivot.data or []
        except Exception as e:
            logger.warning(f"Auto-pivot query failed: {e}")
            pivot_candidates = []

        telemetry.auto_pivot_handles_selected = len(pivot_candidates)

        for ph in pivot_candidates:
            handle = ph["handle_norm"]
            logger.info(f"🔄 Auto-pivoting on: {handle} (score={ph['campaign_score']})")
            try:
                pivot_videos = pivot_on_handle(handle)
                new_pivot_videos = [pv for pv in pivot_videos if not state.is_video_seen(pv.video_id)]
                telemetry.auto_pivot_videos_discovered += len(new_pivot_videos)

                # Process pivot-discovered videos through the SAME pipeline
                # They compete for the SAME llm_call_budget (remaining budget after main sweep)
                for pv in new_pivot_videos[:limit_per_query]:
                    logger.info(f"  [PIVOT] Scanning: '{pv.title[:45]}...'")
                    try:
                        pv_comments = downloader.get_comments_from_url(pv.url, sort_by=1)
                    except Exception:
                        continue

                    pv_parsed = 0
                    try:
                        for comment in pv_comments:
                            if pv_parsed >= max_comments:
                                break
                            cid = comment.get("cid")
                            if not cid or state.is_comment_seen(cid):
                                continue
                            state.mark_comment_seen(cid)
                            pv_parsed += 1

                            raw_text = comment.get("text", "")
                            author = comment.get("author", "Unknown")
                            author_channel = comment.get("channel", "")
                            is_reply = comment.get("reply", False)
                            posted_time = comment.get("time", "")

                            clean_text = normalize_text(raw_text)
                            indicators = extract_indicators(clean_text)
                            if not indicators:
                                continue

                            indicators = [ind for ind in indicators if should_escalate(ind, clean_text)]
                            if not indicators:
                                continue

                            channel_meta = enrich_channel(author_channel) if author_channel else {}
                            is_creator = False
                            if author_channel and pv.channel_id:
                                is_creator = (author_channel == pv.channel_id)

                            for ind in indicators:
                                target = ind.normalized_value
                                sighting_record = build_sighting_record(
                                    target=target,
                                    indicator_type=ind.indicator_type,
                                    raw_value=ind.raw_value,
                                    cid=cid,
                                    author=author,
                                    author_channel=author_channel,
                                    video_id=pv.video_id,
                                    video_title=pv.title,
                                    video_url=pv.url,
                                    lane="PIVOT",
                                    is_reply=is_reply,
                                    raw_text=raw_text,
                                    posted_time=posted_time,
                                    llm_is_fraud=False,
                                    llm_role="NEUTRAL",
                                    llm_confidence=0.0,
                                    llm_reason="Auto-pivot sighting — awaiting LLM",
                                    heuristic_score=ind.confidence,
                                    channel_meta=channel_meta,
                                    llm_status="PENDING_RETRY",
                                    is_creator_author=is_creator,
                                )
                                state.buffer_sighting(sighting_record)
                                telemetry.auto_pivot_sightings_staged += 1
                    except Exception as e:
                        logger.warning(f"  [PIVOT] Comment iteration error: {e}")
                        continue

                    state.mark_video_seen(pv.video_id)
                    state.flush_comments()

            except Exception as e:
                logger.warning(f"Auto-pivot failed for {handle}: {e}")

        # Flush pivot sightings
        state.flush_sightings(telemetry)

    # --- Phase 5: Observability Summary ---
    # Capture Jev cost data into telemetry before printing
    telemetry.jev_spend = get_jev_sweep_cost()
    telemetry.jev_calls = get_jev_sweep_calls()
    telemetry.print_summary()


def main():
    parser = argparse.ArgumentParser(description="BaitTrace Fraud Discovery Sensor v2")
    parser.add_argument("--once", action="store_true", help="Run a single sweep")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT_PER_QUERY, help="Max videos to process per query")
    parser.add_argument("--max-comments", type=int, default=DEFAULT_MAX_COMMENTS, help="Max comments to parse per video")
    parser.add_argument("--query-type", type=str, default="ALL", choices=["ALL", "VICTIM_RICH", "LURE", "EXPOSURE"])
    parser.add_argument(
        "--pivot", "--rescan-known-handles",
        dest="pivot",
        action="store_true",
        help="Enable seed-to-campaign pivot expansion / rescan on known handles from handles table"
    )
    parser.add_argument(
        "--llm-call-budget",
        type=int,
        default=DEFAULT_LLM_CALL_BUDGET,
        help="Max LLM calls per sweep (default 15; each call evaluates all targets in one comment)"
    )
    parser.add_argument(
        "--reprocess-pending",
        action="store_true",
        help="Retry all PENDING_RETRY sightings in the DB using today's remaining quota, then exit"
    )
    parser.add_argument(
        "--query-sample-size",
        type=int,
        default=DEFAULT_QUERY_SAMPLE_SIZE,
        help="Number of queries to sample per lane each sweep (default 10)"
    )
    parser.add_argument(
        "--no-auto-pivot",
        action="store_true",
        help="Disable automatic pivot expansion at end of sweep"
    )

    args = parser.parse_args()

    if args.reprocess_pending:
        reprocess_pending_sightings(budget=args.llm_call_budget)
        return

    if args.once:
        run_pipeline(
            limit_per_query=args.limit,
            max_comments=args.max_comments,
            query_type=args.query_type,
            pivot_mode=args.pivot,
            llm_call_budget=args.llm_call_budget,
            query_sample_size=args.query_sample_size,
            auto_pivot=not args.no_auto_pivot,
        )
    else:
        while True:
            run_pipeline(
                limit_per_query=args.limit,
                max_comments=args.max_comments,
                query_type=args.query_type,
                pivot_mode=args.pivot,
                llm_call_budget=args.llm_call_budget,
                query_sample_size=args.query_sample_size,
                auto_pivot=not args.no_auto_pivot,
            )
            logger.info("Cycle complete. Sleeping for 1 hour...")
            time.sleep(3600)

if __name__ == "__main__":
    main()
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
from brain import evaluate_comment, evaluate_batch, evaluate_handle_campaign, remaining_call_budget
from scoring import compute_campaign_score, author_burner_score
from enricher import enrich_channel
from pivot import pivot_on_handle

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
    llm_status: str = "PENDING_RETRY"
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
        "llm_status": llm_status
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
        self.sightings_written = 0
        self.llm_calls_made = 0
        self.llm_evaluated = 0
        self.llm_pending_retry = 0
        self.handles_scored = 0
        self.promotions_by_tier = defaultdict(int)

    def print_summary(self):
        print("\n" + "=" * 65)
        print("         BAITTRACE v2 PIPELINE OBSERVABILITY MATRIX")
        print("=" * 65)
        print(f"  Total Comments Scanned:           {self.comments_scanned:>6}")
        print(f"  Indicators Extracted (Raw):       {self.indicators_extracted:>6}")
        print(f"  Indicators Rejected (Stopwords):  {self.indicators_rejected:>6}")
        print(f"  Candidate Sightings Staged:       {self.sightings_written:>6}")
        print(f"  LLM Inference Invocations:        {self.llm_calls_made:>6}")
        print(f"  LLM Status: EVALUATED:            {self.llm_evaluated:>6}")
        print(f"  LLM Status: PENDING_RETRY:        {self.llm_pending_retry:>6}")
        print(f"  Campaign Handles Scored:          {self.handles_scored:>6}")
        print("-" * 65)
        print("  PROMOTIONS BY CAMPAIGN TIER:")
        print(f"    - CONFIRMED (High Threat):      {self.promotions_by_tier['CONFIRMED']:>6}")
        print(f"    - PROBABLE (Needs Review):      {self.promotions_by_tier['PROBABLE']:>6}")
        print(f"    - WATCH (Staged Evidence):      {self.promotions_by_tier['WATCH']:>6}")
        print(f"    - DISCARD (Suppressed):         {self.promotions_by_tier['DISCARD']:>6}")
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


def run_pipeline(
    limit_per_query: int = 5,
    max_comments: int = 50,
    query_type: str = "ALL",
    pivot_mode: bool = False,
    llm_call_budget: int = 15
):
    try:
        _run_pipeline_internal(
            limit_per_query=limit_per_query,
            max_comments=max_comments,
            query_type=query_type,
            pivot_mode=pivot_mode,
            llm_call_budget=llm_call_budget
        )
    except RuntimeError as e:
        if "candidate_sightings" in str(e):
            logger.critical("STOPPING: candidate_sightings writes are failing, evidence is not being persisted.")
            return
        raise


def _run_pipeline_internal(
    limit_per_query: int = 5,
    max_comments: int = 50,
    query_type: str = "ALL",
    pivot_mode: bool = False,
    llm_call_budget: int = 15
):
    state = StateManager()
    downloader = YoutubeCommentDownloader()
    telemetry = PipelineTelemetry()

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
    dynamic_queries = generate_dynamic_queries()

    for lane, query_list in dynamic_queries.items():
        if query_type != "ALL" and lane != query_type:
            continue

        for q in query_list:
            sort_by_views = (lane == "VICTIM_RICH")
            found = search_youtube(q, fetch_depth=100, lane=lane, sort_by_views=sort_by_views)

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

                # Channel metadata enrichment (cached)
                channel_meta = enrich_channel(author_channel) if author_channel else {}

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
                        llm_status="PENDING_RETRY"
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
            supabase.table("candidate_sightings").upsert(chunk).execute()
        except Exception as e:
            err_detail = format_supabase_error(e)
            logger.error(f"CRITICAL: Failed to upsert evaluated sightings batch ({len(chunk)} items): {err_detail}")
            raise RuntimeError(f"candidate_sightings write failed: {err_detail}") from e

    # --- Phase 4: Campaign Scoring & Promotion ---
    logger.info("⚖️ Evaluating campaign-level threat scores for all sighted handles...")

    for handle_norm, sightings in handle_sightings_map.items():
        telemetry.handles_scored += 1
        campaign = compute_campaign_score(handle_norm, sightings)
        tier = campaign["tier"]
        telemetry.promotions_by_tier[tier] += 1

        first_sighting = sightings[0]
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

    # --- Phase 5: Observability Summary ---
    telemetry.print_summary()


def main():
    parser = argparse.ArgumentParser(description="BaitTrace Fraud Discovery Sensor v2")
    parser.add_argument("--once", action="store_true", help="Run a single sweep")
    parser.add_argument("--limit", type=int, default=5, help="Max videos to process per query")
    parser.add_argument("--max-comments", type=int, default=50, help="Max comments to parse per video")
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
        default=15,
        help="Max LLM calls per sweep (default 15; each call evaluates all targets in one comment)"
    )
    parser.add_argument(
        "--reprocess-pending",
        action="store_true",
        help="Retry all PENDING_RETRY sightings in the DB using today's remaining quota, then exit"
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
            llm_call_budget=args.llm_call_budget
        )
    else:
        while True:
            run_pipeline(
                limit_per_query=args.limit,
                max_comments=args.max_comments,
                query_type=args.query_type,
                pivot_mode=args.pivot,
                llm_call_budget=args.llm_call_budget
            )
            logger.info("Cycle complete. Sleeping for 1 hour...")
            time.sleep(3600)

if __name__ == "__main__":
    main()
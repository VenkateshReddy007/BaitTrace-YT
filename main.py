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
from brain import evaluate_comment, evaluate_handle_campaign
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


class PipelineTelemetry:
    def __init__(self):
        self.comments_scanned = 0
        self.indicators_extracted = 0
        self.indicators_rejected = 0
        self.sightings_written = 0
        self.llm_calls_made = 0
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
                logger.debug(f"Failed to upsert seen_video: {e}")
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
                logger.debug(f"Failed to flush seen_comments: {e}")
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
                logger.debug(f"Failed to flush candidate_sightings (table may need schema.sql applied): {e}")
        self._sightings_buffer.clear()


def run_pipeline(
    limit_per_query: int = 5,
    max_comments: int = 50,
    query_type: str = "ALL",
    pivot_mode: bool = False
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
            for h in (res_handles.data or []):
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

    # --- Phase 3: Extraction & Triage ---
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

                # Targets for batched Gemini triage
                targets = [ind.normalized_value for ind in indicators]
                target_to_ind = {ind.normalized_value: ind for ind in indicators}

                try:
                    telemetry.llm_calls_made += 1
                    verdicts = evaluate_comment(video.title, clean_text, targets)

                    for target, intel in zip(targets, verdicts):
                        ind = target_to_ind[target]
                        llm_role = intel.get("role", "NEUTRAL")
                        llm_is_fraud = intel.get("is_fraud", False)

                        sighting_record = {
                            "handle_norm": target,
                            "indicator_type": ind.indicator_type,
                            "raw_value": ind.raw_value,
                            "comment_id": cid,
                            "author": author,
                            "author_channel_id": author_channel,
                            "video_id": video.video_id,
                            "video_title": video.title,
                            "video_url": video.url,
                            "lane": video.lane,
                            "is_reply": is_reply,
                            "comment_text": raw_text,
                            "posted_time": posted_time,
                            "llm_is_fraud": llm_is_fraud,
                            "llm_role": llm_role,
                            "llm_confidence": intel.get("confidence", 0.0),
                            "llm_reason": intel.get("reason", ""),
                            "heuristic_score": ind.confidence,
                            "channel_meta": channel_meta
                        }

                        # Stage to candidate_sightings buffer
                        state.buffer_sighting(sighting_record)
                        handle_sightings_map[target].append(sighting_record)

                        if llm_role == "RECRUITER":
                            logger.info(f"🚨 Sighted RECRUITER indicator: {target} ({intel.get('reason')})")

                except Exception as e:
                    logger.warning(f"Triage failed for targets {targets}: {e}")
                    continue

        except Exception as e:
            logger.warning(f"Comment iteration error on {video.url}: {e}")
            continue

        state.mark_video_seen(video.video_id)
        state.flush_comments()
        state.flush_sightings(telemetry)

    # Final flush of all raw sightings
    state.flush_comments()
    state.flush_sightings(telemetry)

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
        handle_payload = {
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
        try:
            supabase.table("handles").upsert(handle_payload).execute()
        except Exception as e:
            logger.debug(f"Failed to upsert handles row: {e}")

        # 2. Promote to actionable_leads ONLY if CONFIRMED or PROBABLE
        if tier in ("CONFIRMED", "PROBABLE"):
            logger.info(f"⭐ PROMOTED TO LEADS: [{tier}] {handle_norm} (Score: {campaign['campaign_score']})")
            lead_payload = {
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
            try:
                supabase.table("actionable_leads").upsert(lead_payload).execute()
            except Exception as e:
                logger.debug(f"Failed to upsert actionable_leads row: {e}")

    # --- Phase 5: Observability Summary ---
    telemetry.print_summary()


def main():
    parser = argparse.ArgumentParser(description="BaitTrace Fraud Discovery Sensor v2")
    parser.add_argument("--once", action="store_true", help="Run a single sweep")
    parser.add_argument("--limit", type=int, default=5, help="Max videos to process per query")
    parser.add_argument("--max-comments", type=int, default=50, help="Max comments to parse per video")
    parser.add_argument("--query-type", type=str, default="ALL", choices=["ALL", "VICTIM_RICH", "LURE", "EXPOSURE"])
    parser.add_argument("--pivot", action="store_true", help="Enable seed-to-campaign pivot expansion")

    args = parser.parse_args()

    if args.once:
        run_pipeline(
            limit_per_query=args.limit,
            max_comments=args.max_comments,
            query_type=args.query_type,
            pivot_mode=args.pivot
        )
    else:
        while True:
            run_pipeline(
                limit_per_query=args.limit,
                max_comments=args.max_comments,
                query_type=args.query_type,
                pivot_mode=args.pivot
            )
            logger.info("Cycle complete. Sleeping for 1 hour...")
            time.sleep(3600)

if __name__ == "__main__":
    main()
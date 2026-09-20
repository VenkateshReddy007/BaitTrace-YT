import argparse
import logging
import os
import sys
import time
import random
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    print("FATAL: SUPABASE_URL and SUPABASE_KEY must be set in environment or .env file.")
    sys.exit(1)

from youtube_comment_downloader import YoutubeCommentDownloader
from supabase import create_client, Client

from discovery import generate_dynamic_queries, search_youtube
from parser import extract_indicators, normalize_text
from brain import evaluate_lead, evaluate_comment
from heuristics import should_escalate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("BaitTrace-Pipeline")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

_BATCH_CHUNK_SIZE = 500


class StateManager:
    def __init__(self):
        logger.info("Syncing state with Supabase...")
        res_v = supabase.table("seen_videos").select("video_id").execute()
        self.seen_videos = {r["video_id"] for r in res_v.data}
        
        res_c = supabase.table("seen_comments").select("comment_id").execute()
        self.seen_comments = {r["comment_id"] for r in res_c.data}

        # Batching buffers
        self._comment_buffer: list[dict] = []
        self._lead_buffer: list[dict] = []
        # In-memory dedup set for leads (target + source_video_id)
        self._lead_dedup: set[str] = set()

    def is_video_seen(self, video_id: str) -> bool:
        return video_id in self.seen_videos

    def mark_video_seen(self, video_id: str):
        if video_id not in self.seen_videos:
            supabase.table("seen_videos").upsert({"video_id": video_id}).execute()
            self.seen_videos.add(video_id)

    def is_comment_seen(self, comment_id: str) -> bool:
        return comment_id in self.seen_comments

    def mark_comment_seen(self, comment_id: str):
        """Buffer a comment ID for batched write. Dedup set updated immediately."""
        if comment_id not in self.seen_comments:
            self.seen_comments.add(comment_id)
            self._comment_buffer.append({"comment_id": comment_id})

    def buffer_lead(self, record: dict):
        """Buffer an actionable lead for batched write. Dedup in Python."""
        dedup_key = f"{record.get('target')}|{record.get('source_video_id')}"
        if dedup_key not in self._lead_dedup:
            self._lead_dedup.add(dedup_key)
            self._lead_buffer.append(record)

    def flush_comments(self):
        """Flush buffered comment IDs to Supabase in chunks."""
        if not self._comment_buffer:
            return
        for i in range(0, len(self._comment_buffer), _BATCH_CHUNK_SIZE):
            chunk = self._comment_buffer[i:i + _BATCH_CHUNK_SIZE]
            supabase.table("seen_comments").upsert(chunk).execute()
        logger.info(f"Flushed {len(self._comment_buffer)} comment IDs to Supabase.")
        self._comment_buffer.clear()

    def flush_leads(self):
        """Flush buffered actionable leads to Supabase in chunks."""
        if not self._lead_buffer:
            return
        for i in range(0, len(self._lead_buffer), _BATCH_CHUNK_SIZE):
            chunk = self._lead_buffer[i:i + _BATCH_CHUNK_SIZE]
            supabase.table("actionable_leads").insert(chunk).execute()
        logger.info(f"Flushed {len(self._lead_buffer)} actionable leads to Supabase.")
        self._lead_buffer.clear()


def run_pipeline(limit_per_query: int = 5, max_comments: int = 150, query_type: str = "ALL"):
    state = StateManager()
    downloader = YoutubeCommentDownloader()
    
    logger.info(f"Initiating dynamic discovery sweep ({query_type}) across scam funnels...")
    
    # Autonomous LLM Threat Discovery
    logger.info("🤖 Querying LLM Matrix for autonomous threat discovery vectors...")
    dynamic_queries = generate_dynamic_queries()
    
    fresh_videos = []
    
    for category, query_list in dynamic_queries.items():
        if query_type != "ALL" and category != query_type:
            continue
            
        for q in query_list:
            logger.info(f"🚀 Deploying AI-Generated Vector: '{q}'")
            found = search_youtube(q, fetch_depth=150, category=category)
            
            fresh_for_this_query = 0
            for v in found:
                if not state.is_video_seen(v.video_id):
                    fresh_videos.append(v)
                    fresh_for_this_query += 1
                    
                if fresh_for_this_query >= limit_per_query:
                    break
                    
            logger.info(f"Found {fresh_for_this_query} net-new videos for AI query '{q}'")

    fresh_videos = fresh_videos[:50]
    logger.info(f"Target pool assembled: {len(fresh_videos)} unscanned videos.")
    
    for idx, video in enumerate(fresh_videos, 1):
        logger.info(f"[{idx}/{len(fresh_videos)}] Inspecting: '{video.title[:45]}...' | Channel: {video.channel_id or 'Unknown'}")
        
        # --- Comment fetch with targeted exception handling ---
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
                
                raw_text = comment.get("text", "")
                author = comment.get("author", "Unknown")
                is_reply = comment.get("reply", False)
                
                clean_text = normalize_text(raw_text)
                indicators = extract_indicators(clean_text)
                
                if not indicators:
                    continue

                # Filter through heuristic gate before LLM
                escalated = [ind for ind in indicators if should_escalate(ind, clean_text)]
                if not escalated:
                    continue

                # Build target list for batched Gemini call
                targets = []
                target_to_ind = {}
                for ind in escalated:
                    target = ind.normalized_value
                    if target.lower() in ("whatsapp", "telegram"):
                        continue
                    targets.append(target)
                    target_to_ind[target] = ind

                if not targets:
                    continue

                # --- Per-indicator triage with targeted exception handling ---
                try:
                    logger.info(f"🧠 Triaging {len(targets)} target(s) via LLM Matrix...")
                    verdicts = evaluate_comment(video.title, clean_text, targets)

                    for target, intel in zip(targets, verdicts):
                        if not intel.get("is_fraud", False):
                            logger.info(f"✅ Cleared by AI: {target} ({intel.get('reason')})")
                            continue

                        ind = target_to_ind[target]
                        fraud_class = intel.get("scam_type", "SUSPICIOUS_COMMENT_BOT")
                        llm_confidence = intel.get("confidence", 0.90)
                        confidence = min(llm_confidence, ind.confidence)
                        logger.info(f"🚨 [{fraud_class}] {ind.indicator_type}: {target} - {intel.get('reason')}")

                        record = {
                            "indicator_type": ind.indicator_type,
                            "target": target,
                            "fraud_class": fraud_class,
                            "confidence": confidence,
                            "ai_reasoning": intel.get("reason", ""),
                            "author": author,
                            "source_video_id": video.video_id,
                            "source_title": video.title,
                            "source_url": video.url,
                            "raw_comment": raw_text
                        }

                        if is_reply:
                            logger.info(f"Target found in a REPLY comment: {target}")

                        state.buffer_lead(record)

                except Exception as e:
                    logger.warning(f"Lead processing failed for targets {targets}: {e}")
                    continue

        except Exception as e:
            logger.warning(f"Comment iteration failed for {video.url}: {e}")
            continue

        # mark_video_seen AFTER the comment loop completes successfully
        state.mark_video_seen(video.video_id)

        # Flush batches per video
        state.flush_comments()
        state.flush_leads()

    # Final flush for any remaining buffered items
    state.flush_comments()
    state.flush_leads()
    logger.info("Sweep complete.")


def main():
    parser = argparse.ArgumentParser(description="BaitTrace Fraud Discovery Sensor")
    parser.add_argument("--once", action="store_true", help="Run a single sweep")
    parser.add_argument("--limit", type=int, default=5, help="Max videos to process per query")
    parser.add_argument("--max-comments", type=int, default=150, help="Max comments to parse per video")
    parser.add_argument("--query-type", type=str, default="ALL", choices=["ALL", "LURE", "PARASITE"])
    
    args = parser.parse_args()
    
    if args.once:
        run_pipeline(limit_per_query=args.limit, max_comments=args.max_comments, query_type=args.query_type)
    else:
        while True:
            run_pipeline(limit_per_query=args.limit, max_comments=args.max_comments, query_type=args.query_type)
            logger.info("Cycle complete. Sleeping for 1 hour...")
            time.sleep(3600)

if __name__ == "__main__":
    main()
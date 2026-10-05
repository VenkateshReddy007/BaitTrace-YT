import argparse
import json
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator
from youtube_comment_downloader import YoutubeCommentDownloader, SORT_BY_RECENT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("BaitTrace-Extractor")

def get_comments(downloader: YoutubeCommentDownloader, video_url: str, limit: int) -> Generator[dict, None, None]:
    try:
        # SORT_BY_RECENT (1) prioritizes new bot drops
        comments = downloader.get_comments_from_url(video_url, sort_by=SORT_BY_RECENT)
        count = 0
        for comment in comments:
            if count >= limit:
                break
            yield comment
            count += 1
    except Exception as e:
        logger.warning(f"Could not extract comments for {video_url}: {e}")

def main():
    parser = argparse.ArgumentParser(description="BaitTrace Phase 2: Comment Extraction Engine")
    parser.add_argument("--input", type=str, default="data/discovered_videos.jsonl", help="Input JSONL file from Phase 1")
    parser.add_argument("--output", type=str, default="data/raw_comments.jsonl", help="Output comments JSONL file")
    parser.add_argument("--max-comments", type=int, default=50, help="Max comments per video")
    parser.add_argument("--video-limit", type=int, default=15, help="Number of videos from the input pool to process")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not input_path.exists():
        logger.error(f"Input file '{input_path}' not found! Run Phase 1 first.")
        return

    downloader = YoutubeCommentDownloader()
    
    # Read discovered video targets
    videos = []
    with open(input_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                videos.append(json.loads(line))

    selected_videos = videos[:args.video_limit]
    logger.info(f"Loaded {len(videos)} videos. Processing the first {len(selected_videos)} targets (limit: {args.max_comments} comments/video)...")

    total_scraped = 0
    with open(output_path, "a", encoding="utf-8") as out_f:
        for idx, vid in enumerate(selected_videos, 1):
            vid_id = vid.get("video_id")
            vid_url = vid.get("url")
            vid_title = vid.get("title", "Unknown")

            logger.info(f"[{idx}/{len(selected_videos)}] Extracting: '{vid_title[:45]}...' ({vid_id})")

            comment_count = 0
            for raw_c in get_comments(downloader, vid_url, limit=args.max_comments):
                record = {
                    "video_id": vid_id,
                    "video_title": vid_title,
                    "comment_id": raw_c.get("cid"),
                    "author": raw_c.get("author"),
                    "channel_id": raw_c.get("channel"),
                    "raw_text": raw_c.get("text"),
                    "published_time": raw_c.get("time"),
                    "votes": raw_c.get("votes"),
                    "scraped_at": datetime.now(timezone.utc).isoformat()
                }
                out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
                comment_count += 1

            total_scraped += comment_count
            logger.info(f"Pulled {comment_count} comments from {vid_id}.")

            # Polite jitter to evade IP rate blocks
            time.sleep(random.uniform(0.8, 1.5))

    logger.info(f"Extraction complete. Appended {total_scraped} comments to '{output_path}'")

if __name__ == "__main__":
    main()
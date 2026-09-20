"""
BaitTrace State Reset Script
Truncates seen_videos and seen_comments tables to allow re-scanning.
Usage: python scripts/reset_state.py --confirm
"""
import argparse
import os
import sys

from dotenv import load_dotenv
load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("FATAL: SUPABASE_URL and SUPABASE_KEY must be set in environment or .env file.")
    sys.exit(1)

from supabase import create_client
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def main():
    parser = argparse.ArgumentParser(description="Reset BaitTrace scan state (truncate seen_videos & seen_comments)")
    parser.add_argument("--confirm", action="store_true", help="Required flag to actually execute the reset")
    args = parser.parse_args()

    # Count rows before
    videos_before = supabase.table("seen_videos").select("video_id", count="exact").execute().count or 0
    comments_before = supabase.table("seen_comments").select("comment_id", count="exact").execute().count or 0

    print(f"BEFORE: seen_videos={videos_before}, seen_comments={comments_before}")

    if not args.confirm:
        print("\nDry run — pass --confirm to actually truncate the tables.")
        sys.exit(0)

    # Delete all rows (Supabase doesn't have TRUNCATE via REST, so we delete with a true condition)
    print("Deleting all rows from seen_videos...")
    supabase.table("seen_videos").delete().neq("video_id", "___impossible___").execute()

    print("Deleting all rows from seen_comments...")
    supabase.table("seen_comments").delete().neq("comment_id", "___impossible___").execute()

    # Count rows after
    videos_after = supabase.table("seen_videos").select("video_id", count="exact").execute().count or 0
    comments_after = supabase.table("seen_comments").select("comment_id", count="exact").execute().count or 0

    print(f"AFTER:  seen_videos={videos_after}, seen_comments={comments_after}")
    print("State reset complete.")


if __name__ == "__main__":
    main()

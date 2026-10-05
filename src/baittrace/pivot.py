import logging
from typing import List, Set
from .discovery import DiscoveredVideo, search_youtube

logger = logging.getLogger("BaitTrace-Pivot")

def pivot_on_handle(handle_norm: str, known_video_ids: List[str] = None) -> List[DiscoveredVideo]:
    """Pivots on a confirmed or suspicious handle to find all related videos:
    1. Searches YouTube for the raw handle itself (e.g. '@fast_earn_2026').
    2. Searches YouTube for 't.me/<handle>'.
    3. Re-includes previously sighted video IDs to allow deep re-scanning.
    """
    clean_handle = handle_norm.lstrip("@").lstrip("+")
    discovered_videos: List[DiscoveredVideo] = []
    seen_ids: Set[str] = set()

    search_queries = [
        handle_norm,
        f'"{handle_norm}"',
        f'"t.me/{clean_handle}"'
    ]

    logger.info(f"🔄 Pivoting on handle: {handle_norm} across YouTube index...")

    for q in search_queries:
        videos = search_youtube(query=q, fetch_depth=30, lane="PIVOT", sort_by_views=False)
        for v in videos:
            if v.video_id not in seen_ids:
                seen_ids.add(v.video_id)
                discovered_videos.append(v)

    # If known video IDs were supplied where this handle was sighted, ensure they are in the pivot list
    if known_video_ids:
        for vid_id in known_video_ids:
            if vid_id not in seen_ids:
                seen_ids.add(vid_id)
                discovered_videos.append(DiscoveredVideo(
                    video_id=vid_id,
                    title=f"Prior Sighting Target ({vid_id})",
                    url=f"https://www.youtube.com/watch?v={vid_id}",
                    category="PIVOT",
                    lane="PIVOT"
                ))

    logger.info(f"🔄 Pivot complete for {handle_norm}: Found {len(discovered_videos)} targeted videos.")
    return discovered_videos

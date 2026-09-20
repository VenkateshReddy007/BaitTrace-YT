import json
import logging
import random
from typing import Dict, List, Optional
from pydantic import BaseModel
import yt_dlp

logger = logging.getLogger("BaitTrace-Discovery")

class DiscoveredVideo(BaseModel):
    video_id: str
    title: str
    url: str
    category: str  # Kept for backward compatibility
    lane: str      # VICTIM_RICH, LURE, EXPOSURE, PIVOT
    channel_id: Optional[str] = None
    channel: Optional[str] = None
    view_count: Optional[int] = 0

# Restructured threat discovery matrix across 3 distinct operational lanes
FALLBACK_QUERIES: Dict[str, List[str]] = {
    # High-traffic legitimate videos where scammers infest the comments
    "VICTIM_RICH": [
        "stock market basics for beginners hindi",
        "intraday option trading strategies beginners",
        "how to apply for government jobs online 2026",
        "sbi instant personal loan online apply process",
        "best side income ideas for students in india",
        "work from home genuine jobs without investment",
        "how to reset google pay upi pin state bank",
        "instant loan without cibil score app download"
    ],
    # Active scam vector lures
    "LURE": [
        "earn money online daily payment telegram channel",
        "work from home review rating job payment proof",
        "google maps review job daily payout whatsapp",
        "part time typing job contact telegram",
        "colour prediction bot hack script proof link",
        "prepaid task income withdrawal proof 2026"
    ],
    # Scam exposure/awareness channels (rich in victim reports & handle seeds)
    "EXPOSURE": [
        "task scam telegram reality exposed",
        "part time job scam telegram victims report",
        "work from home rating scam police complaint cyber cell",
        "colour prediction app scam bust",
        "fake crypto telegram investment scam revealed"
    ]
}

def generate_dynamic_queries() -> Dict[str, List[str]]:
    """Returns the operational discovery query matrix organized by target lane."""
    return FALLBACK_QUERIES

def search_youtube(
    query: str,
    fetch_depth: int = 150,
    lane: str = "VICTIM_RICH",
    category: Optional[str] = None,
    sort_by_views: bool = False
) -> List[DiscoveredVideo]:
    """Scrapes YouTube search results.
    If sort_by_views=True, prioritizes highest-traffic videos (most scam comments).
    """
    effective_lane = lane or category or "VICTIM_RICH"
    effective_category = category or lane or "VICTIM_RICH"
    search_url = f"ytsearch{fetch_depth}:{query}"
    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        'extract_flat': True
    }
    
    results = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(search_url, download=False)
            if info and 'entries' in info:
                entries = [e for e in info['entries'] if e]
                
                if sort_by_views:
                    entries.sort(key=lambda e: e.get('view_count') or 0, reverse=True)
                else:
                    random.shuffle(entries)
                
                for entry in entries:
                    vid_id = entry.get('id')
                    if vid_id:
                        results.append(DiscoveredVideo(
                            video_id=vid_id,
                            title=entry.get('title', 'Unknown'),
                            url=f"https://www.youtube.com/watch?v={vid_id}",
                            category=effective_category,
                            lane=effective_lane,
                            channel_id=entry.get('channel_id'),
                            channel=entry.get('channel') or entry.get('uploader'),
                            view_count=entry.get('view_count') or 0
                        ))
        except Exception as e:
            logger.error(f"Search failed for query '{query}': {e}")
            
    return results
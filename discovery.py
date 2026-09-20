import json
import random
from typing import List, Optional
from pydantic import BaseModel
import yt_dlp

class DiscoveredVideo(BaseModel):
    video_id: str
    title: str
    url: str
    category: str
    channel_id: Optional[str] = None

# Core fallback matrix for robust threat discovery
FALLBACK_QUERIES = {
    "LURE": [
        "earn money online daily payment telegram channel",
        "work from home review rating job payment proof",
        "google maps review job daily payout whatsapp",
        "part time typing job contact telegram",
        "colour prediction bot hack script proof link",
        "prepaid task income withdrawal proof 2026"
    ],
    "PARASITE": [
        "bank nifty live option option buying trap",
        "nifty intraday scalping parasite stream",
        "option trading telegram channel scam exposed"
    ]
}

def generate_dynamic_queries() -> dict:
    """Returns core intelligence threat vectors for robust automated sweeping."""
    return FALLBACK_QUERIES

def search_youtube(query: str, fetch_depth: int = 150, category: str = "LURE") -> List[DiscoveredVideo]:
    search_url = f"ytsearch{fetch_depth}:{query}"
    ydl_opts = {'quiet': True, 'skip_download': True, 'extract_flat': True}
    
    results = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(search_url, download=False)
            if 'entries' in info:
                entries = [e for e in info['entries'] if e]
                random.shuffle(entries)
                
                for entry in entries:
                    vid_id = entry.get('id')
                    if vid_id:
                        results.append(DiscoveredVideo(
                            video_id=vid_id,
                            title=entry.get('title', 'Unknown'),
                            url=f"https://www.youtube.com/watch?v={vid_id}",
                            category=category,
                            channel_id=entry.get('channel_id')
                        ))
        except Exception as e:
            print(f"[ERROR] Search failed for {query}: {e}")
            
    return results
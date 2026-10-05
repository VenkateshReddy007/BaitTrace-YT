import json
import logging
import random
from typing import Dict, List, Optional, Any
from pydantic import BaseModel
import yt_dlp
from .brain import generate_llm_queries
from datetime import datetime, timezone

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
# Each lane has 15+ queries including native-script variants in Hindi, Bengali,
# Malayalam, Telugu, Tamil, and Kannada to match multi-lingual scam targeting.
FALLBACK_QUERIES: Dict[str, List[str]] = {
    # High-traffic legitimate videos where scammers infest the comments
    "VICTIM_RICH": [
        # English / Hinglish
        "stock market basics for beginners hindi",
        "intraday option trading strategies beginners",
        "how to apply for government jobs online 2026",
        "sbi instant personal loan online apply process",
        "best side income ideas for students in india",
        "work from home genuine jobs without investment",
        "how to reset google pay upi pin state bank",
        "instant loan without cibil score app download",
        "mutual fund SIP for beginners india 2026",
        # Hindi (Devanagari)
        "शेयर मार्केट कैसे सीखे नए लोग",
        "ऑनलाइन पैसे कमाने के तरीके 2026",
        "सरकारी नौकरी ऑनलाइन आवेदन कैसे करें",
        # Bengali
        "অনলাইনে টাকা ইনকাম করার উপায় ২০২৬",
        "শেয়ার বাজার শেখার উপায় বাংলা",
        # Malayalam
        "ഓൺലൈൻ പണം സമ്പാദിക്കാനുള്ള വഴികൾ 2026",
        # Telugu
        "ఆన్‌లైన్ డబ్బు సంపాదించే మార్గాలు 2026",
        "షేర్ మార్కెట్ బేసిక్స్ తెలుగు",
        # Tamil
        "ஆன்லைன் பணம் சம்பாதிக்கும் வழிகள் 2026",
        # Kannada
        "ಆನ್‌ಲೈನ್ ಹಣ ಗಳಿಸುವ ಮಾರ್ಗಗಳು 2026",
    ],
    # Active scam vector lures
    "LURE": [
        # English / Hinglish
        "earn money online daily payment telegram channel",
        "work from home review rating job payment proof",
        "google maps review job daily payout whatsapp",
        "part time typing job contact telegram",
        "colour prediction bot hack script proof link",
        "prepaid task income withdrawal proof 2026",
        "crypto investment double money telegram group",
        "online task earning app telegram link join",
        "rating job daily salary without investment 2026",
        # Hindi
        "टेलीग्राम से पैसे कमाओ डेली पेमेंट प्रूफ",
        "ऑनलाइन टास्क जॉब टेलीग्राम ग्रुप ज्वाइन करें",
        "कलर प्रेडिक्शन ऐप हैक स्क्रिप्ट 2026",
        # Bengali
        "টেলিগ্রাম থেকে টাকা আয় করুন প্রমাণ সহ",
        # Telugu
        "టెలిగ్రామ్ ద్వారా డబ్బు సంపాదన రోజు పేమెంట్",
        # Tamil
        "டெலிகிராம் பணம் சம்பாதிக்கும் வழி 2026",
        # Kannada
        "ಟೆಲಿಗ್ರಾಮ್ ಮೂಲಕ ಹಣ ಗಳಿಸಿ ದೈನಂದಿನ ಪಾವತಿ",
    ],
    # Scam exposure/awareness channels (rich in victim reports & handle seeds)
    "EXPOSURE": [
        # English / Hinglish
        "task scam telegram reality exposed",
        "part time job scam telegram victims report",
        "work from home rating scam police complaint cyber cell",
        "colour prediction app scam bust",
        "fake crypto telegram investment scam revealed",
        "telegram job scam victim story india 2026",
        "online fraud complaint cyber crime portal india",
        "rating job scam exposed whatsapp group",
        "crypto scam telegram group exposed 2026",
        "UPI fraud scam complaint how to report",
        # Hindi
        "टेलीग्राम टास्क स्कैम एक्सपोज़्ड पीड़ित रिपोर्ट",
        "ऑनलाइन फ्रॉड शिकायत साइबर क्राइम पोर्टल",
        # Bengali
        "টেলিগ্রাম স্ক্যাম প্রতারণা অভিযোগ ভুক্তভোগী",
        # Telugu
        "టెలిగ్రామ్ స్కామ్ మోసం బాధితుల నివేదిక",
        # Tamil
        "டெலிகிராம் மோசடி அம்பலமானது பாதிக்கப்பட்டவர் புகார்",
    ]
}

def generate_dynamic_queries(recent_patterns: List[Dict[str, Any]] = None, supabase_client = None) -> Dict[str, List[Dict[str, str]]]:
    """Returns the operational discovery query matrix organized by target lane.
    Merges fallback queries with LLM-generated ones based on recent patterns.
    """
    merged = {
        lane: [{"query": q, "source": "FALLBACK"} for q in qs]
        for lane, qs in FALLBACK_QUERIES.items()
    }
    
    if not recent_patterns or not supabase_client:
        return merged

    llm_queries = generate_llm_queries(recent_patterns)
    
    inserts = []
    
    for lane, qs in llm_queries.items():
        if lane not in merged:
            merged[lane] = []
        for q in qs:
            if not any(mq["query"].lower() == q.lower() for mq in merged[lane]):
                merged[lane].append({"query": q, "source": "LLM_GENERATED"})
                inserts.append({
                    "query_text": q,
                    "lane": lane,
                    "source_handles": [p.get("handle_norm", p.get("target", "unknown")) for p in recent_patterns],
                    "source": "LLM_GENERATED"
                })
                
    if inserts:
        try:
            supabase_client.table("generated_queries").upsert(
                inserts, on_conflict="query_text,lane"
            ).execute()
        except Exception as e:
            logger.warning(f"Failed to persist generated queries: {e}")

    return merged

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
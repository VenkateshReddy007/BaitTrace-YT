import json
import logging
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
load_dotenv()

from supabase import create_client, Client
import yt_dlp

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("BaitTrace-Enricher")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    logger.critical("FATAL: SUPABASE_URL and SUPABASE_KEY must be set in environment or .env file.")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CHANNEL_CACHE_FILE = Path("data/channel_cache.json")

def _load_channel_cache() -> Dict[str, Any]:
    if CHANNEL_CACHE_FILE.exists():
        try:
            with open(CHANNEL_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_channel_cache(cache: Dict[str, Any]):
    try:
        CHANNEL_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CHANNEL_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save channel cache: {e}")

_channel_cache = _load_channel_cache()

def enrich_channel(channel_id: str) -> Dict[str, Any]:
    """Fetches channel metadata (subscriber count, video count, join date)
    using yt-dlp flat extraction, backed by an on-disk JSON cache.
    """
    if not channel_id:
        return {}

    if channel_id in _channel_cache:
        return _channel_cache[channel_id]

    channel_url = (
        f"https://www.youtube.com/channel/{channel_id}"
        if channel_id.startswith("UC")
        else f"https://www.youtube.com/{channel_id}"
    )

    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        'extract_flat': True,
        'playlist_items': '1'
    }

    meta = {
        "channel_id": channel_id,
        "subscriber_count": None,
        "video_count": None,
        "channel_name": None,
        "description": ""
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
            if info:
                meta["channel_name"] = info.get("channel") or info.get("uploader") or info.get("title")
                meta["subscriber_count"] = info.get("channel_follower_count") or info.get("subscriber_count")
                meta["video_count"] = info.get("playlist_count")
                meta["description"] = info.get("description", "")
    except Exception as e:
        logger.debug(f"yt-dlp channel extraction failed for {channel_id}: {e}")

    _channel_cache[channel_id] = meta
    _save_channel_cache(_channel_cache)
    return meta

def probe_telegram_handle(handle: str) -> dict:
    clean_handle = handle.replace("@", "").strip()
    url = f"https://t.me/{clean_handle}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode('utf-8')
            
            title_match = re.search(r'<meta property="og:title" content="([^"]+)">', html)
            desc_match = re.search(r'<meta property="og:description" content="([^"]+)">', html)
            extra_match = re.search(r'<div class="tgme_page_extra">([^<]+)</div>', html)
            
            title = title_match.group(1) if title_match else clean_handle
            desc = desc_match.group(1) if desc_match else ""
            extra = extra_match.group(1).lower() if extra_match else ""
            
            entity_type = "UNKNOWN"
            if "subscriber" in extra: entity_type = "BROADCAST_CHANNEL"
            elif "member" in extra: entity_type = "GROUP_CHAT"
            elif "bot" in extra: entity_type = "AUTOMATED_BOT"
            else: entity_type = "DIRECT_CONTACT"
                
            return {
                "status": "ACTIVE", "entity_type": entity_type, "tg_title": title,
                "tg_bio": desc[:150].replace('\n', ' ') + "..." if len(desc) > 150 else desc.replace('\n', ' '),
                "tg_stats": extra.strip()
            }
    except Exception:
        return {"status": "CONNECTION_FAILED", "entity_type": "ERROR", "tg_title": "", "tg_bio": "", "tg_stats": ""}

def main():
    logger.info("Fetching raw leads from Supabase...")
    res_leads = supabase.table("actionable_leads").select("*").execute()
    actionable = res_leads.data
    
    res_enriched = supabase.table("enriched_leads").select("target").execute()
    seen_targets = {r.get("target") for r in res_enriched.data if r.get("target")}

    if not actionable:
        logger.error("No actionable leads found to enrich.")
        return

    new_records = 0
    for lead in actionable:
        target = lead.get("handle_norm") or lead.get("target")
        if not target or target in seen_targets:
            continue

        if lead.get("indicator_type") == "TELEGRAM":
            logger.info(f"Probing NEW target: {target}...")
            intel = probe_telegram_handle(target)
            
            enrich_record = {
                "target": target,
                "handle_norm": target,
                "indicator_type": lead.get("indicator_type"),
                "fraud_class": lead.get("scam_type") or lead.get("fraud_class"),
                "confidence": lead.get("campaign_score") or lead.get("confidence"),
                "ai_reasoning": lead.get("llm_campaign_reason") or lead.get("ai_reasoning"),
                "telegram_intel": intel,
                "source_url": lead.get("representative_video_url") or lead.get("source_url")
            }
            supabase.table("enriched_leads").upsert(enrich_record).execute()
            seen_targets.add(target)
            new_records += 1
            time.sleep(1.0)

    if new_records > 0:
        logger.info(f"Successfully enriched {new_records} net-new targets.")
    else:
        logger.info("No net-new targets to enrich.")

if __name__ == "__main__":
    main()
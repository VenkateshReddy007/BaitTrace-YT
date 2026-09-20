import logging
import os
import re
import sys
import time
import urllib.request

from dotenv import load_dotenv
load_dotenv()

from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("BaitTrace-Enricher")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    logger.critical("FATAL: SUPABASE_URL and SUPABASE_KEY must be set in environment or .env file.")
    sys.exit(1)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

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
    seen_targets = {r["target"] for r in res_enriched.data}

    if not actionable:
        logger.error("No actionable leads found to enrich.")
        return

    new_records = 0
    for lead in actionable:
        target = lead.get("target")
        
        if target in seen_targets:
            continue

        if lead.get("indicator_type") == "TELEGRAM":
            logger.info(f"Probing NEW target: {target}...")
            intel = probe_telegram_handle(target)
            
            enrich_record = {
                "target": target,
                "indicator_type": lead.get("indicator_type"),
                "fraud_class": lead.get("fraud_class"),
                "confidence": lead.get("confidence"),
                "ai_reasoning": lead.get("ai_reasoning"),
                "telegram_intel": intel,
                "author": lead.get("author"),
                "source_video_id": lead.get("source_video_id"),
                "source_title": lead.get("source_title"),
                "source_url": lead.get("source_url"),
                "raw_comment": lead.get("raw_comment")
            }
            supabase.table("enriched_leads").upsert(enrich_record).execute()
            seen_targets.add(target)
            new_records += 1
            time.sleep(1.5)

    if new_records > 0:
        logger.info(f"Successfully enriched {new_records} net-new targets.")
    else:
        logger.info("No net-new targets to enrich.")

if __name__ == "__main__":
    main()
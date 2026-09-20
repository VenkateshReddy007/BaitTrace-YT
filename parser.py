import argparse
import json
import logging
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Literal, Optional
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("BaitTrace-Parser")

class ExtractedIndicator(BaseModel):
    indicator_type: Literal["TELEGRAM", "WHATSAPP", "PHONE", "UPI"]
    raw_value: str
    normalized_value: str
    confidence: float

class ProcessedEvidence(BaseModel):
    source_video_id: str
    source_video_title: str
    comment_id: str
    author: str
    raw_comment: str
    cleaned_comment: str
    indicators: List[ExtractedIndicator]
    extracted_at: datetime

# --- Normalization & De-obfuscation ---

def normalize_text(text: str) -> str:
    if not text:
        return ""
    
    # 1. Decompose unicode (maps 𝟵𝟴𝟮𝟭 to 9821, 𝙏𝙚𝙡𝙚𝙜𝙧𝙖𝙢 to Telegram)
    norm = unicodedata.normalize("NFKD", text)
    
    # 2. Strip zero-width spaces and formatting artifacts
    norm = re.sub(r"[\u200B-\u200D\uFEFF\u00AD]", "", norm)
    
    # 3. Unmask homoglyphs and leetspeak substitutions
    norm = re.sub(r"(?i)\bte[1I|l]egram\b", "telegram", norm)
    
    # 4. Resolve obfuscated link domains
    norm = re.sub(r"(?i)t\s*[\(\[\{]?\s*(?:dot|\.)\s*[\)\]\}]?\s*me", "t.me", norm)
    
    # 4.5. Fix spaces around slash in t.me links (e.g., t.me / joinchat)
    norm = re.sub(r"(?i)(t\.me)\s*/\s*", r"\1/", norm)
    
    # 5. Collapse spaces/separators in handles following @ or t.me/
    norm = re.sub(r"(?i)(@|t\.me/)[\s\.\-_]+([a-zA-Z0-9_\+]+)", r"\1\2", norm)
    
    # 6. Collapse spaced digits: "9 8 2 1 0" -> "98210", "9_8_2_1" -> "9821"
    norm = re.sub(r"(?<=\d)[\s\.\-_]+(?=\d)", "", norm)
    
    return norm

# --- Entity Extractors ---

PATTERNS = {
    # Telegram: t.me/link, telegram.dog, or explicit @handle
    "TG_LINK": re.compile(r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/(?:joinchat/)?([\+a-zA-Z0-9_]{5,32})", re.IGNORECASE),
    "TG_MENTION": re.compile(r"(?:telegram|tg|tele)[^\w\+]*([\+a-zA-Z0-9_]{5,32})", re.IGNORECASE),
    
    # WhatsApp: wa.me links or api.whatsapp.com
    "WA_LINK": re.compile(r"(?:https?://)?(?:wa\.me|api\.whatsapp\.com/send\?phone=)/?(\+?\d{10,13})", re.IGNORECASE),
    "WA_KEYWORD": re.compile(r"(?:whatsapp|wa|wsp|wtsp)[^\w\+]*((?:(?:\+|0{0,2})91)?[6-9]\d{9})", re.IGNORECASE),
    
    # Indian Mobile Numbers: Starts with 6, 7, 8, or 9
    "IN_PHONE": re.compile(r"(?:(?:\+|0{0,2})91[\s\-]*)?([6-9]\d{9})\b"),
    
    # UPI VPA patterns (user@provider)
    "UPI_VPA": re.compile(r"\b([a-zA-Z0-9.\-_]{2,256}@(?!gmail|yahoo|outlook|hotmail)[a-zA-Z]{2,64})\b", re.IGNORECASE)
}

def extract_indicators(cleaned_text: str) -> List[ExtractedIndicator]:
    indicators = []
    seen = set()

    # 1. Telegram Handles
    for match in PATTERNS["TG_LINK"].finditer(cleaned_text):
        handle = match.group(1)
        if handle.lower() not in [h.lower() for h in seen] and handle.lower() not in ["joinchat", "channel"]:
            seen.add(handle.lower())
            indicators.append(ExtractedIndicator(
                indicator_type="TELEGRAM",
                raw_value=match.group(0),
                normalized_value=handle if handle.startswith('+') else f"@{handle}",
                confidence=0.98
            ))

    for match in PATTERNS["TG_MENTION"].finditer(cleaned_text):
        handle = match.group(1)
        if handle.lower() not in [h.lower() for h in seen]:
            seen.add(handle.lower())
            indicators.append(ExtractedIndicator(
                indicator_type="TELEGRAM",
                raw_value=match.group(0),
                normalized_value=handle if handle.startswith('+') else f"@{handle}",
                confidence=0.90
            ))

    # 2. WhatsApp Targets
    for match in PATTERNS["WA_LINK"].finditer(cleaned_text):
        num = re.sub(r"\D", "", match.group(1))
        if num not in seen:
            seen.add(num)
            indicators.append(ExtractedIndicator(
                indicator_type="WHATSAPP",
                raw_value=match.group(0),
                normalized_value=num,
                confidence=0.98
            ))

    for match in PATTERNS["WA_KEYWORD"].finditer(cleaned_text):
        num_raw = re.sub(r"\D", "", match.group(1))
        if len(num_raw) >= 10:
            num = num_raw[-10:]
            if num not in seen:
                seen.add(num)
                indicators.append(ExtractedIndicator(
                    indicator_type="WHATSAPP",
                    raw_value=match.group(0),
                    normalized_value=f"+91{num}",
                    confidence=0.92
                ))

    # 3. Direct Phone Numbers (if not already captured as WA)
    for match in PATTERNS["IN_PHONE"].finditer(cleaned_text):
        num = match.group(1)
        normalized = f"+91{num}"
        if num not in seen and normalized not in seen:
            seen.add(num)
            indicators.append(ExtractedIndicator(
                indicator_type="PHONE",
                raw_value=match.group(0),
                normalized_value=normalized,
                confidence=0.85
            ))

    # 4. UPI VPAs
    for match in PATTERNS["UPI_VPA"].finditer(cleaned_text):
        vpa = match.group(1).lower()
        if vpa not in seen:
            seen.add(vpa)
            indicators.append(ExtractedIndicator(
                indicator_type="UPI",
                raw_value=match.group(0),
                normalized_value=vpa,
                confidence=0.95
            ))

    return indicators

# --- Pipeline Execution ---

def main():
    parser = argparse.ArgumentParser(description="BaitTrace Phase 3: Text Normalization & Entity Extraction Engine")
    parser.add_argument("--input", type=str, default="data/raw_comments.jsonl", help="Input raw comments JSONL")
    parser.add_argument("--output", type=str, default="data/extracted_evidence.jsonl", help="Output parsed evidence JSONL")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not input_path.exists():
        logger.error(f"Input file '{input_path}' does not exist.")
        return

    processed_count = 0
    flagged_count = 0

    with open(input_path, "r", encoding="utf-8") as in_f, open(output_path, "w", encoding="utf-8") as out_f:
        for line in in_f:
            if not line.strip():
                continue
            
            raw_record = json.loads(line)
            raw_text = raw_record.get("raw_text", "")
            cleaned = normalize_text(raw_text)
            
            indicators = extract_indicators(cleaned)
            processed_count += 1

            if indicators:
                flagged_count += 1
                evidence = ProcessedEvidence(
                    source_video_id=raw_record.get("video_id", ""),
                    source_video_title=raw_record.get("video_title", ""),
                    comment_id=raw_record.get("comment_id", ""),
                    author=raw_record.get("author", ""),
                    raw_comment=raw_text,
                    cleaned_comment=cleaned,
                    indicators=indicators,
                    extracted_at=datetime.now(timezone.utc)
                )
                out_f.write(evidence.model_dump_json() + "\n")

    logger.info(f"Parsing complete. Processed: {processed_count} comments.")
    logger.info(f"High-Value Hits Found: {flagged_count} records saved to '{output_path}'")

if __name__ == "__main__":
    main()
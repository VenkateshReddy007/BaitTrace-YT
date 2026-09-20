import json
import logging
import os
import sys

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

logger = logging.getLogger("BaitTrace-Brain")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    logger.critical("FATAL: GEMINI_API_KEY must be set in environment or .env file.")
    sys.exit(1)

# Module-level client — created once, reused across all calls
_client = genai.Client(api_key=GEMINI_API_KEY)


def evaluate_lead(video_title: str, comment_text: str, target_handle: str) -> dict:
    prompt = f"""
    You are a cybercrime analyst. Analyze this YouTube comment for fraud (Telegram task scams, WhatsApp rating jobs, crypto betting bots).
    
    Video Title: {video_title}
    Comment: {comment_text}
    Extracted Target: {target_handle}

    Return ONLY a valid JSON object with this exact structure, nothing else:
    {{"is_fraud": true, "scam_type": "TASK_SCAM", "confidence": 0.95, "reason": "Offers daily payout for tasks"}}
    """

    try:
        response = _client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0
            ),
        )
        
        data = json.loads(response.text)
        logger.info(f"🧠 Gemini evaluated {target_handle} -> Fraud: {data.get('is_fraud')}")
        return data
        
    except Exception as e:
        logger.error(f"Gemini inference failed ({e}). Defaulting to heuristic pass.")
        return {"is_fraud": True, "scam_type": "SUSPICIOUS_COMMENT_BOT", "confidence": 0.85, "reason": "AI fallback pass."}


def evaluate_comment(video_title: str, comment_text: str, targets: list[str]) -> list[dict]:
    """Evaluate ALL targets from a single comment in ONE Gemini call.

    Returns a list of dicts, one per target, each with keys:
    target, is_fraud, scam_type, confidence, reason.
    """
    if not targets:
        return []

    targets_str = "\n".join(f"  - {t}" for t in targets)

    prompt = f"""You are a cybercrime analyst. Analyze this YouTube comment for fraud indicators.
The comment may contain scam recruitment (Telegram task scams, WhatsApp rating jobs, crypto betting bots, UPI fraud).

Video Title: {video_title}
Comment: {comment_text}
Extracted Targets:
{targets_str}

For EACH target listed above, determine if it is associated with fraud in this comment context.
Return ONLY a valid JSON array with one object per target, in the same order. Each object must have:
{{"target": "<the target>", "is_fraud": true/false, "scam_type": "TASK_SCAM", "confidence": 0.95, "reason": "explanation"}}

If you are unsure, lean toward is_fraud=true with lower confidence. Return the JSON array only, no other text."""

    _DEFAULT = {"is_fraud": True, "scam_type": "SUSPICIOUS_COMMENT_BOT", "confidence": 0.85, "reason": "AI fallback pass."}

    try:
        response = _client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0
            ),
        )

        data = json.loads(response.text)

        # Gemini may return a single dict instead of an array
        if isinstance(data, dict):
            data = [data]

        if not isinstance(data, list):
            raise ValueError(f"Expected JSON array, got {type(data).__name__}")

        # Build a lookup by target for defensive matching
        result_map = {}
        for item in data:
            t = item.get("target", "")
            result_map[t] = item

        results = []
        for t in targets:
            if t in result_map:
                results.append(result_map[t])
            else:
                # Fallback for missing targets
                logger.warning(f"Gemini response missing target '{t}', using default.")
                results.append({**_DEFAULT, "target": t})

        logger.info(f"🧠 Gemini batch-evaluated {len(targets)} targets from one comment.")
        return results

    except Exception as e:
        logger.error(f"Gemini batch inference failed ({e}). Defaulting all targets to heuristic pass.")
        return [{**_DEFAULT, "target": t} for t in targets]
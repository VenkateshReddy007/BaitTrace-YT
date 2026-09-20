import json
import logging
import os
import sys
from typing import List, Dict, Any

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

_FALLBACK_DEFAULT = {
    "is_fraud": False,
    "role": "NEUTRAL",
    "scam_type": "UNKNOWN",
    "confidence": 0.0,
    "reason": "LLM unavailable",
    "status": "NEEDS_REVIEW"
}

def evaluate_lead(video_title: str, comment_text: str, target_handle: str) -> dict:
    prompt = f"""You are a senior cybercrime intelligence analyst investigating coordinated scam operations on YouTube.
Target Indicator: {target_handle}
Video Title: {video_title}
Comment Content: {comment_text}

Analyze whether this specific target handle in this comment is engaged in fraud (task scam, rating job, crypto scam, investment recovery).

Classification Policy:
- If the evidence is ambiguous, set is_fraud=false and explain why. Precision matters more than recall. Only mark is_fraud=true when the comment is actively RECRUITING a victim toward an off-platform contact.
- Assign the commenter's role:
    RECRUITER     - commenter is promoting the handle (the scammer/shill)
    VICTIM_REPORT - commenter is WARNING others and quoting the scammer's handle
    NEUTRAL       - handle is incidental / legitimate / creator's own community

Few-Shot Reference Examples:
1. "Join my telegram for course notes @prof_physics" -> {{"is_fraud": false, "role": "NEUTRAL", "scam_type": "NONE", "confidence": 0.95, "reason": "Creator sharing legitimate educational community channel"}}
2. "Beware @fast_crypto_king stole 10k from me do not trust him!" -> {{"is_fraud": false, "role": "VICTIM_REPORT", "scam_type": "CRYPTO_INVESTMENT", "confidence": 0.95, "reason": "Victim warning community against scammer"}}
3. "Part time work daily 2500 rs payout without investment contact telegram @job_recruiter_2026" -> {{"is_fraud": true, "role": "RECRUITER", "scam_type": "TASK_SCAM", "confidence": 0.98, "reason": "Classic task scam lure recruiting victims off-platform"}}
4. "I called the bank telephone yesterday" -> {{"is_fraud": false, "role": "NEUTRAL", "scam_type": "NONE", "confidence": 0.99, "reason": "Vocabulary word, not a contact indicator"}}

Return ONLY a valid JSON object matching:
{{"is_fraud": true, "role": "RECRUITER", "scam_type": "TASK_SCAM", "confidence": 0.95, "reason": "Explanation"}}
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
        logger.info(f"🧠 Gemini evaluated {target_handle} -> Role: {data.get('role')}, Fraud: {data.get('is_fraud')}")
        return data
    except Exception as e:
        logger.error(f"Gemini inference failed ({e}). Defaulting to fail-closed fallback.")
        return {**_FALLBACK_DEFAULT, "target": target_handle}


def evaluate_comment(video_title: str, comment_text: str, targets: list[str]) -> list[dict]:
    """Evaluate ALL targets from a single comment in ONE Gemini call.
    Fails closed: Ambiguous or failed calls produce is_fraud=False, status='NEEDS_REVIEW'.
    """
    if not targets:
        return []

    targets_str = "\n".join(f"  - {t}" for t in targets)

    prompt = f"""You are a cybercrime analyst evaluating threat indicators in a YouTube comment.
Video Title: {video_title}
Comment: {comment_text}
Extracted Targets:
{targets_str}

Precision Policy:
- If the evidence is ambiguous, set is_fraud=false and explain why. Precision matters more than recall. Only mark is_fraud=true when the comment is actively RECRUITING a victim toward an off-platform contact.
- Assign the exact role for each target:
    RECRUITER     - commenter is promoting the handle (the scammer/shill)
    VICTIM_REPORT - commenter is WARNING others and quoting the scammer's handle
    NEUTRAL       - handle is incidental / legitimate / creator's own

Negative Examples:
- Creator community link: "Join our official telegram @my_channel for pdf notes" -> is_fraud: false, role: NEUTRAL
- Business contact: "Partnership inquiries: business@okaxis" -> is_fraud: false, role: NEUTRAL
- Victim alert: "Don't pay @easy_money_bot he is a fraudster" -> is_fraud: false, role: VICTIM_REPORT
- Dictionary words: "Contacted on telephone" -> is_fraud: false, role: NEUTRAL

Return ONLY a valid JSON array of objects, one per target in order:
[
  {{
    "target": "<target>",
    "is_fraud": true/false,
    "role": "RECRUITER" | "VICTIM_REPORT" | "NEUTRAL",
    "scam_type": "TASK_SCAM" | "CRYPTO_INVESTMENT" | "IMPERSONATION" | "UNKNOWN",
    "confidence": 0.95,
    "reason": "explanation"
  }}
]"""

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
        if isinstance(data, dict):
            data = [data]

        if not isinstance(data, list):
            raise ValueError(f"Expected JSON array, got {type(data).__name__}")

        result_map = {item.get("target", ""): item for item in data}
        results = []
        for t in targets:
            if t in result_map:
                res = result_map[t]
                if "role" not in res:
                    res["role"] = "RECRUITER" if res.get("is_fraud") else "NEUTRAL"
                results.append(res)
            else:
                logger.warning(f"Gemini response missing target '{t}', defaulting fail-closed.")
                results.append({**_FALLBACK_DEFAULT, "target": t})

        logger.info(f"🧠 Gemini batch-evaluated {len(targets)} targets.")
        return results

    except Exception as e:
        logger.error(f"Gemini batch inference failed ({e}). Defaulting all targets to fail-closed.")
        return [{**_FALLBACK_DEFAULT, "target": t} for t in targets]


def evaluate_handle_campaign(handle: str, sightings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Synthesizes up to 10 sightings of the SAME handle across different videos/authors
    into a single authoritative campaign-level verdict.
    """
    if not sightings:
        return {**_FALLBACK_DEFAULT, "target": handle}

    sample_sightings = sightings[:10]
    sightings_formatted = []
    for idx, s in enumerate(sample_sightings, 1):
        v_title = s.get("video_title", "Unknown Video")
        author = s.get("author", "Unknown Author")
        c_text = s.get("comment_text", s.get("raw_comment", ""))
        is_rep = s.get("is_reply", False)
        sightings_formatted.append(
            f"Sighting {idx}:\n  Video: {v_title}\n  Author: {author} (Reply: {is_rep})\n  Comment: \"{c_text}\""
        )

    evidence_block = "\n\n".join(sightings_formatted)

    prompt = f"""You are a chief threat intelligence officer evaluating an identified entity sighted across YouTube comments.
Target Handle: {handle}
Total Sightings Provided: {len(sample_sightings)}

Observed Sighting Contexts:
{evidence_block}

Task:
Synthesize all sightings into a unified campaign verdict:
1. Is this handle an active fraud operation (is_fraud: true/false)?
2. Primary operational role:
   - RECRUITER: The entity operates a fraud/task/money funnel or uses bot shills to lure victims.
   - VICTIM_REPORT: The entity was quoted by victims exposing or reporting fraud.
   - NEUTRAL: Legitimate creator community, educational group, or harmless commercial contact.
3. Scam vector classification (TASK_SCAM, CRYPTO_INVESTMENT, IMPERSONATION, UNKNOWN).
4. Confidence score (0.0 to 1.0).
5. Comprehensive campaign assessment reason.

Precision Mandate:
If evidence is insufficient, ambiguous, or purely non-fraudulent, mark is_fraud=false and role=NEUTRAL.

Return ONLY a valid JSON object:
{{
  "is_fraud": true,
  "role": "RECRUITER",
  "scam_type": "TASK_SCAM",
  "confidence": 0.95,
  "reason": "Coordinated spam campaign observed across multiple unrelated finance videos using sock-puppet accounts."
}}"""

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
        logger.info(f"🧠 Campaign verdict for {handle} -> Fraud: {data.get('is_fraud')}, Role: {data.get('role')}")
        return data
    except Exception as e:
        logger.error(f"Gemini campaign evaluation failed ({e}). Defaulting to fail-closed.")
        return {**_FALLBACK_DEFAULT, "target": handle}
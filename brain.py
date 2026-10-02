import json
import logging
import os
import sys
from typing import List, Dict, Any, Optional

import requests as _requests  # for Jev HTTP calls
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

logger = logging.getLogger("BaitTrace-Brain")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    logger.critical("FATAL: GEMINI_API_KEY must be set in environment or .env file.")
    sys.exit(1)

# Model name is configurable via GEMINI_MODEL env var (e.g. gemini-2.0-flash for higher RPD)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

# Module-level client — created once, reused across all calls
_client = genai.Client(api_key=GEMINI_API_KEY)

# Sentinel strings that appear in Gemini quota/rate-limit errors
_QUOTA_ERROR_SIGNALS = (
    "quota",
    "rate limit",
    "rate_limit",
    "resource exhausted",
    "resourceexhausted",
    "429",
    "503",
    "overloaded",
    "unavailable",
)

# Returned when the LLM reviewed the indicator and it's clean (precision fallback)
_EVALUATED_CLEAN = {
    "is_fraud": False,
    "role": "NEUTRAL",
    "scam_type": "UNKNOWN",
    "confidence": 0.0,
    "reason": "LLM parse error — treated as clean",
    "llm_status": "EVALUATED",
}

# Returned when the LLM was never asked due to quota/availability
_PENDING_RETRY = {
    "is_fraud": False,
    "role": "NEUTRAL",
    "scam_type": "UNKNOWN",
    "confidence": 0.0,
    "reason": "LLM unavailable — quota or overload; not yet evaluated",
    "llm_status": "PENDING_RETRY",
}

# Returned when response shape was unexpected / unparseable
_FAILED_PARSE = {
    "is_fraud": False,
    "role": "NEUTRAL",
    "scam_type": "UNKNOWN",
    "confidence": 0.0,
    "reason": "LLM response malformed — treated as clean (fail-closed)",
    "llm_status": "FAILED_PARSE",
}


def _is_quota_error(exc: Exception) -> bool:
    """Return True if the exception looks like a quota exhaustion or service-unavailable error."""
    msg = str(exc).lower()
    return any(sig in msg for sig in _QUOTA_ERROR_SIGNALS)


def remaining_call_budget(used: int, budget: int) -> int:
    """Returns how many more LLM calls can be made within the given budget."""
    return max(0, budget - used)


# ═══════════════════════════════════════════════════════════════════════════
# §1  Jev (TypeSafe) Decisions API Client
# ═══════════════════════════════════════════════════════════════════════════

# Maximum context window for Jev: 32K tokens total (state + questions).
_JEV_MAX_STATE_CHARS = 28_000  # leave headroom for questions overhead

# Cumulative Jev spend tracker (reset per sweep via reset_jev_sweep_cost())
_jev_sweep_cost: float = 0.0
_jev_sweep_calls: int = 0


def reset_jev_sweep_cost():
    """Reset the per-sweep Jev cost accumulator. Called at sweep start."""
    global _jev_sweep_cost, _jev_sweep_calls
    _jev_sweep_cost = 0.0
    _jev_sweep_calls = 0


def get_jev_sweep_cost() -> float:
    """Return cumulative Jev spend (USD) for the current sweep."""
    return _jev_sweep_cost


def get_jev_sweep_calls() -> int:
    """Return cumulative Jev call count for the current sweep."""
    return _jev_sweep_calls


def _truncate_for_jev(video_title: str, comment_text: str, extra: str = "") -> str:
    """Build a state string for Jev, truncating comment_text first if the total
    exceeds the 32K-token context window (~28K chars with headroom).
    Logs when truncation happens.
    """
    base = f"Video Title: {video_title}\n"
    if extra:
        base += extra + "\n"
    remaining = _JEV_MAX_STATE_CHARS - len(base) - 50  # safety margin
    if len(comment_text) > remaining:
        logger.warning(
            f"Jev context truncation: comment_text {len(comment_text)} chars → {remaining} chars"
        )
        comment_text = comment_text[:remaining]
    return base + f"Comment: {comment_text}"


def _call_jev_decision(state: str | dict, questions: dict) -> dict:
    """POST to OpenRouter's Decisions API (alpha) to get typed answers.

    Uses the SAME OpenRouter API key already in .env (OPENROUTER_API_KEY).
    Jev is billed through this key via the Decisions endpoint.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set — required for Jev calls")

    resp = _requests.post(
        "https://openrouter.ai/api/alpha/decisions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": os.environ.get("JEV_MODEL", "typesafe/jev-1.13"),
            "state": state,
            "questions": questions,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _is_jev_retriable_error(exc: Exception) -> bool:
    """Return True if the Jev error is retriable (429/5xx/timeout)."""
    msg = str(exc).lower()
    if isinstance(exc, _requests.exceptions.Timeout):
        return True
    if isinstance(exc, _requests.exceptions.HTTPError):
        status = getattr(exc.response, "status_code", 0)
        return status == 429 or status >= 500
    return any(sig in msg for sig in ("429", "503", "timeout", "unavailable", "overloaded"))


def _record_jev_cost(response_data: dict):
    """Extract and accumulate usage.cost from a Jev response."""
    global _jev_sweep_cost, _jev_sweep_calls
    _jev_sweep_calls += 1
    usage = response_data.get("usage", {})
    cost = usage.get("cost", 0.0)
    if isinstance(cost, (int, float)):
        _jev_sweep_cost += cost
        logger.debug(f"Jev call cost: ${cost:.6f} (sweep total: ${_jev_sweep_cost:.4f})")


def _jev_cost_ceiling_exceeded() -> bool:
    """Check if cumulative Jev cost has exceeded the per-sweep ceiling."""
    from config import JEV_COST_CEILING_PER_SWEEP
    return _jev_sweep_cost >= JEV_COST_CEILING_PER_SWEEP


# ═══════════════════════════════════════════════════════════════════════════
# §2  Jev-based per-comment classification
# ═══════════════════════════════════════════════════════════════════════════

def _jev_evaluate_comment(video_title: str, comment_text: str, targets: list[str]) -> list[dict]:
    """Evaluate ALL targets from a single comment using Jev Decisions API.

    For each target, asks Jev three typed questions:
    - is_fraud (noul): active off-platform scam recruitment?
    - role (choice): RECRUITER / VICTIM_REPORT / NEUTRAL
    - scam_type (choice): TASK_SCAM / RATING_JOB / CRYPTO_BETTING / OTHER / NONE

    Error policy mirrors Gemini's fail-closed semantics:
    - HTTP 429/5xx/timeout → llm_status='PENDING_RETRY'
    - Malformed response    → llm_status='FAILED_PARSE', is_fraud=False
    """
    if not targets:
        return []

    results = []
    for target in targets:
        if _jev_cost_ceiling_exceeded():
            logger.warning(f"Jev cost ceiling exceeded (${_jev_sweep_cost:.4f}) — marking {target} PENDING_RETRY")
            results.append({**_PENDING_RETRY, "target": target})
            continue

        state_str = _truncate_for_jev(
            video_title, comment_text,
            f"Target Indicator: {target}"
        )

        questions = {
            "is_fraud": {
                "type": "noul",
                "question": (
                    "Does this comment actively recruit a victim toward an off-platform "
                    "scam contact (Telegram/WhatsApp/phone/UPI) for a task scam, "
                    "rating-job scam, or crypto/colour-prediction betting scheme?"
                ),
            },
            "role": {
                "type": "choice",
                "question": "What role does the commenter play relative to this indicator?",
                "options": {
                    "RECRUITER": "Commenter is promoting/advertising the handle — the scammer or their shill",
                    "VICTIM_REPORT": "Commenter is WARNING others, quoting the scammer's handle as a cautionary reference",
                    "NEUTRAL": "Handle is incidental, legitimate, or the creator's own community link",
                },
            },
            "scam_type": {
                "type": "choice",
                "question": "What category of scam does this comment promote?",
                "options": {
                    "TASK_SCAM": "Task-based earning scam (e.g. 'complete tasks daily earn 2500')",
                    "RATING_JOB": "App/product rating job scam",
                    "CRYPTO_BETTING": "Crypto investment, colour-prediction, or betting scheme",
                    "OTHER": "Another type of scam not listed above",
                    "NONE": "No scam activity detected",
                },
            },
        }

        try:
            resp_data = _call_jev_decision(state_str, questions)
            _record_jev_cost(resp_data)

            answers = resp_data.get("answers", resp_data)

            # Extract is_fraud from noul probability
            is_fraud_answer = answers.get("is_fraud", {})
            if isinstance(is_fraud_answer, dict):
                noul_prob = is_fraud_answer.get("probability", is_fraud_answer.get("prob", 0.0))
            elif isinstance(is_fraud_answer, (bool, int, float)):
                noul_prob = float(is_fraud_answer)
            else:
                noul_prob = 0.0

            # Extract role choice
            role_answer = answers.get("role", {})
            if isinstance(role_answer, dict):
                role = role_answer.get("choice", role_answer.get("value", "NEUTRAL"))
                role_confidence = role_answer.get("confidence", 0.0)
            elif isinstance(role_answer, str):
                role = role_answer
                role_confidence = 0.0
            else:
                role = "NEUTRAL"
                role_confidence = 0.0

            # Extract scam_type choice
            scam_answer = answers.get("scam_type", {})
            if isinstance(scam_answer, dict):
                scam_type = scam_answer.get("choice", scam_answer.get("value", "NONE"))
            elif isinstance(scam_answer, str):
                scam_type = scam_answer
            else:
                scam_type = "NONE"

            # Validate expected values
            if role not in ("RECRUITER", "VICTIM_REPORT", "NEUTRAL"):
                logger.warning(f"Jev returned unexpected role '{role}' for {target}, defaulting NEUTRAL")
                role = "NEUTRAL"
            if scam_type not in ("TASK_SCAM", "RATING_JOB", "CRYPTO_BETTING", "OTHER", "NONE"):
                logger.warning(f"Jev returned unexpected scam_type '{scam_type}' for {target}, defaulting NONE")
                scam_type = "NONE"

            results.append({
                "target": target,
                "is_fraud": noul_prob >= 0.5,
                "role": role,
                "scam_type": scam_type,
                "confidence": role_confidence,
                "reason": None,  # filled later by explain_promoted_lead() only for promoted handles
                "llm_status": "EVALUATED",
            })
            logger.info(f"🤖 Jev evaluated {target} → Role: {role}, Fraud: {noul_prob >= 0.5} (p={noul_prob:.3f})")

        except Exception as e:
            if _is_jev_retriable_error(e):
                logger.warning(f"⏳ Jev retriable error for {target} — marking PENDING_RETRY. ({e})")
                results.append({**_PENDING_RETRY, "target": target})
            else:
                logger.error(f"Jev inference failed (parse/format error) for {target}: {e}. Fail-closed.")
                results.append({**_FAILED_PARSE, "target": target})

    return results


# ═══════════════════════════════════════════════════════════════════════════
# §3  Jev-based campaign-level judgment
# ═══════════════════════════════════════════════════════════════════════════

def _jev_evaluate_handle_campaign(handle: str, sightings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Campaign-level verdict via Jev for a handle with multiple sightings.

    Asks a choice question over representative sightings with options matching
    the existing tier system (CONFIRMED / PROBABLE / WATCH / DISCARD).
    Also asks a noul for multi-video coordination detection.
    """
    if not sightings:
        return {**_EVALUATED_CLEAN, "target": handle}

    if _jev_cost_ceiling_exceeded():
        logger.warning(f"Jev cost ceiling exceeded — skipping campaign eval for {handle}")
        return {**_PENDING_RETRY, "target": handle}

    sample_sightings = sightings[:10]
    sightings_text_parts = []
    for idx, s in enumerate(sample_sightings, 1):
        v_title = s.get("video_title", "Unknown Video")
        author = s.get("author", "Unknown Author")
        c_text = s.get("comment_text", s.get("raw_comment", ""))
        is_rep = s.get("is_reply", False)
        sightings_text_parts.append(
            f"Sighting {idx}: Video='{v_title}' Author='{author}' Reply={is_rep} Comment=\"{c_text}\""
        )

    evidence_block = "\n".join(sightings_text_parts)
    state_str = _truncate_for_jev(
        f"Campaign Analysis for {handle}",
        evidence_block,
        f"Target Handle: {handle}\nTotal Sightings: {len(sample_sightings)}"
    )

    questions = {
        "is_coordinated_campaign": {
            "type": "noul",
            "question": (
                "Is this a coordinated multi-video scam campaign rather than an isolated incident? "
                "Consider: same handle across multiple unrelated videos, multiple distinct posting authors, "
                "and copy-paste template comments."
            ),
        },
        "campaign_tier": {
            "type": "choice",
            "question": "Based on all sightings, what threat tier should this handle be assigned?",
            "options": {
                "CONFIRMED": "Clear, multi-video coordinated fraud operation with strong evidence",
                "PROBABLE": "Likely fraud with moderate evidence, needs human review",
                "WATCH": "Some suspicious signals but insufficient evidence for action",
                "DISCARD": "Not a fraud operation — legitimate, victim report, or insufficient evidence",
            },
        },
    }

    try:
        resp_data = _call_jev_decision(state_str, questions)
        _record_jev_cost(resp_data)

        answers = resp_data.get("answers", resp_data)

        # Parse campaign_tier choice
        tier_answer = answers.get("campaign_tier", {})
        if isinstance(tier_answer, dict):
            tier_choice = tier_answer.get("choice", tier_answer.get("value", "WATCH"))
            tier_confidence = tier_answer.get("confidence", 0.0)
        elif isinstance(tier_answer, str):
            tier_choice = tier_answer
            tier_confidence = 0.0
        else:
            tier_choice = "WATCH"
            tier_confidence = 0.0

        # Parse coordination noul
        coord_answer = answers.get("is_coordinated_campaign", {})
        if isinstance(coord_answer, dict):
            coord_prob = coord_answer.get("probability", coord_answer.get("prob", 0.0))
        elif isinstance(coord_answer, (bool, int, float)):
            coord_prob = float(coord_answer)
        else:
            coord_prob = 0.0

        is_fraud = tier_choice in ("CONFIRMED", "PROBABLE")
        role = "RECRUITER" if is_fraud else "NEUTRAL"

        result = {
            "target": handle,
            "is_fraud": is_fraud,
            "role": role,
            "scam_type": "UNKNOWN",
            "confidence": tier_confidence,
            "reason": f"Jev campaign: tier={tier_choice}, coordination_prob={coord_prob:.3f}",
            "llm_status": "EVALUATED",
        }
        logger.info(f"🤖 Jev campaign verdict for {handle} → Tier: {tier_choice}, Coordination: {coord_prob:.3f}")
        return result

    except Exception as e:
        if _is_jev_retriable_error(e):
            logger.warning(f"⏳ Jev retriable error for campaign eval of {handle} — marking PENDING_RETRY. ({e})")
            return {**_PENDING_RETRY, "target": handle}
        logger.error(f"Jev campaign eval failed for {handle}: {e}. Fail-closed.")
        return {**_FAILED_PARSE, "target": handle}


# ═══════════════════════════════════════════════════════════════════════════
#  Original Gemini Functions — COMPLETELY UNCHANGED
#  (evaluate_lead, evaluate_comment, evaluate_batch, evaluate_handle_campaign
#   are the original Gemini implementations, preserved as-is for
#   CLASSIFIER_PROVIDER=gemini fallback)
# ═══════════════════════════════════════════════════════════════════════════

def _gemini_evaluate_lead(video_title: str, comment_text: str, target_handle: str) -> dict:
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
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0
            ),
        )
        data = json.loads(response.text)
        data["llm_status"] = "EVALUATED"
        logger.info(f"🧠 Gemini evaluated {target_handle} -> Role: {data.get('role')}, Fraud: {data.get('is_fraud')}")
        return data
    except Exception as e:
        if _is_quota_error(e):
            logger.warning(f"⏳ Quota/availability error evaluating {target_handle} — marking PENDING_RETRY. ({e})")
            return {**_PENDING_RETRY, "target": target_handle}
        logger.error(f"Gemini inference failed (parse/format error): {e}. Treating as clean (EVALUATED).")
        return {**_EVALUATED_CLEAN, "target": target_handle}


def _gemini_evaluate_comment(video_title: str, comment_text: str, targets: list[str]) -> list[dict]:
    """Evaluate ALL targets from a single comment in ONE Gemini call.

    Error policy:
    - Quota / availability error  -> all targets get llm_status='PENDING_RETRY' (not yet evaluated)
    - Parse / format error        -> all targets get llm_status='EVALUATED', is_fraud=False (fail-closed clean)
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
            model=GEMINI_MODEL,
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
                res["llm_status"] = "EVALUATED"
                results.append(res)
            else:
                logger.warning(f"Gemini response missing target '{t}', defaulting fail-closed.")
                results.append({**_EVALUATED_CLEAN, "target": t})

        logger.info(f"🧠 Gemini batch-evaluated {len(targets)} targets.")
        return results

    except Exception as e:
        if _is_quota_error(e):
            logger.warning(f"⏳ Quota/availability error for batch {targets} — marking all PENDING_RETRY. ({e})")
            return [{**_PENDING_RETRY, "target": t} for t in targets]
        logger.error(f"Gemini batch inference failed (parse/format error): {e}. Treating all as clean (EVALUATED).")
        return [{**_EVALUATED_CLEAN, "target": t} for t in targets]


def _gemini_evaluate_handle_campaign(handle: str, sightings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Synthesizes up to 10 sightings of the SAME handle across different videos/authors
    into a single authoritative campaign-level verdict.
    """
    if not sightings:
        return {**_EVALUATED_CLEAN, "target": handle}

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
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0
            ),
        )
        data = json.loads(response.text)
        data["llm_status"] = "EVALUATED"
        logger.info(f"🧠 Campaign verdict for {handle} -> Fraud: {data.get('is_fraud')}, Role: {data.get('role')}")
        return data
    except Exception as e:
        if _is_quota_error(e):
            logger.warning(f"⏳ Quota error for campaign eval of {handle} — marking PENDING_RETRY. ({e})")
            return {**_PENDING_RETRY, "target": handle}
        logger.error(f"Gemini campaign evaluation failed (parse/format error): {e}. Treating as clean.")
        return {**_EVALUATED_CLEAN, "target": handle}


# ═══════════════════════════════════════════════════════════════════════════
#  Provider-Dispatched Public API
#  These are the functions imported by main.py and the rest of the pipeline.
#  When CLASSIFIER_PROVIDER=jev, per-comment/campaign classification routes
#  to Jev.  When CLASSIFIER_PROVIDER=gemini, the original Gemini path runs.
# ═══════════════════════════════════════════════════════════════════════════

def _get_classifier_provider() -> str:
    """Resolve the active classifier provider at call time (not import time)
    so that tests can monkeypatch config or env between calls."""
    from config import CLASSIFIER_PROVIDER
    return os.environ.get("CLASSIFIER_PROVIDER", CLASSIFIER_PROVIDER).lower()


def evaluate_lead(video_title: str, comment_text: str, target_handle: str) -> dict:
    """Single-target evaluation. Dispatches to active provider."""
    if _get_classifier_provider() == "jev":
        results = _jev_evaluate_comment(video_title, comment_text, [target_handle])
        return results[0] if results else {**_EVALUATED_CLEAN, "target": target_handle}
    return _gemini_evaluate_lead(video_title, comment_text, target_handle)


def evaluate_comment(video_title: str, comment_text: str, targets: list[str]) -> list[dict]:
    """Multi-target per-comment evaluation. Dispatches to active provider."""
    if _get_classifier_provider() == "jev":
        return _jev_evaluate_comment(video_title, comment_text, targets)
    return _gemini_evaluate_comment(video_title, comment_text, targets)


def evaluate_batch(
    items: List[Dict[str, Any]],
    budget: int = 15,
) -> List[Dict[str, Any]]:
    """Sweep-level batch evaluator. Consumes a pre-collected list of pending items,
    sorted by heuristic_score DESC, and calls evaluate_comment per unique comment
    until the budget is exhausted.

    Each item must have keys:
      - video_title, comment_text, target, heuristic_score (float), comment_id

    Returns a list of dicts keyed by (comment_id, target) -> verdict dict.
    Items that don't fit in the budget are left as PENDING_RETRY.
    """
    if not items or budget <= 0:
        return []

    # Sort by heuristic_score descending so highest-confidence hits get LLM time first
    items_sorted = sorted(items, key=lambda x: x.get("heuristic_score", 0.0), reverse=True)

    # Group targets by (video_title, comment_text, comment_id) to reuse comment-level batching
    from collections import defaultdict
    comment_groups: Dict[str, Dict[str, Any]] = {}
    for item in items_sorted:
        cid = item["comment_id"]
        if cid not in comment_groups:
            comment_groups[cid] = {
                "video_title": item["video_title"],
                "comment_text": item["comment_text"],
                "targets": [],
                "items": [],
            }
        comment_groups[cid]["targets"].append(item["target"])
        comment_groups[cid]["items"].append(item)

    results: List[Dict[str, Any]] = []
    calls_made = 0

    for cid, group in comment_groups.items():
        if remaining_call_budget(calls_made, budget) <= 0:
            # Budget exhausted — mark remaining as PENDING_RETRY
            for item in group["items"]:
                results.append({
                    "comment_id": cid,
                    "target": item["target"],
                    "verdict": {**_PENDING_RETRY, "target": item["target"]},
                })
            continue

        # Also check Jev cost ceiling for jev provider
        if _get_classifier_provider() == "jev" and _jev_cost_ceiling_exceeded():
            logger.warning(f"Jev cost ceiling reached — marking remaining items PENDING_RETRY")
            for item in group["items"]:
                results.append({
                    "comment_id": cid,
                    "target": item["target"],
                    "verdict": {**_PENDING_RETRY, "target": item["target"]},
                })
            continue

        verdicts = evaluate_comment(
            group["video_title"],
            group["comment_text"],
            group["targets"],
        )
        calls_made += 1

        verdict_map = {v.get("target", ""): v for v in verdicts}
        for item in group["items"]:
            verdict = verdict_map.get(item["target"], {**_PENDING_RETRY, "target": item["target"]})
            results.append({
                "comment_id": cid,
                "target": item["target"],
                "verdict": verdict,
            })

    provider = _get_classifier_provider()
    logger.info(f"🧠 evaluate_batch [{provider}]: {calls_made} LLM calls for {len(items)} indicators (budget={budget}).")
    return results


def evaluate_handle_campaign(handle: str, sightings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Campaign-level judgment. Dispatches to active provider."""
    if _get_classifier_provider() == "jev":
        return _jev_evaluate_handle_campaign(handle, sightings)
    return _gemini_evaluate_handle_campaign(handle, sightings)


# ═══════════════════════════════════════════════════════════════════════════
# §4  Gemini's narrowed role: explain_promoted_lead()
#
# Called ONLY for handles that just crossed into PROBABLE or CONFIRMED tier
# this run.  Sends the comment + Jev's typed verdict to Gemini for a
# one-sentence human-readable explanation to fill the dashboard's `reason`.
# ═══════════════════════════════════════════════════════════════════════════

def explain_promoted_lead(sighting: Dict[str, Any]) -> str:
    """Generate a one-sentence human-readable explanation for a newly promoted lead.

    Called ONLY in the Phase 4 promotion step, ONLY for handles that just
    crossed into PROBABLE or CONFIRMED tier this run.

    If this fails (quota exhaustion, error), returns a fallback string —
    a null/failed reason must NEVER block or reverse a promotion.
    """
    comment_text = sighting.get("comment_text", sighting.get("raw_comment", ""))
    video_title = sighting.get("video_title", "")
    handle = sighting.get("handle_norm", sighting.get("target", ""))
    llm_role = sighting.get("llm_role", "UNKNOWN")
    llm_is_fraud = sighting.get("llm_is_fraud", False)
    scam_type = sighting.get("scam_type", sighting.get("llm_scam_type", "UNKNOWN"))
    confidence = sighting.get("llm_confidence", 0.0)

    prompt = f"""You are a cybercrime analyst writing a brief explanation for a fraud lead dashboard.

Handle: {handle}
Video: {video_title}
Comment: {comment_text}

Classifier verdict:
- Role: {llm_role}
- Is Fraud: {llm_is_fraud}
- Scam Type: {scam_type}
- Confidence: {confidence}

Write ONE concise sentence explaining why this handle was flagged as a likely scam operation.
Focus on the observable evidence (what the comment says, the off-platform contact method, the scam type).
Return ONLY the explanation sentence, nothing else."""

    try:
        response = _client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=150,
            ),
        )
        reason = response.text.strip()
        if reason:
            logger.info(f"🧠 Gemini explanation for {handle}: {reason[:80]}...")
            return reason
        return f"Promoted on calibrated-decision evidence (role={llm_role}, scam_type={scam_type}); explanation pending"
    except Exception as e:
        logger.warning(f"explain_promoted_lead failed for {handle}: {e}")
        return f"Promoted on calibrated-decision evidence (role={llm_role}, scam_type={scam_type}); explanation pending"
"""
BaitTrace Pre-LLM Heuristic Gate
Filters indicators before sending to Gemini to cut false positives and API spend.
"""
import re
from .parser import ExtractedIndicator

LURE_TOKENS = {
    "payment", "paytm", "withdraw", "withdrawal", "daily",
    "income", "earn", "job", "work", "proof", "dm", "join",
    "invest", "profit", "trust", "legit", "guarantee", "free",
    "task", "recharge", "bonus"
}

# Pre-compiled word-boundary pattern for efficient matching
_LURE_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in LURE_TOKENS) + r")\b",
    re.IGNORECASE
)


def should_escalate(ind: ExtractedIndicator, cleaned_comment: str) -> bool:
    """Decide whether an extracted indicator warrants LLM triage.

    Rules:
    - TELEGRAM and UPI indicators -> always escalate.
    - PHONE and WHATSAPP -> escalate only if the cleaned comment contains
      at least one LURE_TOKEN (case-insensitive word match).
    """
    if ind.indicator_type in ("TELEGRAM", "UPI"):
        return True

    if ind.indicator_type in ("PHONE", "WHATSAPP"):
        return bool(_LURE_PATTERN.search(cleaned_comment))

    return False

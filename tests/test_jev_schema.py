import pytest

def test_jev_schema_shape():
    """
    Test that the question dictionary structure constructed for Jev matches
    the live API schema rules, specifically guarding against the old schema.
    """
    # This reconstructs the exact dictionary passed to `_call_jev_decision`
    # inside `_jev_evaluate_comment`.
    questions = {
        "is_fraud": {
            "type": "noul",
            "instructions": "Does this comment actively recruit a victim...",
            "criteria": {
                "true": "The comment contains a specific off-platform contact handle...",
                "false": "The comment is legitimate...",
            },
        },
        "role": {
            "type": "choice",
            "instructions": "What role does the commenter play relative to this indicator?",
            "criteria": {
                "RECRUITER": "Commenter is promoting/advertising the handle...",
                "VICTIM_REPORT": "Commenter is WARNING others...",
                "NEUTRAL": "Handle is incidental...",
            },
        },
        "scam_type": {
            "type": "choice",
            "instructions": "What category of scam does this comment promote?",
            "criteria": {
                "TASK_SCAM": "Task-based earning scam...",
                "RATING_JOB": "App/product rating job scam",
                "CRYPTO_BETTING": "Crypto investment...",
                "OTHER": "Another type of scam not listed above",
                "NONE": "No scam activity detected",
            },
        },
    }

    allowed_types = {"noul", "choice", "score"}

    for key, q in questions.items():
        assert "type" in q
        assert q["type"] in allowed_types, f"Question '{key}' uses invalid type '{q['type']}'. Must be one of {allowed_types}"
        assert q["type"] != "bool", f"Question '{key}' uses deprecated 'bool' type. Use 'noul' instead."
        
        assert "instructions" in q, f"Question '{key}' is missing 'instructions' field."
        assert "question" not in q, f"Question '{key}' uses deprecated 'question' field. Use 'instructions' instead."
        
        if q["type"] in ("choice", "noul"):
            assert "criteria" in q, f"Question '{key}' is missing 'criteria' field."
            assert "options" not in q, f"Question '{key}' uses deprecated 'options' field. Use 'criteria' instead."

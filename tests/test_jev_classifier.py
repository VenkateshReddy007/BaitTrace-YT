"""
BaitTrace — Jev Classifier Unit Tests

Monkeypatches _call_jev_decision to verify:
1. Normalized output shape matches exactly what evaluate_comment returns
   (same keys, same types) regardless of provider.
2. Fail-closed behavior on simulated 429/5xx errors.
3. Cost ceiling enforcement.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import baittrace.brain
import requests as _requests


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_jev_success_response(is_fraud_prob=0.95, role="RECRUITER", scam_type="TASK_SCAM"):
    """Returns a mock Jev response dict with the expected structure."""
    return {
        "answers": {
            "is_fraud": {"probability": is_fraud_prob},
            "role": {"choice": role, "confidence": 0.92},
            "scam_type": {"choice": scam_type, "confidence": 0.88},
        },
        "usage": {"cost": 0.0001},
    }


def _make_jev_clean_response():
    """Returns a mock Jev response for a clean/neutral comment."""
    return {
        "answers": {
            "is_fraud": {"probability": 0.05},
            "role": {"choice": "NEUTRAL", "confidence": 0.97},
            "scam_type": {"choice": "NONE", "confidence": 0.95},
        },
        "usage": {"cost": 0.0001},
    }


# ── Required output shape ────────────────────────────────────────────────
# These are the EXACT keys and types the rest of the pipeline expects from
# evaluate_comment(), regardless of whether the classifier is Jev or Gemini.
REQUIRED_KEYS = {
    "target": str,
    "is_fraud": bool,
    "role": str,
    "scam_type": str,
    "confidence": (int, float),
    "llm_status": str,
}


def _force_jev_provider(monkeypatch):
    """Force CLASSIFIER_PROVIDER=jev at both env and config level."""
    monkeypatch.setenv("CLASSIFIER_PROVIDER", "jev")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-000")
    import config
    monkeypatch.setattr(config, "CLASSIFIER_PROVIDER", "jev")


# ══════════════════════════════════════════════════════════════════════════
# Test: Jev evaluate_comment returns correct normalized shape
# ══════════════════════════════════════════════════════════════════════════

class TestJevEvaluateComment:
    """Verify Jev-based evaluate_comment returns the exact same dict shape
    that the pipeline (main.py) expects — identical to what Gemini returns."""

    def test_jev_returns_correct_shape_for_fraud(self, monkeypatch):
        _force_jev_provider(monkeypatch)
        brain.reset_jev_sweep_cost()

        monkeypatch.setattr(
            brain, "_call_jev_decision",
            lambda state, questions: _make_jev_success_response(0.95, "RECRUITER", "TASK_SCAM"),
        )

        verdicts = brain.evaluate_comment(
            video_title="Earn Money Online Daily",
            comment_text="Part time work daily 2500 rs payout contact telegram @scam_bot_99",
            targets=["@scam_bot_99"],
        )

        assert len(verdicts) == 1
        v = verdicts[0]

        # Check every required key exists and has the right type
        for key, expected_type in REQUIRED_KEYS.items():
            assert key in v, f"Missing required key '{key}' in Jev verdict"
            assert isinstance(v[key], expected_type), (
                f"Key '{key}' should be {expected_type}, got {type(v[key])}"
            )

        assert v["target"] == "@scam_bot_99"
        assert v["is_fraud"] is True
        assert v["role"] == "RECRUITER"
        assert v["scam_type"] == "TASK_SCAM"
        assert v["llm_status"] == "EVALUATED"

    def test_jev_returns_correct_shape_for_clean(self, monkeypatch):
        _force_jev_provider(monkeypatch)
        brain.reset_jev_sweep_cost()

        monkeypatch.setattr(
            brain, "_call_jev_decision",
            lambda state, questions: _make_jev_clean_response(),
        )

        verdicts = brain.evaluate_comment(
            video_title="Python Tutorial",
            comment_text="Join our official discord @study_group",
            targets=["@study_group"],
        )

        assert len(verdicts) == 1
        v = verdicts[0]
        assert v["is_fraud"] is False
        assert v["role"] == "NEUTRAL"
        assert v["scam_type"] == "NONE"
        assert v["llm_status"] == "EVALUATED"

    def test_jev_handles_multiple_targets(self, monkeypatch):
        _force_jev_provider(monkeypatch)
        brain.reset_jev_sweep_cost()

        call_count = {"n": 0}

        def mock_jev(state, questions):
            call_count["n"] += 1
            return _make_jev_success_response(0.85, "RECRUITER", "CRYPTO_BETTING")

        monkeypatch.setattr(brain, "_call_jev_decision", mock_jev)

        targets = ["@scam_1", "@scam_2", "@scam_3"]
        verdicts = brain.evaluate_comment(
            video_title="Crypto Secrets",
            comment_text="Join @scam_1 @scam_2 @scam_3 for guaranteed profits",
            targets=targets,
        )

        assert len(verdicts) == 3
        # Jev makes one call per target (no batch mode in Decisions API)
        assert call_count["n"] == 3
        for v, t in zip(verdicts, targets):
            assert v["target"] == t
            assert v["is_fraud"] is True


# ══════════════════════════════════════════════════════════════════════════
# Test: Fail-closed behavior on HTTP errors
# ══════════════════════════════════════════════════════════════════════════

class TestJevFailClosed:
    """Verify that Jev HTTP errors produce the correct fail-closed behavior,
    exactly matching the fail-closed semantics built for Gemini."""

    def test_jev_429_returns_pending_retry(self, monkeypatch):
        """A 429 (rate limit) should produce llm_status='PENDING_RETRY', never a verdict."""
        _force_jev_provider(monkeypatch)
        brain.reset_jev_sweep_cost()

        mock_response = type("MockResp", (), {"status_code": 429, "text": "rate limited"})()
        error = _requests.exceptions.HTTPError(response=mock_response)
        monkeypatch.setattr(brain, "_call_jev_decision", lambda s, q: (_ for _ in ()).throw(error))

        verdicts = brain.evaluate_comment(
            video_title="Test Video",
            comment_text="Contact @scam_test for profits",
            targets=["@scam_test"],
        )

        assert len(verdicts) == 1
        v = verdicts[0]
        assert v["is_fraud"] is False, "Must NEVER manufacture fraud on HTTP error"
        assert v["llm_status"] == "PENDING_RETRY"
        assert v["role"] == "NEUTRAL"

    def test_jev_500_returns_pending_retry(self, monkeypatch):
        """A 500 server error should produce llm_status='PENDING_RETRY'."""
        _force_jev_provider(monkeypatch)
        brain.reset_jev_sweep_cost()

        mock_response = type("MockResp", (), {"status_code": 500, "text": "internal error"})()
        error = _requests.exceptions.HTTPError(response=mock_response)
        monkeypatch.setattr(brain, "_call_jev_decision", lambda s, q: (_ for _ in ()).throw(error))

        verdicts = brain.evaluate_comment(
            video_title="Test",
            comment_text="Join @fail_test",
            targets=["@fail_test"],
        )

        assert len(verdicts) == 1
        assert verdicts[0]["llm_status"] == "PENDING_RETRY"
        assert verdicts[0]["is_fraud"] is False

    def test_jev_timeout_returns_pending_retry(self, monkeypatch):
        """A timeout should produce llm_status='PENDING_RETRY'."""
        _force_jev_provider(monkeypatch)
        brain.reset_jev_sweep_cost()

        error = _requests.exceptions.Timeout("Connection timed out")
        monkeypatch.setattr(brain, "_call_jev_decision", lambda s, q: (_ for _ in ()).throw(error))

        verdicts = brain.evaluate_comment(
            video_title="Test",
            comment_text="Join @timeout_test",
            targets=["@timeout_test"],
        )

        assert len(verdicts) == 1
        assert verdicts[0]["llm_status"] == "PENDING_RETRY"

    def test_jev_malformed_response_returns_failed_parse(self, monkeypatch):
        """A malformed/unexpected response shape should produce llm_status='FAILED_PARSE'."""
        _force_jev_provider(monkeypatch)
        brain.reset_jev_sweep_cost()

        # Return something that will cause a KeyError or parse failure
        monkeypatch.setattr(
            brain, "_call_jev_decision",
            lambda s, q: (_ for _ in ()).throw(ValueError("Unexpected response format")),
        )

        verdicts = brain.evaluate_comment(
            video_title="Test",
            comment_text="Join @parse_fail_test",
            targets=["@parse_fail_test"],
        )

        assert len(verdicts) == 1
        assert verdicts[0]["llm_status"] == "FAILED_PARSE"
        assert verdicts[0]["is_fraud"] is False

    def test_jev_error_does_not_affect_other_targets(self, monkeypatch):
        """If one target errors, other targets should still be evaluated."""
        _force_jev_provider(monkeypatch)
        brain.reset_jev_sweep_cost()

        call_count = {"n": 0}

        def mock_jev_with_failure(state, questions):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise _requests.exceptions.Timeout("timeout on second call")
            return _make_jev_success_response()

        monkeypatch.setattr(brain, "_call_jev_decision", mock_jev_with_failure)

        verdicts = brain.evaluate_comment(
            video_title="Test",
            comment_text="Join @ok_1 @fail_2 @ok_3",
            targets=["@ok_1", "@fail_2", "@ok_3"],
        )

        assert len(verdicts) == 3
        assert verdicts[0]["llm_status"] == "EVALUATED"
        assert verdicts[1]["llm_status"] == "PENDING_RETRY"
        assert verdicts[2]["llm_status"] == "EVALUATED"


# ══════════════════════════════════════════════════════════════════════════
# Test: Jev cost tracking
# ══════════════════════════════════════════════════════════════════════════

class TestJevCostTracking:
    """Verify that Jev usage.cost is accumulated correctly."""

    def test_cost_accumulates(self, monkeypatch):
        _force_jev_provider(monkeypatch)
        brain.reset_jev_sweep_cost()

        monkeypatch.setattr(
            brain, "_call_jev_decision",
            lambda s, q: {
                "answers": {
                    "is_fraud": {"probability": 0.1},
                    "role": {"choice": "NEUTRAL", "confidence": 0.9},
                    "scam_type": {"choice": "NONE", "confidence": 0.9},
                },
                "usage": {"cost": 0.001},
            },
        )

        brain.evaluate_comment("Title", "Comment text", ["@t1"])
        brain.evaluate_comment("Title", "Comment text", ["@t2"])

        assert brain.get_jev_sweep_cost() == pytest.approx(0.002, abs=1e-6)
        assert brain.get_jev_sweep_calls() == 2

    def test_cost_ceiling_stops_evaluation(self, monkeypatch):
        """Once the cost ceiling is exceeded, new evaluations should be PENDING_RETRY."""
        _force_jev_provider(monkeypatch)
        brain.reset_jev_sweep_cost()

        import config
        monkeypatch.setattr(config, "JEV_COST_CEILING_PER_SWEEP", 0.001)

        # First call: succeeds and costs 0.001 (exactly at ceiling)
        call_count = {"n": 0}

        def mock_jev(state, questions):
            call_count["n"] += 1
            return {
                "answers": {
                    "is_fraud": {"probability": 0.9},
                    "role": {"choice": "RECRUITER", "confidence": 0.9},
                    "scam_type": {"choice": "TASK_SCAM", "confidence": 0.9},
                },
                "usage": {"cost": 0.001},
            }

        monkeypatch.setattr(brain, "_call_jev_decision", mock_jev)

        v1 = brain.evaluate_comment("T", "C", ["@first"])
        assert v1[0]["llm_status"] == "EVALUATED"

        # Second call: should be blocked by cost ceiling
        v2 = brain.evaluate_comment("T", "C", ["@second"])
        assert v2[0]["llm_status"] == "PENDING_RETRY"
        # _call_jev_decision should NOT have been called for the second target
        assert call_count["n"] == 1


# ══════════════════════════════════════════════════════════════════════════
# Test: Campaign-level Jev evaluation
# ══════════════════════════════════════════════════════════════════════════

class TestJevCampaignEvaluation:
    """Verify Jev campaign-level evaluation returns expected shape."""

    def test_campaign_verdict_shape(self, monkeypatch):
        _force_jev_provider(monkeypatch)
        brain.reset_jev_sweep_cost()

        monkeypatch.setattr(
            brain, "_call_jev_decision",
            lambda s, q: {
                "answers": {
                    "is_coordinated_campaign": {"probability": 0.85},
                    "campaign_tier": {"choice": "CONFIRMED", "confidence": 0.90},
                },
                "usage": {"cost": 0.002},
            },
        )

        verdict = brain.evaluate_handle_campaign(
            "@campaign_handle",
            [
                {"video_title": "V1", "comment_text": "Join @campaign_handle", "author": "u1"},
                {"video_title": "V2", "comment_text": "Join @campaign_handle daily", "author": "u2"},
                {"video_title": "V3", "comment_text": "Earn via @campaign_handle", "author": "u3"},
            ],
        )

        assert verdict["llm_status"] == "EVALUATED"
        assert verdict["is_fraud"] is True
        assert "target" in verdict
        assert "confidence" in verdict

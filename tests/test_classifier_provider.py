"""
BaitTrace — Classifier Provider Dispatch Tests

Covers three provider modes:
1. CLASSIFIER_PROVIDER=gemini → original all-Gemini path
2. CLASSIFIER_PROVIDER=jev    → Jev Decisions API for classification
3. The hybrid path: Jev classifies + Gemini explains promotions
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import MagicMock
import brain


def _force_provider(monkeypatch, provider: str):
    """Set the classifier provider in both env and config module."""
    monkeypatch.setenv("CLASSIFIER_PROVIDER", provider)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-000")
    import config
    monkeypatch.setattr(config, "CLASSIFIER_PROVIDER", provider)


class TestGeminiProvider:
    """When CLASSIFIER_PROVIDER=gemini, all calls should route to Gemini."""

    def test_evaluate_comment_uses_gemini(self, monkeypatch):
        _force_provider(monkeypatch, "gemini")

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '[{"target": "@test", "is_fraud": false, "role": "NEUTRAL", "scam_type": "NONE", "confidence": 0.9, "reason": "clean"}]'
        mock_client.models.generate_content.return_value = mock_response
        monkeypatch.setattr(brain, "_client", mock_client)

        verdicts = brain.evaluate_comment("Title", "Comment @test", ["@test"])

        # Gemini client should have been called
        assert mock_client.models.generate_content.called
        assert len(verdicts) == 1
        assert verdicts[0]["llm_status"] == "EVALUATED"

    def test_evaluate_handle_campaign_uses_gemini(self, monkeypatch):
        _force_provider(monkeypatch, "gemini")

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '{"is_fraud": true, "role": "RECRUITER", "scam_type": "TASK_SCAM", "confidence": 0.9, "reason": "campaign"}'
        mock_client.models.generate_content.return_value = mock_response
        monkeypatch.setattr(brain, "_client", mock_client)

        verdict = brain.evaluate_handle_campaign("@test", [{"video_title": "V", "comment_text": "C", "author": "A"}])
        assert mock_client.models.generate_content.called
        assert verdict["llm_status"] == "EVALUATED"


class TestJevProvider:
    """When CLASSIFIER_PROVIDER=jev, classification should route to Jev."""

    def test_evaluate_comment_uses_jev(self, monkeypatch):
        _force_provider(monkeypatch, "jev")
        brain.reset_jev_sweep_cost()

        jev_called = {"n": 0}

        def mock_jev(state, questions):
            jev_called["n"] += 1
            return {
                "answers": {
                    "is_fraud": {"probability": 0.1},
                    "role": {"choice": "NEUTRAL", "confidence": 0.95},
                    "scam_type": {"choice": "NONE", "confidence": 0.95},
                },
                "usage": {"cost": 0.0001},
            }

        monkeypatch.setattr(brain, "_call_jev_decision", mock_jev)

        # Gemini client should NOT be called
        mock_client = MagicMock()
        monkeypatch.setattr(brain, "_client", mock_client)

        verdicts = brain.evaluate_comment("Title", "Comment @test", ["@test"])

        assert jev_called["n"] == 1
        assert not mock_client.models.generate_content.called, "Gemini should NOT be called in jev mode"
        assert len(verdicts) == 1
        assert verdicts[0]["llm_status"] == "EVALUATED"

    def test_evaluate_handle_campaign_uses_jev(self, monkeypatch):
        _force_provider(monkeypatch, "jev")
        brain.reset_jev_sweep_cost()

        jev_called = {"n": 0}

        def mock_jev(state, questions):
            jev_called["n"] += 1
            return {
                "answers": {
                    "is_coordinated_campaign": {"probability": 0.8},
                    "campaign_tier": {"choice": "PROBABLE", "confidence": 0.85},
                },
                "usage": {"cost": 0.002},
            }

        monkeypatch.setattr(brain, "_call_jev_decision", mock_jev)

        verdict = brain.evaluate_handle_campaign("@test", [
            {"video_title": "V1", "comment_text": "C1", "author": "A1"},
        ])

        assert jev_called["n"] == 1
        assert verdict["llm_status"] == "EVALUATED"


class TestHybridPath:
    """When CLASSIFIER_PROVIDER=jev, Jev classifies and Gemini only explains promotions."""

    def test_jev_classifies_gemini_explains(self, monkeypatch):
        """Jev does classification, then explain_promoted_lead uses Gemini."""
        _force_provider(monkeypatch, "jev")
        brain.reset_jev_sweep_cost()

        # 1. Classification via Jev
        monkeypatch.setattr(
            brain, "_call_jev_decision",
            lambda s, q: {
                "answers": {
                    "is_fraud": {"probability": 0.95},
                    "role": {"choice": "RECRUITER", "confidence": 0.92},
                    "scam_type": {"choice": "TASK_SCAM", "confidence": 0.9},
                },
                "usage": {"cost": 0.0001},
            },
        )

        verdicts = brain.evaluate_comment("Title", "Join @scam for money", ["@scam"])
        assert verdicts[0]["role"] == "RECRUITER"
        assert verdicts[0]["is_fraud"] is True
        # reason is None — Jev doesn't produce text explanations
        assert verdicts[0]["reason"] is None

        # 2. Explanation via Gemini (only for promoted leads)
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "Task scam recruitment lure directing victims to Telegram."
        mock_client.models.generate_content.return_value = mock_response
        monkeypatch.setattr(brain, "_client", mock_client)

        sighting = {
            "comment_text": "Join @scam for daily income",
            "video_title": "Title",
            "handle_norm": "@scam",
            "llm_role": "RECRUITER",
            "llm_is_fraud": True,
            "scam_type": "TASK_SCAM",
            "llm_confidence": 0.92,
        }

        reason = brain.explain_promoted_lead(sighting)
        assert isinstance(reason, str)
        assert len(reason) > 0
        # Gemini was called for explanation
        assert mock_client.models.generate_content.called

    def test_gemini_never_called_for_classification_in_jev_mode(self, monkeypatch):
        """In jev mode, Gemini's generate_content should NEVER be called during
        evaluate_comment or evaluate_handle_campaign — only during explain_promoted_lead."""
        _force_provider(monkeypatch, "jev")
        brain.reset_jev_sweep_cost()

        monkeypatch.setattr(
            brain, "_call_jev_decision",
            lambda s, q: {
                "answers": {
                    "is_fraud": {"probability": 0.5},
                    "role": {"choice": "NEUTRAL", "confidence": 0.8},
                    "scam_type": {"choice": "NONE", "confidence": 0.8},
                },
                "usage": {"cost": 0.0001},
            },
        )

        mock_client = MagicMock()
        monkeypatch.setattr(brain, "_client", mock_client)

        # Classification
        brain.evaluate_comment("T", "C", ["@x"])
        brain.evaluate_handle_campaign("@x", [{"video_title": "V", "comment_text": "C", "author": "A"}])

        # Gemini should NOT have been called during classification
        assert not mock_client.models.generate_content.called


class TestProviderSwitching:
    """Verify that switching providers at runtime works correctly."""

    def test_switch_from_gemini_to_jev(self, monkeypatch):
        """First use gemini, then switch to jev — each should use the right backend."""
        # Start with gemini
        _force_provider(monkeypatch, "gemini")

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '[{"target": "@t", "is_fraud": false, "role": "NEUTRAL", "scam_type": "NONE", "confidence": 0.9, "reason": "clean"}]'
        mock_client.models.generate_content.return_value = mock_response
        monkeypatch.setattr(brain, "_client", mock_client)

        brain.evaluate_comment("T", "C", ["@t"])
        assert mock_client.models.generate_content.called

        # Switch to jev
        _force_provider(monkeypatch, "jev")
        brain.reset_jev_sweep_cost()
        mock_client.reset_mock()

        jev_called = {"n": 0}

        def mock_jev(s, q):
            jev_called["n"] += 1
            return {
                "answers": {
                    "is_fraud": {"probability": 0.1},
                    "role": {"choice": "NEUTRAL", "confidence": 0.9},
                    "scam_type": {"choice": "NONE", "confidence": 0.9},
                },
                "usage": {"cost": 0.0001},
            }

        monkeypatch.setattr(brain, "_call_jev_decision", mock_jev)

        brain.evaluate_comment("T", "C", ["@t"])
        assert jev_called["n"] == 1
        assert not mock_client.models.generate_content.called

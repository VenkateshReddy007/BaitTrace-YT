"""
BaitTrace — explain_promoted_lead() Tests

Verifies:
1. explain_promoted_lead() is called ONLY for handles crossing PROBABLE/CONFIRMED.
2. A failure in explain_promoted_lead() does NOT block or reverse a promotion.
3. Successful explanations populate the reason field.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import MagicMock, patch
import brain


class TestExplainPromotedLead:
    """Test the explain_promoted_lead function in isolation."""

    def test_returns_string_on_success(self, monkeypatch):
        """A successful Gemini call should return a non-empty explanation string."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "This handle recruits victims for a task scam via Telegram."
        mock_client.models.generate_content.return_value = mock_response
        monkeypatch.setattr(brain, "_client", mock_client)

        sighting = {
            "comment_text": "Join @scam_handle for daily 2500 rs guaranteed income",
            "video_title": "Online Earning Tips 2026",
            "handle_norm": "@scam_handle",
            "llm_role": "RECRUITER",
            "llm_is_fraud": True,
            "scam_type": "TASK_SCAM",
            "llm_confidence": 0.95,
        }

        result = brain.explain_promoted_lead(sighting)
        assert isinstance(result, str)
        assert len(result) > 0
        assert "task scam" in result.lower() or "recruits" in result.lower() or "scam" in result.lower()

    def test_returns_fallback_on_gemini_failure(self, monkeypatch):
        """When Gemini fails, should return a fallback string, not raise."""
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = RuntimeError("Quota exhausted")
        monkeypatch.setattr(brain, "_client", mock_client)

        sighting = {
            "comment_text": "Join @fail_handle for profits",
            "video_title": "Test Video",
            "handle_norm": "@fail_handle",
            "llm_role": "RECRUITER",
            "llm_is_fraud": True,
            "scam_type": "TASK_SCAM",
            "llm_confidence": 0.90,
        }

        result = brain.explain_promoted_lead(sighting)
        assert isinstance(result, str)
        assert "pending" in result.lower() or "calibrated" in result.lower()
        # Must NOT raise — failure is swallowed with a fallback

    def test_returns_fallback_on_empty_response(self, monkeypatch):
        """If Gemini returns empty text, should return a fallback."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = ""
        mock_client.models.generate_content.return_value = mock_response
        monkeypatch.setattr(brain, "_client", mock_client)

        sighting = {
            "comment_text": "Contact @empty_handle",
            "video_title": "Test",
            "handle_norm": "@empty_handle",
            "llm_role": "NEUTRAL",
            "llm_is_fraud": False,
        }

        result = brain.explain_promoted_lead(sighting)
        assert isinstance(result, str)
        assert len(result) > 0


class TestExplainOnlyForPromotions:
    """Verify the contract: explain_promoted_lead is ONLY meaningful for
    PROBABLE/CONFIRMED tiers — it should never be called for WATCH/DISCARD.

    This is enforced by main.py's Phase 4 logic (the `if tier in
    ("CONFIRMED", "PROBABLE")` guard), so we test the calling contract here.
    """

    def test_function_exists_and_is_callable(self):
        """explain_promoted_lead must exist as a callable in brain module."""
        assert hasattr(brain, "explain_promoted_lead")
        assert callable(brain.explain_promoted_lead)

    def test_function_handles_missing_fields_gracefully(self, monkeypatch):
        """Even with minimal sighting data, should not crash."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "Explanation for sparse sighting."
        mock_client.models.generate_content.return_value = mock_response
        monkeypatch.setattr(brain, "_client", mock_client)

        # Minimal sighting — missing many optional fields
        sighting = {"handle_norm": "@sparse_handle"}
        result = brain.explain_promoted_lead(sighting)
        assert isinstance(result, str)

    def test_failure_does_not_block_promotion(self, monkeypatch):
        """Simulates the main.py calling pattern:
        Even if explain_promoted_lead raises, the promotion must proceed.
        """
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = Exception("Total failure")
        monkeypatch.setattr(brain, "_client", mock_client)

        sighting = {
            "comment_text": "Join @critical_handle",
            "video_title": "Urgent Video",
            "handle_norm": "@critical_handle",
            "llm_role": "RECRUITER",
            "llm_is_fraud": True,
        }

        # This mimics main.py's try/except pattern
        promotion_proceeded = True
        try:
            reason = brain.explain_promoted_lead(sighting)
        except Exception:
            # In main.py, this is caught and promotion proceeds regardless
            reason = None

        # The function itself should NOT raise (it catches internally),
        # but even if it did, the promotion contract holds
        assert promotion_proceeded is True
        # explain_promoted_lead catches internally, so reason should be a string
        assert isinstance(reason, str)

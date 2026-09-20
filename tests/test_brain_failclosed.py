import pytest
from unittest.mock import MagicMock
import brain

def test_brain_evaluate_comment_fails_closed(monkeypatch):
    """Ensure that an API outage/exception causes evaluate_comment to fail closed
    with is_fraud=False, status=NEEDS_REVIEW, confidence=0.0.
    """
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = RuntimeError("API outage / timeout")
    monkeypatch.setattr(brain, "_client", mock_client)

    targets = ["@scam_lead_1", "@scam_lead_2"]
    verdicts = brain.evaluate_comment(
        video_title="Intraday Trading Tips",
        comment_text="Join my group @scam_lead_1 and @scam_lead_2",
        targets=targets
    )

    assert len(verdicts) == 2
    for v in verdicts:
        assert v["is_fraud"] is False, "Brain must NEVER manufacture fraud leads on API outage!"
        assert v["status"] == "NEEDS_REVIEW"
        assert v["confidence"] == 0.0
        assert v["role"] == "NEUTRAL"
        assert "LLM unavailable" in v["reason"]


def test_brain_evaluate_handle_campaign_fails_closed(monkeypatch):
    """Ensure that evaluate_handle_campaign fails closed when Gemini fails."""
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = RuntimeError("Service Unavailable")
    monkeypatch.setattr(brain, "_client", mock_client)

    verdict = brain.evaluate_handle_campaign(
        handle="@test_handle",
        sightings=[{"video_title": "Test", "comment_text": "Sample comment", "author": "user1"}]
    )

    assert verdict["is_fraud"] is False
    assert verdict["status"] == "NEEDS_REVIEW"
    assert verdict["confidence"] == 0.0
    assert verdict["role"] == "NEUTRAL"


def test_brain_evaluate_lead_fails_closed(monkeypatch):
    """Ensure evaluate_lead fails closed when Gemini fails."""
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = RuntimeError("Quota exceeded")
    monkeypatch.setattr(brain, "_client", mock_client)

    verdict = brain.evaluate_lead("Title", "Comment", "@test")
    assert verdict["is_fraud"] is False
    assert verdict["status"] == "NEEDS_REVIEW"
    assert verdict["confidence"] == 0.0

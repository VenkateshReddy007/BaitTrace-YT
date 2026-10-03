import pytest
from unittest.mock import MagicMock
import brain
import requests

def test_brain_evaluate_comment_fails_closed(monkeypatch):
    """Ensure that an API outage/exception causes evaluate_comment to fail closed
    with is_fraud=False, llm_status=PENDING_RETRY.
    """
    mock_jev = MagicMock(side_effect=requests.exceptions.Timeout("API timeout"))
    monkeypatch.setattr(brain, "_call_jev_decision", mock_jev)

    targets = ["@scam_lead_1", "@scam_lead_2"]
    verdicts = brain.evaluate_comment(
        video_title="Intraday Trading Tips",
        comment_text="Join my group @scam_lead_1 and @scam_lead_2",
        targets=targets
    )

    assert len(verdicts) == 2
    for v in verdicts:
        assert v["is_fraud"] is False, "Brain must NEVER manufacture fraud leads on API outage!"
        assert v["llm_status"] == "PENDING_RETRY"
        assert v["confidence"] == 0.0
        assert v["role"] == "NEUTRAL"
        assert "LLM unavailable" in v["reason"]


def test_brain_evaluate_handle_campaign_fails_closed(monkeypatch):
    """Ensure that evaluate_handle_campaign fails closed when Jev fails."""
    mock_jev = MagicMock(side_effect=requests.exceptions.HTTPError("503 Service Unavailable"))
    # Add a mock response to the exception so the status logic works
    mock_jev.side_effect.response = MagicMock(status_code=503)
    monkeypatch.setattr(brain, "_call_jev_decision", mock_jev)

    verdict = brain.evaluate_handle_campaign(
        handle="@test_handle",
        sightings=[{"video_title": "Test", "comment_text": "Sample comment", "author": "user1"}]
    )

    assert verdict["is_fraud"] is False
    assert verdict["llm_status"] == "PENDING_RETRY"
    assert verdict["confidence"] == 0.0
    assert verdict["role"] == "NEUTRAL"


def test_brain_evaluate_lead_fails_closed(monkeypatch):
    """Ensure evaluate_lead fails closed when Jev fails."""
    mock_jev = MagicMock(side_effect=requests.exceptions.HTTPError("429 Quota exceeded"))
    mock_jev.side_effect.response = MagicMock(status_code=429)
    monkeypatch.setattr(brain, "_call_jev_decision", mock_jev)

    verdict = brain.evaluate_lead("Title", "Comment", "@test")
    assert verdict["is_fraud"] is False
    assert verdict["llm_status"] == "PENDING_RETRY"
    assert verdict["confidence"] == 0.0


def test_brain_evaluate_comment_success_path(monkeypatch):
    """Ensure successful Jev response sets llm_status=EVALUATED and doesn't fail closed."""
    mock_jev = MagicMock(return_value={
        "usage": {"cost": 0.001},
        "answers": {
            "is_fraud": {"probability": 0.9},
            "role": {"choice": "RECRUITER", "confidence": 0.8},
            "scam_type": {"choice": "TASK_SCAM"}
        }
    })
    monkeypatch.setattr(brain, "_call_jev_decision", mock_jev)

    verdicts = brain.evaluate_comment(
        video_title="Test",
        comment_text="Contact @test",
        targets=["@test"]
    )
    
    assert len(verdicts) == 1
    v = verdicts[0]
    assert v["is_fraud"] is True
    assert v["llm_status"] == "EVALUATED"
    assert v["role"] == "RECRUITER"
    assert v["scam_type"] == "TASK_SCAM"

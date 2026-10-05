"""
BaitTrace Pipeline Integration Tests
Tests that would have caught the original pydantic unpacking bug (#1)
and validates the full normalize -> extract -> escalate -> lead-build flow.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from baittrace.parser import ExtractedIndicator, extract_indicators, normalize_text
from baittrace.heuristics import should_escalate


class TestExtractIndicatorsReturnType:
    """Pin the contract: extract_indicators returns ExtractedIndicator instances, not tuples."""

    def test_returns_extracted_indicator_instances(self):
        cleaned = normalize_text("Join now bro t.me/+AbC123xyZ99 daily payment guaranteed 9821045678")
        indicators = extract_indicators(cleaned)
        assert len(indicators) > 0, "Expected at least one indicator from scam comment"
        for ind in indicators:
            assert isinstance(ind, ExtractedIndicator), (
                f"Expected ExtractedIndicator, got {type(ind).__name__}. "
                f"This is the bug that caused the original ValueError on 'for ind_type, target in indicators:'"
            )

    def test_indicator_fields_accessible(self):
        """Ensure we can access indicator fields by attribute, not by unpacking."""
        cleaned = normalize_text("Contact us telegram: @scam_channel_99")
        indicators = extract_indicators(cleaned)
        assert len(indicators) > 0
        ind = indicators[0]
        # These attribute accesses must work — the old code tried tuple unpacking
        assert hasattr(ind, "indicator_type")
        assert hasattr(ind, "raw_value")
        assert hasattr(ind, "normalized_value")
        assert hasattr(ind, "confidence")


class TestFullPipelineIntegration:
    """End-to-end test: comment text -> normalize -> extract -> escalate -> lead record."""

    def test_scam_comment_produces_lead(self):
        raw_comment = "Join now bro t.me/+AbC123xyZ99 daily payment guaranteed 9821045678"
        
        # Step 1: Normalize
        cleaned = normalize_text(raw_comment)
        assert cleaned, "normalize_text should return non-empty string"
        
        # Step 2: Extract indicators
        indicators = extract_indicators(cleaned)
        assert len(indicators) >= 1, f"Expected indicators from scam comment, got {len(indicators)}"
        
        # Step 3: Filter through heuristic gate
        escalated = [ind for ind in indicators if should_escalate(ind, cleaned)]
        assert len(escalated) >= 1, f"Expected at least one escalated indicator, got {len(escalated)}"
        
        # Step 4: Build target list (mimics main.py logic)
        targets = []
        target_to_ind = {}
        for ind in escalated:
            target = ind.normalized_value
            if target.lower() in ("whatsapp", "telegram"):
                continue
            targets.append(target)
            target_to_ind[target] = ind
        
        assert len(targets) >= 1, f"Expected at least one target for LLM, got {len(targets)}"
        
        # Step 5: Simulate evaluate_comment with canned verdict
        canned_verdicts = [
            {
                "target": t,
                "is_fraud": True,
                "scam_type": "TASK_SCAM",
                "confidence": 0.95,
                "reason": "Offers daily payout for Telegram tasks"
            }
            for t in targets
        ]
        
        # Step 6: Build lead records (mimics main.py logic)
        leads = []
        for target, intel in zip(targets, canned_verdicts):
            if not intel.get("is_fraud", False):
                continue
            
            ind = target_to_ind[target]
            fraud_class = intel.get("scam_type", "SUSPICIOUS_COMMENT_BOT")
            llm_confidence = intel.get("confidence", 0.90)
            confidence = min(llm_confidence, ind.confidence)
            
            record = {
                "indicator_type": ind.indicator_type,
                "target": target,
                "fraud_class": fraud_class,
                "confidence": confidence,
                "ai_reasoning": intel.get("reason", ""),
                "author": "test_author",
                "source_video_id": "test_vid_001",
                "source_title": "Earn Money Online Daily",
                "source_url": "https://youtube.com/watch?v=test",
                "raw_comment": raw_comment
            }
            leads.append(record)
        
        # Assertions on the lead record
        assert len(leads) >= 1, "Expected at least one lead record"
        lead = leads[0]
        assert "indicator_type" in lead
        assert "target" in lead
        assert "confidence" in lead
        assert isinstance(lead["confidence"], float)
        assert 0 < lead["confidence"] <= 1.0


class TestNegativeCase:
    """Verify that benign comments with incidental numbers don't produce escalated leads."""

    def test_view_count_not_escalated(self):
        """'9876543210 views' is just a view count, not a phone number to escalate."""
        raw_comment = "I bought it in 2019 for 9876543210 views"
        cleaned = normalize_text(raw_comment)
        indicators = extract_indicators(cleaned)
        
        # Even if the regex matches the number, the heuristic gate should block it
        escalated_phones = [
            ind for ind in indicators
            if ind.indicator_type in ("PHONE", "WHATSAPP") and should_escalate(ind, cleaned)
        ]
        assert len(escalated_phones) == 0, (
            f"Expected no escalated PHONE/WHATSAPP leads for a benign view-count comment, "
            f"got: {[(ind.indicator_type, ind.normalized_value) for ind in escalated_phones]}"
        )


class TestHeuristicGate:
    """Direct tests for the should_escalate heuristic."""

    def test_telegram_always_escalated(self):
        ind = ExtractedIndicator(
            indicator_type="TELEGRAM",
            raw_value="t.me/scam_channel",
            normalized_value="@scam_channel",
            confidence=0.98
        )
        assert should_escalate(ind, "nice video thanks") is True

    def test_phone_with_lure_token_escalated(self):
        ind = ExtractedIndicator(
            indicator_type="PHONE",
            raw_value="9876543210",
            normalized_value="+919876543210",
            confidence=0.85
        )
        assert should_escalate(ind, "earn daily payment call now") is True

    def test_phone_without_lure_token_not_escalated(self):
        ind = ExtractedIndicator(
            indicator_type="PHONE",
            raw_value="9876543210",
            normalized_value="+919876543210",
            confidence=0.85
        )
        assert should_escalate(ind, "great tutorial thanks for sharing") is False

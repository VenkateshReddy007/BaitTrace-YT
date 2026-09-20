import pytest
from scoring import compute_campaign_score, author_burner_score, compute_simhash, hamming_distance

def test_single_sighting_never_confirmed():
    """A single-sighting handle must NEVER reach CONFIRMED on LLM opinion alone."""
    single_sighting = [{
        "video_id": "vid_single_001",
        "video_title": "Daily Earnings Tutorial",
        "author": "@user_burner99",
        "comment_text": "Earn 5000 daily without investment contact @scam_target_bot now",
        "llm_role": "RECRUITER",
        "llm_confidence": 0.99,
        "is_creator_author": False
    }]

    result = compute_campaign_score("@scam_target_bot", single_sighting)
    assert result["tier"] != "CONFIRMED", (
        f"Single sighting reached {result['tier']}! Must NEVER reach CONFIRMED."
    )
    assert result["tier"] in ("PROBABLE", "WATCH")


def test_four_videos_four_authors_confirms():
    """The same handle across 4 videos / 4 burner authors MUST reach CONFIRMED."""
    sightings = [
        {
            "video_id": f"vid_00{i}",
            "video_title": f"Trading Basics Part {i}",
            "author": f"@user-bot{i}9921",
            "comment_text": "Join VIP signal group daily payouts guaranteed @coordinated_fraud",
            "llm_role": "RECRUITER",
            "llm_confidence": 0.95,
            "channel_meta": {"video_count": 0, "subscriber_count": 0},
            "is_creator_author": False
        }
        for i in range(1, 5)
    ]

    result = compute_campaign_score("@coordinated_fraud", sightings)
    assert result["tier"] == "CONFIRMED", (
        f"Expected CONFIRMED for 4 videos / 4 burner authors, got: {result['tier']} (Score: {result['campaign_score']})"
    )
    assert result["status"] == "CONFIRMED"
    assert result["distinct_video_count"] == 4
    assert result["distinct_author_count"] == 4


def test_victim_report_majority_not_confirmed():
    """A handle whose sightings are all VICTIM_REPORT must NOT reach CONFIRMED."""
    sightings = [
        {
            "video_id": f"vid_exposed_{i}",
            "video_title": f"Scam Exposed Episode {i}",
            "author": f"victim_{i}",
            "comment_text": f"Beware guys @fake_trader took all my money and blocked me!",
            "llm_role": "VICTIM_REPORT",
            "llm_confidence": 0.95,
            "is_creator_author": False
        }
        for i in range(1, 5)
    ]

    result = compute_campaign_score("@fake_trader", sightings)
    assert result["tier"] != "CONFIRMED", (
        f"VICTIM_REPORT campaign must not reach CONFIRMED! Got {result['tier']} (Score: {result['campaign_score']})"
    )


def test_author_burner_score():
    """Test author burner detection signals."""
    # Burner account: auto-generated handle, 0 videos, 0 subs
    burner_meta = {"video_count": 0, "subscriber_count": 0, "description": ""}
    score = author_burner_score("@user-xy8812", burner_meta)
    assert score >= 0.70

    # Legit creator account
    creator_meta = {"video_count": 250, "subscriber_count": 50000, "description": "Official Tech Channel"}
    score = author_burner_score("TechGuruOfficial", creator_meta)
    assert score <= 0.15


def test_simhash_near_duplicates():
    """Test simhash fingerprint distance for comment template variants."""
    t1 = "Earn 5000 daily from home guaranteed contact telegram @cash_bot"
    t2 = "Earn 5000 daily from home guaranteed payout contact telegram @cash_bot"
    t3 = "Completely unrelated video about cooking biryani and Indian spices recipe"

    h1 = compute_simhash(t1)
    h2 = compute_simhash(t2)
    h3 = compute_simhash(t3)

    assert hamming_distance(h1, h2) <= 5
    assert hamming_distance(h1, h3) > 10

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


class TestCrossRunHistoricalScoring:
    """§4: Verify that historical sightings from a prior run are merged
    into the current run's campaign score, so a handle seen once in run 1
    and once in run 2 scores as 2 distinct videos / 2 distinct authors.
    """

    def test_two_runs_merge_sightings(self):
        """Simulate two separate runs for the same handle.
        Run 1: 1 sighting from video A / author X.
        Run 2: 1 sighting from video B / author Y (fresh in-memory state).
        After merging historical rows, compute_campaign_score must see
        distinct_video_count=2 and distinct_author_count=2.
        """
        handle = "@cross_run_scammer"

        # --- Run 1 sighting ---
        sighting_run1 = {
            "video_id": "vid_run1_AAA",
            "video_title": "Earn Money Online",
            "comment_id": "comment_run1_001",
            "author": "author_X_burner",
            "author_channel_id": "UC_authorX",
            "comment_text": "Join telegram @cross_run_scammer daily income guaranteed",
            "llm_role": "RECRUITER",
            "llm_confidence": 0.92,
            "heuristic_score": 0.98,
            "is_creator_author": False,
            "channel_meta": {"video_count": 0, "subscriber_count": 0},
        }

        # --- Run 2 sighting (fresh process, no memory of run 1) ---
        sighting_run2 = {
            "video_id": "vid_run2_BBB",
            "video_title": "Stock Market Tips",
            "comment_id": "comment_run2_002",
            "author": "author_Y_shill",
            "author_channel_id": "UC_authorY",
            "comment_text": "Contact @cross_run_scammer for VIP signals profit daily",
            "llm_role": "RECRUITER",
            "llm_confidence": 0.90,
            "heuristic_score": 0.98,
            "is_creator_author": False,
            "channel_meta": {"video_count": 0, "subscriber_count": 0},
        }

        # Without historical merge (run 2 alone):
        score_single = compute_campaign_score(handle, [sighting_run2])
        assert score_single["distinct_video_count"] == 1
        assert score_single["distinct_author_count"] == 1
        # Single-sighting must NEVER reach CONFIRMED
        assert score_single["tier"] != "CONFIRMED"

        # With historical merge (run 1 + run 2):
        merged = [sighting_run2, sighting_run1]  # run2 is "current", run1 from DB
        score_merged = compute_campaign_score(handle, merged)
        assert score_merged["distinct_video_count"] == 2, (
            f"Expected 2 distinct videos after merge, got {score_merged['distinct_video_count']}"
        )
        assert score_merged["distinct_author_count"] == 2, (
            f"Expected 2 distinct authors after merge, got {score_merged['distinct_author_count']}"
        )
        # Merged score must be higher than single-sighting score
        assert score_merged["campaign_score"] > score_single["campaign_score"], (
            f"Merged score ({score_merged['campaign_score']}) should be higher than "
            f"single ({score_single['campaign_score']})"
        )


class TestCreatorAuthorPenalty:
    """§6: Verify that is_creator_author=True triggers the -50 penalty and
    suppresses what would otherwise be a CONFIRMED campaign.
    """

    def test_creator_handle_suppressed(self):
        """4 videos / 4 authors with RECRUITER should be CONFIRMED,
        but if is_creator_author=True the -50 penalty should prevent it."""
        sightings_legit = [
            {
                "video_id": f"vid_creator_{i}",
                "video_title": f"My Channel Update {i}",
                "author": f"@fan_user_{i}",
                "comment_text": "Join our community @creator_channel for updates",
                "llm_role": "RECRUITER",
                "llm_confidence": 0.90,
                "channel_meta": {"video_count": 0, "subscriber_count": 0},
                "is_creator_author": True,  # <-- the video creator posted it
            }
            for i in range(1, 5)
        ]

        result = compute_campaign_score("@creator_channel", sightings_legit)
        # The -50 penalty should prevent CONFIRMED
        assert result["tier"] != "CONFIRMED", (
            f"Creator's own handle should NOT reach CONFIRMED, got {result['tier']} "
            f"(score={result['campaign_score']})"
        )

    def test_non_creator_same_data_confirms(self):
        """Same data as above but is_creator_author=False should reach CONFIRMED."""
        sightings = [
            {
                "video_id": f"vid_noncreator_{i}",
                "video_title": f"Trading Tips Part {i}",
                "author": f"@shill_bot_{i}",
                "comment_text": "Join telegram @scam_channel daily profits guaranteed",
                "llm_role": "RECRUITER",
                "llm_confidence": 0.95,
                "channel_meta": {"video_count": 0, "subscriber_count": 0},
                "is_creator_author": False,
            }
            for i in range(1, 5)
        ]

        result = compute_campaign_score("@scam_channel", sightings)
        assert result["tier"] == "CONFIRMED", (
            f"Non-creator 4-video campaign should be CONFIRMED, got {result['tier']}"
        )


class TestDiscoveryQueryPoolSizes:
    """§2: Verify each lane has at least 15 queries after expansion."""

    def test_all_lanes_have_minimum_15_queries(self):
        from discovery import FALLBACK_QUERIES
        for lane, queries in FALLBACK_QUERIES.items():
            assert len(queries) >= 15, (
                f"Lane {lane} has only {len(queries)} queries, expected >= 15"
            )

    def test_lure_lane_has_non_english_queries(self):
        from discovery import FALLBACK_QUERIES
        lure_queries = FALLBACK_QUERIES["LURE"]
        # At least one query should contain non-ASCII (Hindi/Bengali/etc.)
        has_non_ascii = any(
            any(ord(c) > 127 for c in q) for q in lure_queries
        )
        assert has_non_ascii, "LURE lane should include native-script query variants"


class TestConfigConstants:
    """§5: Verify config.py constants are importable and sane."""

    def test_config_imports_and_values(self):
        from config import (
            DEFAULT_LIMIT_PER_QUERY,
            DEFAULT_MAX_COMMENTS,
            DEFAULT_QUERY_SAMPLE_SIZE,
            DEFAULT_LLM_CALL_BUDGET,
            PIVOT_THRESHOLD,
            PIVOT_MAX_HANDLES_PER_RUN,
            DEFAULT_SWEEP_INTERVAL_MINUTES,
        )
        assert DEFAULT_LIMIT_PER_QUERY == 20
        assert DEFAULT_MAX_COMMENTS == 50
        assert DEFAULT_QUERY_SAMPLE_SIZE == 10
        assert DEFAULT_LLM_CALL_BUDGET == 15
        assert PIVOT_THRESHOLD == 20
        assert PIVOT_MAX_HANDLES_PER_RUN == 5
        assert DEFAULT_SWEEP_INTERVAL_MINUTES == 180



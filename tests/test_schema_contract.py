import re
from pathlib import Path
import pytest

from baittrace.main import build_sighting_record, build_handle_payload, build_lead_payload

SCHEMA_SQL_PATH = Path(__file__).resolve().parent.parent / "scripts" / "schema.sql"

def parse_schema_columns(sql_text: str) -> dict[str, set[str]]:
    """Extracts column sets for all tables defined in CREATE TABLE blocks in schema.sql."""
    tables = {}
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z0-9_]+)\s*\((.*?)\);",
        re.DOTALL | re.IGNORECASE
    )
    for match in pattern.finditer(sql_text):
        table_name = match.group(1).lower()
        body = match.group(2)
        columns = set()
        for line in body.splitlines():
            line = line.strip().rstrip(",")
            if not line or line.startswith("--"):
                continue
            if re.match(r"^(?:CONSTRAINT|PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK)\b", line, re.IGNORECASE):
                continue
            col_match = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s+", line)
            if col_match:
                col_name = col_match.group(1).lower()
                columns.add(col_name)
        tables[table_name] = columns
    return tables


def test_schema_contract_candidate_sightings():
    """Asserts that every key in a sighting_record constructed by main.py
    exists as a column in candidate_sightings table definition in schema.sql.
    """
    assert SCHEMA_SQL_PATH.exists(), f"schema.sql not found at {SCHEMA_SQL_PATH}"
    sql_text = SCHEMA_SQL_PATH.read_text(encoding="utf-8")
    table_columns = parse_schema_columns(sql_text)
    
    assert "candidate_sightings" in table_columns, "candidate_sightings table missing from schema.sql"
    sightings_cols = table_columns["candidate_sightings"]

    # Construct representative sighting record using the actual main.py code path
    record = build_sighting_record(
        target="@sample_scammer",
        indicator_type="TELEGRAM",
        raw_value="t.me/sample_scammer",
        cid="comment_xyz_123",
        author="burner_user_99",
        author_channel="UC_sample_channel",
        video_id="vid_test_001",
        video_title="Online Income Secrets 2026",
        video_url="https://youtube.com/watch?v=vid_test_001",
        lane="VICTIM_RICH",
        is_reply=False,
        raw_text="Earn 5000 daily contact @sample_scammer",
        posted_time="2 hours ago",
        llm_is_fraud=True,
        llm_role="RECRUITER",
        llm_confidence=0.98,
        llm_reason="Task scam recruitment lure",
        heuristic_score=0.95,
        channel_meta={"subscriber_count": 0, "video_count": 0}
    )

    extra_keys = set(record.keys()) - sightings_cols
    assert not extra_keys, (
        f"Schema Contract Violation! Keys in sighting_record missing from candidate_sightings table in schema.sql: "
        f"{extra_keys}. This causes PostgREST HTTP 400 Bad Request on upsert."
    )


def test_schema_contract_handles():
    """Asserts that every key in handle_payload constructed by main.py
    exists as a column in handles table definition in schema.sql.
    """
    sql_text = SCHEMA_SQL_PATH.read_text(encoding="utf-8")
    table_columns = parse_schema_columns(sql_text)
    
    assert "handles" in table_columns, "handles table missing from schema.sql"
    handles_cols = table_columns["handles"]

    mock_campaign = {
        "distinct_video_count": 3,
        "distinct_author_count": 3,
        "campaign_score": 85.0,
        "status": "CONFIRMED",
        "evidence": {"test": "data"}
    }

    handle_payload = build_handle_payload(
        handle_norm="@sample_scammer",
        indicator_type="TELEGRAM",
        campaign=mock_campaign,
        tier="CONFIRMED",
        rep_video_url="https://youtube.com/watch?v=vid_test_001"
    )

    extra_keys = set(handle_payload.keys()) - handles_cols
    assert not extra_keys, (
        f"Schema Contract Violation! Keys in handle_payload missing from handles table in schema.sql: "
        f"{extra_keys}."
    )


def test_schema_contract_actionable_leads():
    """Asserts that every key in lead_payload constructed by main.py
    exists as a column in actionable_leads table definition in schema.sql.
    """
    sql_text = SCHEMA_SQL_PATH.read_text(encoding="utf-8")
    table_columns = parse_schema_columns(sql_text)
    
    assert "actionable_leads" in table_columns, "actionable_leads table missing from schema.sql"
    leads_cols = table_columns["actionable_leads"]

    mock_campaign = {
        "distinct_video_count": 3,
        "distinct_author_count": 3,
        "campaign_score": 85.0,
        "status": "CONFIRMED",
        "evidence": {"test": "data"}
    }

    lead_payload = build_lead_payload(
        handle_norm="@sample_scammer",
        indicator_type="TELEGRAM",
        campaign=mock_campaign,
        tier="CONFIRMED",
        rep_video_url="https://youtube.com/watch?v=vid_test_001"
    )

    extra_keys = set(lead_payload.keys()) - leads_cols
    assert not extra_keys, (
        f"Schema Contract Violation! Keys in lead_payload missing from actionable_leads table in schema.sql: "
        f"{extra_keys}."
    )

-- BaitTrace v2 Schema: Candidate Sightings, Handles, and Actionable Threat Leads
-- Migration: v2_campaign_architecture

-- 1. Actionable Leads: High-Confidence Promoted Campaigns
DROP TABLE IF EXISTS actionable_leads CASCADE;

CREATE TABLE actionable_leads (
    handle_norm TEXT PRIMARY KEY,
    indicator_type TEXT NOT NULL,
    first_seen TIMESTAMPTZ DEFAULT NOW(),
    last_seen TIMESTAMPTZ DEFAULT NOW(),
    distinct_video_count INT DEFAULT 1,
    distinct_author_count INT DEFAULT 1,
    campaign_score DOUBLE PRECISION DEFAULT 0.0,
    tier TEXT DEFAULT 'CONFIRMED',
    scam_type TEXT DEFAULT 'UNKNOWN',
    evidence JSONB DEFAULT '{}'::jsonb,
    llm_campaign_reason TEXT,
    llm_role TEXT DEFAULT 'RECRUITER',
    enrichment JSONB DEFAULT '{}'::jsonb,
    status TEXT DEFAULT 'CONFIRMED',
    representative_video_url TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_actionable_tier ON actionable_leads(tier);
CREATE INDEX IF NOT EXISTS idx_actionable_score ON actionable_leads(campaign_score);

-- 2. Candidate Sightings: The Comprehensive Raw Evidence Log
-- EVERY extracted indicator that passes parser precision rules is saved here.
CREATE TABLE IF NOT EXISTS candidate_sightings (
    id BIGSERIAL PRIMARY KEY,
    handle_norm TEXT NOT NULL,
    indicator_type TEXT NOT NULL,
    raw_value TEXT,
    comment_id TEXT NOT NULL,
    author TEXT,
    author_channel_id TEXT,
    video_id TEXT NOT NULL,
    video_title TEXT,
    video_url TEXT,
    lane TEXT DEFAULT 'VICTIM_RICH',
    is_reply BOOLEAN DEFAULT FALSE,
    comment_text TEXT,
    posted_time TEXT,
    llm_is_fraud BOOLEAN DEFAULT FALSE,
    llm_role TEXT DEFAULT 'NEUTRAL',
    llm_confidence DOUBLE PRECISION DEFAULT 0.0,
    llm_reason TEXT,
    heuristic_score DOUBLE PRECISION DEFAULT 0.0,
    channel_meta JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT uq_sighting_handle_comment UNIQUE (handle_norm, comment_id)
);

CREATE INDEX IF NOT EXISTS idx_sightings_handle ON candidate_sightings(handle_norm);
CREATE INDEX IF NOT EXISTS idx_sightings_video ON candidate_sightings(video_id);
CREATE INDEX IF NOT EXISTS idx_sightings_role ON candidate_sightings(llm_role);

-- 3. Handles: Unified Campaign Tracking Table
CREATE TABLE IF NOT EXISTS handles (
    handle_norm TEXT PRIMARY KEY,
    indicator_type TEXT NOT NULL,
    first_seen TIMESTAMPTZ DEFAULT NOW(),
    last_seen TIMESTAMPTZ DEFAULT NOW(),
    distinct_video_count INT DEFAULT 1,
    distinct_author_count INT DEFAULT 1,
    campaign_score DOUBLE PRECISION DEFAULT 0.0,
    tier TEXT DEFAULT 'WATCH', -- CONFIRMED, PROBABLE, WATCH, DISCARD
    scam_type TEXT DEFAULT 'UNKNOWN',
    evidence JSONB DEFAULT '{}'::jsonb,
    llm_campaign_reason TEXT,
    enrichment JSONB DEFAULT '{}'::jsonb,
    status TEXT DEFAULT 'ACTIVE',
    representative_video_url TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_handles_tier ON handles(tier);
CREATE INDEX IF NOT EXISTS idx_handles_score ON handles(campaign_score);

-- Migration-safe idempotent column additions:
ALTER TABLE IF EXISTS candidate_sightings ADD COLUMN IF NOT EXISTS channel_meta JSONB DEFAULT '{}'::jsonb;

-- v2.1: LLM quota tracking — distinguishes a real verdict from a quota-exhausted placeholder.
-- EVALUATED   = LLM actually reviewed this indicator and returned a verdict.
-- PENDING_RETRY = LLM was unavailable (quota/overload); llm_is_fraud/llm_role hold fail-closed defaults.
ALTER TABLE IF EXISTS candidate_sightings ADD COLUMN IF NOT EXISTS llm_status TEXT DEFAULT 'EVALUATED';

-- ============================================================
-- 0033 — Keyword feed
-- ============================================================
-- Per-channel keywords live in account_keywords (Postgres only). The
-- acquire job (ContentEngine refresh_keywords.py) searches each distinct
-- (keyword, platform) on Apify, saves the top posts as normal `videos`
-- rows and links them here in video_keywords. /feed then shows an
-- account the videos found for its active keywords next to the videos
-- of the creators it tracks.
--
-- account_keywords already exists in production but was never in a
-- migration file; CREATE TABLE IF NOT EXISTS records its shape and is a
-- no-op there.
--
-- RLS is ON with no policies on both tables: only swipeup-api and the
-- Cloud Run job (direct DATABASE_URL connection) touch them, never the
-- browser's anon key. Safe to re-run.
-- ============================================================

-- 1. account_keywords -------------------------------------------------------

CREATE TABLE IF NOT EXISTS account_keywords (
    account_id               TEXT        NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    keyword                  TEXT        NOT NULL,
    platform                 TEXT        NOT NULL
        CHECK (platform IN ('instagram', 'tiktok', 'youtube', 'x')),
    active                   BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_pulled_at           TIMESTAMPTZ,
    consecutive_empty_pulls  INTEGER     NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, keyword, platform)
);

CREATE INDEX IF NOT EXISTS account_keywords_due_idx
    ON account_keywords (last_pulled_at NULLS FIRST) WHERE active;

-- The scraper pulls each (keyword, platform) once for every account using it.
CREATE INDEX IF NOT EXISTS account_keywords_keyword_idx
    ON account_keywords (keyword, platform) WHERE active;

-- The keyword pull claims a (keyword, platform) by stamping every row for it,
-- so two overlapping job runs never pay Apify for the same search twice.
ALTER TABLE account_keywords
    ADD COLUMN IF NOT EXISTS pull_claimed_at TIMESTAMPTZ;

ALTER TABLE account_keywords ENABLE ROW LEVEL SECURITY;

-- 2. video_keywords ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS video_keywords (
    video_id   TEXT        NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    keyword    TEXT        NOT NULL,
    platform   TEXT        NOT NULL,
    found_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (video_id, keyword, platform)
);

CREATE INDEX IF NOT EXISTS video_keywords_keyword_idx
    ON video_keywords (keyword, platform, found_at DESC);

ALTER TABLE video_keywords ENABLE ROW LEVEL SECURITY;

-- 3. Settings ---------------------------------------------------------------
-- system_config.value is JSONB (see 0023). Defaults only; edit in place.
-- A search that finds nothing (or fails) waits 2x, then 4x the interval.

INSERT INTO system_config (key, value) VALUES
    ('keyword_refresh_enabled',        'true'::jsonb),  -- off switch for the keyword step only
    ('keyword_refresh_interval_hours', '12'::jsonb),    -- a search is repeated after this
    ('keyword_search_items',           '30'::jsonb),    -- posts asked from Apify per search
    ('keyword_posts_per_pull',         '15'::jsonb),    -- top posts by views kept per search
    ('keyword_lookback_days',          '14'::jsonb),    -- ignore posts older than this
    ('keyword_per_run_limit',          '5'::jsonb),     -- searches per job run (every 2h)
    ('keyword_daily_cap',              '40'::jsonb)     -- searches per UTC day, all users
ON CONFLICT (key) DO NOTHING;

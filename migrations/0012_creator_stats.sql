-- ============================================================
-- 0012 — creator_stats + lifecycle states
-- ============================================================
-- 1. Splits creator metrics off creators into a 1:1 creator_stats
--    table so identity (handle, platform, status) and metrics
--    (avg_views, post_frequency, acceptance_rate, ...) live apart.
--
--    Two flavours of stats co-exist on creator_stats:
--      a) Cumulative — touched by run_pipeline.py after every scrape
--         (total_videos_scraped, total_videos_accepted, total_views,
--          acceptance_rate, filter_stats)
--      b) Period — overwritten by the InitialScraper / re-evaluation
--         (period_avg_*, engagement_rate, post_frequency_per_week,
--          recency, viral_hit_count). evaluated_at + evaluation_period_days
--         say "as of when these are accurate".
--
-- 2. Repurposes creators.status as the lifecycle column with a
--    CHECK constraint and sets every existing row to 'pending'
--    so they re-enter through the new evaluation pipeline.
-- ============================================================

BEGIN;

-- 1. New table
CREATE TABLE creator_stats (
  creator_id              TEXT PRIMARY KEY REFERENCES creators(creator_id) ON DELETE CASCADE,

  -- Cumulative
  total_videos_scraped    INT    NOT NULL DEFAULT 0,
  total_videos_accepted   INT    NOT NULL DEFAULT 0,
  total_views             BIGINT NOT NULL DEFAULT 0,
  acceptance_rate         NUMERIC,
  filter_stats            JSONB,

  -- Period (set by InitialScraper / re-eval)
  evaluated_at            TIMESTAMPTZ,
  evaluation_period_days  INT,
  period_video_count      INT,
  period_avg_views        NUMERIC,
  period_avg_likes        NUMERIC,
  period_avg_comments     NUMERIC,
  period_avg_shares       NUMERIC,
  engagement_rate         NUMERIC,
  post_frequency_per_week NUMERIC,
  last_post_at            TIMESTAMPTZ,
  recency_days            NUMERIC,
  viral_hit_count         INT NOT NULL DEFAULT 0,

  -- Composite + extension
  creator_score           NUMERIC,
  extras                  JSONB,

  updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX ON creator_stats (evaluated_at);
CREATE INDEX ON creator_stats (acceptance_rate);
ALTER TABLE creator_stats ENABLE ROW LEVEL SECURITY;

-- 2. Move existing values across so the 41 rows don't lose their numbers
INSERT INTO creator_stats (
  creator_id, total_videos_scraped, total_videos_accepted, total_views,
  viral_hit_count, period_avg_views, acceptance_rate, filter_stats,
  post_frequency_per_week, creator_score
)
SELECT
  creator_id,
  COALESCE(videos_scraped, 0),
  COALESCE(videos_accepted, 0),
  COALESCE(total_views, 0),
  COALESCE(viral_hits, 0),
  avg_views,
  acceptance_rate,
  filter_stats,
  post_frequency,
  creator_score
FROM creators;

-- 3. Drop the legacy stat columns from creators
ALTER TABLE creators
  DROP COLUMN videos_scraped,
  DROP COLUMN videos_accepted,
  DROP COLUMN total_views,
  DROP COLUMN viral_hits,
  DROP COLUMN avg_views,
  DROP COLUMN acceptance_rate,
  DROP COLUMN filter_stats,
  DROP COLUMN post_frequency,
  DROP COLUMN creator_score;

-- 4. Lifecycle states + reset everyone to pending
ALTER TABLE creators
  ADD CONSTRAINT creators_status_check
  CHECK (status IN ('pending','evaluating','active','rejected','degraded'));

UPDATE creators SET status = 'pending';

COMMIT;

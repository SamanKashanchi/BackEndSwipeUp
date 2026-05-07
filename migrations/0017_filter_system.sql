-- ============================================================
-- 0017 — Filter system: thresholds, batch jobs, per-creator outcomes
-- ============================================================
-- Three tables drive the filter step (evaluating -> active/rejected):
--   1. filter_thresholds      per-platform knobs the operator tunes
--   2. filter_jobs            one row per batch run (mirror of evaluation_jobs)
--   3. filter_job_creators    per (job, creator) verdict + reasons
--
-- Knobs: NULL means "ignore this knob for that platform". A creator passes
-- only when every non-null knob is satisfied.
-- ============================================================

BEGIN;

CREATE TABLE filter_thresholds (
  platform                     TEXT PRIMARY KEY CHECK (platform IN ('tiktok','youtube','instagram','x')),
  min_followers                BIGINT,
  min_avg_views                NUMERIC,
  min_engagement_rate          NUMERIC,           -- 0..1
  min_post_frequency_per_week  NUMERIC,
  max_recency_days             NUMERIC,
  min_period_video_count       INT,
  updated_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_by                   TEXT
);
ALTER TABLE filter_thresholds ENABLE ROW LEVEL SECURITY;

-- Seed defaults: TikTok concrete numbers (we have data); other platforms
-- start blank so they don't reject anything until you tune them.
INSERT INTO filter_thresholds (
  platform, min_followers, min_avg_views, min_engagement_rate,
  min_post_frequency_per_week, max_recency_days, min_period_video_count,
  updated_by
) VALUES
  ('tiktok',    1000, 5000, 0.02, 1, 14, 3, 'migration'),
  ('youtube',   NULL, NULL, NULL, NULL, NULL, NULL, 'migration'),
  ('instagram', NULL, NULL, NULL, NULL, NULL, NULL, 'migration'),
  ('x',         NULL, NULL, NULL, NULL, NULL, NULL, 'migration');

CREATE TABLE filter_jobs (
  job_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  batch_size         INT NOT NULL,
  status             TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
  started_at         TIMESTAMPTZ,
  finished_at        TIMESTAMPTZ,
  total_creators     INT NOT NULL DEFAULT 0,
  active_creators    INT NOT NULL DEFAULT 0,
  rejected_creators  INT NOT NULL DEFAULT 0,
  error              TEXT,
  log_tail           TEXT,
  triggered_by       TEXT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ON filter_jobs (status, created_at DESC);
ALTER TABLE filter_jobs ENABLE ROW LEVEL SECURITY;

CREATE TABLE filter_job_creators (
  job_id        UUID NOT NULL REFERENCES filter_jobs(job_id) ON DELETE CASCADE,
  creator_id    TEXT NOT NULL REFERENCES creators(creator_id) ON DELETE CASCADE,
  verdict       TEXT NOT NULL CHECK (verdict IN ('active','rejected')),
  reasons       JSONB,                              -- list of objects: [{knob, expected, actual}]
  evaluated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (job_id, creator_id)
);
CREATE INDEX ON filter_job_creators (creator_id);
CREATE INDEX ON filter_job_creators (verdict);
ALTER TABLE filter_job_creators ENABLE ROW LEVEL SECURITY;

COMMIT;

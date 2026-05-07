-- ============================================================
-- 0016 — evaluation_jobs + evaluation_job_creators
-- ============================================================
-- Tracks batch evaluation runs (CreatorStatScraper). Mirrors the
-- discovery_jobs / discovery_job_creators pattern:
--   * one row per batch in evaluation_jobs (status, counts, timing)
--   * one row per creator-in-batch in evaluation_job_creators
--     (per-creator outcome — succeeded/failed, error, evaluated_at)
-- ============================================================

CREATE TABLE evaluation_jobs (
  job_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  batch_size          INT NOT NULL,
  status              TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
  started_at          TIMESTAMPTZ,
  finished_at         TIMESTAMPTZ,
  total_creators      INT NOT NULL DEFAULT 0,
  succeeded_creators  INT NOT NULL DEFAULT 0,
  failed_creators     INT NOT NULL DEFAULT 0,
  error               TEXT,
  log_tail            TEXT,
  triggered_by        TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ON evaluation_jobs (status, created_at DESC);

CREATE TABLE evaluation_job_creators (
  job_id        UUID NOT NULL REFERENCES evaluation_jobs(job_id) ON DELETE CASCADE,
  creator_id    TEXT NOT NULL REFERENCES creators(creator_id)    ON DELETE CASCADE,
  status        TEXT NOT NULL CHECK (status IN ('succeeded','failed')),
  videos_found  INT,
  error         TEXT,
  evaluated_at  TIMESTAMPTZ,
  PRIMARY KEY (job_id, creator_id)
);
CREATE INDEX ON evaluation_job_creators (creator_id);

ALTER TABLE evaluation_jobs           ENABLE ROW LEVEL SECURITY;
ALTER TABLE evaluation_job_creators   ENABLE ROW LEVEL SECURITY;

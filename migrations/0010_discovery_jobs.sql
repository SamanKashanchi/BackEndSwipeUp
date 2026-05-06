-- ============================================================
-- 0010 — discovery_jobs
-- ============================================================
-- Tracks creator-discovery runs (keyword search via Apify, etc.).
-- Distinct from scrape_jobs because the data shape differs:
--   * no creator_ids (we're FINDING creators, not iterating known ones)
--   * has input_params (the actor input, jsonb)
--   * counts are creator-centric (unique/new/existing) not video-centric
-- ============================================================

CREATE TABLE discovery_jobs (
  job_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  platform            TEXT NOT NULL CHECK (platform IN ('tiktok','youtube','x')),
  method              TEXT NOT NULL DEFAULT 'keyword' CHECK (method IN ('keyword')),
  input_params        JSONB NOT NULL,
  status              TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
  started_at          TIMESTAMPTZ,
  finished_at         TIMESTAMPTZ,
  total_videos        INT NOT NULL DEFAULT 0,
  unique_creators     INT NOT NULL DEFAULT 0,
  new_creators        INT NOT NULL DEFAULT 0,
  existing_creators   INT NOT NULL DEFAULT 0,
  error               TEXT,
  log_tail            TEXT,
  triggered_by        TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX ON discovery_jobs (status, created_at DESC);
CREATE INDEX ON discovery_jobs (platform, created_at DESC);
CREATE INDEX ON discovery_jobs (method, created_at DESC);

ALTER TABLE discovery_jobs ENABLE ROW LEVEL SECURITY;

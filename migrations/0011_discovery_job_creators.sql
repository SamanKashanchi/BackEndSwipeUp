-- ============================================================
-- 0011 — discovery_job_creators (junction)
-- ============================================================
-- One row per (discovery_job, creator-found-by-that-job). Lets us
-- answer "which creators did this run find?" and "which jobs first
-- surfaced this creator?" without storing creator metadata redundantly.
--
-- was_new captures whether *this* job's INSERT INTO creators actually
-- created the row, vs hitting ON CONFLICT (already in pool). Useful
-- for surfacing "5 new / 7 existing" with explicit creator lists.
-- position preserves the actor's natural ordering within the run.
-- ============================================================

CREATE TABLE discovery_job_creators (
  job_id       UUID NOT NULL REFERENCES discovery_jobs(job_id) ON DELETE CASCADE,
  creator_id   TEXT NOT NULL REFERENCES creators(creator_id)    ON DELETE CASCADE,
  was_new      BOOLEAN NOT NULL,
  position     INT,
  found_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (job_id, creator_id)
);

CREATE INDEX ON discovery_job_creators (creator_id);
CREATE INDEX ON discovery_job_creators (job_id, position);

ALTER TABLE discovery_job_creators ENABLE ROW LEVEL SECURITY;

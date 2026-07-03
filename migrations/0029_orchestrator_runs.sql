-- ============================================================
-- 0029 — orchestrator_runs: durable per-run log for the 5 jobs
-- ============================================================
-- The supervisor's per-job run state (last_run, running, summary) lived
-- only in the Flask process's _orch_jobs_state dict — lost on restart,
-- which reset cadence (all enabled jobs re-fired on the next tick) and
-- wiped run history. This table persists one row per supervisor run so:
--   * cadence survives restarts (derive "last ran" from MAX(started_at)),
--   * every job (incl. maintain/plan, which create no other job rows) has
--     a durable history + summary,
--   * the run-log UI reads real data instead of memory.
-- One row per run: opened 'running' at start, finalized ok|error at end.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS orchestrator_runs (
  id           BIGSERIAL PRIMARY KEY,
  job_name     TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'running',   -- running | ok | error
  started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at  TIMESTAMPTZ,
  duration_ms  INT,
  summary      JSONB,
  error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_orchestrator_runs_job ON orchestrator_runs (job_name, started_at DESC);
ALTER TABLE orchestrator_runs ENABLE ROW LEVEL SECURITY;

COMMIT;

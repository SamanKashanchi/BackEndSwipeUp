-- ============================================================
-- 0027 — Split the orchestrator into independent jobs + scrape queue
-- ============================================================
-- The single self-driving "tick" is broken into 5 independently
-- scheduled jobs (Maintain / Evaluate / Filter / Plan / Acquire),
-- each with its own enable flag + cadence, under one master kill-switch.
--
-- New state: scrape_queue — the Plan→Acquire hand-off. Plan computes
-- demand and ENQUEUES scrape requests here; Acquire DRAINS the queue
-- and runs the content pipeline, metered by daily_scrape_cap. This
-- decouples "decide what to scrape" from "do the scraping".
-- See CREATOR_SYSTEM.md.
-- ============================================================

BEGIN;

-- ── Scrape queue (Plan enqueues, Acquire drains) ──────────────
CREATE TABLE IF NOT EXISTS scrape_queue (
  id            BIGSERIAL PRIMARY KEY,
  creator_id    TEXT NOT NULL REFERENCES creators(creator_id) ON DELETE CASCADE,
  niche_id      TEXT,
  target_videos INT  NOT NULL DEFAULT 0,
  reason        TEXT,
  status        TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','claimed','done','failed')),
  scrape_job_id UUID,
  enqueued_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  claimed_at    TIMESTAMPTZ,
  finished_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_scrape_queue_status ON scrape_queue(status, enqueued_at);
-- One active request per creator: Plan can re-run freely without duplicating.
CREATE UNIQUE INDEX IF NOT EXISTS uq_scrape_queue_active
  ON scrape_queue(creator_id) WHERE status IN ('queued','claimed');
ALTER TABLE scrape_queue ENABLE ROW LEVEL SECURITY;

-- ── Replace the single-tick config with per-job config ────────
DELETE FROM system_config WHERE key IN (
  'orchestrator_enabled', 'orchestrator_dry_run', 'tick_interval_minutes',
  'per_tick_eval_cap', 'per_tick_filter_cap');

INSERT INTO system_config (key, value, description) VALUES
  ('orchestrator_master_enabled', 'false', 'Master kill-switch. When false NO job runs (fully dormant). When true, each job runs per its own toggle + cadence.'),
  ('job_maintain_enabled',  'true',  'Maintenance job: recover stuck jobs + age-out dormant/stale creators. Safe to leave on.'),
  ('job_maintain_interval_min', '15', 'Maintenance cadence (minutes).'),
  ('job_evaluate_enabled',  'false', 'Evaluate job: pending creators -> evaluating (Apify stats + cache + provisional niche).'),
  ('job_evaluate_interval_min', '30', 'Evaluate cadence (minutes).'),
  ('job_evaluate_batch',    '10',    'Creators evaluated per Evaluate run.'),
  ('job_filter_enabled',    'false', 'Filter job: evaluating creators -> active/rejected (threshold check). Free + re-runnable.'),
  ('job_filter_interval_min', '10',  'Filter cadence (minutes).'),
  ('job_filter_batch',      '25',    'Creators filtered per Filter run.'),
  ('job_plan_enabled',      'false', 'Plan job: compute demand -> enqueue scrape requests into scrape_queue. Spends nothing.'),
  ('job_plan_interval_min', '30',    'Plan cadence (minutes).'),
  ('job_acquire_enabled',   'false', 'Acquire job: drain the scrape queue -> run content scrapes (metered by daily_scrape_cap).'),
  ('job_acquire_interval_min', '60', 'Acquire cadence (minutes).')
ON CONFLICT (key) DO NOTHING;

COMMIT;

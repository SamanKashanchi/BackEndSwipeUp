-- ============================================================
-- 0031 — Reliable scrape_queue: leases, retry, dead-letter
-- ============================================================
-- On 2026-08-01 the content pipeline had been dead for ~17 days. Root cause:
-- 4 rows stuck in scrape_queue.status='claimed', orphaned when acquire workers
-- (Cloud Run Jobs) were killed mid-scrape (the "server restarted" errors)
-- between claiming a row and marking it done. Nothing ever reclaimed a stale
-- claim: enqueue_scrapes dedups on status IN ('queued','claimed') so Plan
-- refused to re-enqueue them, and claim_scrape_batch only claims 'queued' — so
-- the whole queue deadlocked and every Acquire run claimed 0.
--
-- This migration gives the queue the state it needs to self-heal:
--   * attempts    — how many times this request has been claimed/tried, so a
--                   reclaim can retry a few times then give up.
--   * last_error  — why the last attempt failed (lease expiry or scrape error),
--                   surfaced in the dashboard queue view.
--   * status 'dead' — dead-letter: a request that exhausted queue_max_attempts.
--                   Not in the active set, so Plan can still enqueue a fresh
--                   request later; the dead row stays as an audit trail.
--
-- The reclaim itself lives in code (orchestrator.reclaim_stale_claims), run at
-- the start of every orchestrator job: it resets 'claimed' rows older than
-- claim_lease_min back to 'queued' (or 'dead' past queue_max_attempts).
--
-- Also seeds orchestrator_master_enabled into system_config as a REAL row:
-- it was referenced by the dashboard but never defaulted in the brain and never
-- checked by the Cloud Run jobs (dead switch). The workers now self-gate on it.
-- ============================================================

-- 1) New columns (idempotent) ---------------------------------------------------
ALTER TABLE scrape_queue ADD COLUMN IF NOT EXISTS attempts   INT  NOT NULL DEFAULT 0;
ALTER TABLE scrape_queue ADD COLUMN IF NOT EXISTS last_error TEXT;

-- 2) Extend the status CHECK to allow the 'dead' dead-letter state ---------------
-- The original constraint from 0027 is named scrape_queue_status_check by PG's
-- default naming; drop-if-exists then re-add so this is safe to re-run.
ALTER TABLE scrape_queue DROP CONSTRAINT IF EXISTS scrape_queue_status_check;
ALTER TABLE scrape_queue
  ADD CONSTRAINT scrape_queue_status_check
  CHECK (status IN ('queued','claimed','done','failed','dead'));

-- 3) Config: master switch + reclaim knobs (idempotent) -------------------------
-- master defaults TRUE so wiring the workers to self-gate does NOT silently stop
-- production (the Cloud Scheduler cadence keeps firing; the jobs now honor this).
INSERT INTO system_config (key, value, description) VALUES
  ('orchestrator_master_enabled', 'true',
     'Global orchestrator kill-switch. When false, every job (local + Cloud Run) no-ops. Now actually enforced.'),
  ('claim_lease_min', '30',
     'Minutes a scrape_queue claim may sit in ''claimed'' before reclaim treats it as an orphaned (worker-died) claim and requeues it.'),
  ('queue_max_attempts', '3',
     'Max claim attempts for a scrape_queue request before it is sent to the ''dead'' dead-letter state.')
ON CONFLICT (key) DO NOTHING;

-- 4) Index to make the reclaim sweep cheap (partial: only live claims) ----------
CREATE INDEX IF NOT EXISTS idx_scrape_queue_claimed_at
  ON scrape_queue(claimed_at) WHERE status = 'claimed';

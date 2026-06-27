-- ============================================================
-- 0024 — creators failure tracking + provisional-niche semantics
-- ============================================================
-- Adds a retry counter so chronically-failing handles get condemned
-- to 'rejected' after max_failures instead of reverting to 'pending'
-- forever (the current infinite-retry money leak in run_eval).
--
-- Also formally repurposes the legacy creators.niche_id column as the
-- PROVISIONAL niche (best guess from origin context / eval text
-- estimate). Measured affinity — the source of truth — is computed on
-- demand from videos.niche_id and is NOT stored here.
-- ============================================================

BEGIN;

ALTER TABLE creators
  ADD COLUMN IF NOT EXISTS failure_count        INT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS last_failure_at      TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_failure_reason  TEXT;

COMMENT ON COLUMN creators.niche_id IS
  'Provisional niche: best guess from origin context (swipe->video niche, discovery search niche, onboarding) or the cheap eval-time text estimate. Measured affinity (videos.niche_id grouped by creator) is the source of truth and is computed on demand, not stored here.';

COMMENT ON COLUMN creators.failure_count IS
  'Consecutive eval/scrape failures. Reset to 0 on any success. At system_config.max_failures the creator is condemned to status=rejected (unscrapable).';

COMMIT;

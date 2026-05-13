-- ============================================================
-- 0020 — Multi-niche accounts: account_niches many-to-many
-- ============================================================
-- Phase 2 of the taxonomy refactor. Today an account has a single
-- `niche_id` column; users like more than one thing. Replace the
-- single column with a (account_id, niche_id, weight) table so the
-- feed engine can mix slot-by-slot across the user's interests.
--
-- After this migration:
--   - accounts.niche_id is gone
--   - account_niches has one row per (account, niche) the user
--     either auto-matched into or hand-picked at the niche-confirm
--     screen
--   - source distinguishes auto-match vs user-confirmed vs the
--     one-time legacy backfill below
--   - weight is the static onboarding similarity (0..1), locked at
--     confirm time. No behavioral drift in this phase.
--
-- Backfill rule: every existing accounts.niche_id becomes a single
-- account_niches row with weight=1.0 and source='legacy_migration'.
-- ============================================================

BEGIN;

CREATE TABLE account_niches (
  account_id          TEXT        NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
  niche_id            TEXT        NOT NULL REFERENCES niches(niche_id),
  weight              REAL        NOT NULL,
  source              TEXT        NOT NULL CHECK (source IN ('auto', 'user_confirmed', 'legacy_migration')),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  weight_computed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (account_id, niche_id)
);
CREATE INDEX idx_account_niches_account ON account_niches (account_id);
CREATE INDEX idx_account_niches_niche   ON account_niches (niche_id);

-- Backfill from accounts.niche_id (one row per account with a niche set).
INSERT INTO account_niches (account_id, niche_id, weight, source)
SELECT account_id, niche_id, 1.0, 'legacy_migration'
  FROM accounts
 WHERE niche_id IS NOT NULL;

-- Now safe to drop the single-niche column.
ALTER TABLE accounts DROP COLUMN niche_id;

COMMIT;

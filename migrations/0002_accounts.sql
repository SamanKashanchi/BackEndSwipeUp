-- ============================================================
-- Migration 0002 — Accounts shadow table + rename user_* to account_*
-- ============================================================
-- Reason: Embeddings are per-account, not per-user. One user has
--         multiple accounts; each account has its own feed and
--         swipe history. Adds FK integrity for interactions and
--         embeddings.
--
-- Firestore remains authoritative for full account/user profile.
-- This Postgres `accounts` table is a thin shadow — just enough
-- to anchor FKs and enable SQL joins for feed ranking queries.
-- ============================================================

BEGIN;

-- 1. Minimal accounts shadow table
CREATE TABLE accounts (
  account_id       TEXT PRIMARY KEY,                  -- matches Firestore Accounts/{id}
  user_id          TEXT NOT NULL,                     -- Firebase UID (no FK; Firestore is source of truth)
  platform         TEXT NOT NULL,
  handle           TEXT,
  niche_id         TEXT REFERENCES niches(niche_id),
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ON accounts (user_id);
CREATE INDEX ON accounts (niche_id);

-- 2. Rename user_* tables to account_* to match semantics
ALTER TABLE user_summary_embeddings RENAME TO account_summary_embeddings;
ALTER TABLE user_frame_embeddings   RENAME TO account_frame_embeddings;

-- 3. FK from embedding tables to accounts
ALTER TABLE account_summary_embeddings
  ADD CONSTRAINT fk_account_summary_emb_account
  FOREIGN KEY (account_id) REFERENCES accounts(account_id) ON DELETE CASCADE;

ALTER TABLE account_frame_embeddings
  ADD CONSTRAINT fk_account_frame_emb_account
  FOREIGN KEY (account_id) REFERENCES accounts(account_id) ON DELETE CASCADE;

-- 4. FK from interactions to accounts
ALTER TABLE interactions
  ADD CONSTRAINT fk_interactions_account
  FOREIGN KEY (account_id) REFERENCES accounts(account_id) ON DELETE CASCADE;

COMMIT;

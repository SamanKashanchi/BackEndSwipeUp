-- ============================================================
-- 0009 — account_creators (personal creator lists)
-- ============================================================
-- Replaces the per-user Firestore Creators subcollection.
-- One row = "this account follows this creator (and how it
-- learned about them)". Origin lets us distinguish onboarding
-- self/inspirations, in-feed Track button, keyword search,
-- swipe seed, etc. Composite PK enforces "one association
-- per (account, creator)".
-- ============================================================

CREATE TABLE account_creators (
  account_id   TEXT NOT NULL REFERENCES accounts(account_id)  ON DELETE CASCADE,
  creator_id   TEXT NOT NULL REFERENCES creators(creator_id)  ON DELETE CASCADE,
  origin       TEXT NOT NULL,
  added_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (account_id, creator_id)
);

CREATE INDEX ON account_creators (creator_id);
CREATE INDEX ON account_creators (account_id, added_at DESC);
CREATE INDEX ON account_creators (origin, added_at DESC);

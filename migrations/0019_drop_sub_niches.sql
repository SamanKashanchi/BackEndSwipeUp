-- ============================================================
-- 0019 — Drop sub_niches layer; collapse animals / combat_sports
--         mother niches in favor of promoted standalone niches
-- ============================================================
-- Phase 1 of the taxonomy refactor. Sub-niches go away entirely;
-- the previously-sub-niche entries become mother niches in their
-- own right (boxing, mma, cats, dogs, horses, wildlife).
--
-- Prerequisite (must run BEFORE this migration):
--   ContentEngine/migrations/001_split_reclassify.py has run end-
--   to-end. That single script ingests the 6 new mother niches
--   into Postgres (boxing, mma, cats, dogs, horses, wildlife) AND
--   reassigns every video and creator currently pointing at
--   'animals' or 'combat_sports' to one of those new niches.
--
-- Safety guards below abort the transaction if any video or
-- creator still references 'animals' or 'combat_sports' — so a
-- premature run is fully reversible (it just rolls back).
-- ============================================================

BEGIN;

-- Supabase's default statement_timeout (60s) is too tight for DROP TABLE
-- CASCADE on a table with multiple indexes + FK fanout. Bump just for
-- this txn; resets at COMMIT.
SET LOCAL statement_timeout = '600s';
SET LOCAL lock_timeout      = '60s';

-- ── Safety guards ─────────────────────────────────────────────
DO $$
DECLARE
  stuck_videos   INT;
  stuck_creators INT;
  stuck_accounts INT;
BEGIN
  SELECT COUNT(*) INTO stuck_videos
    FROM videos
   WHERE niche_id IN ('animals', 'combat_sports');

  SELECT COUNT(*) INTO stuck_creators
    FROM creators
   WHERE niche_id IN ('animals', 'combat_sports');

  SELECT COUNT(*) INTO stuck_accounts
    FROM accounts
   WHERE niche_id IN ('animals', 'combat_sports');

  IF stuck_videos > 0 OR stuck_creators > 0 OR stuck_accounts > 0 THEN
    RAISE EXCEPTION
      'Cannot drop animals/combat_sports niches: % videos, % creators, % accounts still reference them. Run 001_split_reclassify.py and 002_reclassify_accounts.py first.',
      stuck_videos, stuck_creators, stuck_accounts;
  END IF;
END $$;

-- ── Drop sub_niche columns from videos ───────────────────────
-- FK to sub_niches comes off implicitly with DROP COLUMN.
ALTER TABLE videos
  DROP COLUMN IF EXISTS sub_niche_id,
  DROP COLUMN IF EXISTS sub_niche_scores;

-- ── Drop sub_niches table entirely ───────────────────────────
DROP TABLE IF EXISTS sub_niches CASCADE;

-- ── Remove the now-orphan parent niche rows ──────────────────
DELETE FROM niches
 WHERE niche_id IN ('animals', 'combat_sports');

COMMIT;

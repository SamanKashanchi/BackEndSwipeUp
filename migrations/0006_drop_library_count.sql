-- ============================================================
-- Migration 0006 — Drop creators.library_count
-- ============================================================
-- Reason: library_count was a denormalized counter that the
-- application code maintained manually. It drifted because the
-- three video-delete paths in scrape_api.py never decremented it
-- (cats_of_instagram had library_count=2 with 0 actual videos).
--
-- The value is fully derivable from `videos`:
--   SELECT COUNT(*) FROM videos WHERE creator_id = $1
--
-- API now derives it on read via subquery in _CREATOR_SELECT_SQL.
-- The pipeline's manual `library_count + 1` increment is removed.
-- Drop the column entirely so future code can't accidentally read
-- a stale value.
-- ============================================================

ALTER TABLE creators DROP COLUMN IF EXISTS library_count;

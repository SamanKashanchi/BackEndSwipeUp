-- ============================================================
-- 0013 — Move swipe counters from creator_swipe_stats matview onto creator_stats
-- ============================================================
-- The matview was a denormalised aggregate of interactions JOIN videos.
-- Refreshing it is one more moving piece (APScheduler in swipeup-api).
-- Folding the same five counters onto creator_stats means:
--   * one place for ALL creator metrics
--   * the SAME UPDATE cadence still runs, but writes columns directly
--   * the dashboard SELECT loses one JOIN
--
-- Refresh logic (the periodic UPDATE) lives in Backend/api/main.py.
-- ============================================================

BEGIN;

-- 1. Add the columns to creator_stats
ALTER TABLE creator_stats
  ADD COLUMN swipe_right_count        INT NOT NULL DEFAULT 0,
  ADD COLUMN swipe_left_count         INT NOT NULL DEFAULT 0,
  ADD COLUMN swipe_up_count           INT NOT NULL DEFAULT 0,
  ADD COLUMN total_swipes             INT NOT NULL DEFAULT 0,
  ADD COLUMN unique_swipers           INT NOT NULL DEFAULT 0,
  ADD COLUMN swipe_stats_refreshed_at TIMESTAMPTZ;

-- 2. Backfill from the existing matview so we don't lose what's already counted
UPDATE creator_stats cs
SET swipe_right_count        = m.swipe_right_count,
    swipe_left_count         = m.swipe_left_count,
    swipe_up_count           = m.swipe_up_count,
    total_swipes             = m.total_swipes,
    unique_swipers           = m.unique_swipers,
    swipe_stats_refreshed_at = m.refreshed_at,
    updated_at               = NOW()
FROM creator_swipe_stats m
WHERE cs.creator_id = m.creator_id;

-- 3. Drop the matview (no longer needed)
DROP MATERIALIZED VIEW creator_swipe_stats;

COMMIT;

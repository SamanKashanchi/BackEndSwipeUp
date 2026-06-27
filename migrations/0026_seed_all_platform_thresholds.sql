-- ============================================================
-- 0026 — Seed gentle filter thresholds for non-TikTok platforms
-- ============================================================
-- 0017 seeded concrete TikTok knobs but left instagram/youtube/x
-- all-NULL, which means _check_creator finds nothing to fail and every
-- non-TikTok creator auto-passes filter. That made filter a no-op for
-- 3 of 4 platforms.
--
-- We set only the universally-safe knobs (posted recently, posts a
-- minimum amount, has a minimal following) and leave views/engagement
-- NULL — those are platform-specific and better tuned from the dashboard
-- once we have data. Conservative on purpose: cull obvious junk without
-- over-rejecting a small pool. Operator tunes via PUT /api/filter/thresholds.
--
-- Only fills knobs that are still NULL so we never clobber operator edits.
-- ============================================================

BEGIN;

UPDATE creator_filter_thresholds SET
  min_followers           = COALESCE(min_followers, 1000),
  min_post_frequency_per_week = COALESCE(min_post_frequency_per_week, 0.5),
  max_recency_days        = COALESCE(max_recency_days, 30),
  min_period_video_count  = COALESCE(min_period_video_count, 3),
  updated_at              = NOW(),
  updated_by              = 'migration_0026'
WHERE platform IN ('instagram', 'youtube', 'x');

COMMIT;

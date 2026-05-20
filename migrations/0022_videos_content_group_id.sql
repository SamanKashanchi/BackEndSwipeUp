-- ============================================================
-- Migration 0022 — videos.content_group_id for near-duplicate dedup
-- ============================================================
-- A nightly job (ContentEngine/dedup/run_content_grouping.py) clusters
-- videos by SigLIP summary similarity (cosine ≥ 0.95) per niche and
-- writes the assigned group id here. The feed engine uses this column
-- to suppress repeats: once an interaction (any swipe) exists on any
-- member of a group, no other member of that group is ever served to
-- that account again.
--
-- NULL means "not yet grouped" (newly-scraped videos between nightly
-- runs). Feed queries treat NULL as ungrouped, so new videos surface
-- normally until the next dedup pass.
-- ============================================================

ALTER TABLE videos
  ADD COLUMN IF NOT EXISTS content_group_id INTEGER;

CREATE INDEX IF NOT EXISTS idx_videos_content_group
  ON videos(content_group_id);

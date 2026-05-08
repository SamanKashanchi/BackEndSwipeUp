-- ============================================================
-- 0018 — Rename filter tables for level-of-abstraction symmetry
-- ============================================================
-- The "filter" namespace had two unrelated concepts colliding:
--
--   filter_log              video-level (LayerOneFilter pipeline,
--                           pre-existing since 0001)
--   filter_thresholds       creator-level (operator knobs, 0017)
--   filter_jobs             creator-level (batch tracking, 0017)
--   filter_job_creators     creator-level (per-creator outcomes, 0017)
--
-- After this migration:
--   video_filter_log
--   creator_filter_thresholds
--   creator_filter_jobs
--   creator_filter_job_creators
--
-- ALTER TABLE RENAME auto-renames the indexes and updates the FK
-- constraints' target reference, so nothing else needs to change.
-- Code on disk has already been updated to the new names.
-- ============================================================

BEGIN;

ALTER TABLE filter_log           RENAME TO video_filter_log;
ALTER TABLE filter_thresholds    RENAME TO creator_filter_thresholds;
ALTER TABLE filter_jobs          RENAME TO creator_filter_jobs;
ALTER TABLE filter_job_creators  RENAME TO creator_filter_job_creators;

COMMIT;

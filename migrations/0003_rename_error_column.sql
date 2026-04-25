-- ============================================================
-- Migration 0003 — Rename scrape_jobs.error to error_message
-- ============================================================
-- Reason: Pipeline migration (0002/pipeline) wrote Python code
-- using `error_message` as the field name. Column in 0001 DDL
-- was named `error`. Rename for consistency so future migrations
-- are reproducible.
-- ============================================================

ALTER TABLE scrape_jobs RENAME COLUMN error TO error_message;

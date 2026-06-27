-- ============================================================
-- 0025 — creator_scrape_cache: hand the eval Apify pull to the scrape
-- ============================================================
-- Evaluation and the content scrape call the IDENTICAL Apify actor for
-- a creator, and the content download URL comes straight out of that
-- actor's response. Today eval strips the pull to 6 numeric stat fields
-- and throws the rest away, then the scrape pays Apify again.
--
-- This table caches eval's raw actor items so the content scrape can
-- reuse them (no 2nd Apify call) within system_config.cache_freshness_hours.
-- The signed download URLs expire (hours); the metadata/niche signal does
-- not. One row per creator (latest pull), replaced on each eval.
-- See CREATOR_SYSTEM.md §5.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS creator_scrape_cache (
  creator_id  TEXT PRIMARY KEY REFERENCES creators(creator_id) ON DELETE CASCADE,
  platform    TEXT NOT NULL,
  fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  item_count  INT NOT NULL DEFAULT 0,
  items       JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_creator_scrape_cache_fetched ON creator_scrape_cache (fetched_at);
ALTER TABLE creator_scrape_cache ENABLE ROW LEVEL SECURITY;

COMMIT;

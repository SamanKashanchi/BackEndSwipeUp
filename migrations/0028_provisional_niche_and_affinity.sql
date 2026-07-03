-- ============================================================
-- 0028 — Rename provisional niche column + durable creator_niche_affinity
-- ============================================================
-- 1. creators.niche_id -> creators.provisional_niche_id. It was always a
--    *guess* (origin hint / eval estimate); the new name says so. The measured
--    truth lives in creator_niche_affinity (below), not here.
--
-- 2. creator_niche_affinity: one row per (creator, niche) the creator has
--    produced ACCEPTED, classified videos in. accepted_count accumulates as the
--    scrape pipeline classifies videos and SURVIVES video deletions (the whole
--    point — deleting library inventory must not erase what a creator produces).
--    The denominator for the "is this their niche?" rule is the creator's
--    cumulative creator_stats.total_videos_scraped.
--
--    Selection rule (in the orchestrator): a creator's MEASURED niche = a niche
--    where accepted_count / total_videos_scraped >= 0.50 AND total_videos_scraped
--    > 10. Below that, the orchestrator falls back to provisional_niche_id.
-- ============================================================

BEGIN;

ALTER TABLE creators RENAME COLUMN niche_id TO provisional_niche_id;

CREATE TABLE IF NOT EXISTS creator_niche_affinity (
  creator_id     TEXT NOT NULL REFERENCES creators(creator_id) ON DELETE CASCADE,
  niche_id       TEXT NOT NULL REFERENCES niches(niche_id),
  accepted_count INT  NOT NULL DEFAULT 0,
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (creator_id, niche_id)
);
CREATE INDEX IF NOT EXISTS idx_cna_niche ON creator_niche_affinity (niche_id);
ALTER TABLE creator_niche_affinity ENABLE ROW LEVEL SECURITY;

COMMIT;

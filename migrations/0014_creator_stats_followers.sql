-- ============================================================
-- 0014 — Add followers column to creator_stats
-- ============================================================
-- Promotes follower count from channel_meta JSONB to a first-class
-- queryable column. KeywordScraper already collected it on every
-- discovery run; the value just lived in creators.channel_meta.
-- Filter SQL like "creators with >10K followers" now hits an indexed
-- BIGINT column instead of jsonb extract.
--
-- BIGINT (not INT) so we don't cap at 2.1B for top-tier creators.
-- ============================================================

BEGIN;

ALTER TABLE creator_stats ADD COLUMN followers BIGINT;

CREATE INDEX ON creator_stats (followers);

-- Backfill from any existing channel_meta.followers values.
UPDATE creator_stats cs
SET followers  = (c.channel_meta ->> 'followers')::bigint,
    updated_at = NOW()
FROM creators c
WHERE c.creator_id = cs.creator_id
  AND c.channel_meta ? 'followers'
  AND (c.channel_meta ->> 'followers') ~ '^[0-9]+$';

COMMIT;

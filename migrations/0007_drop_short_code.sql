-- ============================================================
-- 0007 — Drop videos.short_code
-- ============================================================
-- short_code was Instagram-only and redundant with video_id,
-- which is already platform-prefixed (e.g. "instagram_DUtOmsVCsjd").
-- The unprefixed remainder is the shortcode. URL construction
-- moves to deriving from video_id directly.
-- ============================================================

ALTER TABLE videos DROP COLUMN IF EXISTS short_code;

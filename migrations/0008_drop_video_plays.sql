-- ============================================================
-- 0008 — Drop videos.video_plays
-- ============================================================
-- video_plays was a duplicate of views: every platform's
-- build_firestore_doc maps the same source field (views /
-- viewCount / playCount) into both. The Firestore-era schema
-- intended a videoViews vs videoPlays distinction the actors
-- never delivered, so the column held nothing views didn't.
-- No frontend reads video_plays.
-- ============================================================

ALTER TABLE videos DROP COLUMN IF EXISTS video_plays;

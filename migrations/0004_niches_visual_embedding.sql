-- ============================================================
-- Migration 0004 — Add visual_embedding to niches + sub_niches
-- ============================================================
-- Reason: Firestore niches had two embedding fields:
--   - `embedding`         (text-based SigLIP encoding of the niche description)
--   - `visual_prototype`  (image-based SigLIP, averaged from exemplar video
--                          frames — used as an image->image anchor during
--                          classification)
--
-- The initial migration (0001) only carried over `embedding`. Pipeline
-- classification code references the second one and crashed at runtime
-- ("column visual_prototype does not exist").
--
-- Renaming on the way in: `visual_prototype` -> `visual_embedding`. Same
-- concept, less opaque name. Backfill from Firestore happens in
-- Backend/seed/02_backfill_visual_embedding.py.
-- ============================================================

ALTER TABLE niches     ADD COLUMN IF NOT EXISTS visual_embedding vector(1152);
ALTER TABLE sub_niches ADD COLUMN IF NOT EXISTS visual_embedding vector(1152);

-- ============================================================
-- Migration 0005 — Widen summary_text to match SigLIP text dim
-- ============================================================
-- Reason: account_summary_embeddings.summary_text was created as
-- vector(768) in 0001, intended for an earlier text-encoder plan.
-- The Session 6 onboarding rewrite uses SigLIP's text encoder
-- (siglip-so400m-patch14-384), which outputs 1152-dim vectors —
-- matching the SigLIP image side so we can compare the two in the
-- same space (the whole point of SigLIP).
--
-- Every onboarding since the Session 6 deploy has failed at the
-- INSERT into summary_text with:
--   psycopg.errors.DataException: expected 768 dimensions, not 1152
--
-- The column has zero rows (every prior onboarding bailed), so the
-- ALTER is non-destructive. There is no HNSW index on summary_text
-- (only on summary_siglip), so no index rebuild required.
-- ============================================================

ALTER TABLE account_summary_embeddings
    ALTER COLUMN summary_text TYPE vector(1152);

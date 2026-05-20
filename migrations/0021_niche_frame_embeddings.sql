-- ============================================================
-- Migration 0021 — Per-frame niche prototype storage
-- ============================================================
-- Mirror of video_frame_embeddings / account_frame_embeddings,
-- but keyed by niche_id. Each niche keeps every reference frame
-- it was built from instead of collapsing them into a single
-- mean vector — so video-to-niche scoring can do a pairwise
-- frame similarity matrix + top-K mean (the same pattern the
-- account-to-video ranker already uses), not a single dot
-- product against a smoothed centroid.
--
-- niches.visual_embedding stays as a derived mean cache (used
-- as a stage-1 ANN fallback anchor by the ranker when an
-- account has no summary yet) — generate_visual_prototype.py
-- recomputes it from the frames in this table on every run.
-- ============================================================

CREATE TABLE niche_frame_embeddings (
  niche_id          TEXT NOT NULL REFERENCES niches(niche_id) ON DELETE CASCADE,
  frame_idx         SMALLINT NOT NULL,
  source_url        TEXT,
  siglip_embedding  vector(1152),
  dino_embedding    vector(768),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (niche_id, frame_idx)
);

CREATE INDEX ON niche_frame_embeddings USING hnsw (siglip_embedding vector_cosine_ops);
CREATE INDEX ON niche_frame_embeddings USING hnsw (dino_embedding   vector_cosine_ops);

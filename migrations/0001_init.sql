-- ============================================================
-- SwipeUP  Backend — Initial Schema (v0)
-- ============================================================
-- Target: Supabase Postgres 17.6 + pgvector 0.8.0
-- Scope: Phase 2 Core — videos, interactions, creators,
--        embeddings (frame-level + summary), scrape jobs,
--        filter logs, niches. Users/Accounts stay in Firestore.
--
-- Decisions locked in:
--   video_id:   platform-prefixed ("instagram_<native_id>")
--   phash:      BYTEA (16 bytes binary)
--   filter_log: retained forever
--   DINO:       ViT-B/14, 768-dim
--   embeddings: frame-level (Option A, one row per frame, both
--               encoders on same row) + summary (averaged) kept
--               for retrieve-then-rerank
--   swipes:     single interactions table with UPSERT
--   counters:   materialized view refreshed on schedule
-- ============================================================

-- ============================================================
-- Extensions
-- ============================================================
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================
-- Niches + sub-niches (static reference data)
-- ============================================================
CREATE TABLE niches (
  niche_id              TEXT PRIMARY KEY,
  name                  TEXT NOT NULL,
  slug                  TEXT UNIQUE NOT NULL,
  description           TEXT,
  core_concept          TEXT,
  platform_context      TEXT,
  visual_characteristics JSONB,
  examples              JSONB,
  negative_examples     JSONB,
  canonical_keywords    TEXT[] DEFAULT '{}',
  embedding             vector(1152),
  embedding_version     INT NOT NULL DEFAULT 1,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE sub_niches (
  sub_niche_id          TEXT PRIMARY KEY,
  parent_niche_id       TEXT NOT NULL REFERENCES niches(niche_id) ON DELETE CASCADE,
  name                  TEXT NOT NULL,
  slug                  TEXT NOT NULL,
  description           TEXT,
  visual_characteristics JSONB,
  canonical_keywords    TEXT[] DEFAULT '{}',
  embedding             vector(1152),
  embedding_version     INT NOT NULL DEFAULT 1,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ON sub_niches (parent_niche_id);

-- ============================================================
-- Creators
-- ============================================================
CREATE TABLE creators (
  creator_id            TEXT PRIMARY KEY,
  platform              TEXT NOT NULL CHECK (platform IN ('instagram','tiktok','youtube','x')),
  handle                TEXT NOT NULL,
  creator_name          TEXT,
  bio                   TEXT,
  profile_picture_url   TEXT,
  channel_meta          JSONB,
  status                TEXT DEFAULT 'active',
  origin                TEXT,
  niche_id              TEXT REFERENCES niches(niche_id),

  videos_scraped        INT NOT NULL DEFAULT 0,
  videos_accepted       INT NOT NULL DEFAULT 0,
  library_count         INT NOT NULL DEFAULT 0,
  total_views           BIGINT NOT NULL DEFAULT 0,
  viral_hits            INT NOT NULL DEFAULT 0,
  avg_views             NUMERIC,
  acceptance_rate       NUMERIC,
  filter_stats          JSONB,
  post_frequency        NUMERIC,
  creator_score         NUMERIC,

  date_added            TIMESTAMPTZ DEFAULT NOW(),
  last_scraped_at       TIMESTAMPTZ,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ON creators (platform);
CREATE INDEX ON creators (niche_id);
CREATE INDEX ON creators (last_scraped_at);

CREATE TABLE creator_embeddings (
  creator_id            TEXT NOT NULL REFERENCES creators(creator_id) ON DELETE CASCADE,
  encoder               TEXT NOT NULL CHECK (encoder IN ('siglip','dino')),
  embedding_1152        vector(1152),
  embedding_768         vector(768),
  video_count           INT NOT NULL,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (creator_id, encoder)
);

-- ============================================================
-- Videos
-- ============================================================
CREATE TABLE videos (
  video_id              TEXT PRIMARY KEY,
  platform              TEXT NOT NULL,
  creator_id            TEXT NOT NULL REFERENCES creators(creator_id) ON DELETE CASCADE,
  short_code            TEXT,

  public_url            TEXT NOT NULL,
  original_url          TEXT NOT NULL,
  display_url           TEXT,

  caption               TEXT,
  hashtags              TEXT[] DEFAULT '{}',
  ocr_text              TEXT,
  transcript            TEXT,
  music                 TEXT,

  time_posted           TIMESTAMPTZ,
  scraped_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  views                 BIGINT DEFAULT 0,
  likes                 INT DEFAULT 0,
  comments              INT DEFAULT 0,
  shares                INT DEFAULT 0,
  video_plays           BIGINT DEFAULT 0,
  virality_score        NUMERIC,

  video_duration        NUMERIC,
  file_size_bytes       BIGINT,
  phash                 BYTEA,

  niche_id              TEXT REFERENCES niches(niche_id),
  sub_niche_id          TEXT REFERENCES sub_niches(sub_niche_id),
  niche_scores          JSONB,
  sub_niche_scores      JSONB,

  summary_embedding_siglip vector(1152),
  summary_embedding_dino   vector(768),
  embedding_version     INT NOT NULL DEFAULT 1,

  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ON videos (niche_id, virality_score DESC);
CREATE INDEX ON videos (niche_id, scraped_at DESC);
CREATE INDEX ON videos (creator_id, scraped_at DESC);
CREATE INDEX ON videos (phash);
CREATE INDEX ON videos USING hnsw (summary_embedding_siglip vector_cosine_ops);
CREATE INDEX ON videos USING hnsw (summary_embedding_dino   vector_cosine_ops);

-- ============================================================
-- Frame-level video embeddings
-- ============================================================
CREATE TABLE video_frame_embeddings (
  video_id              TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
  frame_idx             SMALLINT NOT NULL,
  siglip_embedding      vector(1152),
  dino_embedding        vector(768),
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (video_id, frame_idx)
);
CREATE INDEX ON video_frame_embeddings USING hnsw (siglip_embedding vector_cosine_ops);
CREATE INDEX ON video_frame_embeddings USING hnsw (dino_embedding   vector_cosine_ops);

-- ============================================================
-- User embeddings
-- ============================================================
CREATE TABLE user_summary_embeddings (
  account_id            TEXT PRIMARY KEY,
  user_id               TEXT NOT NULL,
  summary_siglip        vector(1152),
  summary_dino          vector(768),
  summary_text          vector(768),
  source                TEXT,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ON user_summary_embeddings (user_id);
CREATE INDEX ON user_summary_embeddings USING hnsw (summary_siglip vector_cosine_ops);

CREATE TABLE user_frame_embeddings (
  account_id            TEXT NOT NULL,
  frame_idx             SMALLINT NOT NULL,
  source_type           TEXT NOT NULL,
  source_ref            TEXT,
  siglip_embedding      vector(1152),
  dino_embedding        vector(768),
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (account_id, frame_idx)
);
CREATE INDEX ON user_frame_embeddings USING hnsw (siglip_embedding vector_cosine_ops);
CREATE INDEX ON user_frame_embeddings USING hnsw (dino_embedding   vector_cosine_ops);

-- ============================================================
-- Interactions (hot table)
-- ============================================================
CREATE TABLE interactions (
  account_id            TEXT NOT NULL,
  video_id              TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
  user_id               TEXT NOT NULL,
  first_seen_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  swipe                 TEXT CHECK (swipe IN ('right','left','up')),
  swiped_at             TIMESTAMPTZ,
  watch_ms              INT,
  completion_pct        NUMERIC,
  PRIMARY KEY (account_id, video_id)
);
CREATE INDEX ON interactions (user_id);
CREATE INDEX ON interactions (video_id, swipe);
CREATE INDEX ON interactions (account_id, last_seen_at DESC);

-- UPSERT pattern for swipe writes:
-- INSERT INTO interactions (account_id, user_id, video_id, last_seen_at, swipe, swiped_at)
-- VALUES ($1,$2,$3,NOW(),$4,NOW())
-- ON CONFLICT (account_id, video_id) DO UPDATE
-- SET last_seen_at=EXCLUDED.last_seen_at, swipe=EXCLUDED.swipe, swiped_at=EXCLUDED.swiped_at;

-- ============================================================
-- Creator swipe stats (materialized view)
-- ============================================================
CREATE MATERIALIZED VIEW creator_swipe_stats AS
SELECT
  v.creator_id,
  COUNT(*) FILTER (WHERE i.swipe = 'right')            AS swipe_right_count,
  COUNT(*) FILTER (WHERE i.swipe = 'left')             AS swipe_left_count,
  COUNT(*) FILTER (WHERE i.swipe = 'up')               AS swipe_up_count,
  COUNT(*) FILTER (WHERE i.swipe IS NOT NULL)          AS total_swipes,
  COUNT(DISTINCT i.account_id) FILTER (WHERE i.swipe IS NOT NULL) AS unique_swipers,
  NOW()                                                AS refreshed_at
FROM interactions i
JOIN videos v ON v.video_id = i.video_id
GROUP BY v.creator_id;
CREATE UNIQUE INDEX ON creator_swipe_stats (creator_id);

-- Refresh pattern (run on schedule every ~5 min):
-- REFRESH MATERIALIZED VIEW CONCURRENTLY creator_swipe_stats;

-- ============================================================
-- Scrape jobs
-- ============================================================
CREATE TABLE scrape_jobs (
  job_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  platform              TEXT NOT NULL,
  creator_ids           TEXT[] NOT NULL,
  status                TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
  started_at            TIMESTAMPTZ,
  finished_at           TIMESTAMPTZ,
  videos_scraped        INT DEFAULT 0,
  videos_accepted       INT DEFAULT 0,
  videos_uploaded       INT DEFAULT 0,
  videos_failed         INT DEFAULT 0,
  error                 TEXT,
  log_tail              TEXT,
  triggered_by          TEXT,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ON scrape_jobs (status, created_at DESC);
CREATE INDEX ON scrape_jobs (platform, created_at DESC);

-- ============================================================
-- Filter log
-- ============================================================
CREATE TABLE filter_log (
  id                    BIGSERIAL PRIMARY KEY,
  job_id                UUID REFERENCES scrape_jobs(job_id) ON DELETE SET NULL,
  video_id              TEXT,
  creator_id            TEXT REFERENCES creators(creator_id) ON DELETE SET NULL,
  platform              TEXT,
  niche_id              TEXT,
  passed                BOOLEAN NOT NULL,
  rejection_reason      TEXT,
  width                 INT,
  height                INT,
  aspect_ratio          NUMERIC,
  fps                   NUMERIC,
  duration_sec          NUMERIC,
  bpp                   NUMERIC,
  watermark_coverage    NUMERIC,
  best_niche            TEXT,
  best_score            NUMERIC,
  target_score          NUMERIC,
  audio_type            TEXT,
  language              TEXT,
  speech_ratio          NUMERIC,
  motion_entropy        NUMERIC,
  cuts_per_second       NUMERIC,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ON filter_log (job_id);
CREATE INDEX ON filter_log (creator_id, created_at DESC);
CREATE INDEX ON filter_log (passed, rejection_reason);

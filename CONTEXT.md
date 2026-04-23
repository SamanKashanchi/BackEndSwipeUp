# DoomSwipe Backend — Session Handoff Context

**Last updated:** 2026-04-22
**Status:** Supabase connected + working. Schema designed, not yet applied. No DDL executed.

This file is a context dump so you (or a fresh Claude session) can resume the migration planning work without re-researching from scratch.

---

## 1. What we're building

Three major changes the user wants to land this migration cycle:

### Change 1 — Hybrid data architecture
Move from Firestore-only to three stores:

| Store | Provider | What lives there |
|---|---|---|
| Firestore | Firebase (existing) | `users`, `Accounts`, `Keywords`, `Settings/schedule`, small configs |
| Postgres | Supabase | `interactions` (swipes/views), `creators`, `videos` metadata, `creator_performance`, `scrape_jobs`, `filter_log` |
| pgvector | Supabase (same DB, different tables) | `video_frame_embeddings`, `user_frame_embeddings`, summary vectors |

Firestore stays additive — we keep using it for user/auth/config data. Supabase is the new home for everything high-volume, analytical, or vector-similarity-based.

### Change 2 — Frame-level embeddings (no averaging)
Today: one averaged SigLIP vector per video (`video_embedding`, 1152-dim).
Target: 8 frame-level SigLIP embeddings + 8 frame-level DINOv2 embeddings per video.
For users: 12–20 SigLIP frames + 12–20 DINO frames + 1 text embedding (BGE, 768-dim).
**Store as arrays of vectors, NOT averaged.** Normalize before storage. Float32 to start; float16 (`halfvec`) later if storage pressure.

### Change 3 — DINOv2 structural embeddings
Add DINOv2 (ViT-B/14, 768-dim) alongside SigLIP. DINO captures structural/compositional similarity (meme formats, caption placement, editing style). Used ONLY for structure similarity, never for text. Must complement SigLIP, not replace it. Same frames feed both encoders.

---

## 2. Provider decision (DONE)

**Supabase.** One provider for Postgres AND vector. Chosen because:

1. One connection string, one billing, one admin UI.
2. pgvector lets you JOIN across relational and vector in one SQL query.
3. Avoids learning a dedicated vector DB (Pinecone/Qdrant/Weaviate) while also doing schema redesign.
4. At current scale (100K videos, 10K users target) pgvector is more than fast enough.

**Alternative considered but rejected:** Neon + Pinecone (best-in-class each, but 2x ops burden). Google Cloud SQL + pgvector (more ops work than Supabase for the same result). Turbopuffer/LanceDB (too new).

Cost trajectory: Free tier → Supabase Pro $25/mo when we outgrow storage.

---

## 3. Current stack state

### Running services
- **Vite dashboard:** `http://localhost:5173` (task `br3lq6c7d` in background)
- **Flask scrape API:** `http://127.0.0.1:5001` (task `bq4bmwoos` in background, may be dead if user restarted)
- **Supabase:** `https://amtcnzpswskojnmfogmx.supabase.co` in region `aws-1-us-east-1`

### Backend folder (`C:/Users/skash/Desktop/Pers/DoomSwipe/Backend/`)
Files present:
- `.env` — contains `DATABASE_URL=postgresql://postgres.amtcnzpswskojnmfogmx:...@aws-1-us-east-1.pooler.supabase.com:5432/postgres` (Session pooler)
- `requirements.txt` — `psycopg[binary]>=3.2`, `pgvector>=0.3`, `python-dotenv>=1.0`
- `test_connection.py` — connection sanity check script (PASSES)
- `README.md`
- `.gitignore`

Packages installed to system Python 3.11.9:
- `psycopg 3.3.3` + `psycopg-binary 3.3.3`
- `pgvector 0.4.2` (Python client)
- `python-dotenv 1.1.0`

### Supabase project state
- Postgres 17.6 on aarch64
- pgvector 0.8.0 extension ENABLED and tested working
- Round-trip latency from user's Windows machine: ~66ms
- **No tables created yet.** Only the extension is set up.

### Existing DoomSwipe layout
- `C:/Users/skash/Desktop/Pers/DoomSwipe/ContentEngine/` — Python scraping pipeline (works, recent fixes applied for UTF-8)
- `C:/Users/skash/Desktop/Pers/DoomSwipe/Creator_Dashboard/` — Vite + Flask admin (works)
- `C:/Users/skash/Desktop/Pers/DoomSwipe/WebApp/` — React swipe app (works, reads Firestore directly)
- `C:/Users/skash/Desktop/Pers/DoomSwipe/Backend/` — this folder, the new Postgres/Supabase layer

### Recent pipeline fixes (already landed — unrelated to schema work)
- UTF-8 stdout guard added to all pipeline scripts so they don't crash on Windows cp1252 on arrow chars / emojis / progress bars
- `scrape_api.py` passes `PYTHONIOENCODING=utf-8` + `PYTHONUTF8=1` to subprocesses, decodes Popen stdout as UTF-8
- `_safe_print` helper wraps Flask log-relay prints

### Known minor pipeline issues NOT YET FIXED (deferred by user)
- **Step 4 Firebase Storage 409 race** — `blob.upload_from_filename()` then `blob.make_public()` occasionally 409s on re-upload of existing blobs. Fix: `predefined_acl="publicRead"` on upload, OR `blob.reload()` before `make_public()`.
- **Step 5 full-collection scan** — `sync_creator_stats.py` walks all 51 creators on every scrape when it should only touch affected ones. User started a dispatch for this and then CANCELLED it to focus on the big migration instead.

---

## 4. Key findings from the data audit (inform the schema)

A researcher did a deep pass on all three codebases. Main discoveries the user needs to know:

1. **The feed has no ranking.** `WebApp/.../SwipeFeed.jsx:71-72` does `where('niche_id', '==', accountNicheId)` with no `orderBy`, no `limit`, no pagination cursor. Pulls entire niche slice on every session. Seen-filter happens client-side by downloading the whole `VideoInteractions` subcollection.
2. **Onboarding generates user embeddings that are never read at feed time.** `user_frame_embedding`, `user_text_embedding`, `user_embedding` are written to `users/{uid}/Accounts/{accountId}` but SwipeFeed doesn't use them. All that compute wasted.
3. **Swipe writes hit a Firestore hot doc.** Every swipe does `global_creators/{creator_id}.SwipeRightCount += 1`. At viral traffic this will exceed Firestore's 1 write/sec/doc limit. This is one of the primary drivers to move to Postgres.
4. **Scrape jobs live in a Python dict** (`scrape_api.py`), lost on restart. Needs to become a real table.
5. **Filter logs live only in local CSV** on the pipeline server. Not queryable. Needs a real table.
6. **The averaged `video_embedding` (1152-dim SigLIP) is shipped to the client on every feed query** even though it's never used client-side. This wastes bandwidth.
7. **Per-frame SigLIP embeddings are computed, used for classification, then discarded** — not stored anywhere. This is why Change 2 requires a re-embed or lazy-backfill strategy; we can't recover the frame data from Firestore.

### Firestore collections the audit found

| Collection | Writer | Cardinality | Hot/cold | Fate in new design |
|---|---|---|---|---|
| `videos` | pipeline `upload_to_firebase.py` | ~10K–100K | HOT read | MOVE to Postgres + pgvector |
| `global_creators` | pipeline + webapp (swipe counters) | ~500–5K | WARM | MOVE to Postgres; swipe counts become matview |
| `niches` | `nichePopulate.py` | ~20–50 | COLD | MOVE to Postgres (cheap, enables joins) |
| `sub_niches` | `nichePopulate.py` | ~100–300 | COLD | MOVE to Postgres |
| `users/{uid}` | Onboarding | 1/user | WARM | KEEP in Firestore (auth coupling) |
| `users/{uid}/Accounts/{id}` | Onboarding + ManageFeed | 1–5/user | WARM | KEEP in Firestore; mirror embedding fields to Postgres |
| `users/{uid}/Accounts/{id}/VideoInteractions/{vid}` | SwipeFeed | 0–∞/account | HOT WRITE | MOVE to Postgres `interactions` table |
| `users/{uid}/Accounts/{id}/Remix/{id}` | SwipeFeed (right swipe) | bounded | WARM | KEEP in Firestore for phase 1 |
| `users/{uid}/Accounts/{id}/Schedule/{id}` | Schedule page | ≤200/account | WARM | KEEP in Firestore for phase 1 |
| `users/{uid}/Accounts/{id}/Keywords/{id}` | ManageFeed | ≤50 | COLD | KEEP |
| `users/{uid}/Accounts/{id}/Creators/{id}` | SwipeFeed track, ManageFeed | ≤200 | COLD | KEEP |
| `users/{uid}/Accounts/{id}/Settings/schedule` | ManageFeed | 1 | COLD | KEEP |

---

## 5. Decisions locked in during this session

User answered these four schema questions:

### Q1 — Frame-level embedding modeling
**Chosen: Option A — one row per frame.**
- Table: `video_frame_embeddings(video_id, frame_idx, siglip_embedding vector(1152), dino_embedding vector(768))` with PK `(video_id, frame_idx)`.
- Rationale: Same 8 frames feed both encoders, so one row per frame with both columns is clean + half the rows of per-encoder tables.

### Q2 — Swipe writes structure
**Chosen: One table with UPSERT. No event log yet.**
- Table: `interactions(account_id, video_id, user_id, first_seen_at, last_seen_at, swipe, swiped_at, watch_ms, completion_pct)` with PK `(account_id, video_id)`.
- Rationale: Double-write of event log not justified without an actual analytics use case. Add event log later if needed.

### Q3 — Creator counters (SwipeRightCount etc.)
**Chosen: Materialized view refreshed on schedule.**
- View: `creator_swipe_stats` aggregated from `interactions` joined on `videos.creator_id`.
- Refresh: `REFRESH MATERIALIZED VIEW CONCURRENTLY creator_swipe_stats` every ~5 minutes via scheduler.
- Rationale: Removes the write-contention hot doc, no drift between events and counters, slight staleness is fine for creator scoring.

### Q4 — Keep averaged embedding alongside frame-level?
**Chosen: Yes, keep a summary embedding.**
- Store `summary_embedding_siglip vector(1152)` and `summary_embedding_dino vector(768)` on `videos` table.
- Rationale: Retrieve-then-rerank pattern — use summary HNSW index to fetch top ~500 candidates, then frame-level max-sim re-ranking in application code. Cheap to store, makes the hot query fast.

---

## 6. The schema (v0 DDL)

This is the full migration target. **NOT YET APPLIED** — no tables exist in Supabase yet.

Save this as `Backend/migrations/0001_init.sql` when we're ready.

```sql
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
  creator_id            TEXT PRIMARY KEY,   -- e.g. "instagram_cats_of_instagram"
  platform              TEXT NOT NULL CHECK (platform IN ('instagram','tiktok','youtube','x')),
  handle                TEXT NOT NULL,
  creator_name          TEXT,
  bio                   TEXT,
  profile_picture_url   TEXT,
  channel_meta          JSONB,
  status                TEXT DEFAULT 'active',
  origin                TEXT,
  niche_id              TEXT REFERENCES niches(niche_id),

  -- pipeline counters (writer: pipeline only)
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
  video_id              TEXT PRIMARY KEY,   -- platform-prefixed: "instagram_<native_id>"
  platform              TEXT NOT NULL,
  creator_id            TEXT NOT NULL REFERENCES creators(creator_id) ON DELETE CASCADE,
  short_code            TEXT,               -- Instagram only

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
  phash                 BYTEA,              -- 16 bytes binary (changed from hex text)

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
-- User embeddings (paralllel to videos)
-- ============================================================
CREATE TABLE user_summary_embeddings (
  account_id            TEXT PRIMARY KEY,
  user_id               TEXT NOT NULL,
  summary_siglip        vector(1152),
  summary_dino          vector(768),
  summary_text          vector(768),        -- BGE
  source                TEXT,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ON user_summary_embeddings (user_id);
CREATE INDEX ON user_summary_embeddings USING hnsw (summary_siglip vector_cosine_ops);

CREATE TABLE user_frame_embeddings (
  account_id            TEXT NOT NULL,
  frame_idx             SMALLINT NOT NULL,
  source_type           TEXT NOT NULL,      -- 'profile_post' | 'liked_video' | 'onboarding'
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
-- Creator swipe stats (matview)
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

-- Refresh every ~5 min:
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
```

---

## 7. What's intentionally NOT in phase 1

- **`users`, `Accounts`, `Keywords`, `Settings/schedule`** — stay in Firestore (Firebase Auth coupling, low write volume).
- **`Remix`, `Schedule`** — stay in Firestore (small volume, WebApp rewrite cost > benefit for phase 1).
- **`halfvec` (float16 embeddings)** — start with float32. At ~500K videos we migrate via single `ALTER TABLE`.
- **Full-text/trigram indexes on caption/transcript** — add when we need search.
- **IVFFlat fallback indexes** — HNSW is fine up to a few million vectors.

---

## 8. Open questions (user hasn't answered yet)

1. **`video_id` PK format** — raw (`3878815678858117847`) or prefixed (`instagram_3878815678858117847`)?
   - My recommendation: **prefixed** (safer against cross-platform ID collision).
2. **`phash` storage** — I switched to `BYTEA` (16 bytes binary) in the DDL. Is anything downstream relying on the hex string representation? If yes, keep as `TEXT`.
3. **`filter_log` retention** — grow forever (small) or TTL to 90 days?
4. **DINOv2 variant** — I assumed ViT-B/14 (768-dim). If user wants ViT-L/14 (1024-dim), the DDL vector column sizes change.

---

## 9. Known blind spots from the audit

Not blockers, but follow-up work once schema is applied:

1. **Remix → Schedule promotion code** — not found in audited files. Might be in an unread component or a Cloud Function.
2. **`scrape_api.py` full shape** — file was too large for the researcher to fully read; only partial coverage.
3. **Cloud Run `onboarding-service`** — produces `user_frame_embedding` etc. during onboarding; dimensions unconfirmed. Assumed 1152 (SigLIP).
4. **`notified30` / `notified5` on Schedule docs** — something writes these (probably a Cloud Function for "post goes live in N minutes" notifications). Not found in the audit.

---

## 10. Next steps when the user returns

In order:

1. **Answer open questions** in section 8 (video_id format, phash format, filter_log retention, DINO variant).
2. **Apply the DDL** to Supabase:
   - Save the SQL as `Backend/migrations/0001_init.sql`
   - Apply via `psycopg` script OR the Supabase SQL editor
   - Verify all tables, indexes, extensions are in place
3. **Write a seed/backfill script** to import from Firestore:
   - Step A: niches, sub_niches (small, static, do first)
   - Step B: creators (from `global_creators`)
   - Step C: videos + `summary_embedding_siglip` (using the current averaged `video_embedding` field from Firestore)
   - Step D: skip `video_frame_embeddings` for now — we decided on lazy backfill (only videos scraped after cutover get frame embeddings).
4. **Update the pipeline** (`ContentEngine/CreatorScraper/run_pipeline.py`, `upload_to_firebase.py`) to ALSO write to Postgres:
   - Write per-frame SigLIP embeddings to `video_frame_embeddings` (preserve the 8 frames instead of discarding)
   - Add DINOv2 inference alongside SigLIP (same frames)
   - Write to `videos` and `filter_log` tables
   - Dual-write to Firestore during cutover
5. **Update the WebApp** (`SwipeFeed.jsx`):
   - Replace the Firestore feed query with a FastAPI endpoint backed by Postgres
   - Swipe writes go to Postgres `interactions` table
   - Remove the `global_creators` increment (matview replaces it)
6. **Build a minimal FastAPI skeleton** in `Backend/` to serve the feed query and accept swipe writes. Holds the psycopg connection pool.
7. **Schedule the matview refresh** (cron or APScheduler every 5 min).

---

## 11. Why decisions were made the way they were (the "why" for future-you)

- **Why Supabase over Neon + Pinecone:** one provider, JOIN across tables and vectors in one SQL query, less ops surface area. Dedicated vector DBs are for when you exceed ~10M vectors with high-QPS requirements — not now.
- **Why frame-level embeddings (not averaging):** averaging loses information about WHICH frame matches a query. With frame-level, max-sim similarity captures "this one frame is very similar to that one frame" — better ranking fidelity. The tradeoff is 8x storage per entity.
- **Why DINOv2 alongside SigLIP:** SigLIP captures semantic similarity (what the content IS). DINO captures structural/compositional similarity (how it's formatted — meme layout, caption style, edit pacing). Two different axes of "similar," both useful for feed diversity.
- **Why summary + frame embeddings (not just frame):** HNSW on 8x the vectors is slower to query. Retrieve-then-rerank is the standard pattern — use summary to narrow candidates to top ~500, then frame-level max-sim re-rank on that small set.
- **Why materialized view for creator counters:** Firestore hot-doc limit was going to bite on viral videos. Matview removes the write-contention entirely, slight staleness is fine for creator scoring which isn't sub-second critical.
- **Why keep users + accounts in Firestore:** Firebase Auth tightly coupled to Firestore users collection. Migrating buys nothing (low volume, no pain today) and costs significant rewiring.
- **Why Session pooler (port 5432) not Transaction pooler (6543):** long-lived Python server connections need session features (prepared statements, temp tables). Transaction pooler breaks these.

---

## 12. Quick reference — how to pick up

When you start a fresh session, tell Claude:
> "Read `Backend/CONTEXT.md` — we're mid-migration from Firestore to Supabase + pgvector. Schema is designed but not applied. Pick up at Section 10, Next Steps."

Then have Claude read this file and confirm it understands before continuing.

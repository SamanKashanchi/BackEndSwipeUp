# DoomSwipe Backend — Session Handoff Context

**Last updated:** 2026-04-23 (end of Session 6)
**Status:** Pipeline + Dashboard fully migrated to Postgres. **Onboarding service (siglip-server) rewritten this session** to read niches from Postgres and persist per-frame embeddings directly to `accounts` + `account_summary_embeddings` + `account_frame_embeddings`. WebApp swipe feed is the last major piece still on Firestore.

---

## ⚠️ IMPORTANT — READ BEFORE STARTING FRESH CLAUDE SESSION

Launch Claude from `C:/Users/skash/Desktop/Pers/DoomSwipe/` (NOT from `Backend/` or any subfolder).

Each subfolder has its own `.git` directory, so if Claude launches from a subfolder the security guard treats that subfolder as the project boundary and blocks cross-folder edits. Launching from the DoomSwipe root makes the whole tree one project.

```bash
cd C:/Users/skash/Desktop/Pers/DoomSwipe
claude
```

Then tell Claude:

> "Read `Backend/CONTEXT.md` end-to-end. Confirm understanding: pipeline + dashboard fully Postgres and verified working. WebApp swipe feed NOT migrated yet. Next major task is the WebApp feed repoint — a plan with 9 design decisions was drafted in Session 5 but the user hasn't confirmed them yet. Walk me through the 9 decisions and ask for my calls before implementing."

Restart services if they're dead:

```bash
# Dashboard Vite (already-allowlisted)
cd Creator_Dashboard && npm run dev

# Dashboard Flask API (serves /api/*)
cd Creator_Dashboard && python api/scrape_api.py

# WebApp Vite (for testing the feed migration)
cd WebApp/swipeUp_frontend_v1 && npm run dev
```

Flask runs on port 5001, Dashboard Vite on 5173, WebApp Vite on 5179.

---

## 1. What we're building

Three architectural changes the user wanted:

### Change 1 — Hybrid data architecture ✅ DONE (mostly)

| Store | Provider | What lives there |
|---|---|---|
| Firestore | Firebase | `users`, `Accounts`, `Keywords`, `Settings`, `Schedule`, `Remix`, `Creators` subcollections |
| Postgres | Supabase | `videos`, `creators`, `niches`, `sub_niches`, `interactions`, `scrape_jobs`, `filter_log`, `accounts` shadow, `creator_swipe_stats` matview |
| pgvector | Supabase (same DB) | `video_frame_embeddings`, `account_frame_embeddings`, `summary_embedding_siglip`, `summary_embedding_dino`, `niches.embedding`, `niches.visual_embedding` |
| Firebase Storage | Firebase | mp4 blobs only |

### Change 2 — Frame-level embeddings ✅ DONE (SigLIP), ⏳ DINO pending
- 8 SigLIP frame embeddings per video, stored as individual rows in `video_frame_embeddings` keyed by `(video_id, frame_idx)`.
- Summary embedding (mean-pooled across frames) stored on `videos.summary_embedding_siglip` for retrieve-then-rerank.
- **NOT averaged before storage** — frame fidelity preserved.

### Change 3 — DINOv2 ⏳ NOT YET INTEGRATED
- Columns exist (`video_frame_embeddings.dino_embedding`, `videos.summary_embedding_dino`, 768-dim) but all NULL.
- Pipeline has SigLIP only. Adding DINO = ~150 lines in `run_pipeline.py`, no schema change.

---

## 2. Current state at a glance

| Layer | Status |
|---|---|
| Supabase Postgres | ✅ Live, 11 tables + 1 matview, HNSW vector indexes |
| Migrations applied | ✅ 0001_init, 0002_accounts, 0003_rename_error_column, 0004_niches_visual_embedding |
| Seed | ✅ 4 niches, 7 sub-niches, 51 creators. `visual_embedding` backfilled from Firestore. |
| Pipeline (ContentEngine) | ✅ Fully on Postgres, verified end-to-end |
| Dashboard — Scrape page | ✅ Fully on Postgres |
| Dashboard — Creators page | ✅ Fully on Postgres |
| Dashboard — Content/Library page | ✅ Reads Postgres; niche CRUD still Firestore (see §5) |
| Dashboard — Signal test page | ✅ Migrated to Postgres this session |
| **WebApp — SwipeFeed** | ❌ **Still on Firestore ← NEXT MAJOR TASK** |
| WebApp — Onboarding | ✅ Server-side Postgres writes (Session 6). Client drops embedding writes to Firestore. Needs Cloud Run re-deploy with new env vars. |
| Firebase Storage (mp4s) | ✅ Unchanged, intentional |
| Firestore | ✅ Kept for users/Accounts/Keywords/Schedule/Remix/Creators |

### Live services

- **Supabase:** `https://amtcnzpswskojnmfogmx.supabase.co`, region `aws-1-us-east-1`, Postgres 17.6 + pgvector 0.8.0
- **DATABASE_URL** is in `Backend/.env` (Session pooler, port 5432)
- **Firebase project:** `reel-swipe-app`, bucket `reel-swipe-app.firebasestorage.app`
- **Flask API** (dashboard): `http://127.0.0.1:5001`
- **Dashboard Vite:** `http://localhost:5173`
- **WebApp Vite:** `http://localhost:5179`

### Existing tree

```
C:/Users/skash/Desktop/Pers/DoomSwipe/
├── Backend/           — Postgres layer + migrations + seed + (future) FastAPI
├── ContentEngine/     — Python scraping pipeline (runs as subprocess from Flask)
├── Creator_Dashboard/ — Vite React admin + Flask API (port 5173 + 5001)
└── WebApp/            — React consumer app (port 5179) — still on Firestore
```

---

## 3. What's been verified working end-to-end (Session 5)

A scrape of Instagram `@britishmemes` completed in Session 5:

- 14 videos scraped
- 14 passed filter (all correctly classified as Memes niche)
- 14 uploaded to Firebase Storage + Postgres
- 14 `video_frame_embeddings` rows per video (112 frame rows total)
- 14 `filter_log` rows with all 22 columns populated (job_id, creator_id, aspect_ratio, target_score, audio_type, language, speech_ratio, motion_entropy, cuts_per_second — everything)
- `scrape_jobs` row finalized with `videos_scraped=14, videos_accepted=14, videos_uploaded=14, videos_failed=0, log_tail=...`
- `creators.library_count` correctly bumped by 14 (not double-counted on re-scrape)
- `creators.channel_meta` populated: `{"channel_name": "British Memes"}`

**`creator_swipe_stats` matview is still empty** because `interactions` is empty — that table will start filling once the WebApp swipe feed migrates.

---

## 3a. Session 6 changes (onboarding service)

Session 6 rewired the Cloud Run siglip-server and its client counterpart.

### `WebApp/swipeUp_frontend_v1/siglip-server/server.py` — full rewrite
- **Reads niches from Postgres** (not Firestore). Prefers `visual_embedding` over `embedding`; normalises to unit vectors at load time.
- **Per-frame embeddings** — dropped the `embed_video_frames` average-per-video helper. New flow: 4 posts × 8 frames = up to 32 frame rows stored individually, each with `frame_idx = post_idx * 8 + f_in_post`, `source_type='profile_post'`, `source_ref=<post shortCode/id/url>`.
- **Server-side Postgres persistence** — new `persist_onboarding` helper runs a single transaction:
  - UPSERT `accounts(account_id, user_id, platform, handle, niche_id)`
  - UPSERT `account_summary_embeddings(account_id, user_id, summary_siglip, summary_text, source='onboarding')` (summary_dino NULL until DINO integration)
  - `DELETE FROM account_frame_embeddings WHERE account_id = %s` + bulk `executemany` insert of all frames
  - Re-onboarding wipes and rewrites the frame rows (never stale residue)
- **Request model** now requires `account_id` + `user_id` (passed from client).
- **Response model** slimmed — no embedding floats returned. Just `{niche_id, niche_name, similarity_score, frames_stored, used_fallback}`.
- **Niche scoring** unchanged: `0.6 * image_to_image + 0.4 * text_to_image` when frames exist, else text-only. Uses mean-pooled summary SigLIP as the image side (not max-sim — phase 2b upgrade).
- **Niche cache reload**: `POST /reload-niches` still works — call it after creating/deleting niches via the (not-yet-built) `POST /api/niches` endpoint.
- `firebase_admin` dependency removed from server entirely. `firebase-admin` line dropped from requirements.txt. Dockerfile has DATABASE_URL env var documented.

### `WebApp/swipeUp_frontend_v1/src/pages/Onboarding.jsx` — client rewire
- `POST /match-niche` body now includes `account_id` (the Firestore-generated Accounts doc ID) + `user_id` (Firebase uid).
- `setDoc` merge into Firestore Accounts doc **no longer writes** `user_frame_embedding`, `user_text_embedding`, `user_embedding`. Postgres is the only home for embeddings now.
- Firestore still gets `niche_id`, `niche_name`, `similarity_score` (UI reads these + SwipeFeed uses `niche_id` until that migrates).
- Existing Firestore Account docs created under the old flow have stale `user_*_embedding` fields — **harmless, no readers once WebApp feed migrates**, optional cleanup pass later.

### Deployment status
- **Code updated in-tree, NOT yet deployed to Cloud Run.** User needs to re-deploy:
  - `cd WebApp/swipeUp_frontend_v1/siglip-server/`
  - `gcloud run deploy onboarding-service --source . --region us-central1` (or whatever the existing deploy cmd is)
  - Set runtime env var `DATABASE_URL` (copy from `Backend/.env`)
  - Keep `HF_TOKEN` env var set (unchanged)
- Local test: run `uvicorn server:app --port 8080` with `Backend/.env` present → the loader picks it up automatically.

### What's in Postgres after a successful onboarding
```sql
SELECT * FROM accounts WHERE user_id = '<firebase-uid>';
-- 1 row per account with platform, handle, niche_id

SELECT account_id, source, vector_dims(summary_siglip) AS siglip_dim,
       vector_dims(summary_text) AS text_dim
FROM account_summary_embeddings WHERE user_id = '<firebase-uid>';
-- 1 row per account, source='onboarding', siglip_dim=1152, text_dim=1152

SELECT account_id, COUNT(*) AS frames, COUNT(DISTINCT source_ref) AS posts
FROM account_frame_embeddings WHERE account_id = '<account-doc-id>'
GROUP BY account_id;
-- frames = up to 32 (4 posts × 8 frames), posts = up to 4
```

### Deferred (same pattern as video pipeline)
- `summary_dino` + `dino_embedding` columns stay NULL. DINO integration is a single future pass that adds a second encoder alongside SigLIP in both the pipeline and the onboarding server.
- No auth on `/match-niche` — endpoint trusts client-provided `account_id`/`user_id`. Future hardening: verify a Firebase ID token and cross-check uid against `user_id`.
- Niche matching uses mean-pool summary, not max-sim over frames. Phase 2b upgrade; matches how the video side currently works.

---

## 4. Session 5 changes (new bugs fixed, new code shipped)

### New migration + backfill
- `Backend/migrations/0004_niches_visual_embedding.sql` — adds `visual_embedding vector(1152)` to `niches` and `sub_niches`. The old Firestore-era name `visual_prototype` is gone from Postgres code; Firestore still has `visual_prototype` as historical data.
- `Backend/seed/02_backfill_visual_embedding.py` — reads Firestore `visual_prototype` into Postgres `visual_embedding`. Ran once: 4/4 niches + 7/7 sub-niches loaded.

### Pipeline fixes (ContentEngine/CreatorScraper/)

**`upload_to_postgres.py`** — this file was broken in multiple places before Session 5. Before today, zero videos ever landed in Postgres despite the log saying "Uploaded". Fixes:
- `build_firestore_doc(video)` called with 1 arg but takes 2 — fixed to pass `public_url` too
- Doc field-mapping: `doc["likes"]` → `doc["performance"]["likes"]` (and comments/shares/video_plays); `doc["duration"]` → `doc["videoDuration"]`; raw_id → `doc["shortCode"]`
- `extract_video_id` — was reading `video.get("video_id", "")` which is always empty on Apify items. Now uses `platform.extract_video_id`.
- `local_path` — was reading `video.get("local_path")` which is never set. Now derived as `{VIDEOS_DIR}/{raw_id}.mp4`.
- **Silent transaction rollback bug:** before `with conn.transaction():` blocks, the pipeline's shared conn had open implicit read transactions from `fetch_creators` + `get_existing_video_ids` SELECTs. Because autocommit=False, our transaction blocks became SAVEPOINTs inside that outer transaction, and `conn.close()` with no explicit commit rolled everything back. Fix: `conn.commit()` at the top of `upload_videos` to close the prior read transaction before writes start.
- `library_count` was bumping on every re-scrape (ON CONFLICT UPDATE path). Fix: added `RETURNING (xmax = 0) AS inserted` and only bump when the row was genuinely inserted.
- Exception visibility: `logger.exception` was eaten (no handler). Replaced with `print(...)` + `traceback.print_exc()`.
- INSERT expanded from 16 to 26 columns.

**`run_pipeline.py`**:
- All `visual_prototype` → `visual_embedding` (3 locations, plus dict keys)
- `run_content_signals` bare `except:` replaced with explicit error print
- `_rejection_reason` set on `video_data` before `continue` so `_write_creator_acceptance_stats` surfaces real reasons (not "unknown")
- `filter_videos` now takes `job_id` kwarg, plumbed through
- `log_filter_result` signature: added `creator_id`, `job_id` kwargs; INSERT expanded to all 22 filter_log columns (was missing 9: aspect_ratio, target_score, audio_type, language, speech_ratio, motion_entropy, cuts_per_second, creator_id, job_id)
- `log_filter_result` uses **short-lived Postgres connection per insert** — the old long-lived `_fl_conn` was getting killed by Supabase pooler idle-timeout during 20-minute filter runs

**`scrape_all_creators.py`**:
- Channel_meta write migrated from Firestore `global_creators` to Postgres `creators.channel_meta` (JSONB merge)
- Dedup against `existing_firebase_ids` now uses namespaced IDs (`f"{platform.NAME}_{video_id}"`) — the old check never matched, causing silent re-downloads
- Added `MIN_VIDEO_BYTES = 100_000` validation — rejects 0-byte / truncated mp4s before they propagate to Storage

### Dashboard fixes (Creator_Dashboard/)

**`modules/scrape/scrape.js`** — all 7 Firestore call sites migrated:
- `loadCreatorDropdown` → `/api/scrape/creators/<platform>`
- Video batch lookups → `/api/videos?ids=...` (new param)
- Platform fallback → `/api/videos?platform=...` (new param)
- Last-scraped per-platform → new `/api/creators/last-scraped?platform=...` endpoint
- Dead `firebase-init.js` import removed

**`modules/content/content.js`**:
- 3D PCA map handles NULL embeddings — niches without embeddings fan out on a fallback circle (radius 1400) instead of collapsing to origin
- Prefers `visual_embedding` over `embedding` for both niches + sub-niches

**`api/scrape_api.py`**:
- `_pg_finalize_job` — new function writing `videos_scraped/accepted/uploaded/failed/log_tail/triggered_by` to `scrape_jobs` at job end (was only writing `status`)
- `error` column → `error_message` in the scrape_jobs UPDATE (matched migration 0003 rename; this was failing silently before)
- `parse_stats` regex now matches actual pipeline output `"Uploaded: N, Skipped/Failed: N"` (was looking for `"Upload complete: ..."` which never matched → `videos_uploaded` always 0)
- `/api/scrape/creators/<platform>` returns `handle` as display name (not `creator_name`) to match creators page convention
- `/api/videos` accepts `?ids=a,b,c` + `?platform=X`
- `/api/creators/last-scraped?platform=X` — new endpoint
- `/api/niches` + `/api/sub-niches` return `visual_embedding` too when `?include_embeddings=1`
- `_refresh_niche_data` (signal-test path) migrated from Firestore to Postgres
- `run_delete_job` rewritten: deletes from Postgres with Firebase Storage blob cleanup (was Firestore-only — bulk deletes from Content page were leaving Postgres rows orphaned)
- `triggered_by` on `_pg_upsert_job` accepts `X-Triggered-By` header, defaults to `'dashboard-api'`

---

## 5. Remaining work (prioritized)

### 🔴 CRITICAL — blocks closing the original ticket

#### 5.1 WebApp feed repoint — THE BIG ONE

Current state of `WebApp/swipeUp_frontend_v1/src/pages/SwipeFeed.jsx` (956 lines):

**Firestore reads on mount:**
- `users/{uid}/Accounts/{id}` → `niche_id`
- `videos where niche_id == X` — entire niche slice, no ranking, no limit, no pagination
- `VideoInteractions` subcollection → seen-filter client-side
- `Creators` subcollection → "fromCreator" badge

**Firestore writes on every swipe (the hot path the ticket targets):**
- `VideoInteractions/{vid}.swipedRight/swipedLeft/lastSeenAt`
- `global_creators/{creator_id}.SwipeRightCount += 1` / `SwipeLeftCount += 1` ← **primary driver for the migration (hot-doc contention at viral traffic)**
- Unknown creator + right swipe → creates new `global_creators/{id}` doc

**Out of scope (stays Firestore):** Track button → Creators subcollection; Right-swipe "Remix" → Remix subcollection.

##### Draft plan from Session 5 (9 decisions, not yet confirmed with user)

| # | Decision | Session 5 Recommendation | Notes |
|---|---|---|---|
| 1 | Where does FastAPI live? | `Backend/api/`, port 8000 | No HTTP layer exists there yet |
| 2 | Ranking fidelity phase 1 | **ANN by niche embedding** (not full retrieve-rerank yet) | Account embeddings don't exist yet; upgrade path is (5) |
| 3 | Seen-filter | Server-side SQL anti-join against `interactions` | PK `(account_id, video_id)` makes it cheap |
| 4 | Pagination | 50 per page, cursor-based | Fetch more when client is ~10 from end of buffer |
| 5 | Account embedding source | **`account_summary_embeddings.summary_siglip`** for accounts that have it; fall back to niche embedding when empty | Session 6 migrated onboarding — newly-onboarded accounts have personalization data ready. Existing pre-migration accounts don't; handle both paths |
| 6 | Auth | Verify Firebase ID token + check account ownership | Requires `firebase-admin` Python SDK. User was unsure if phase 1 or later. |
| 7 | Swipe endpoint timing | Fire-and-forget, 202 response | Matches current UX |
| 8 | "fromCreator" badge | Client enriches `/feed` response with its own Firestore Creators list | Postgres owns the feed, Firestore owns personal lists |
| 9 | Rollout | Full switch, no feature flag | Smaller scope; revert via git if broken |

**The user said "No need to implement rn" at end of Session 5.** Session 6 should start by walking through these 9 decisions and asking the user for final calls, especially on #6 (auth scope) and #2 (ranking fidelity).

##### Planned phases (total ~5-6 hours focused work)

1. **FastAPI scaffold** (~60 min) — `pip install fastapi uvicorn[standard] firebase-admin psycopg_pool`, `Backend/api/main.py` with lifespan, `auth.py`, `db.py`, `/health`. Start: `python -m uvicorn api.main:app --port 8000 --reload`.
2. **`GET /feed?account_id=X&limit=50&cursor=...`** (~90 min) — verify token, check Firestore `users/{uid}/Accounts/{id}` ownership, read niche_id + niche embedding, SQL query with anti-join on interactions, ORDER BY `<=>` cosine distance. Response shape matches what SwipeFeed.jsx already consumes.
3. **`POST /swipe` + `POST /view`** (~30 min) — UPSERT into `interactions(account_id, video_id, swipe, swiped_at, last_seen_at, watch_ms, completion_pct)`. Return 202.
4. **Vite proxy** (~10 min) — add `proxy: { '/feed': 'http://localhost:8000', '/swipe': ..., '/view': ... }` to `WebApp/swipeUp_frontend_v1/vite.config.js`.
5. **`SwipeFeed.jsx` rewrite** (~90 min) — replace Firestore fetchVideos useEffect with `fetch('/feed')`; replace `logVideoInteraction` with `fetch('/swipe')`/`fetch('/view')`; **delete `updateGlobalCreator` entirely** (matview replaces it); keep Track + Remix writes (out of scope).
6. **Matview refresh** (~30 min) — APScheduler inside FastAPI lifespan. `REFRESH MATERIALIZED VIEW CONCURRENTLY creator_swipe_stats` every 5 min + once at startup.
7. **Verification** (~30 min) — swipe 10 videos, check `interactions` + `creator_swipe_stats`, test seen-filter + multi-account dedup.
8. **CONTEXT.md update** (~5 min) — mark complete.

### ✅ 5.2 Cloud Run `onboarding-service` → Postgres (DONE in Session 6)

Lives in-tree at `WebApp/swipeUp_frontend_v1/siglip-server/`. Code migrated this session — see §3a for full details. **Needs a Cloud Run re-deploy** with the new `DATABASE_URL` env var before it's live in prod:

```bash
cd WebApp/swipeUp_frontend_v1/siglip-server/
gcloud run deploy onboarding-service --source . --region us-central1 \
    --set-env-vars "DATABASE_URL=postgresql://...,HF_TOKEN=hf_..."
# (or use the Cloud Run console to add DATABASE_URL alongside existing HF_TOKEN)
```

Local test first:
```bash
cd WebApp/swipeUp_frontend_v1/siglip-server
pip install -r requirements.txt
python -m uvicorn server:app --port 8080
# Check: curl localhost:8080/health → db_configured: true, niches_loaded: 4
```

### 🟡 HIGH — dashboard leftover

#### 5.3 Niche CRUD on Content page still writes Firestore

Creating/deleting a niche in the dashboard Edit modal goes to Firestore `niches` / `sub_niches` collections. Since the pipeline reads from Postgres, **new niches are invisible to scraping until manually synced**. Fix requires:
- `POST /api/niches` — accepts name/description/canonical_keywords, generates embedding via ContentEngine's SigLIP encoder (already loaded in scrape_api.py for signal-test), inserts into Postgres niches + sub_niches
- `DELETE /api/niches/<id>` — cascade delete sub_niches, clear creator.niche_id FKs
- ~80 lines of code, pure additive. Orthogonal to WebApp migration.

### 🟢 MEDIUM / deferred

#### 5.4 DINOv2 integration
Add second encoder (ViT-B/14, 768-dim) alongside SigLIP in `run_pipeline.py`. No schema change. Additive.

#### 5.5 Feed analytics / ranking logs
Future: a `ranking_log` table recording "top-K served, which were swiped" for ML-ops. Not in current ticket.

#### 5.6 Overview + users pages still Firestore
`modules/overview/` + `modules/users/` still read Firestore. **Intentional per the migration plan** — users stay in Firestore — but worth flagging.

### ⚪ Known, deferred, not worth fixing now

- In-memory `jobs` dict in scrape_api.py loses state on Flask restart. `_pg_recover_stuck_jobs` handles `running → failed`; `queued` jobs stay stuck but it's a rare edge case.
- Concurrent-scrape same-creator race on `library_count + 1`. Theoretical — current use is single-scrape-at-a-time.
- pHash dedup is O(N²) within a batch. Performance not correctness.
- TikTok `get_publish_date` can't parse relative timestamps ("1 hour ago") — falls through as None, bypasses date filter.
- YouTube `get_video_page_url` returning None isn't validated separately from download URL.
- Hardcoded proxy credentials in `scrape_all_creators.py`.
- Hardcoded Mac path `/Users/saman/...` in `overlay_intrusion_filter.py:22` — only hit when file is run directly, not via pipeline import.

---

## 6. File-level reference

### Postgres schema

See migration files:
- `Backend/migrations/0001_init.sql` — videos, creators, niches, sub_niches, video_frame_embeddings, interactions, scrape_jobs, filter_log, creator_swipe_stats matview
- `Backend/migrations/0002_accounts.sql` — accounts shadow + renamed user_*_embeddings → account_*_embeddings
- `Backend/migrations/0003_rename_error_column.sql` — scrape_jobs.error → error_message
- `Backend/migrations/0004_niches_visual_embedding.sql` — added visual_embedding to niches + sub_niches

### Dashboard API endpoints (scrape_api.py)

Health: `GET /api/health`

Scrape orchestration:
- `POST /api/scrape/<platform>` — kick off a scrape job (reads `X-Triggered-By` header)
- `GET /api/scrape/status/<job_id>` — poll a job
- `GET /api/scrape/creators/<platform>` — creator dropdown source (platform-filtered, ordered by handle)

Creators:
- `GET /api/creators?platform=X&limit=N`
- `GET /api/creators/last-scraped?platform=X` — max(last_scraped_at)
- `GET /api/creators/<creator_id>`
- `POST /api/creators` — create
- `POST /api/creators/<creator_id>/mark-scraped`
- `POST /api/creators/bulk-update-last-scraped`
- `DELETE /api/creators/<creator_id>`

Videos:
- `GET /api/videos?creator_id=X&niche_id=Y&sub_niche_id=Z&platform=P&ids=a,b,c&limit=N&offset=N&count_only=1&include_embeddings=1`
- `GET /api/videos/count`
- `DELETE /api/videos/<video_id>`
- `POST /api/videos/bulk-delete`
- `POST /api/delete-videos` — scope-based bulk delete (Postgres now, was Firestore)

Taxonomy:
- `GET /api/niches?include_embeddings=1` — returns embedding + visual_embedding
- `GET /api/sub-niches?include_embeddings=1`

Legacy / signal-test:
- `POST /api/populate-niches`, `POST /api/populate-tags` — taxonomy create/update via ContentEngine scripts
- `GET /api/signal/health`, `GET /api/signal/niches`, `POST /api/signal/analyze` — frame classification debug (reads Postgres niches now)

---

## 7. Why decisions were made the way they were

- **Supabase over Neon + Pinecone:** one provider, JOIN across tables and vectors in one SQL query, less ops surface area.
- **Frame-level embeddings (not averaging):** averaging loses which-frame-matches. Max-sim over frame embeddings captures finer similarity. Tradeoff: 8× storage per video.
- **SigLIP + (eventually) DINO:** SigLIP for semantic (what it is), DINO for structural (how it's formatted). Two orthogonal similarity axes.
- **Summary + frame embeddings:** HNSW on 8× vectors is slower. Retrieve-then-rerank is the standard pattern.
- **Materialized view for creator counters:** Firestore hot-doc limit was going to bite on viral videos. Matview removes write contention; slight staleness is fine for creator scoring.
- **Users + Accounts stay Firestore:** Firebase Auth tightly couples to Firestore users collection. Migrating adds rewiring with no value.
- **Session pooler (5432), not Transaction pooler (6543):** long-lived Python server connections need session features (prepared statements, temp tables).
- **`visual_prototype` → `visual_embedding`:** new name is accurate ("embedding" not "prototype"). Rename landed with migration 0004.
- **`RETURNING (xmax = 0) AS inserted`:** clean insert/update discriminator on UPSERT without a second query. Fixes library_count double-count.
- **Short-lived Postgres conns in filter_log:** long filter runs (20+ min) hit Supabase pooler idle timeout. Short-lived conns sidestep it with negligible cost.
- **Firebase Storage stays for mp4 bytes:** Storage is a CDN, not a DB. No value migrating blobs out. Public URLs are stable + already wired into clients.
- **Server-side Postgres writes for onboarding (Session 6):** alternative was client-writes-via-JSON (server returns float arrays, client does N setDocs). Chose server-writes because (a) embeddings never transit the public internet again post-match, (b) atomic transaction on the server — no risk of partial state where summary was written but frames weren't, (c) simpler client.
- **Delete-then-bulk-insert for frame rows:** when re-onboarding, the primary key is `(account_id, frame_idx)`. If the new run has fewer frames than the old one, simple UPSERT would leave orphan frames with higher indices. Delete-first guarantees the row set exactly matches the new scrape.

---

## 8. Quick pickup checklist for Session 7

1. ✅ Confirm launched from `C:/Users/skash/Desktop/Pers/DoomSwipe/` (root)
2. ✅ Read this file end-to-end (§1-7 are history; §5 is active work)
3. ✅ Check whether Cloud Run `onboarding-service` has been re-deployed with the new code. If not, the user needs to do that (see §5.2). Until deploy, onboarding is still on the old server that writes Firestore.
4. ✅ Restart Flask + dashboard Vite if dead
5. ✅ Walk through the 9 WebApp-feed design decisions with the user (§5.1) — **decision #5 is now different: new accounts have `account_summary_embeddings.summary_siglip`, so we can actually do account-based personalization for those, with niche-embedding fallback for pre-migration accounts.**
6. ✅ Once decisions are locked in, follow the 8-phase plan (§5.1) to migrate WebApp feed
7. ✅ After WebApp feed is live, update §2 and §3 of this file to mark item 5.1 complete

That's it. Pipeline works. Dashboard works. Onboarding works (code-wise; needs deploy). The last major piece is SwipeFeed.jsx.

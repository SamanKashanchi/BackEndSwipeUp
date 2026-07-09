"""
SwipeUp onboarding service — SigLIP-based niche matcher + embedding persistence.

Data flow:
  - Reads niches from Postgres (Supabase), preferring `visual_embedding`
    (image-based) over `embedding` (text-based).
  - Scrapes N recent posts from the user's profile via Apify.
  - Extracts 8 frames per post, embeds each with SigLIP vision (per-frame,
    NOT averaged — the frames are the target storage unit).
  - Builds a text embedding from description + keywords (+ captions if any).
  - Scores niches via (mean-pooled) summary against niche.visual_embedding.
  - Writes per-account rows into Postgres:
      accounts                       (shadow row)
      account_summary_embeddings     (one row: summary_siglip + summary_text)
      account_frame_embeddings       (N rows: one per frame, per post)
  - Returns compact metadata to the client (no embedding floats).

Environment:
  DATABASE_URL   — Postgres connection string (Supabase Session pooler)
  HF_TOKEN       — optional Hugging Face token for SigLIP download
"""

import os
import tempfile
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import torch
import httpx
import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pgvector.psycopg import register_vector
from PIL import Image
from pydantic import BaseModel
from transformers import AutoImageProcessor, AutoModel, AutoProcessor, AutoTokenizer


# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

MODEL_NAME = "google/siglip-so400m-patch14-384"
DINO_MODEL_NAME = "facebook/dinov2-base"  # ViT-B/14, 768-dim CLS token output
HF_TOKEN = os.environ.get("HF_TOKEN")

# Apify: Instagram reel scraper. Token MUST come from the APIFY_TOKEN env var
# (set on Cloud Run / in Backend/.env locally) — no hardcoded fallback so the
# secret never lands in git.
APIFY_TOKEN = os.environ.get("APIFY_TOKEN")
APIFY_IG_SCRAPER = "apidojo~instagram-scraper"

# How many recent posts to pull during onboarding. 4 posts × 8 frames = 32 frames per account.
MAX_POSTS_PER_ONBOARDING = 4
FRAMES_PER_VIDEO = 8


def _load_env() -> None:
    """Locally we keep DATABASE_URL in Backend/.env; in Cloud Run it's a real env var.
    This function loads the .env file if DATABASE_URL isn't already set."""
    if os.environ.get("DATABASE_URL"):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    # server.py sits at Backend/siglip-server/server.py, so Backend/.env is the
    # parent folder's .env (this service lives inside the Backend repo now).
    here = Path(__file__).resolve()
    candidates = [
        here.parent / ".env",                             # siglip-server/.env
        here.parents[1] / ".env",                         # Backend/.env
    ]
    for p in candidates:
        if p.exists():
            load_dotenv(p)
            break


_load_env()


# ──────────────────────────────────────────────────────────────────────────────
# Postgres
# ──────────────────────────────────────────────────────────────────────────────

def _get_pg_conn() -> psycopg.Connection:
    """Fresh Postgres connection with pgvector registered. Caller closes."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL not set. For local dev place it in Backend/.env; "
            "for Cloud Run deploy set it as an environment variable."
        )
    conn = psycopg.connect(database_url)
    register_vector(conn)
    return conn


# ──────────────────────────────────────────────────────────────────────────────
# Model load (happens once at process start — heavy)
# ──────────────────────────────────────────────────────────────────────────────

print(f"Loading SigLIP model: {MODEL_NAME} ...")
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, token=HF_TOKEN)
processor = AutoProcessor.from_pretrained(MODEL_NAME, token=HF_TOKEN)
model = AutoModel.from_pretrained(MODEL_NAME, token=HF_TOKEN)
model.eval()

device = "cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
model = model.to(device)
print(f"SigLIP model loaded on device: {device}")

print(f"Loading DINOv2 model: {DINO_MODEL_NAME} ...")
dino_processor = AutoImageProcessor.from_pretrained(DINO_MODEL_NAME)
dino_model = AutoModel.from_pretrained(DINO_MODEL_NAME).to(device).eval()
print(f"DINOv2 model loaded on device: {device}")


# ──────────────────────────────────────────────────────────────────────────────
# Niche embeddings cache (loaded at startup from Postgres)
# ──────────────────────────────────────────────────────────────────────────────

# Each entry: { 'name', 'description', 'visual_embedding': np.ndarray(1152) }
NICHE_EMBEDDINGS: dict[str, dict] = {}


def load_niche_embeddings() -> None:
    """Pull niches from Postgres. Stores BOTH embeddings per niche:

      visual_embedding: avg of exemplar frame embeddings — currently unused at
                        request time (the visual job persists per-account
                        vectors but doesn't score against niches anymore).
                        None when the niche has zero exemplar videos yet.
      text_embedding:   SigLIP text encoding of the niche description+keywords —
                        used for text→text scoring in /match-niche-light.

    Storing both is important: the two embedding kinds live in different cosine
    regimes (text↔text similarity is much higher than text↔image), so the light
    match MUST cosine against text_embedding consistently or niches with NULL
    visual fall back to text and dominate the rankings."""
    global NICHE_EMBEDDINGS
    NICHE_EMBEDDINGS = {}

    conn = _get_pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT niche_id, name, description, visual_embedding, embedding"
                " FROM niches ORDER BY name"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    def _normalize(vec):
        if vec is None:
            return None
        arr = np.asarray(vec, dtype=np.float32)
        n = np.linalg.norm(arr)
        return arr / n if n > 0 else arr

    skipped = []
    for niche_id, name, description, visual_emb, text_emb in rows:
        visual_vec = _normalize(visual_emb)
        text_vec   = _normalize(text_emb)

        if visual_vec is None and text_vec is None:
            skipped.append(niche_id)
            continue

        NICHE_EMBEDDINGS[niche_id] = {
            "name": name,
            "description": description or "",
            # `visual_embedding` retained as the legacy key — heavy-path callers
            # (score_niches) read it; falls back to text when visual is NULL so
            # the blended scoring still has a vector to work with.
            "visual_embedding": visual_vec if visual_vec is not None else text_vec,
            # `text_embedding` is the dedicated text anchor for the light match.
            "text_embedding":   text_vec if text_vec is not None else visual_vec,
        }

    if skipped:
        print(f"Skipped {len(skipped)} niches with no embeddings: {skipped}")
    print(f"Loaded {len(NICHE_EMBEDDINGS)} niches from Postgres: {list(NICHE_EMBEDDINGS.keys())}")


load_niche_embeddings()


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="SwipeUp Onboarding Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten when onboarding goes through a known origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────────────────────────
# Embedding helpers
# ──────────────────────────────────────────────────────────────────────────────

def embed_text(text: str) -> np.ndarray:
    """Embed text with SigLIP's text encoder. Returns a unit-norm 1152-dim vector."""
    inputs = tokenizer(text, return_tensors="pt", padding=True, truncation=True, max_length=64)
    inputs = {k: v.to(device) for k, v in inputs.items()}
    with torch.no_grad():
        text_outputs = model.text_model(**inputs)
    emb = text_outputs.pooler_output[0].cpu().numpy().astype(np.float32)
    norm = np.linalg.norm(emb)
    if norm > 0:
        emb = emb / norm
    return emb


def _l2_normalize_rows(arr: np.ndarray) -> np.ndarray:
    """Row-wise L2 normalize. Zero rows stay zero (no NaN)."""
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


def embed_frames_batch(frames: list[Image.Image]) -> tuple[np.ndarray, np.ndarray]:
    """Run SigLIP and DINOv2 over a batch of PIL frames in one forward pass each.
    Returns (siglip_embs, dino_embs), both unit-normalized.
      siglip_embs: shape (N, 1152)  — SigLIP vision pooler output
      dino_embs:   shape (N, 768)   — DINOv2 CLS token (last_hidden_state[:, 0])
    Each model's processor handles its own resize/normalize from the same source frames.
    """
    if not frames:
        empty_siglip = np.zeros((0, 1152), dtype=np.float32)
        empty_dino = np.zeros((0, 768), dtype=np.float32)
        return empty_siglip, empty_dino

    siglip_inputs = processor(images=frames, return_tensors="pt")
    siglip_inputs = {k: v.to(device) for k, v in siglip_inputs.items()}
    dino_inputs = dino_processor(images=frames, return_tensors="pt")
    dino_inputs = {k: v.to(device) for k, v in dino_inputs.items()}

    with torch.no_grad():
        siglip_out = model.vision_model(**siglip_inputs).pooler_output.cpu().numpy().astype(np.float32)
        dino_out = dino_model(**dino_inputs).last_hidden_state[:, 0].cpu().numpy().astype(np.float32)

    return _l2_normalize_rows(siglip_out), _l2_normalize_rows(dino_out)


def extract_frames(video_path: str, num_frames: int = FRAMES_PER_VIDEO) -> list[Image.Image]:
    """Pull `num_frames` evenly-spaced frames from a video."""
    cap = cv2.VideoCapture(video_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total <= 0:
        cap.release()
        return []
    indices = np.linspace(0, total - 1, num_frames, dtype=int)
    frames: list[Image.Image] = []
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, frame = cap.read()
        if ok:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frames.append(Image.fromarray(rgb))
    cap.release()
    return frames


def embed_video_frames_individual(
    video_path: str, post_idx: int, source_ref: str,
) -> list[dict]:
    """Extract frames, embed all of them in batched SigLIP + DINOv2 forward
    passes, return one record per frame ready for INSERT."""
    frames = extract_frames(video_path, num_frames=FRAMES_PER_VIDEO)
    if not frames:
        return []
    siglip_embs, dino_embs = embed_frames_batch(frames)
    out = []
    for f_in_post in range(len(frames)):
        out.append({
            # frame_idx is globally unique within the account.
            # Pattern: post_idx * FRAMES_PER_VIDEO + f_in_post.
            "frame_idx": post_idx * FRAMES_PER_VIDEO + f_in_post,
            "source_type": "profile_post",
            "source_ref": source_ref,
            "siglip_embedding": siglip_embs[f_in_post],
            "dino_embedding": dino_embs[f_in_post],
        })
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Apify scraping
# ──────────────────────────────────────────────────────────────────────────────

async def scrape_recent_posts(handle: str, platform: str, max_posts: int) -> list[dict]:
    """Return up to `max_posts` recent posts for this creator."""
    clean_handle = handle.lstrip("@")

    if platform != "instagram":
        print(f"Scraping not supported for platform: {platform}")
        return []

    reels_url = f"https://www.instagram.com/{clean_handle}/reels"
    apify_input = {
        "customMapFunction": "(object) => { return {...object} }",
        "maxItems": max_posts,
        "startUrls": [reels_url],
    }

    url = f"https://api.apify.com/v2/acts/{APIFY_IG_SCRAPER}/run-sync-get-dataset-items?token={APIFY_TOKEN}"
    print(f"Scraping {max_posts} recent posts from @{clean_handle} on {platform}...")
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=apify_input)

    if resp.status_code not in (200, 201):
        print(f"Apify error: {resp.status_code}")
        return []

    items = resp.json()
    if not isinstance(items, list):
        items = [items] if items else []
    items = [x for x in items if not x.get("noResults")]
    print(f"Got {len(items)} posts from Apify")
    return items[:max_posts]


async def download_video(video_url: str) -> str | None:
    """Download to a temp file. Returns path or None."""
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(video_url)
        if resp.status_code != 200:
            return None
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp.write(resp.content)
        tmp.close()
        return tmp.name
    except Exception as e:
        print(f"Download failed: {e}")
        return None


def get_video_url(item: dict) -> str | None:
    video = item.get("video") or {}
    return video.get("url") or item.get("videoUrl")


def get_post_ref(item: dict) -> str:
    """Stable reference string for a post — used as source_ref on frame rows."""
    return (
        item.get("shortCode")
        or item.get("id")
        or item.get("url")
        or get_video_url(item)
        or ""
    )


# ──────────────────────────────────────────────────────────────────────────────
# Request / response models
# ──────────────────────────────────────────────────────────────────────────────

# Three-endpoint design (post-2026-05 rework):
#   /match-niche-light  — text-only, sub-second, returns ranked list. Runs at
#     the niche-confirm screen so the user picks from real options.
#   /build-visual-vector — fired at handle-entry step of onboarding. Scrapes
#     posts, embeds frames with SigLIP+DINOv2, persists summary_siglip +
#     summary_dino + per-frame rows. Synchronous (no BackgroundTasks — Cloud
#     Run throttles CPU after the response). Frontend fire-and-forgets; the
#     work runs in parallel with the user filling out the remaining steps.
#   /build-text-vector — fired at niche-confirm submit. Embeds the form text
#     (description + keywords + bio) and persists summary_text. Fast (~100ms).
#     Independent of the visual job — no shared state.


class LightMatchRequest(BaseModel):
    """All fields optional except platform. Light match doesn't write anything
    to the DB — it just scores text against the niches cache and returns."""
    platform:    str           = "instagram"
    handle:      str           = ""
    description: str           = ""
    keywords:    list[str]     = []
    bio:         str           = ""
    creators:    list[str]     = []


class NicheMatch(BaseModel):
    niche_id:   str
    similarity: float


class NicheLite(BaseModel):
    niche_id: str
    name:     str


class LightMatchResponse(BaseModel):
    matches:        list[NicheMatch]
    auto_selected:  list[str]
    all_niches:     list[NicheLite]
    used_fallback:  bool   # true when text inputs were too sparse or top similarity too low


class BuildVisualVectorRequest(BaseModel):
    """Account_id is the only input — handle/platform are pulled from the
    accounts row (created by swipeup-api's POST /account before this fires)."""
    account_id: str


class BuildVisualVectorResponse(BaseModel):
    status:      str    # 'ok' | 'no_posts' | 'scrape_failed'
    account_id:  str
    frame_count: int


class BuildTextVectorRequest(BaseModel):
    account_id:  str
    description: str       = ""
    keywords:    list[str] = []
    bio:         str       = ""


class BuildTextVectorResponse(BaseModel):
    status:     str   # 'ok'
    account_id: str


# ──────────────────────────────────────────────────────────────────────────────
# Scoring
# ──────────────────────────────────────────────────────────────────────────────

# Light match thresholds — tuned to SigLIP-text cosine regime (typical scores
# cluster in 0.5–0.8 for any two related descriptions, so the auto_selected
# gap has to be narrow or every niche gets picked).
LIGHT_MIN_TOKEN_COUNT = 10     # below this, text is too sparse to trust
LIGHT_MIN_TOP_SCORE   = 0.40   # below this, top match is too weak to pre-select anything
LIGHT_AUTO_GAP        = 0.05   # niches within 0.05 of the top score get auto_selected


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Both vectors assumed unit-norm at call time."""
    return float(np.dot(a, b))


def rank_niches_by_text(user_text: np.ndarray) -> list[tuple[str, str, float]]:
    """Return [(niche_id, niche_name, similarity), ...] sorted desc.

    Uses each niche's TEXT embedding (not visual) so cosines are comparable
    across all niches — otherwise niches with NULL visual_embedding fall back
    to text and dominate the rankings because text↔text cosine is much higher
    than text↔image cosine."""
    out: list[tuple[str, str, float]] = []
    for niche_id, niche_data in NICHE_EMBEDDINGS.items():
        sim = cosine_similarity(user_text, niche_data["text_embedding"])
        out.append((niche_id, niche_data["name"], sim))
    out.sort(key=lambda r: r[2], reverse=True)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Postgres persistence
# ──────────────────────────────────────────────────────────────────────────────

def persist_visual_vector(
    *,
    account_id: str,
    user_id: str,
    summary_siglip: np.ndarray | None,
    summary_dino: np.ndarray | None,
    frame_rows: list[dict],
) -> None:
    """Upsert the visual halves of the summary row + replace frame embeddings.
    Leaves summary_text untouched (the text job owns that column).

    Both visual fields can legitimately be NULL — happens when the scrape
    returned zero posts (new account, private account, Apify failure). The
    row still gets created so the text job can UPDATE summary_text into it."""
    conn = _get_pg_conn()
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO account_summary_embeddings"
                    "    (account_id, user_id, summary_siglip, summary_dino, source)"
                    " VALUES (%s, %s, %s, %s, 'onboarding')"
                    " ON CONFLICT (account_id) DO UPDATE SET"
                    "    user_id        = EXCLUDED.user_id,"
                    "    summary_siglip = EXCLUDED.summary_siglip,"
                    "    summary_dino   = EXCLUDED.summary_dino,"
                    "    source         = 'onboarding',"
                    "    updated_at     = NOW()",
                    (account_id, user_id, summary_siglip, summary_dino),
                )

                # Replace frame embeddings. DELETE first so re-runs don't
                # leave stale frames alongside fresh ones with different
                # frame_idx values.
                cur.execute(
                    "DELETE FROM account_frame_embeddings WHERE account_id = %s",
                    (account_id,),
                )
                if frame_rows:
                    cur.executemany(
                        "INSERT INTO account_frame_embeddings"
                        "    (account_id, frame_idx, source_type, source_ref,"
                        "     siglip_embedding, dino_embedding)"
                        " VALUES (%s, %s, %s, %s, %s, %s)",
                        [
                            (
                                account_id,
                                fr["frame_idx"],
                                fr["source_type"],
                                fr["source_ref"],
                                fr["siglip_embedding"],
                                fr["dino_embedding"],
                            )
                            for fr in frame_rows
                        ],
                    )
    finally:
        conn.close()


def persist_text_vector(
    *,
    account_id: str,
    user_id: str,
    summary_text: np.ndarray,
) -> None:
    """Upsert the summary_text column. Works whether the visual job has run
    yet or not — UPDATE path leaves siglip/dino alone; INSERT path leaves
    them NULL for the visual job to fill later."""
    conn = _get_pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO account_summary_embeddings"
                "    (account_id, user_id, summary_text, source)"
                " VALUES (%s, %s, %s, 'onboarding')"
                " ON CONFLICT (account_id) DO UPDATE SET"
                "    user_id      = EXCLUDED.user_id,"
                "    summary_text = EXCLUDED.summary_text,"
                "    updated_at   = NOW()",
                (account_id, user_id, summary_text),
            )
        conn.commit()
    finally:
        conn.close()


def fetch_account_meta(account_id: str) -> tuple[str, str, str] | None:
    """Look up (user_id, platform, handle) for the account. None if missing."""
    conn = _get_pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, platform, handle"
                " FROM accounts WHERE account_id = %s",
                (account_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return (row[0], row[1], row[2] or "")


# ──────────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────────

def _build_light_match_text(req: LightMatchRequest) -> tuple[str, int]:
    """Concatenate text inputs for light match. Returns (text, token_count)."""
    parts: list[str] = []
    if req.description: parts.append(req.description)
    if req.keywords:    parts.append(" ".join(req.keywords))
    if req.bio:         parts.append(req.bio)
    if req.handle:      parts.append(req.handle.lstrip("@"))
    text = " ".join(parts).strip()
    token_count = len(text.split()) if text else 0
    return (text or "general content"), token_count


@app.post("/match-niche-light", response_model=LightMatchResponse)
def match_niche_light(req: LightMatchRequest) -> LightMatchResponse:
    """Fast text-only niche match for the niche-confirm screen.

    No video scraping, no DB writes. Just a cosine of an embedded text
    concatenation against the cached niche vectors. Sub-second.

    used_fallback flips true when:
      - the concatenated text is too sparse (< LIGHT_MIN_TOKEN_COUNT tokens), OR
      - the top similarity is below LIGHT_MIN_TOP_SCORE
    In either case the frontend renders the "you pick" empty state with all
    niches available and nothing pre-checked."""
    if not NICHE_EMBEDDINGS:
        raise HTTPException(status_code=503, detail="No niches loaded — call /reload-niches")

    text, token_count = _build_light_match_text(req)
    user_text = embed_text(text)
    ranked = rank_niches_by_text(user_text)

    top_score = ranked[0][2] if ranked else 0.0
    sparse = token_count < LIGHT_MIN_TOKEN_COUNT
    weak = top_score < LIGHT_MIN_TOP_SCORE
    used_fallback = bool(sparse or weak)

    auto_selected: list[str] = []
    if not used_fallback:
        cutoff = top_score - LIGHT_AUTO_GAP
        auto_selected = [nid for (nid, _name, sim) in ranked if sim >= cutoff]

    return LightMatchResponse(
        matches=[
            NicheMatch(niche_id=nid, similarity=round(float(sim), 4))
            for (nid, _name, sim) in ranked
        ],
        auto_selected=auto_selected,
        all_niches=[
            NicheLite(niche_id=nid, name=data["name"])
            for nid, data in NICHE_EMBEDDINGS.items()
        ],
        used_fallback=used_fallback,
    )


@app.post("/build-visual-vector", response_model=BuildVisualVectorResponse)
async def build_visual_vector(req: BuildVisualVectorRequest) -> BuildVisualVectorResponse:
    """Scrape + frame-embed + persist visual halves of the user vector.

    Synchronous: holds the request open for the duration of the scrape so
    Cloud Run keeps CPU allocated. Frontend fire-and-forgets — the request
    runs for 30s-2min and the client never reads the response. This was a
    BackgroundTasks job until 2026-05; on Cloud Run the task got killed the
    instant the 202 returned (no CPU between requests), so all work happens
    inline now."""
    if not req.account_id:
        raise HTTPException(status_code=400, detail="account_id is required")

    meta = fetch_account_meta(req.account_id)
    if meta is None:
        raise HTTPException(
            status_code=409,
            detail="accounts row does not exist yet — POST /account first",
        )
    user_id, platform, handle = meta
    if not handle:
        raise HTTPException(status_code=400, detail="accounts row has no handle")
    handle = handle.lstrip("@")

    print(f"[build-visual-vector] start account={req.account_id} handle={handle}")

    all_frames: list[dict] = []
    scrape_ok = True
    try:
        posts = await scrape_recent_posts(handle, platform, max_posts=MAX_POSTS_PER_ONBOARDING)
        for post_idx, post in enumerate(posts):
            video_url = get_video_url(post)
            if not video_url:
                continue
            video_path = await download_video(video_url)
            if not video_path:
                continue
            try:
                source_ref = get_post_ref(post)
                rows = embed_video_frames_individual(video_path, post_idx, source_ref)
                all_frames.extend(rows)
                print(f"[build-visual-vector]   post {post_idx+1}: {len(rows)} frame embeddings")
            finally:
                try:
                    os.unlink(video_path)
                except OSError:
                    pass
    except Exception as e:
        scrape_ok = False
        print(f"[build-visual-vector] scrape/embed failed for {req.account_id}: {e}")

    summary_siglip: np.ndarray | None = None
    summary_dino: np.ndarray | None = None
    if all_frames:
        siglip_stack = np.stack([fr["siglip_embedding"] for fr in all_frames])
        summary_siglip = siglip_stack.mean(axis=0).astype(np.float32)
        s_norm = np.linalg.norm(summary_siglip)
        if s_norm > 0:
            summary_siglip = summary_siglip / s_norm

        dino_stack = np.stack([fr["dino_embedding"] for fr in all_frames])
        summary_dino = dino_stack.mean(axis=0).astype(np.float32)
        d_norm = np.linalg.norm(summary_dino)
        if d_norm > 0:
            summary_dino = summary_dino / d_norm

    try:
        persist_visual_vector(
            account_id=req.account_id,
            user_id=user_id,
            summary_siglip=summary_siglip,
            summary_dino=summary_dino,
            frame_rows=all_frames,
        )
    except Exception as e:
        print(f"[build-visual-vector] persist failed for {req.account_id}: {e}")
        raise HTTPException(status_code=500, detail=f"persist failed: {e}")

    if not scrape_ok:
        status_str = "scrape_failed"
    elif not all_frames:
        status_str = "no_posts"
    else:
        status_str = "ok"
    print(f"[build-visual-vector] done account={req.account_id} status={status_str} frames={len(all_frames)}")
    return BuildVisualVectorResponse(
        status=status_str,
        account_id=req.account_id,
        frame_count=len(all_frames),
    )


@app.post("/build-text-vector", response_model=BuildTextVectorResponse)
def build_text_vector(req: BuildTextVectorRequest) -> BuildTextVectorResponse:
    """Embed the form text (description + keywords + bio) and persist as
    summary_text. Fast — no scrape, no GPU pass other than SigLIP text encode.
    Idempotent: re-calls just rewrite summary_text."""
    if not req.account_id:
        raise HTTPException(status_code=400, detail="account_id is required")

    meta = fetch_account_meta(req.account_id)
    if meta is None:
        raise HTTPException(
            status_code=409,
            detail="accounts row does not exist yet — POST /account first",
        )
    user_id, _platform, _handle = meta

    core_text_parts: list[str] = []
    if req.description: core_text_parts.append(req.description)
    if req.keywords:    core_text_parts.append(" ".join(req.keywords))
    if req.bio:         core_text_parts.append(req.bio)
    core_text = " ".join(core_text_parts).strip() or "general content"

    user_text = embed_text(core_text)

    try:
        persist_text_vector(
            account_id=req.account_id,
            user_id=user_id,
            summary_text=user_text,
        )
    except Exception as e:
        print(f"[build-text-vector] persist failed for {req.account_id}: {e}")
        raise HTTPException(status_code=500, detail=f"persist failed: {e}")

    print(f"[build-text-vector] done account={req.account_id}")
    return BuildTextVectorResponse(status="ok", account_id=req.account_id)


@app.post("/embed-text")
async def embed_text_endpoint(body: dict) -> dict:
    """Debug utility — embed arbitrary text."""
    text = body.get("text", "")
    emb = embed_text(text)
    return {"embedding": emb.tolist(), "dim": len(emb)}


@app.post("/reload-niches")
async def reload_niches() -> dict:
    """Pull the niches cache fresh from Postgres. Call after creating/deleting niches."""
    load_niche_embeddings()
    return {"status": "reloaded", "niches": list(NICHE_EMBEDDINGS.keys())}


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "device": device,
        "niches_loaded": len(NICHE_EMBEDDINGS),
        "db_configured": bool(os.environ.get("DATABASE_URL")),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)

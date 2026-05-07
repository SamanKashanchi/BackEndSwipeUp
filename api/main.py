"""FastAPI service for consumer-facing operations against Postgres.

Endpoints:
  GET    /health                — readiness probe (also pokes Postgres)
  DELETE /account/{account_id}  — wipe an account's Postgres footprint
  GET    /feed                  — ranked, personalized feed
  POST   /swipe                 — record a swipe interaction
  POST   /view                  — record a view (no swipe yet)

Ranking pipeline (GET /feed):
  Stage 1 — pgvector ANN by account.summary_siglip (or niche embedding
            fallback) to retrieve top-CANDIDATE_LIMIT candidates from the
            user's niche, excluding seen interactions and the client's
            in-buffer exclude list.
  Stage 2 — Frame-level rerank in NumPy: SigLIP top-K, DINO top-K, text
            max, weighted 0.6/0.2/0.2. Sort, take limit, hydrate with
            creator info.

Background:
  APScheduler refreshes creator-level swipe counters on creator_stats
  every 5 minutes (and once at startup) — the same aggregation that
  used to back the creator_swipe_stats matview, but written as columns
  on creator_stats since 0013_swipe_stats_to_creator_stats.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import numpy as np
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

from auth import assert_account_owner, init_firebase, verify_id_token
from db import close_pool, get_pool, open_pool
from ranking import score_videos

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("api")

CANDIDATE_LIMIT = 200          # stage-1 ANN candidate set size
DEFAULT_FEED_LIMIT = 50        # default top-K returned to client
MAX_FEED_LIMIT = 100
MATVIEW_REFRESH_INTERVAL_MIN = 5

_scheduler: BackgroundScheduler | None = None


def _refresh_creator_swipe_stats() -> None:
    """Recompute creator-level swipe counters and write them onto creator_stats.

    Replaces the old creator_swipe_stats matview (dropped in migration 0013).
    Single UPDATE...FROM (aggregate) so creators that have never been swiped
    keep their default zeros and creators with swipes get the latest counts."""
    try:
        with get_pool().connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE creator_stats cs
                    SET swipe_right_count        = agg.r,
                        swipe_left_count         = agg.l,
                        swipe_up_count           = agg.u,
                        total_swipes             = agg.t,
                        unique_swipers           = agg.us,
                        swipe_stats_refreshed_at = NOW(),
                        updated_at               = NOW()
                    FROM (
                      SELECT v.creator_id,
                        COUNT(*) FILTER (WHERE i.swipe = 'right')::int AS r,
                        COUNT(*) FILTER (WHERE i.swipe = 'left')::int  AS l,
                        COUNT(*) FILTER (WHERE i.swipe = 'up')::int    AS u,
                        COUNT(*) FILTER (WHERE i.swipe IS NOT NULL)::int AS t,
                        COUNT(DISTINCT i.account_id)
                          FILTER (WHERE i.swipe IS NOT NULL)::int AS us
                      FROM interactions i
                      JOIN videos v ON v.video_id = i.video_id
                      GROUP BY v.creator_id
                    ) agg
                    WHERE cs.creator_id = agg.creator_id
                    """
                )
                log.info("creator_stats swipe refresh: %d rows updated", cur.rowcount)
    except Exception as exc:
        log.warning("creator_stats swipe refresh failed: %s: %s", type(exc).__name__, exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    log.info("startup: opening Postgres pool + firebase admin")
    open_pool()
    init_firebase()

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        _refresh_creator_swipe_stats,
        "interval",
        minutes=MATVIEW_REFRESH_INTERVAL_MIN,
        id="creator_swipe_stats_refresh",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    _refresh_creator_swipe_stats()
    log.info("startup: scheduler started, creator_stats swipe counters refreshed once")

    yield

    log.info("shutdown: stopping scheduler + closing pool")
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
    close_pool()


app = FastAPI(title="SwipeUp API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://([a-z0-9-]+\.)?getswipeup\.com|https://.*\.vercel\.app|http://localhost:\d+",
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    db_ok = False
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                db_ok = cur.fetchone()[0] == 1
    except Exception as exc:
        log.warning("health: db check failed: %s", exc)

    return {
        "status": "ok",
        "db_ok": db_ok,
        "project_id": os.getenv("FIREBASE_PROJECT_ID", "reel-swipe-app"),
    }


@app.delete("/account/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(account_id: str, uid: str = Depends(verify_id_token)):
    assert_account_owner(account_id, uid)
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM accounts WHERE account_id = %s", (account_id,))
            deleted = cur.rowcount
        conn.commit()

    log.info("delete_account: account_id=%s uid=%s deleted_rows=%s", account_id, uid, deleted)
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Feed
# ──────────────────────────────────────────────────────────────────────────────


def _load_account_state(account_id: str) -> dict[str, Any]:
    """Pull everything stage-1 + stage-2 need for one account in two queries."""
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a.user_id, a.niche_id, a.platform, a.handle,"
                "       n.visual_embedding, n.embedding,"
                "       s.summary_siglip, s.summary_dino, s.summary_text"
                " FROM accounts a"
                " LEFT JOIN niches n ON n.niche_id = a.niche_id"
                " LEFT JOIN account_summary_embeddings s ON s.account_id = a.account_id"
                " WHERE a.account_id = %s",
                (account_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="account not found")
            (user_id, niche_id, platform, handle,
             niche_visual, niche_text,
             summary_siglip, summary_dino, summary_text) = row

            cur.execute(
                "SELECT siglip_embedding, dino_embedding"
                " FROM account_frame_embeddings"
                " WHERE account_id = %s AND siglip_embedding IS NOT NULL"
                "   AND dino_embedding IS NOT NULL"
                " ORDER BY frame_idx",
                (account_id,),
            )
            frame_rows = cur.fetchall()

    user_siglip_frames = (
        np.stack([r[0] for r in frame_rows]).astype(np.float32) if frame_rows else None
    )
    user_dino_frames = (
        np.stack([r[1] for r in frame_rows]).astype(np.float32) if frame_rows else None
    )

    # Stage-1 anchor: prefer account summary; fall back to niche visual then text.
    if summary_siglip is not None:
        stage1_anchor = np.asarray(summary_siglip, dtype=np.float32)
        anchor_source = "account_summary_siglip"
    elif niche_visual is not None:
        stage1_anchor = np.asarray(niche_visual, dtype=np.float32)
        anchor_source = "niche_visual_embedding"
    elif niche_text is not None:
        stage1_anchor = np.asarray(niche_text, dtype=np.float32)
        anchor_source = "niche_embedding"
    else:
        raise HTTPException(
            status_code=409,
            detail="account has no siglip summary and niche has no embedding — cannot rank",
        )

    return {
        "user_id": user_id,
        "niche_id": niche_id,
        "platform": platform,
        "handle": handle,
        "stage1_anchor": stage1_anchor,
        "stage1_anchor_source": anchor_source,
        "summary_siglip": np.asarray(summary_siglip, dtype=np.float32) if summary_siglip is not None else None,
        "summary_dino":   np.asarray(summary_dino,   dtype=np.float32) if summary_dino   is not None else None,
        "summary_text":   np.asarray(summary_text,   dtype=np.float32) if summary_text   is not None else None,
        "user_siglip_frames": user_siglip_frames,
        "user_dino_frames":   user_dino_frames,
        "has_full_embeddings": (
            user_siglip_frames is not None
            and user_dino_frames is not None
            and summary_text is not None
        ),
    }


def _retrieve_candidates(
    *, account_id: str, niche_id: str, anchor: np.ndarray, exclude_ids: list[str], limit: int
) -> list[dict]:
    """Stage 1: pgvector ANN by anchor against videos.summary_embedding_siglip,
    excluding seen interactions and the client's in-buffer ids. Returns the
    metadata + creator info needed for the response, plus the video_id list
    for the frame fetch."""
    sql = (
        "SELECT v.video_id, v.platform, v.public_url, v.display_url, v.caption,"
        "       v.hashtags, v.video_duration, v.views, v.likes, v.comments, v.shares,"
        "       v.time_posted, v.scraped_at,"
        "       c.handle, c.creator_name, c.profile_picture_url"
        " FROM videos v"
        " JOIN creators c ON c.creator_id = v.creator_id"
        " WHERE v.niche_id = %s"
        "   AND v.summary_embedding_siglip IS NOT NULL"
        "   AND NOT EXISTS ("
        "       SELECT 1 FROM interactions i"
        "       WHERE i.account_id = %s AND i.video_id = v.video_id"
        "   )"
        "   AND NOT (v.video_id = ANY(%s::text[]))"
        " ORDER BY v.summary_embedding_siglip <=> %s"
        " LIMIT %s"
    )
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (niche_id, account_id, exclude_ids, anchor, limit))
            rows = cur.fetchall()

    out = []
    for row in rows:
        (video_id, platform, public_url, display_url, caption,
         hashtags, video_duration, views, likes, comments, shares,
         time_posted, scraped_at,
         handle, creator_name, profile_pic) = row
        out.append({
            "video_id": video_id,
            "platform": platform or "unknown",
            "public_url": public_url,
            "display_url": display_url,
            "caption": caption or "",
            "hashtags": list(hashtags or []),
            "video_duration": float(video_duration) if video_duration is not None else None,
            "views": int(views or 0),
            "likes": int(likes or 0),
            "comments": int(comments or 0),
            "shares": int(shares or 0),
            "time_posted": time_posted.isoformat() if time_posted else None,
            "scraped_at": scraped_at.isoformat() if scraped_at else None,
            "creator_handle": handle,
            "creator_name": creator_name,
            "creator_profile_pic": profile_pic,
        })
    return out


def _fetch_video_frames(video_ids: list[str]) -> dict[str, dict[str, np.ndarray]]:
    """Returns video_id -> {siglip: (N,1152), dino: (N,768)}. Videos with any
    null frame embeddings are dropped — we only score against complete data."""
    if not video_ids:
        return {}
    sql = (
        "SELECT video_id, frame_idx, siglip_embedding, dino_embedding"
        " FROM video_frame_embeddings"
        " WHERE video_id = ANY(%s::text[])"
        "   AND siglip_embedding IS NOT NULL"
        "   AND dino_embedding IS NOT NULL"
        " ORDER BY video_id, frame_idx"
    )
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (video_ids,))
            rows = cur.fetchall()

    by_id: dict[str, list[tuple[np.ndarray, np.ndarray]]] = {}
    for video_id, _frame_idx, siglip_emb, dino_emb in rows:
        by_id.setdefault(video_id, []).append((siglip_emb, dino_emb))

    out: dict[str, dict[str, np.ndarray]] = {}
    for video_id, frames in by_id.items():
        siglip_stack = np.stack([f[0] for f in frames]).astype(np.float32)
        dino_stack = np.stack([f[1] for f in frames]).astype(np.float32)
        out[video_id] = {"siglip": siglip_stack, "dino": dino_stack}
    return out


def _rerank(
    candidates: list[dict],
    video_frames: dict[str, dict[str, np.ndarray]],
    user_siglip: np.ndarray,
    user_dino: np.ndarray,
    user_text: np.ndarray,
) -> list[dict]:
    """Run frame-level scoring across all candidates that have full frame data.
    Candidates missing frame embeddings are dropped from the result (rare —
    every video scraped post-DINO-deploy has them)."""
    scorable = [c for c in candidates if c["video_id"] in video_frames]
    if not scorable:
        return []

    # All candidates must have the same N for the einsum. New scrapes give
    # FRAMES_PER_VIDEO=8 uniformly, but right-pad defensively.
    max_n = max(video_frames[c["video_id"]]["siglip"].shape[0] for c in scorable)
    V = len(scorable)
    sig_dim = user_siglip.shape[1]
    dino_dim = user_dino.shape[1]
    video_siglip_3d = np.zeros((V, max_n, sig_dim), dtype=np.float32)
    video_dino_3d = np.zeros((V, max_n, dino_dim), dtype=np.float32)
    for i, c in enumerate(scorable):
        s = video_frames[c["video_id"]]["siglip"]
        d = video_frames[c["video_id"]]["dino"]
        video_siglip_3d[i, : s.shape[0]] = s
        video_dino_3d[i, : d.shape[0]] = d

    scores = score_videos(
        user_siglip=user_siglip,
        user_dino=user_dino,
        user_text=user_text,
        video_siglip=video_siglip_3d,
        video_dino=video_dino_3d,
    )

    for i, c in enumerate(scorable):
        c["score"] = float(scores["final"][i])
        c["score_semantic"] = float(scores["semantic"][i])
        c["score_structure"] = float(scores["structure"][i])
        c["score_text"] = float(scores["text"][i])

    scorable.sort(key=lambda c: c["score"], reverse=True)
    return scorable


def _candidates_summary_only(
    candidates: list[dict], anchor_source: str
) -> list[dict]:
    """Fallback path for legacy accounts without frame embeddings or summary
    text. Already in stage-1 ANN order; just stamp source metadata."""
    for c in candidates:
        c["score"] = None
        c["score_semantic"] = None
        c["score_structure"] = None
        c["score_text"] = None
    return candidates


@app.get("/feed")
def get_feed(
    account_id: str,
    limit: int = DEFAULT_FEED_LIMIT,
    exclude: str = "",
    uid: str = Depends(verify_id_token),
) -> dict:
    """Returns up to `limit` ranked videos for this account.

    Query params:
      account_id  — Firestore Accounts/{id}
      limit       — top-N to return (default 50, max 100)
      exclude     — comma-separated video_ids the client already has buffered
                    and wants the server to skip (in addition to seen-via-
                    interactions filtering)
    """
    assert_account_owner(account_id, uid)
    if limit <= 0 or limit > MAX_FEED_LIMIT:
        raise HTTPException(status_code=400, detail=f"limit must be in 1..{MAX_FEED_LIMIT}")
    exclude_ids = [s for s in exclude.split(",") if s]

    state = _load_account_state(account_id)
    if state["niche_id"] is None:
        raise HTTPException(status_code=409, detail="account has no niche_id")

    candidates = _retrieve_candidates(
        account_id=account_id,
        niche_id=state["niche_id"],
        anchor=state["stage1_anchor"],
        exclude_ids=exclude_ids,
        limit=CANDIDATE_LIMIT,
    )

    used_rerank = False
    if state["has_full_embeddings"] and candidates:
        video_ids = [c["video_id"] for c in candidates]
        video_frames = _fetch_video_frames(video_ids)
        ranked = _rerank(
            candidates,
            video_frames,
            user_siglip=state["user_siglip_frames"],
            user_dino=state["user_dino_frames"],
            user_text=state["summary_text"],
        )
        used_rerank = True
    else:
        ranked = _candidates_summary_only(candidates, state["stage1_anchor_source"])

    top = ranked[:limit]

    log.info(
        "feed: account=%s niche=%s anchor=%s candidates=%d returned=%d rerank=%s",
        account_id, state["niche_id"], state["stage1_anchor_source"],
        len(candidates), len(top), used_rerank,
    )

    return {
        "videos": top,
        "candidate_count": len(candidates),
        "ranked_count": len(top),
        "used_rerank": used_rerank,
        "stage1_anchor": state["stage1_anchor_source"],
    }


# ──────────────────────────────────────────────────────────────────────────────
# Swipe / view
# ──────────────────────────────────────────────────────────────────────────────


class SwipeBody(BaseModel):
    account_id: str
    video_id: str
    swipe: str = Field(..., pattern=r"^(right|left|up)$")
    watch_ms: int | None = None
    completion_pct: float | None = None


class ViewBody(BaseModel):
    account_id: str
    video_id: str


@app.post("/swipe", status_code=status.HTTP_202_ACCEPTED)
def post_swipe(body: SwipeBody, uid: str = Depends(verify_id_token)) -> dict:
    assert_account_owner(body.account_id, uid)
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO interactions"
                "    (account_id, user_id, video_id, first_seen_at, last_seen_at,"
                "     swipe, swiped_at, watch_ms, completion_pct)"
                " VALUES (%s, %s, %s, NOW(), NOW(), %s, NOW(), %s, %s)"
                " ON CONFLICT (account_id, video_id) DO UPDATE SET"
                "    last_seen_at   = EXCLUDED.last_seen_at,"
                "    swipe          = EXCLUDED.swipe,"
                "    swiped_at      = EXCLUDED.swiped_at,"
                "    watch_ms       = COALESCE(EXCLUDED.watch_ms,       interactions.watch_ms),"
                "    completion_pct = COALESCE(EXCLUDED.completion_pct, interactions.completion_pct)",
                (
                    body.account_id, uid, body.video_id,
                    body.swipe, body.watch_ms, body.completion_pct,
                ),
            )
        conn.commit()
    return {"accepted": True}


@app.post("/view", status_code=status.HTTP_202_ACCEPTED)
def post_view(body: ViewBody, uid: str = Depends(verify_id_token)) -> dict:
    """Mark a video as seen without a swipe yet. Idempotent on re-view —
    bumps last_seen_at, keeps any existing swipe verdict intact."""
    assert_account_owner(body.account_id, uid)
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO interactions"
                "    (account_id, user_id, video_id, first_seen_at, last_seen_at)"
                " VALUES (%s, %s, %s, NOW(), NOW())"
                " ON CONFLICT (account_id, video_id) DO UPDATE SET"
                "    last_seen_at = EXCLUDED.last_seen_at",
                (body.account_id, uid, body.video_id),
            )
        conn.commit()
    return {"accepted": True}


# ──────────────────────────────────────────────────────────────────────────────
# Personal creator lists (account_creators)
#
# Replaces the per-user Firestore Creators subcollection. Adding a creator
# upserts into the global `creators` pool AND inserts an account_creators
# row in one transaction, so a personal "Track" automatically seeds the
# global pool. `origin` records where the link came from
# (onboarding_self, onboarding_inspiration, user_track, keyword_search, ...).
# ──────────────────────────────────────────────────────────────────────────────

from typing import Literal
from urllib.parse import urlparse

Platform = Literal["tiktok", "youtube", "instagram", "x"]


def _normalize_handle(raw: str) -> str:
    """Strip @, parse URL down to first path component, lowercase. Returns ''
    if nothing usable remains. Mirrors the JS normalizeHandle in SwipeFeed."""
    if not raw:
        return ""
    s = raw.strip()
    if s.startswith("@"):
        s = s[1:]
    if "://" in s:
        try:
            parsed = urlparse(s)
            parts = [p for p in parsed.path.split("/") if p]
            if parts:
                s = parts[0].lstrip("@").split("?")[0]
        except Exception:
            pass
    return s.lower()


def _make_creator_id(platform: str, handle: str) -> str:
    return f"{platform.lower()}_{handle}"


class AddCreatorRequest(BaseModel):
    platform: Platform
    handle: str = Field(..., min_length=1)
    origin: str = Field(..., min_length=1, max_length=64)


@app.post("/creators", status_code=status.HTTP_201_CREATED)
def seed_creator(
    body: AddCreatorRequest,
    uid: str = Depends(verify_id_token),
):
    """Seed the global creator pool only — does NOT link to any account.
    Used when an action surfaces a candidate creator without expressing
    a tracking relationship (e.g. swipe-right adds the video's creator
    to the pool, but the user isn't 'tracking' them by saving the clip).
    Idempotent: re-seeding an existing creator_id is a no-op."""
    handle = _normalize_handle(body.handle)
    if not handle:
        raise HTTPException(status_code=400, detail="invalid handle")
    creator_id = _make_creator_id(body.platform, handle)

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO creators (creator_id, platform, handle, origin)"
                " VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (creator_id) DO NOTHING",
                (creator_id, body.platform, handle, body.origin),
            )
        conn.commit()

    return {
        "creator_id": creator_id,
        "platform": body.platform,
        "handle": handle,
        "origin": body.origin,
    }


@app.post("/account/{account_id}/creators", status_code=status.HTTP_201_CREATED)
def add_account_creator(
    account_id: str,
    body: AddCreatorRequest,
    uid: str = Depends(verify_id_token),
):
    """Upsert a creator into the global pool and link it to this account."""
    assert_account_owner(account_id, uid)

    handle = _normalize_handle(body.handle)
    if not handle:
        raise HTTPException(status_code=400, detail="invalid handle")
    creator_id = _make_creator_id(body.platform, handle)

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO creators (creator_id, platform, handle, origin)"
                " VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (creator_id) DO NOTHING",
                (creator_id, body.platform, handle, body.origin),
            )
            cur.execute(
                "INSERT INTO account_creators (account_id, creator_id, origin)"
                " VALUES (%s, %s, %s)"
                " ON CONFLICT (account_id, creator_id) DO NOTHING",
                (account_id, creator_id, body.origin),
            )
        conn.commit()

    return {
        "creator_id": creator_id,
        "platform": body.platform,
        "handle": handle,
        "origin": body.origin,
    }


@app.get("/account/{account_id}/creators")
def list_account_creators(
    account_id: str,
    uid: str = Depends(verify_id_token),
):
    """Return the personal creator list for this account, newest first."""
    assert_account_owner(account_id, uid)

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.creator_id, c.platform, c.handle, c.creator_name,"
                "       c.profile_picture_url, ac.origin, ac.added_at"
                " FROM account_creators ac"
                " JOIN creators c ON c.creator_id = ac.creator_id"
                " WHERE ac.account_id = %s"
                " ORDER BY ac.added_at DESC",
                (account_id,),
            )
            rows = cur.fetchall()

    return {
        "creators": [
            {
                "creator_id": r[0],
                "platform": r[1],
                "handle": r[2],
                "creator_name": r[3],
                "profile_picture_url": r[4],
                "origin": r[5],
                "added_at": r[6].isoformat() if r[6] else None,
            }
            for r in rows
        ]
    }


@app.delete(
    "/account/{account_id}/creators/{creator_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_account_creator(
    account_id: str,
    creator_id: str,
    uid: str = Depends(verify_id_token),
):
    """Remove a creator from this account's personal list. Leaves the
    global creators row in place (it may still be tracked by other accounts
    or referenced by videos)."""
    assert_account_owner(account_id, uid)

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM account_creators"
                " WHERE account_id = %s AND creator_id = %s",
                (account_id, creator_id),
            )
        conn.commit()
    return None

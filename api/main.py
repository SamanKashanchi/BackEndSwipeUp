"""FastAPI service for consumer-facing operations against Postgres.

Endpoints:
  GET    /health                — readiness probe (also pokes Postgres)
  DELETE /account/{account_id}  — wipe an account's Postgres footprint
  GET    /feed                  — slot-composed feed batch (see feed.py)
  POST   /swipe                 — record a swipe interaction
  POST   /view                  — record a view (no swipe yet)

The feed engine (GET /feed) lives in feed.py — slot mix is computed from
unseen Tracked-creator supply + selected niches; each pool retrieves via
pgvector ANN and reranks with frame-level scoring (when the user vector
has built). See feed.py / feed_pools.py / feed_slots.py for details.

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

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

from auth import assert_account_owner, init_firebase, verify_id_token
from db import close_pool, get_pool, open_pool
from feed import router as feed_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("api")

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

# /feed endpoint lives in feed.py (slot engine + creator cooldown + dedup).
app.include_router(feed_router)


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


# ──────────────────────────────────────────────────────────────────────────────
# Accounts shadow-row create — POST /account
#
# Fired at the handle-entry step of onboarding, BEFORE niche selection. The
# row needs to exist this early so the onboarding service can start building
# the visual user-vector in parallel with the rest of the form (frame
# embeddings FK to accounts). niche_id stays NULL until /account/{id}/niches
# runs at the niche-confirm screen.
# ──────────────────────────────────────────────────────────────────────────────


class CreateAccountBody(BaseModel):
    account_id: str = Field(..., min_length=1, max_length=128)
    handle:     str = Field(..., min_length=1, max_length=128)
    platform:   Platform


@app.post("/account", status_code=status.HTTP_201_CREATED)
def create_account(
    body: CreateAccountBody,
    uid: str = Depends(verify_id_token),
):
    """Create the Postgres accounts shadow row for a new sign-up. Idempotent
    on (account_id, uid) — re-calls during step navigation are no-ops; calls
    for the same account_id from a different uid are rejected."""
    handle = _normalize_handle(body.handle)
    if not handle:
        raise HTTPException(status_code=400, detail="invalid handle")

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            # Ownership check: if the row exists under a different uid the
            # caller is impersonating someone else's account_id, reject hard.
            cur.execute(
                "SELECT user_id FROM accounts WHERE account_id = %s",
                (body.account_id,),
            )
            existing = cur.fetchone()
            if existing is not None and existing[0] != uid:
                raise HTTPException(status_code=403, detail="account_id owned by another user")

            cur.execute(
                "INSERT INTO accounts (account_id, user_id, platform, handle)"
                " VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (account_id) DO UPDATE SET"
                "    platform   = EXCLUDED.platform,"
                "    handle     = EXCLUDED.handle,"
                "    updated_at = NOW()",
                (body.account_id, uid, body.platform, handle),
            )
        conn.commit()

    log.info("create_account: account_id=%s uid=%s handle=%s", body.account_id, uid, handle)
    return {
        "account_id": body.account_id,
        "user_id": uid,
        "platform": body.platform,
        "handle": handle,
    }


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
                "INSERT INTO creators (creator_id, platform, handle, origin, status)"
                " VALUES (%s, %s, %s, %s, 'pending')"
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
                "INSERT INTO creators (creator_id, platform, handle, origin, status)"
                " VALUES (%s, %s, %s, %s, 'pending')"
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


# ──────────────────────────────────────────────────────────────────────────────
# Multi-niche selections — POST /account/{id}/niches
#
# Lands at the niche-confirm screen during onboarding. The accounts shadow
# row already exists at this point (created by POST /account at handle entry),
# so this endpoint only replaces the account_niches set.
#
# `source` is per-selection so the frontend can attribute each row as either
# auto-matched (kept from the /match-niche-light auto_selected list) or
# user_confirmed (the user explicitly checked or added it). Weights come
# from /match-niche-light's response and are cached client-side.
# ──────────────────────────────────────────────────────────────────────────────


class NicheSelection(BaseModel):
    niche_id: str = Field(..., min_length=1, max_length=128)
    weight:   float = Field(..., ge=0.0, le=1.0)
    source:   Literal["auto", "user_confirmed"]


class AccountNichesBody(BaseModel):
    selections: list[NicheSelection] = Field(..., min_length=1, max_length=8)


@app.post("/account/{account_id}/niches", status_code=status.HTTP_201_CREATED)
def set_account_niches(
    account_id: str,
    body: AccountNichesBody,
    uid: str = Depends(verify_id_token),
):
    """Replace this account's niche selections. The accounts row must
    already exist (POST /account creates it at handle entry)."""
    assert_account_owner(account_id, uid)

    seen: set[str] = set()
    for s in body.selections:
        if s.niche_id in seen:
            raise HTTPException(status_code=400, detail=f"duplicate niche_id: {s.niche_id}")
        seen.add(s.niche_id)

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM accounts WHERE account_id = %s",
                (account_id,),
            )
            if cur.fetchone() is None:
                raise HTTPException(
                    status_code=409,
                    detail="accounts row does not exist — POST /account first",
                )

            # Validate every niche_id exists. Bad ids surface as a 400 instead
            # of a less-actionable FK violation.
            cur.execute(
                "SELECT niche_id FROM niches WHERE niche_id = ANY(%s)",
                ([s.niche_id for s in body.selections],),
            )
            known = {r[0] for r in cur.fetchall()}
            unknown = [s.niche_id for s in body.selections if s.niche_id not in known]
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown niche_id(s): {unknown}",
                )

            with conn.transaction():
                cur.execute(
                    "DELETE FROM account_niches WHERE account_id = %s",
                    (account_id,),
                )
                cur.executemany(
                    "INSERT INTO account_niches"
                    "    (account_id, niche_id, weight, source)"
                    " VALUES (%s, %s, %s, %s)",
                    [
                        (account_id, s.niche_id, s.weight, s.source)
                        for s in body.selections
                    ],
                )

    log.info(
        "set_account_niches: account=%s niches=%s",
        account_id, [s.niche_id for s in body.selections],
    )
    return {
        "account_id": account_id,
        "niches": [
            {"niche_id": s.niche_id, "weight": s.weight, "source": s.source}
            for s in body.selections
        ],
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

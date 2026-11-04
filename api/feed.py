"""SwipeUp Feed Engine — MVP creator feed.

GET /feed
  Returns up to FEED_BATCH_SIZE videos for an account: the newest videos from
  the creators the account tracks (account_creators) that it hasn't seen yet.
  A per-creator cooldown keeps one creator from filling a whole batch.

  No niches and no embeddings. The previous slot-composed engine
  (feed_pools.py / feed_slots.py / ranking.py) is left in the repo but is no
  longer called from here.

Wired into main.py via `app.include_router(feed.router)`.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import unquote_plus

from fastapi import APIRouter, Depends, HTTPException, Request

from auth import assert_account_owner, verify_id_token
from db import get_pool
from feed_slots import creator_penalty

log = logging.getLogger("feed")

# ── Tunable knobs ─────────────────────────────────────────────────────────────

FEED_BATCH_SIZE = 15
MAX_FEED_BATCH_SIZE = 50

# How many of the newest unseen videos to pull before applying the creator
# cooldown. Large enough that a batch can skip past one prolific creator.
CANDIDATE_LIMIT = 200

SLOT_LABEL = "creator"

router = APIRouter()


# ── Candidate retrieval ───────────────────────────────────────────────────────

_CANDIDATES_SQL = (
    "SELECT v.video_id, v.niche_id, v.creator_id, v.platform, v.public_url,"
    "       v.display_url, v.caption, v.hashtags, v.video_duration,"
    "       v.views, v.likes, v.comments, v.shares, v.time_posted, v.scraped_at,"
    "       c.handle, c.creator_name, c.profile_picture_url"
    "  FROM videos v"
    "  JOIN creators c ON c.creator_id = v.creator_id"
    " WHERE v.creator_id IN ("
    "       SELECT ac.creator_id FROM account_creators ac WHERE ac.account_id = %s"
    "   )"
    "   AND NOT EXISTS ("
    "       SELECT 1 FROM interactions i"
    "        WHERE i.account_id = %s AND i.video_id = v.video_id"
    "   )"
    "   AND (v.content_group_id IS NULL OR v.content_group_id NOT IN ("
    "       SELECT DISTINCT vv.content_group_id FROM videos vv"
    "         JOIN interactions i ON i.video_id = vv.video_id"
    "        WHERE i.account_id = %s AND vv.content_group_id IS NOT NULL"
    "   ))"
    "   AND NOT (v.video_id = ANY(%s::text[]))"
    " ORDER BY COALESCE(v.time_posted, v.scraped_at) DESC, v.video_id"
    " LIMIT %s"
)


def _account_exists(account_id: str) -> bool:
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM accounts WHERE account_id = %s", (account_id,))
            return cur.fetchone() is not None


# Tracked creators whose first pull hasn't landed yet: never scraped, no
# failure recorded (private/broken accounts fail and drop out), added recently
# (so one the scraper never got to doesn't show "gathering" forever).
GATHERING_WINDOW_MINUTES = 60
_GATHERING_SQL = (
    "SELECT c.creator_id, c.platform, c.handle"
    "  FROM account_creators ac"
    "  JOIN creators c ON c.creator_id = ac.creator_id"
    " WHERE ac.account_id = %s"
    "   AND c.last_scraped_at IS NULL"
    "   AND c.last_failure_at IS NULL"
    "   AND ac.added_at > NOW() - %s * INTERVAL '1 minute'"
    " ORDER BY ac.added_at, c.creator_id"
)


def _fetch_gathering(account_id: str) -> list[dict]:
    """Creators we're still pulling first videos for. Lets the swipe page show
    a "grabbing videos" screen instead of "you're all caught up"."""
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_GATHERING_SQL, (account_id, GATHERING_WINDOW_MINUTES))
            rows = cur.fetchall()
    return [{"creator_id": r[0], "platform": r[1], "handle": r[2]} for r in rows]


def _fetch_candidates(account_id: str, exclude_ids: list[str], limit: int) -> list[dict]:
    """Newest-first unseen videos from the account's tracked creators. Each
    candidate gets a recency score in (0, 1], newest == 1.0, so the creator
    cooldown in _pick_best can trade recency against variety."""
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                _CANDIDATES_SQL,
                (account_id, account_id, account_id, exclude_ids, limit),
            )
            rows = cur.fetchall()

    n = len(rows)
    candidates: list[dict] = []
    for i, row in enumerate(rows):
        (video_id, niche_id, creator_id, platform, public_url, display_url, caption,
         hashtags, video_duration, views, likes, comments, shares,
         time_posted, scraped_at, handle, creator_name, profile_pic) = row
        candidates.append({
            "video_id":            video_id,
            "niche_id":            niche_id,
            "creator_id":          creator_id,
            "platform":            platform or "unknown",
            "public_url":          public_url,
            "display_url":         display_url,
            "caption":             caption or "",
            "hashtags":            list(hashtags or []),
            "video_duration":      float(video_duration) if video_duration is not None else None,
            "views":               int(views or 0),
            "likes":               int(likes or 0),
            "comments":            int(comments or 0),
            "shares":              int(shares or 0),
            "time_posted":         time_posted.isoformat() if time_posted else None,
            "scraped_at":          scraped_at.isoformat() if scraped_at else None,
            "creator_handle":      handle,
            "creator_name":        creator_name,
            "creator_profile_pic": profile_pic,
            "score":               (n - i) / n,
            "score_semantic":      None,
            "score_structure":     None,
            "score_text":          None,
        })
    return candidates


# ── build_batch ───────────────────────────────────────────────────────────────

def _parse_session_creator_counts(raw: str | None) -> dict[str, int]:
    """Front-end sends JSON-encoded {creator_id: count} as a query param. Parse
    defensively — bad input becomes an empty session rather than a 400."""
    if not raw:
        return {}
    try:
        decoded = unquote_plus(raw)
        parsed  = json.loads(decoded)
        if not isinstance(parsed, dict):
            return {}
        return {str(k): int(v) for k, v in parsed.items() if isinstance(v, (int, float))}
    except (ValueError, TypeError):
        log.warning("feed: bad session_creator_counts payload, ignoring")
        return {}


def build_batch(
    account_id: str,
    session_creator_counts: dict[str, int],
    exclude_ids: list[str],
    limit: int,
) -> dict[str, Any]:
    """Assemble one /feed batch. Returns the response payload dict."""
    if not _account_exists(account_id):
        raise HTTPException(status_code=404, detail="account not found")

    pool = _fetch_candidates(account_id, exclude_ids, CANDIDATE_LIMIT)
    unseen_count = len(pool)

    batch: list[dict] = []
    batch_creators: list[tuple[str, int]] = []
    # Local mutable copy so we don't poison the caller's dict.
    session_creator_counts = dict(session_creator_counts)

    for slot_pos in range(limit):
        chosen, _ = _pick_best(pool, batch_creators, session_creator_counts, slot_pos)
        if chosen is None:
            break  # Out of unseen videos — frontend shows "You're all caught up".
        batch.append({
            "slot":     SLOT_LABEL,
            "position": slot_pos,
            "video":    _shape_response_video(chosen),
        })
        batch_creators.append((chosen["creator_id"], slot_pos))
        session_creator_counts[chosen["creator_id"]] = (
            session_creator_counts.get(chosen["creator_id"], 0) + 1
        )
        pool = [v for v in pool if v["video_id"] != chosen["video_id"]]

    # Response keeps the keys the slot engine returned so existing clients
    # (SwipeFeed logs mix_used / stage1_anchor / rerank_applied) don't break.
    return {
        "batch":      batch,
        "batch_size": len(batch),
        "mix_used": {
            "inspiration_slots": len(batch),
            "niche_slots":       0,
            "niche_allocation":  {},
        },
        "unseen_inspiration_count": unseen_count,
        "stage1_anchor":            "none",
        "rerank_applied":           False,
        "fallback_slots":           0,
        "session_creator_counts":   session_creator_counts,
        # Non-empty while new creators' first videos are still being scraped.
        "gathering":                _fetch_gathering(account_id),
    }


def _pick_best(
    pool: list[dict],
    batch_creators: list[tuple[str, int]],
    session_creator_counts: dict[str, int],
    slot_pos: int,
) -> tuple[dict | None, bool]:
    """Apply creator_penalty multiplicatively and pick the top scorer.
    Returns (chosen_dict_or_None, used_fallback)."""
    if not pool:
        return None, False
    best_score = float("-inf")
    best_v: dict | None = None
    for v in pool:
        base_score = v.get("score")
        if base_score is None:
            continue
        penalty = creator_penalty(
            v["creator_id"], batch_creators, session_creator_counts, slot_pos
        )
        adj = base_score * penalty
        if adj > best_score:
            best_score = adj
            best_v = v
    return best_v, False


def _shape_response_video(c: dict) -> dict:
    """Project the internal candidate dict down to the spec's response shape."""
    return {
        "video_id":       c["video_id"],
        "platform":       c.get("platform"),
        "creator": {
            "creator_id":           c["creator_id"],
            "handle":               c.get("creator_handle"),
            "creator_name":         c.get("creator_name"),
            "profile_picture_url":  c.get("creator_profile_pic"),
        },
        "url":            c.get("public_url"),
        "display_url":    c.get("display_url"),
        "caption":        c.get("caption", ""),
        "hashtags":       c.get("hashtags", []),
        "video_duration": c.get("video_duration"),
        "metrics": {
            "views":    c.get("views", 0),
            "likes":    c.get("likes", 0),
            "comments": c.get("comments", 0),
            "shares":   c.get("shares", 0),
        },
        "time_posted":     c.get("time_posted"),
        "scraped_at":      c.get("scraped_at"),
        "score":           c.get("score"),
        "score_semantic":  c.get("score_semantic"),
        "score_structure": c.get("score_structure"),
        "score_text":      c.get("score_text"),
        "niche_id":        c.get("niche_id"),
    }


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.get("/feed")
def get_feed(
    request: Request,
    account_id: str,
    limit: int = FEED_BATCH_SIZE,
    exclude: str = "",
    session_creator_counts: str = "",
    uid: str = Depends(verify_id_token),
) -> dict:
    """Newest unseen videos from the account's tracked creators.

    Query params:
      account_id              — Firestore Accounts/{id}
      limit                   — batch size (default 15, max 50)
      exclude                 — comma-separated video_ids already buffered client-side
      session_creator_counts  — JSON-encoded {creator_id: count} for the
                                current SwipeFeed mount. Frontend stores this
                                in React state and echoes it on every call so
                                the cooldown carries across batches in the
                                same session.
    """
    assert_account_owner(account_id, uid)
    if limit <= 0 or limit > MAX_FEED_BATCH_SIZE:
        raise HTTPException(status_code=400, detail=f"limit must be in 1..{MAX_FEED_BATCH_SIZE}")

    exclude_ids = [s for s in exclude.split(",") if s]

    # Header takes precedence over query param if both are sent — query strings
    # have length limits and creator counts can grow.
    header_counts = request.headers.get("X-Session-Creator-Counts")
    raw_counts = header_counts if header_counts else session_creator_counts
    counts = _parse_session_creator_counts(raw_counts)

    result = build_batch(
        account_id=account_id,
        session_creator_counts=counts,
        exclude_ids=exclude_ids,
        limit=limit,
    )

    log.info(
        "feed: account=%s mix_ins=%d mix_niches=%s returned=%d fallback=%d anchor=%s rerank=%s",
        account_id,
        result.get("mix_used", {}).get("inspiration_slots", 0),
        result.get("mix_used", {}).get("niche_allocation", {}),
        result.get("batch_size", 0),
        result.get("fallback_slots", 0),
        result.get("stage1_anchor", ""),
        result.get("rerank_applied", False),
    )
    return result


__all__ = ["router", "build_batch", "FEED_BATCH_SIZE", "MAX_FEED_BATCH_SIZE"]

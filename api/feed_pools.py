"""Candidate pool retrieval + rerank for the SwipeUp feed engine.

One pool per slot type (inspiration, plus one per allocated niche).
Each pool is built in two stages:

  Stage 1 — pgvector ANN against `videos.summary_embedding_siglip`,
            scoped to the pool's filter (Tracked creators for inspiration,
            single niche for niche pools). Up to STAGE1_CANDIDATE_LIMIT
            candidates per pool. Excludes seen interactions and any video
            in a content_group_id the user has already interacted with.

  Stage 2 — Frame-level rerank via ranking.score_videos when the user has
            full embeddings (account_frame_embeddings + summary_text).
            Otherwise the stage-1 distance is used directly as the score
            (`1 - distance` so higher == better).

All hydrated candidate dicts share the same shape so build_batch can pick
from any pool uniformly.
"""
from __future__ import annotations

import logging

import numpy as np

from db import get_pool
from ranking import score_videos

log = logging.getLogger("feed.pools")

STAGE1_CANDIDATE_LIMIT_PER_NICHE  = 100
STAGE1_CANDIDATE_LIMIT_INSPIRATION = 100

# Tracked-creator origins. Matches the values written by /account/{id}/creators
# (managefeed, swipefeed) and onboarding's inspiration seeding.
TRACKED_ORIGINS = ('swipefeed', 'onboarding', 'managefeed')


# ── Unseen inspiration count ──────────────────────────────────────────────────

def count_unseen_inspiration(account_id: str) -> int:
    """How many videos from Tracked creators the user hasn't interacted with,
    excluding members of content groups they've already seen. Drives slot mix
    in compute_slot_mix."""
    sql = (
        "SELECT COUNT(*) FROM videos v"
        " JOIN account_creators ac ON ac.creator_id = v.creator_id"
        " WHERE ac.account_id = %s"
        "   AND ac.origin = ANY(%s)"
        "   AND v.summary_embedding_siglip IS NOT NULL"
        "   AND (v.content_group_id IS NULL OR v.content_group_id NOT IN ("
        "       SELECT DISTINCT vv.content_group_id FROM videos vv"
        "       JOIN interactions i ON i.video_id = vv.video_id"
        "       WHERE i.account_id = %s AND vv.content_group_id IS NOT NULL"
        "   ))"
        "   AND NOT EXISTS ("
        "       SELECT 1 FROM interactions i"
        "       WHERE i.account_id = %s AND i.video_id = v.video_id"
        "   )"
    )
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (account_id, list(TRACKED_ORIGINS), account_id, account_id))
            (count,) = cur.fetchone()
    return int(count or 0)


# ── Hydration ─────────────────────────────────────────────────────────────────

def _hydrate_row(row: tuple, niche_id_hint: str | None = None) -> dict:
    """Normalize a candidate row into the dict shape build_batch expects."""
    (video_id, niche_id, creator_id, platform, public_url, display_url, caption,
     hashtags, video_duration, views, likes, comments, shares,
     time_posted, scraped_at,
     handle, creator_name, profile_pic, stage1_distance) = row
    return {
        "video_id":            video_id,
        "niche_id":            niche_id or niche_id_hint,
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
        "stage1_distance":     float(stage1_distance) if stage1_distance is not None else None,
        # Score fields populated by rerank (or fallback below)
        "score":               None,
        "score_semantic":      None,
        "score_structure":     None,
        "score_text":          None,
    }


# The SELECT projection shared by the two pool queries. Keep both queries in
# lockstep so _hydrate_row's unpacking stays valid.
_BASE_PROJECTION = (
    "v.video_id, v.niche_id, v.creator_id, v.platform, v.public_url, v.display_url,"
    " v.caption, v.hashtags, v.video_duration, v.views, v.likes, v.comments, v.shares,"
    " v.time_posted, v.scraped_at,"
    " c.handle, c.creator_name, c.profile_picture_url,"
    " (v.summary_embedding_siglip <=> %s) AS stage1_distance"
)


# ── Pool 1: inspiration ───────────────────────────────────────────────────────

def _retrieve_inspiration_candidates(
    account_id: str,
    anchor: np.ndarray,
    exclude_ids: list[str],
    limit: int,
) -> list[dict]:
    sql = (
        f"SELECT {_BASE_PROJECTION}"
        "  FROM videos v"
        "  JOIN creators c ON c.creator_id = v.creator_id"
        "  JOIN account_creators ac ON ac.creator_id = v.creator_id"
        " WHERE ac.account_id = %s"
        "   AND ac.origin = ANY(%s)"
        "   AND v.summary_embedding_siglip IS NOT NULL"
        "   AND (v.content_group_id IS NULL OR v.content_group_id NOT IN ("
        "       SELECT DISTINCT vv.content_group_id FROM videos vv"
        "       JOIN interactions i ON i.video_id = vv.video_id"
        "       WHERE i.account_id = %s AND vv.content_group_id IS NOT NULL"
        "   ))"
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
            cur.execute(
                sql,
                (anchor, account_id, list(TRACKED_ORIGINS), account_id, account_id,
                 exclude_ids, anchor, limit),
            )
            rows = cur.fetchall()
    return [_hydrate_row(r) for r in rows]


# ── Pool 2: per niche ─────────────────────────────────────────────────────────

def _retrieve_niche_candidates(
    account_id: str,
    niche_id: str,
    anchor: np.ndarray,
    exclude_ids: list[str],
    limit: int,
) -> list[dict]:
    sql = (
        f"SELECT {_BASE_PROJECTION}"
        "  FROM videos v"
        "  JOIN creators c ON c.creator_id = v.creator_id"
        " WHERE v.niche_id = %s"
        "   AND v.summary_embedding_siglip IS NOT NULL"
        "   AND (v.content_group_id IS NULL OR v.content_group_id NOT IN ("
        "       SELECT DISTINCT vv.content_group_id FROM videos vv"
        "       JOIN interactions i ON i.video_id = vv.video_id"
        "       WHERE i.account_id = %s AND vv.content_group_id IS NOT NULL"
        "   ))"
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
            cur.execute(
                sql,
                (anchor, niche_id, account_id, account_id, exclude_ids, anchor, limit),
            )
            rows = cur.fetchall()
    return [_hydrate_row(r, niche_id_hint=niche_id) for r in rows]


# ── Frame fetch + rerank ──────────────────────────────────────────────────────

def _fetch_video_frames(video_ids: list[str]) -> dict[str, dict[str, np.ndarray]]:
    """video_id -> {siglip: (N, 1152), dino: (N, 768)}. Videos missing either
    encoder are dropped from the dict (they can't be reranked)."""
    if not video_ids:
        return {}
    sql = (
        "SELECT video_id, frame_idx, siglip_embedding, dino_embedding"
        "  FROM video_frame_embeddings"
        " WHERE video_id = ANY(%s::text[])"
        "   AND siglip_embedding IS NOT NULL"
        "   AND dino_embedding   IS NOT NULL"
        " ORDER BY video_id, frame_idx"
    )
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (video_ids,))
            rows = cur.fetchall()

    by_id: dict[str, list[tuple[np.ndarray, np.ndarray]]] = {}
    for video_id, _frame_idx, sig_emb, dino_emb in rows:
        by_id.setdefault(video_id, []).append((sig_emb, dino_emb))

    out: dict[str, dict[str, np.ndarray]] = {}
    for video_id, frames in by_id.items():
        out[video_id] = {
            "siglip": np.stack([f[0] for f in frames]).astype(np.float32),
            "dino":   np.stack([f[1] for f in frames]).astype(np.float32),
        }
    return out


def _rerank_in_place(
    candidates: list[dict],
    user_siglip: np.ndarray,
    user_dino: np.ndarray,
    user_text: np.ndarray,
) -> list[dict]:
    """Run frame-level scoring against all candidates that have frame data.
    Drops candidates with no frame embeddings (rare post-DINO-deploy). Returns
    a new list sorted DESC by score; the same dicts are mutated in place with
    `score`, `score_semantic`, `score_structure`, `score_text` fields."""
    if not candidates:
        return []

    video_ids   = [c["video_id"] for c in candidates]
    video_frames = _fetch_video_frames(video_ids)

    scorable = [c for c in candidates if c["video_id"] in video_frames]
    if not scorable:
        return []

    max_n     = max(video_frames[c["video_id"]]["siglip"].shape[0] for c in scorable)
    V         = len(scorable)
    sig_dim   = user_siglip.shape[1]
    dino_dim  = user_dino.shape[1]
    video_siglip_3d = np.zeros((V, max_n, sig_dim),  dtype=np.float32)
    video_dino_3d   = np.zeros((V, max_n, dino_dim), dtype=np.float32)
    for i, c in enumerate(scorable):
        s = video_frames[c["video_id"]]["siglip"]
        d = video_frames[c["video_id"]]["dino"]
        video_siglip_3d[i, : s.shape[0]] = s
        video_dino_3d  [i, : d.shape[0]] = d

    scores = score_videos(
        user_siglip=user_siglip,
        user_dino=user_dino,
        user_text=user_text,
        video_siglip=video_siglip_3d,
        video_dino=video_dino_3d,
    )

    for i, c in enumerate(scorable):
        c["score"]           = float(scores["final"][i])
        c["score_semantic"]  = float(scores["semantic"][i])
        c["score_structure"] = float(scores["structure"][i])
        c["score_text"]      = float(scores["text"][i])

    scorable.sort(key=lambda c: c["score"], reverse=True)
    return scorable


def _fallback_score_in_place(candidates: list[dict]) -> list[dict]:
    """When the user vector hasn't built yet, fall back to stage-1 distance:
    `score = 1 - distance`. Candidates come ordered ASC by distance already,
    so this is just a no-op sort plus a score stamp."""
    for c in candidates:
        d = c.get("stage1_distance")
        c["score"] = float(1.0 - d) if d is not None else 0.0
    # Already in best-first order from the ANN; re-sort defensively.
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


# ── Public API: per-pool retrieve + rerank ────────────────────────────────────

def retrieve_and_rerank_niche(
    account_id: str,
    niche_id: str,
    anchor: np.ndarray,
    user_siglip_frames: np.ndarray | None,
    user_dino_frames:   np.ndarray | None,
    user_summary_text:  np.ndarray | None,
    exclude_ids: list[str],
    limit: int = STAGE1_CANDIDATE_LIMIT_PER_NICHE,
) -> list[dict]:
    """Stage-1 retrieve + (if user vector available) stage-2 rerank for one
    niche's pool. Returns candidates ordered best-first."""
    candidates = _retrieve_niche_candidates(account_id, niche_id, anchor, exclude_ids, limit)
    if not candidates:
        return []
    if user_siglip_frames is not None and user_dino_frames is not None and user_summary_text is not None:
        return _rerank_in_place(candidates, user_siglip_frames, user_dino_frames, user_summary_text)
    return _fallback_score_in_place(candidates)


def retrieve_and_rerank_inspiration(
    account_id: str,
    anchor: np.ndarray,
    user_siglip_frames: np.ndarray | None,
    user_dino_frames:   np.ndarray | None,
    user_summary_text:  np.ndarray | None,
    exclude_ids: list[str],
    limit: int = STAGE1_CANDIDATE_LIMIT_INSPIRATION,
) -> list[dict]:
    """Stage-1 retrieve + (optional) stage-2 rerank for Tracked-creator pool.
    Same shape as the niche pool, just a different filter on the SQL side."""
    candidates = _retrieve_inspiration_candidates(account_id, anchor, exclude_ids, limit)
    if not candidates:
        return []
    if user_siglip_frames is not None and user_dino_frames is not None and user_summary_text is not None:
        return _rerank_in_place(candidates, user_siglip_frames, user_dino_frames, user_summary_text)
    return _fallback_score_in_place(candidates)


__all__ = [
    'STAGE1_CANDIDATE_LIMIT_PER_NICHE',
    'STAGE1_CANDIDATE_LIMIT_INSPIRATION',
    'TRACKED_ORIGINS',
    'count_unseen_inspiration',
    'retrieve_and_rerank_niche',
    'retrieve_and_rerank_inspiration',
]

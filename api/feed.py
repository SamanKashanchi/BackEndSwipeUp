"""SwipeUp Feed Engine — slot-composed inspiration feed.

GET /feed
  Returns up to FEED_BATCH_SIZE videos for an account, assembled by reserving
  a share of the batch for Tracked-creator content and interleaving the rest
  across the user's selected niches. Two-stage ranking (pgvector ANN +
  optional frame-level rerank) is applied per pool before slot assignment.

Wired into main.py via `app.include_router(feed.router)`.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import unquote_plus

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Request

from auth import assert_account_owner, verify_id_token
from db import get_pool
from feed_pools import (
    count_unseen_inspiration,
    retrieve_and_rerank_inspiration,
    retrieve_and_rerank_niche,
)
from feed_slots import (
    INSPIRATION_KEY,
    compute_slot_mix,
    creator_penalty,
    generate_interleaved_sequence,
)

log = logging.getLogger("feed")

# ── Tunable knobs ─────────────────────────────────────────────────────────────

FEED_BATCH_SIZE = 15
MAX_FEED_BATCH_SIZE = 50

router = APIRouter()


# ── Account state load ────────────────────────────────────────────────────────

def _load_account_state(account_id: str) -> dict[str, Any]:
    """Pull niches, summary embeddings, frame embeddings, and per-niche fallback
    anchors in a single DB roundtrip. Mirrors the legacy _load_account_state in
    main.py but returns per-niche anchors so the slot engine can score each
    niche's pool against the right vector."""
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a.user_id, a.platform, a.handle,"
                "       s.summary_siglip, s.summary_dino, s.summary_text"
                "  FROM accounts a"
                "  LEFT JOIN account_summary_embeddings s ON s.account_id = a.account_id"
                " WHERE a.account_id = %s",
                (account_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="account not found")
            (user_id, platform, handle,
             summary_siglip, summary_dino, summary_text) = row

            cur.execute(
                "SELECT an.niche_id, an.weight, n.visual_embedding, n.embedding"
                "  FROM account_niches an"
                "  LEFT JOIN niches n ON n.niche_id = an.niche_id"
                " WHERE an.account_id = %s"
                " ORDER BY an.weight DESC, an.niche_id",
                (account_id,),
            )
            niche_rows = cur.fetchall()

            cur.execute(
                "SELECT siglip_embedding, dino_embedding"
                "  FROM account_frame_embeddings"
                " WHERE account_id = %s AND siglip_embedding IS NOT NULL"
                "   AND dino_embedding   IS NOT NULL"
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

    account_niches: list[tuple[str, float]] = [
        (r[0], float(r[1])) for r in niche_rows
    ]

    # Per-niche anchor map: prefer account_summary_siglip everywhere, else
    # the niche's own visual_embedding, else its text embedding. Slot pools
    # can each have a different anchor in principle — currently they all use
    # the same summary when one exists.
    summary_siglip_np = (
        np.asarray(summary_siglip, dtype=np.float32) if summary_siglip is not None else None
    )
    summary_text_np = (
        np.asarray(summary_text, dtype=np.float32) if summary_text is not None else None
    )

    niche_anchors: dict[str, tuple[np.ndarray, str]] = {}
    for niche_id, _w, visual_emb, text_emb in niche_rows:
        if summary_siglip_np is not None:
            niche_anchors[niche_id] = (summary_siglip_np, "account_summary_siglip")
        elif visual_emb is not None:
            niche_anchors[niche_id] = (np.asarray(visual_emb, dtype=np.float32), "niche_visual_embedding")
        elif text_emb is not None:
            niche_anchors[niche_id] = (np.asarray(text_emb, dtype=np.float32), "niche_embedding")
        # else: no anchor — that niche's pool will be empty, allocation falls through

    # Inspiration pool uses the same anchor preference — summary first, then
    # the top-weight niche's visual_embedding as a fallback.
    inspiration_anchor: np.ndarray | None = None
    inspiration_anchor_source = ""
    if summary_siglip_np is not None:
        inspiration_anchor = summary_siglip_np
        inspiration_anchor_source = "account_summary_siglip"
    else:
        for _nid, _w, visual_emb, text_emb in niche_rows:
            if visual_emb is not None:
                inspiration_anchor = np.asarray(visual_emb, dtype=np.float32)
                inspiration_anchor_source = "niche_visual_embedding"
                break
            if text_emb is not None:
                inspiration_anchor = np.asarray(text_emb, dtype=np.float32)
                inspiration_anchor_source = "niche_embedding"
                break

    return {
        "user_id":             user_id,
        "platform":            platform,
        "handle":              handle,
        "account_niches":      account_niches,             # [(niche_id, weight), ...] DESC by weight
        "niche_anchors":       niche_anchors,              # niche_id -> (anchor, source)
        "inspiration_anchor":  inspiration_anchor,
        "inspiration_anchor_source": inspiration_anchor_source,
        "summary_siglip":      summary_siglip_np,
        "summary_text":        summary_text_np,
        "user_siglip_frames":  user_siglip_frames,
        "user_dino_frames":    user_dino_frames,
        "stage1_anchor_source": (
            "account_summary_siglip" if summary_siglip_np is not None
            else (inspiration_anchor_source or "none")
        ),
    }


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
    state = _load_account_state(account_id)

    account_niches = state["account_niches"]
    if not account_niches:
        return {
            "batch": [],
            "batch_size": 0,
            "reason": "no_niches_selected",
            "session_creator_counts": session_creator_counts,
        }

    # 1. Supply count + mix
    unseen_inspiration = count_unseen_inspiration(account_id)
    inspiration_slots, niche_allocation = compute_slot_mix(
        unseen_inspiration, account_niches, batch_size=limit
    )
    slot_sequence = generate_interleaved_sequence(niche_allocation, inspiration_slots)

    # 2. Build pools
    pools: dict[str, list[dict]] = {}
    for niche_id, slot_count in niche_allocation.items():
        if slot_count <= 0:
            continue
        anchor_pair = state["niche_anchors"].get(niche_id)
        if anchor_pair is None:
            pools[niche_id] = []
            continue
        anchor, _source = anchor_pair
        pools[niche_id] = retrieve_and_rerank_niche(
            account_id=account_id,
            niche_id=niche_id,
            anchor=anchor,
            user_siglip_frames=state["user_siglip_frames"],
            user_dino_frames=state["user_dino_frames"],
            user_summary_text=state["summary_text"],
            exclude_ids=exclude_ids,
        )

    if inspiration_slots > 0:
        if state["inspiration_anchor"] is not None:
            pools[INSPIRATION_KEY] = retrieve_and_rerank_inspiration(
                account_id=account_id,
                anchor=state["inspiration_anchor"],
                user_siglip_frames=state["user_siglip_frames"],
                user_dino_frames=state["user_dino_frames"],
                user_summary_text=state["summary_text"],
                exclude_ids=exclude_ids,
            )
        else:
            pools[INSPIRATION_KEY] = []

    # 3. Slot assignment with creator cooldown
    batch: list[dict] = []
    batch_creators: list[tuple[str, int]] = []
    fallback_count = 0
    rerank_applied = state["user_siglip_frames"] is not None  # whether stage-2 was usable

    # Local mutable copy so we don't poison the caller's dict.
    session_creator_counts = dict(session_creator_counts)

    for slot_pos, slot_key in enumerate(slot_sequence):
        slot_label   = slot_key
        primary_pool = pools.get(slot_key, [])

        chosen, used_fallback = _pick_best(
            primary_pool, batch_creators, session_creator_counts, slot_pos
        )

        if chosen is None:
            # Fall through. Inspiration → merged niche pools. Niche → other niche pools.
            if slot_key == INSPIRATION_KEY:
                fallback_pool = [
                    v for nid, pool in pools.items() if nid != INSPIRATION_KEY for v in pool
                ]
                slot_label = "inspiration_fallback_to_niche"
            else:
                fallback_pool = [
                    v for nid, pool in pools.items()
                    if nid != INSPIRATION_KEY and nid != slot_key
                    for v in pool
                ]
                slot_label = "niche_fallback"

            chosen, used_fallback = _pick_best(
                fallback_pool, batch_creators, session_creator_counts, slot_pos
            )
            if chosen is None:
                # Pools fully exhausted — return partial batch (frontend shows
                # "You're all caught up").
                break
            fallback_count += 1

        batch.append({
            "slot":      slot_label,
            "position":  slot_pos,
            "video":     _shape_response_video(chosen),
        })
        batch_creators.append((chosen["creator_id"], slot_pos))
        session_creator_counts[chosen["creator_id"]] = (
            session_creator_counts.get(chosen["creator_id"], 0) + 1
        )

        # Remove the chosen video from every pool so it can't be picked twice.
        chosen_id = chosen["video_id"]
        for pk in list(pools.keys()):
            pools[pk] = [v for v in pools[pk] if v["video_id"] != chosen_id]

    return {
        "batch":      batch,
        "batch_size": len(batch),
        "mix_used": {
            "inspiration_slots": inspiration_slots,
            "niche_slots":       sum(niche_allocation.values()),
            "niche_allocation":  niche_allocation,
        },
        "unseen_inspiration_count": unseen_inspiration,
        "stage1_anchor":            state["stage1_anchor_source"],
        "rerank_applied":           rerank_applied,
        "fallback_slots":           fallback_count,
        "session_creator_counts":   session_creator_counts,
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
    """Slot-composed feed batch.

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

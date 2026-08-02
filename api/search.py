"""Text/topic semantic search over the whole video catalog.

GET /search?q=<text> embeds the query with SigLIP's text encoder (via the
onboarding-service /embed-text) and runs a pgvector cosine ANN against
videos.summary_embedding_siglip — the SAME vector space the feed retrieves in.
SigLIP is contrastively trained image<->text, so a text query and a video's
visual summary are directly comparable (both unit-norm, 1152-dim).

Whole-catalog by default (every video with an embedding, regardless of which
creators the account tracks — search is discovery beyond the feed). If
account_id is supplied, the caller's already-seen videos are excluded.

v1 is Stage-1 ANN only: rank purely by query->video cosine similarity. The
frame-level rerank the feed uses is account-anchored and doesn't apply to a
text query, so it's intentionally omitted here.
"""
from __future__ import annotations

import logging
import os

import httpx
import numpy as np
from fastapi import APIRouter, Depends, HTTPException

from auth import assert_account_owner, verify_id_token
from db import get_pool
from feed import _shape_response_video
from feed_pools import _BASE_PROJECTION, _hydrate_row

log = logging.getLogger("api.search")

router = APIRouter()

SEARCH_LIMIT_DEFAULT = 30
SEARCH_LIMIT_MAX = 100

# SigLIP text-embedding service (a.k.a. onboarding-service). Same model that
# produced videos.summary_embedding_siglip, so query and video vectors align.
SIGLIP_SERVICE_URL = os.getenv(
    "SIGLIP_SERVICE_URL",
    "https://onboarding-service-1023666874527.us-central1.run.app",
).rstrip("/")

# Generous timeout: a cold onboarding-service takes ~70s to load the model on
# first request. Keep it warm (min-instances=1) in prod so this is never hit;
# once warm, /embed-text responds in well under a second.
EMBED_TIMEOUT_S = float(os.getenv("SEARCH_EMBED_TIMEOUT_S", "90"))


def _embed_query(text: str) -> np.ndarray:
    """Embed the search text via the SigLIP text encoder. Returns a unit-norm
    1152-dim float32 vector aligned with videos.summary_embedding_siglip."""
    try:
        resp = httpx.post(
            f"{SIGLIP_SERVICE_URL}/embed-text",
            json={"text": text},
            timeout=EMBED_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        log.warning("query embed failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail="search embedding service unavailable")

    emb = data.get("embedding")
    if not emb or data.get("dim") != 1152:
        raise HTTPException(status_code=502, detail="search embedding service returned a bad payload")
    return np.asarray(emb, dtype=np.float32)


def _search_catalog(anchor: np.ndarray, account_id: str, limit: int) -> list[dict]:
    """Cosine ANN over the whole catalog, ordered by similarity to the query
    vector. Reuses the feed's projection + hydration so results are shaped
    exactly like /feed items (the grid already consumes that shape)."""
    where = ["v.summary_embedding_siglip IS NOT NULL"]
    # _BASE_PROJECTION's first %s is the distance term, so anchor leads the params.
    params: list = [anchor]
    if account_id:
        where.append(
            "NOT EXISTS (SELECT 1 FROM interactions i"
            " WHERE i.account_id = %s AND i.video_id = v.video_id)"
        )
        params.append(account_id)

    sql = (
        f"SELECT {_BASE_PROJECTION}"
        "  FROM videos v"
        "  JOIN creators c ON c.creator_id = v.creator_id"
        f" WHERE {' AND '.join(where)}"
        " ORDER BY v.summary_embedding_siglip <=> %s"
        " LIMIT %s"
    )
    params.extend([anchor, limit])

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    out = []
    for r in rows:
        c = _hydrate_row(r)
        dist = c.get("stage1_distance")
        # cosine distance -> similarity; higher == better (drives "Best match").
        c["score"] = (1.0 - dist) if dist is not None else None
        c["score_semantic"] = c["score"]
        out.append(_shape_response_video(c))
    return out


@router.get("/search")
def search(
    q: str,
    limit: int = SEARCH_LIMIT_DEFAULT,
    account_id: str = "",
    uid: str = Depends(verify_id_token),
) -> dict:
    """Semantic topic search over the video catalog.

    Query params:
      q           — free-text topic (e.g. "german shepherds", "knockouts")
      limit       — max results (default 30, max 100)
      account_id  — optional; when set, excludes the account's already-seen
                    videos (ownership is verified against the caller's token)
    """
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="q must be a non-empty string")
    if limit <= 0 or limit > SEARCH_LIMIT_MAX:
        raise HTTPException(status_code=400, detail=f"limit must be in 1..{SEARCH_LIMIT_MAX}")
    if account_id:
        assert_account_owner(account_id, uid)

    anchor = _embed_query(query)
    results = _search_catalog(anchor, account_id, limit)
    log.info("search: q=%r account=%s returned=%d", query, account_id or "-", len(results))
    return {"query": query, "count": len(results), "results": results}

"""Run build_batch() locally for an account, bypassing FastAPI/auth, so we can
see exactly what the slot engine produces and where (if anywhere) it goes wrong."""
from __future__ import annotations
import os, sys, json
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "api"))

from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")

# Pool init must happen before feed.py tries to use it.
from db import open_pool
open_pool()

import feed as feed_module
from feed_pools import count_unseen_inspiration
import psycopg

account_id = sys.argv[1] if len(sys.argv) > 1 else "sLhnMmNzWrdrMdEwXB7q"

print(f"=== build_batch for {account_id} ===\n")

# Sanity: ask the unseen-inspiration count directly
ins = count_unseen_inspiration(account_id)
print(f"count_unseen_inspiration = {ins}")

# Also peek at the account state the way feed.py loads it
state = feed_module._load_account_state(account_id)
print(f"account_niches            = {state['account_niches']}")
print(f"summary_siglip is None?   = {state['summary_siglip'] is None}")
print(f"user_siglip_frames is None? = {state['user_siglip_frames'] is None}")
print(f"inspiration_anchor src    = {state['inspiration_anchor_source']!r}")
print(f"niche_anchors keys        = {list(state['niche_anchors'].keys())}")
for nid, (vec, src) in state['niche_anchors'].items():
    print(f"  niche={nid!r} anchor_src={src!r} anchor_shape={vec.shape}")

print(f"\n--- calling build_batch ---")
result = feed_module.build_batch(
    account_id=account_id,
    session_creator_counts={},
    exclude_ids=[],
    limit=15,
)

# Trim videos in output so it's readable
batch = result.get("batch", [])
trimmed_batch = [
    {
        "slot": item["slot"],
        "position": item["position"],
        "video_id": item["video"]["video_id"],
        "niche_id": item["video"].get("niche_id"),
        "creator_handle": item["video"]["creator"]["handle"],
        "score": item["video"].get("score"),
    }
    for item in batch
]
result_for_print = {**result, "batch": trimmed_batch}
print(json.dumps(result_for_print, indent=2, default=str))

# And raw SQL sanity check — what would the niche pool query return?
print(f"\n--- raw SQL: memes videos available to this account ---")
db_url = os.environ["DATABASE_URL"]
import psycopg
from pgvector.psycopg import register_vector
import numpy as np
conn = psycopg.connect(db_url, autocommit=True)
register_vector(conn)
with conn.cursor() as cur:
    anchor = state['niche_anchors'].get('memes')
    if anchor is None:
        print("  NO ANCHOR FOR MEMES — bailing")
    else:
        vec, src = anchor
        cur.execute(
            "SELECT v.video_id, v.creator_id, v.niche_id, (v.summary_embedding_siglip <=> %s) AS dist"
            "  FROM videos v"
            " WHERE v.niche_id = 'memes'"
            "   AND v.summary_embedding_siglip IS NOT NULL"
            "   AND NOT EXISTS ("
            "       SELECT 1 FROM interactions i"
            "       WHERE i.account_id = %s AND i.video_id = v.video_id"
            "   )"
            " ORDER BY v.summary_embedding_siglip <=> %s"
            " LIMIT 20",
            (vec, account_id, vec),
        )
        rows = cur.fetchall()
        print(f"  rows returned: {len(rows)}")
        for r in rows:
            print(f"  video={r[0]:42s}  creator={r[1]:30s}  niche={r[2]}  dist={float(r[3]):.4f}")
conn.close()

"""Inspect the Tracked creators for an account and what niche their content is in."""
from __future__ import annotations
import os, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
import psycopg

account_id = sys.argv[1] if len(sys.argv) > 1 else "UJbQj9lJ2UVFZS52j7Ps"

conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
with conn.cursor() as cur:
    print(f"=== Inspirations for account {account_id} ===\n")
    cur.execute(
        "SELECT ac.creator_id, ac.origin, c.handle, c.creator_name, c.niche_id"
        "  FROM account_creators ac"
        "  LEFT JOIN creators c ON c.creator_id = ac.creator_id"
        " WHERE ac.account_id = %s"
        " ORDER BY ac.creator_id",
        (account_id,),
    )
    rows = cur.fetchall()
    if not rows:
        print("  (no tracked creators)")
    for r in rows:
        print(f"  creator={r[0]:30s}  origin={r[1]:15s}  handle={r[2]!r:25s}  name={r[3]!r:30s}  niche={r[4]}")

    print(f"\n=== Videos under those inspirations, by niche ===")
    cur.execute(
        """
        SELECT v.niche_id, COUNT(*),
               COUNT(*) FILTER (WHERE v.summary_embedding_siglip IS NOT NULL)
          FROM videos v
          JOIN account_creators ac ON ac.creator_id = v.creator_id
         WHERE ac.account_id = %s
         GROUP BY v.niche_id
         ORDER BY 2 DESC
        """,
        (account_id,),
    )
    for niche, total, with_emb in cur.fetchall():
        print(f"  niche={niche!r:15s}  videos={total:3d}  with_summary_emb={with_emb}")

    print(f"\n=== Unseen inspiration count (same query feed_pools uses) ===")
    cur.execute(
        "SELECT COUNT(*) FROM videos v"
        " JOIN account_creators ac ON ac.creator_id = v.creator_id"
        " WHERE ac.account_id = %s"
        "   AND ac.origin = ANY(ARRAY['swipefeed','onboarding','managefeed'])"
        "   AND v.summary_embedding_siglip IS NOT NULL"
        "   AND (v.content_group_id IS NULL OR v.content_group_id NOT IN ("
        "       SELECT DISTINCT vv.content_group_id FROM videos vv"
        "       JOIN interactions i ON i.video_id = vv.video_id"
        "       WHERE i.account_id = %s AND vv.content_group_id IS NOT NULL))"
        "   AND NOT EXISTS ("
        "       SELECT 1 FROM interactions i"
        "       WHERE i.account_id = %s AND i.video_id = v.video_id)",
        (account_id, account_id, account_id),
    )
    print(f"  unseen_inspiration_count = {cur.fetchone()[0]}")

    print(f"\n=== Sample of recent feed-eligible inspiration videos ===")
    cur.execute(
        "SELECT v.video_id, v.niche_id, c.handle, v.time_posted"
        "  FROM videos v"
        "  JOIN account_creators ac ON ac.creator_id = v.creator_id"
        "  JOIN creators c ON c.creator_id = v.creator_id"
        " WHERE ac.account_id = %s"
        "   AND v.summary_embedding_siglip IS NOT NULL"
        "   AND NOT EXISTS ("
        "       SELECT 1 FROM interactions i"
        "       WHERE i.account_id = %s AND i.video_id = v.video_id)"
        " ORDER BY v.time_posted DESC NULLS LAST"
        " LIMIT 10",
        (account_id, account_id),
    )
    for r in cur.fetchall():
        print(f"  {r[0]:40s}  niche={r[1]!r:12s}  handle={r[2]!r:20s}  posted={r[3]}")

conn.close()

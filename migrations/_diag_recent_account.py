"""Dump state of the most recently created account, to debug feed misbehavior."""
from __future__ import annotations
import os, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
import psycopg

conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
with conn.cursor() as cur:
    cur.execute(
        "SELECT account_id, user_id, platform, handle, created_at"
        " FROM accounts ORDER BY created_at DESC LIMIT 5"
    )
    rows = cur.fetchall()
    print("=== 5 most recent accounts ===")
    for r in rows:
        print(f"  {r[0]:32s}  user={r[1][:8]}  platform={r[2]:10s}  handle={r[3]!r:25s}  created={r[4]}")
    if not rows:
        print("  (none)")
        sys.exit(0)

    account_id = rows[0][0]
    print(f"\n=== Drilldown on most recent: {account_id} ===")

    cur.execute(
        "SELECT niche_id, weight, source, created_at"
        " FROM account_niches WHERE account_id = %s ORDER BY weight DESC",
        (account_id,),
    )
    print("\naccount_niches:")
    for r in cur.fetchall():
        print(f"  niche={r[0]:10s}  weight={r[1]:.3f}  source={r[2]:20s}  created={r[3]}")

    cur.execute(
        "SELECT (summary_siglip IS NOT NULL) AS has_sig,"
        "       (summary_dino   IS NOT NULL) AS has_dino,"
        "       (summary_text   IS NOT NULL) AS has_text,"
        "       source, updated_at"
        "  FROM account_summary_embeddings WHERE account_id = %s",
        (account_id,),
    )
    row = cur.fetchone()
    if row:
        print(f"\naccount_summary_embeddings: sig={row[0]} dino={row[1]} text={row[2]} source={row[3]} updated={row[4]}")
    else:
        print("\naccount_summary_embeddings: NONE (user vector hasn't built yet)")

    cur.execute(
        "SELECT COUNT(*) FROM account_frame_embeddings WHERE account_id = %s",
        (account_id,),
    )
    print(f"account_frame_embeddings: {cur.fetchone()[0]} rows")

    cur.execute(
        "SELECT origin, COUNT(*) FROM account_creators WHERE account_id = %s GROUP BY origin",
        (account_id,),
    )
    by_origin = cur.fetchall()
    print(f"\naccount_creators by origin:")
    for o, c in by_origin:
        print(f"  {o:25s}  {c}")
    if not by_origin:
        print("  (no Tracked creators)")

    cur.execute(
        "SELECT COUNT(*) FROM interactions WHERE account_id = %s",
        (account_id,),
    )
    print(f"\ninteractions: {cur.fetchone()[0]} rows")

    # What's in supply for each of their niches?
    cur.execute(
        "SELECT n.niche_id, COUNT(v.video_id) FROM account_niches an"
        " JOIN niches n ON n.niche_id = an.niche_id"
        " LEFT JOIN videos v ON v.niche_id = n.niche_id"
        "                   AND v.summary_embedding_siglip IS NOT NULL"
        " WHERE an.account_id = %s"
        " GROUP BY n.niche_id ORDER BY 2 DESC",
        (account_id,),
    )
    print(f"\nvideo supply per selected niche:")
    for n, c in cur.fetchall():
        print(f"  {n:10s}  {c} videos with embeddings")

conn.close()

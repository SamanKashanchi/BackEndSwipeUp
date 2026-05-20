"""For every account from the last 30 days, check if the user vector was built."""
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
        """
        SELECT
            a.account_id,
            a.handle,
            a.created_at,
            (s.account_id IS NOT NULL) AS has_summary,
            (s.summary_siglip IS NOT NULL) AS has_siglip,
            (s.summary_dino   IS NOT NULL) AS has_dino,
            (s.summary_text   IS NOT NULL) AS has_text,
            (SELECT COUNT(*) FROM account_frame_embeddings f WHERE f.account_id = a.account_id) AS frame_count
        FROM accounts a
        LEFT JOIN account_summary_embeddings s ON s.account_id = a.account_id
        WHERE a.created_at >= NOW() - INTERVAL '60 days'
        ORDER BY a.created_at DESC
        """
    )
    rows = cur.fetchall()
    print(f"{'account_id':24s} {'handle':22s} {'created':24s} sum sig dino txt frames")
    for r in rows:
        print(f"  {r[0]:22s} {r[1]!r:22s} {str(r[2])[:23]:24s} "
              f"{int(r[3])}   {int(r[4])}   {int(r[5])}    {int(r[6])}   {r[7]}")

conn.close()

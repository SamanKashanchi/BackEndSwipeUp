"""Verify Phase 1 final state: 8 mother niches, no sub_niches table,
no videos.sub_niche_id column, all videos+creators+accounts on valid
niches."""

from __future__ import annotations
import os, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import psycopg

conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
with conn.cursor() as cur:
    print("--- niches ---")
    cur.execute("SELECT niche_id, name FROM niches ORDER BY niche_id")
    for r in cur.fetchall():
        print(f"  {r[0]:12s}  {r[1]}")

    print("\n--- video counts per niche ---")
    cur.execute(
        "SELECT n.niche_id, COUNT(v.video_id)"
        " FROM niches n LEFT JOIN videos v ON v.niche_id = n.niche_id"
        " GROUP BY n.niche_id ORDER BY n.niche_id"
    )
    for r in cur.fetchall():
        print(f"  {r[0]:12s}  {r[1]} videos")

    print("\n--- sub_niches table exists? ---")
    cur.execute(
        "SELECT to_regclass('public.sub_niches')"
    )
    print(f"  {cur.fetchone()[0]}")

    print("\n--- videos.sub_niche_id column exists? ---")
    cur.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'videos' AND column_name IN ('sub_niche_id','sub_niche_scores')"
    )
    cols = [r[0] for r in cur.fetchall()]
    print(f"  {cols if cols else 'gone (as expected)'}")

    print("\n--- anything still on legacy niches? ---")
    for tbl in ("videos", "creators", "accounts"):
        cur.execute(
            f"SELECT COUNT(*) FROM {tbl} WHERE niche_id IN ('animals','combat_sports')"
        )
        print(f"  {tbl}: {cur.fetchone()[0]}")

    print("\n--- visual_embedding coverage on new niches ---")
    cur.execute(
        "SELECT niche_id, (visual_embedding IS NOT NULL) AS has_centroid"
        " FROM niches WHERE niche_id IN ('boxing','mma','cats','dogs','horses','wildlife')"
        " ORDER BY niche_id"
    )
    for r in cur.fetchall():
        marker = "✓" if r[1] else "·"
        print(f"  {marker} {r[0]}")

conn.close()

"""Snapshot niche prototype state — mean centroid + per-frame gallery counts."""
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
        SELECT n.niche_id, n.name,
               (n.visual_embedding IS NOT NULL) AS has_mean,
               COALESCE(f.frames, 0)            AS n_frames
          FROM niches n
          LEFT JOIN (
              SELECT niche_id, COUNT(*) AS frames
                FROM niche_frame_embeddings
               GROUP BY niche_id
          ) f ON f.niche_id = n.niche_id
         ORDER BY n.name
        """
    )
    print(f"{'niche_id':12s}  {'name':18s}  mean   frames")
    print("-" * 50)
    for nid, name, has_mean, n_frames in cur.fetchall():
        mean_mark = "✓" if has_mean else "·"
        print(f"  {nid:10s}  {name:18s}  {mean_mark}     {n_frames}")
conn.close()

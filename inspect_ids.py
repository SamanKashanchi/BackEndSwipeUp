"""Inspect creator_id and video_id formats across all tables."""
from __future__ import annotations
import os, sys
from pathlib import Path
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dotenv import load_dotenv
import psycopg

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

with psycopg.connect(os.getenv("DATABASE_URL")) as conn:
    with conn.cursor() as cur:
        print("=== creators (sample 10) ===")
        cur.execute("SELECT creator_id, platform, handle FROM creators ORDER BY creator_id LIMIT 10")
        for r in cur.fetchall():
            print(f"  creator_id={r[0]!r:50s} platform={r[1]:10s} handle={r[2]!r}")

        print()
        print("=== videos (sample 12) ===")
        cur.execute(
            "SELECT video_id, creator_id, platform, original_url "
            "FROM videos ORDER BY scraped_at DESC LIMIT 12"
        )
        for r in cur.fetchall():
            url = (r[3] or '')[:60]
            print(f"  video_id={r[0]!r:50s} creator_id={r[1]!r:40s} platform={r[2]:10s} url={url!r}")

        print()
        print("=== id-format consistency check ===")
        cur.execute(
            "SELECT platform, "
            "       COUNT(*) AS rows, "
            "       COUNT(*) FILTER (WHERE creator_id LIKE platform || '_%%') AS prefixed, "
            "       COUNT(*) FILTER (WHERE creator_id !~ '^[a-z]+_') AS no_prefix "
            "  FROM creators GROUP BY platform ORDER BY platform"
        )
        print("  creators:")
        for r in cur.fetchall():
            print(f"    platform={r[0]:10s} rows={r[1]} prefixed={r[2]} unprefixed_or_other={r[3]}")

        cur.execute(
            "SELECT platform, "
            "       COUNT(*) AS rows, "
            "       COUNT(*) FILTER (WHERE video_id LIKE platform || '_%%') AS prefixed, "
            "       COUNT(*) FILTER (WHERE video_id !~ '^[a-z]+_') AS no_prefix "
            "  FROM videos GROUP BY platform ORDER BY platform"
        )
        print("  videos:")
        for r in cur.fetchall():
            print(f"    platform={r[0]:10s} rows={r[1]} prefixed={r[2]} unprefixed_or_other={r[3]}")

        # FK integrity check
        print()
        print("=== FK integrity ===")
        cur.execute(
            "SELECT COUNT(*) FROM videos v LEFT JOIN creators c "
            "ON c.creator_id = v.creator_id WHERE c.creator_id IS NULL"
        )
        orphan = cur.fetchone()[0]
        print(f"  videos with no matching creators row: {orphan}")

        # Total counts
        print()
        print("=== totals ===")
        cur.execute("SELECT COUNT(*) FROM creators")
        print(f"  creators: {cur.fetchone()[0]}")
        cur.execute("SELECT COUNT(*) FROM videos")
        print(f"  videos:   {cur.fetchone()[0]}")
        cur.execute("SELECT COUNT(DISTINCT creator_id) FROM videos")
        print(f"  distinct creator_id in videos: {cur.fetchone()[0]}")

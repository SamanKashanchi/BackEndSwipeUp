"""Terminate Supabase backends that have been idle in transaction for
longer than 1 hour. They hold AccessShareLock on tables and block DDL.

The migration 0019 was blocked by two such zombies stuck on
'SELECT video_id FROM videos' for ~4 days. Safe to kill — the txns
are idle and stale; any work they were doing was abandoned long ago.
"""

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
        SELECT pid, application_name, state,
               NOW() - state_change AS state_age,
               LEFT(query, 120) AS query,
               pg_terminate_backend(pid) AS killed
          FROM pg_stat_activity
         WHERE state = 'idle in transaction'
           AND NOW() - state_change > INTERVAL '1 hour'
           AND pid <> pg_backend_pid()
        """
    )
    rows = cur.fetchall()
    if not rows:
        print("no stale idle-in-transaction backends found")
    for row in rows:
        print("killed:", row)

conn.close()

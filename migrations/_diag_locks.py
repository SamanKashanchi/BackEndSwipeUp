"""Diagnose what's holding locks on sub_niches / videos so we know
who needs to stop before 0019 can run."""

from __future__ import annotations
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import psycopg

conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
with conn.cursor() as cur:
    print("--- pg_locks on sub_niches / videos ---")
    cur.execute(
        """
        SELECT l.locktype, l.mode, l.granted, l.pid,
               a.application_name, a.client_addr, a.state,
               a.query_start, LEFT(a.query, 200) AS query
          FROM pg_locks l
          LEFT JOIN pg_stat_activity a ON a.pid = l.pid
         WHERE l.relation::regclass::text IN ('sub_niches','videos')
         ORDER BY l.granted DESC, l.pid
        """
    )
    for row in cur.fetchall():
        print(row)

    print("\n--- pg_stat_activity (active + idle in txn) ---")
    cur.execute(
        """
        SELECT pid, application_name, client_addr, state,
               NOW() - state_change AS state_age,
               LEFT(query, 200) AS query
          FROM pg_stat_activity
         WHERE state IN ('active','idle in transaction')
           AND pid <> pg_backend_pid()
         ORDER BY state_change
        """
    )
    for row in cur.fetchall():
        print(row)

conn.close()

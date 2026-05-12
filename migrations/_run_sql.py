"""Tiny runner for plain .sql migrations.

Usage:
    python Backend/migrations/_run_sql.py 0019_drop_sub_niches.sql

Reads DATABASE_URL from Backend/.env, executes the SQL in a single
transaction (psycopg `with conn.transaction():`), commits on success.
Aborts and rolls back on error (including raised EXCEPTION blocks from
DO $$ ... $$ guards). Designed for one-off schema migrations like
0019; not a general migration framework — just enough to run the file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE.parent / ".env")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: _run_sql.py <migration.sql>", file=sys.stderr)
        return 2

    sql_path = _HERE / sys.argv[1]
    if not sql_path.exists():
        print(f"missing: {sql_path}", file=sys.stderr)
        return 2

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set in Backend/.env", file=sys.stderr)
        return 2

    import psycopg
    sql = sql_path.read_text(encoding="utf-8")

    print(f"--- running {sql_path.name} ---")
    conn = psycopg.connect(db_url, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        print(f"--- {sql_path.name} committed ---")
    except Exception as e:
        conn.rollback()
        print(f"!!! {sql_path.name} FAILED, rolled back: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())

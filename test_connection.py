from __future__ import annotations

import os
import sys
import time

# Windows UTF-8 safety — reconfigure stdout/stderr before any print calls.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path

# Load .env from the same directory as this script.
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

import psycopg
from pgvector.psycopg import register_vector


def _classify_operational_error(exc: psycopg.OperationalError) -> str:
    """Map common psycopg OperationalError messages to friendly guidance."""
    msg = str(exc).lower()
    if "password authentication failed" in msg or "authentication failed" in msg:
        return (
            "Authentication failed.\n"
            "  -> Check the password in your DATABASE_URL.\n"
            "  -> Supabase uses the database password, not your dashboard login."
        )
    if (
        "could not connect to server" in msg
        or "connection refused" in msg
        or "name or service not known" in msg
        or "nodename nor servname provided" in msg
        or "temporary failure in name resolution" in msg
    ):
        return (
            "Could not reach the database host.\n"
            "  -> Verify the host URL in DATABASE_URL.\n"
            "  -> Check that your Supabase project is not paused (free tier pauses after inactivity).\n"
            "  -> Check your internet connection."
        )
    if "ssl" in msg or "certificate" in msg:
        return (
            "SSL handshake error.\n"
            "  -> Add '?sslmode=require' to the end of your DATABASE_URL if it is missing.\n"
            "  -> Example: postgresql://user:pass@host:5432/postgres?sslmode=require"
        )
    if "timeout" in msg:
        return (
            "Connection timed out.\n"
            "  -> The Supabase project may be paused or under heavy load.\n"
            "  -> Try again in a few seconds, or check the Supabase dashboard."
        )
    # Generic fallback — include the raw message so it is still useful.
    return f"Operational error connecting to database:\n  {exc}"


def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print(
            "ERROR: DATABASE_URL is not set.\n"
            "  -> Create a .env file in this directory with:\n"
            "     DATABASE_URL=postgresql://postgres:<password>@<host>:5432/postgres?sslmode=require"
        )
        sys.exit(1)

    print("Connecting to Supabase Postgres...")
    print(f"  Host: {_redact_dsn(database_url)}")
    print()

    try:
        with psycopg.connect(database_url) as conn:
            _run_checks(conn)
    except psycopg.OperationalError as exc:
        print(f"\nCONNECTION FAILED\n{_classify_operational_error(exc)}")
        sys.exit(1)
    except psycopg.Error as exc:
        print(f"\nDATABASE ERROR: {exc}")
        sys.exit(1)


def _run_checks(conn: psycopg.Connection) -> None:
    # ── 1. Postgres server version ────────────────────────────────────────────
    row = conn.execute("SELECT version()").fetchone()
    print(f"[OK] Postgres version:\n     {row[0]}\n")

    # ── 2. Round-trip latency for a trivial SELECT 1 ──────────────────────────
    t0 = time.perf_counter()
    conn.execute("SELECT 1")
    latency_ms = (time.perf_counter() - t0) * 1000
    print(f"[OK] Round-trip latency (SELECT 1): {latency_ms:.1f} ms\n")

    # ── 3. pgvector extension presence ───────────────────────────────────────
    row = conn.execute(
        "SELECT extname, extversion FROM pg_extension WHERE extname = 'vector'"
    ).fetchone()

    if row is None:
        print(
            "[WARN] pgvector extension is NOT installed.\n"
            "  -> Go to your Supabase dashboard:\n"
            "     Database -> Extensions -> search 'vector' -> enable it.\n"
            "  -> Then re-run this script."
        )
        return

    print(f"[OK] pgvector extension installed: version {row[1]}\n")

    # ── 4. End-to-end pgvector functional test ───────────────────────────────
    print("Running end-to-end pgvector smoke test...")
    register_vector(conn)

    try:
        conn.execute(
            """
            CREATE TEMP TABLE _smoke_test (
                id   serial PRIMARY KEY,
                vec  vector(3)
            )
            """
        )

        import numpy as np

        test_vec = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        conn.execute(
            "INSERT INTO _smoke_test (vec) VALUES (%s)",
            (test_vec,),
        )

        fetched = conn.execute("SELECT vec FROM _smoke_test LIMIT 1").fetchone()[0]
        assert list(fetched) == list(test_vec), f"Round-trip mismatch: {fetched!r}"

        # TEMP tables are dropped automatically at session end, but be explicit.
        conn.execute("DROP TABLE _smoke_test")
        conn.commit()

        print(f"[OK] pgvector smoke test passed.")
        print(f"     Inserted vector(3): {list(test_vec)}")
        print(f"     Read back:          {list(fetched)}")
        print()
        print("All checks passed. Supabase + pgvector is ready.")

    except Exception as exc:
        print(f"[FAIL] pgvector smoke test failed: {exc}")
        sys.exit(1)


def _redact_dsn(dsn: str) -> str:
    """Show host:port/db only — hide the password."""
    try:
        # Simple regex-free approach: strip credentials from the URL.
        # postgresql://user:pass@host:5432/db?params
        after_scheme = dsn.split("://", 1)[-1]
        at_idx = after_scheme.rfind("@")
        if at_idx == -1:
            return dsn  # No credentials found — show as-is.
        return "postgresql://<credentials>@" + after_scheme[at_idx + 1:]
    except Exception:
        return "<DSN redacted>"


if __name__ == "__main__":
    main()

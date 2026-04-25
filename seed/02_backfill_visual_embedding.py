"""
Backfill the new `visual_embedding` column on niches + sub_niches from
Firestore's `visual_prototype` field.

Run after applying migration 0004_niches_visual_embedding.sql.

Run from the Backend directory:
    python seed/02_backfill_visual_embedding.py

Idempotent: re-running overwrites any existing visual_embedding with the
current Firestore value. Rows where Firestore has no visual_prototype are
left with NULL (pipeline treats NULL as "no prototype blend available",
which is the legacy behaviour).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(_ENV_PATH)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

_FIREBASE_CREDS = Path("C:/Users/skash/Desktop/Pers/DoomSwipe/firebase_creds.json")


def _coerce_embedding(value: Any) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, list) and len(value) > 0:
        return [float(x) for x in value]
    return None


def _init_firebase() -> Any:
    import firebase_admin
    from firebase_admin import credentials, firestore

    if not _FIREBASE_CREDS.exists():
        log.error("Firebase credentials file not found: %s", _FIREBASE_CREDS)
        sys.exit(1)

    cred = credentials.Certificate(str(_FIREBASE_CREDS))
    try:
        firebase_admin.initialize_app(cred)
    except ValueError:
        pass
    return firestore.client()


def _open_pg_connection(database_url: str) -> Any:
    import psycopg
    from pgvector.psycopg import register_vector

    conn = psycopg.connect(database_url, autocommit=False)
    register_vector(conn)
    return conn


def backfill(collection_name: str, table_name: str, id_column: str, fs_db: Any, pg_conn: Any) -> None:
    log.info("Fetching %s from Firestore ...", collection_name)
    docs = list(fs_db.collection(collection_name).stream())
    log.info("  Found %d %s documents.", len(docs), collection_name)

    updated, missing_proto, missing_row = 0, 0, 0

    sql = f"""
        UPDATE {table_name}
        SET visual_embedding = %s,
            updated_at       = NOW()
        WHERE {id_column} = %s
    """

    with pg_conn.transaction():
        cur = pg_conn.cursor()
        for doc in docs:
            d = doc.to_dict() or {}
            row_id = d.get(id_column) or doc.id
            embedding = _coerce_embedding(d.get("visual_prototype"))
            if embedding is None:
                missing_proto += 1
                continue
            if len(embedding) != 1152:
                log.warning(
                    "  %s %r: visual_prototype has %d dims (expected 1152), skipping.",
                    collection_name, row_id, len(embedding),
                )
                continue
            cur.execute(sql, (embedding, row_id))
            if cur.rowcount == 0:
                missing_row += 1
                log.warning("  %s %r: no matching Postgres row.", collection_name, row_id)
            else:
                updated += 1

    log.info(
        "%s: %d updated, %d had no visual_prototype in Firestore, %d had no Postgres row.",
        collection_name, updated, missing_proto, missing_row,
    )


def main() -> None:
    log.info("=== Backfill visual_embedding (Firestore -> Postgres) ===")

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        log.error("DATABASE_URL not set. Add it to %s.", _ENV_PATH)
        sys.exit(1)

    log.info("Initialising Firebase ...")
    fs_db = _init_firebase()
    log.info("Connecting to Postgres ...")
    pg_conn = _open_pg_connection(database_url)

    try:
        log.info("--- Step 1/2: niches ---")
        backfill("niches", "niches", "niche_id", fs_db, pg_conn)

        log.info("--- Step 2/2: sub_niches ---")
        backfill("sub_niches", "sub_niches", "sub_niche_id", fs_db, pg_conn)

        with pg_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM niches     WHERE visual_embedding IS NOT NULL")
            n_with  = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM sub_niches WHERE visual_embedding IS NOT NULL")
            sn_with = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM niches")
            n_all   = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM sub_niches")
            sn_all  = cur.fetchone()[0]
        log.info("Final coverage: niches %d/%d, sub_niches %d/%d", n_with, n_all, sn_with, sn_all)
        log.info("=== Backfill complete. ===")
    finally:
        pg_conn.close()


if __name__ == "__main__":
    main()

"""
Firestore → Postgres seed script.

Seeding order: niches → sub_niches → creators (FK dependency order).
Each collection is seeded in its own transaction so a partial failure
in creators does not roll back the already-committed niches/sub_niches.

Run from the Backend directory:
    python seed/01_firestore_to_postgres.py
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# UTF-8 safe stdout for Windows (cp1252 default chokes on emoji/arrows)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

import os

from dotenv import load_dotenv

# Load .env from Backend/ regardless of cwd
_ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(_ENV_PATH)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
_FIREBASE_CREDS = Path("C:/Users/skash/Desktop/Pers/DoomSwipe/firebase_creds.json")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _coerce_keywords(value: Any) -> list[str]:
    """Coerce Firestore canonical_keywords to a plain list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        return [value] if value else []
    return []


def _coerce_jsonb(value: Any) -> Any:
    """
    Return a JSON-serialisable value suitable for a JSONB column.
    Lists and dicts pass through; anything else becomes None.
    """
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    return None


def _coerce_embedding(value: Any) -> list[float] | None:
    """
    Return a list[float] for a vector column, or None if missing/empty.
    Firestore stores vectors as list[float].
    """
    if value is None:
        return None
    if isinstance(value, list) and len(value) > 0:
        return [float(x) for x in value]
    return None


def _coerce_platform(value: Any) -> str | None:
    """Lowercase platform to match CHECK constraint."""
    if value is None:
        return None
    return str(value).lower()


def _derive_handle(creator_id: str) -> str:
    """
    Derive handle from creator_id by stripping the platform prefix.
    e.g. 'instagram_cats_of_instagram' -> 'cats_of_instagram'
    e.g. 'tiktok_funny.clips' -> 'funny.clips'
    """
    parts = creator_id.split("_", 1)
    if len(parts) == 2:
        return parts[1]
    return creator_id


def _parse_date_added(value: Any) -> datetime | None:
    """
    Parse the date_added field from Firestore.
    It may be a string ISO datetime, a Firestore Timestamp, or None.
    Always returns a timezone-aware datetime or None.
    """
    if value is None:
        return None
    # Firestore DatetimeWithNanoseconds is already a datetime
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        # Handle both 'Z' suffix and '+00:00' offsets
        s = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            log.warning("Could not parse date_added string: %r", value)
            return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Firebase init
# ─────────────────────────────────────────────────────────────────────────────

def _init_firebase() -> Any:
    """Initialise Firebase Admin SDK and return a Firestore client."""
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        if not _FIREBASE_CREDS.exists():
            log.error(
                "Firebase credentials file not found: %s", _FIREBASE_CREDS
            )
            sys.exit(1)

        cred = credentials.Certificate(str(_FIREBASE_CREDS))
        try:
            firebase_admin.initialize_app(cred)
        except ValueError:
            # Already initialised in the same process
            pass

        return firestore.client()
    except Exception as exc:
        log.error("Firebase initialisation failed: %s", exc)
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Postgres init
# ─────────────────────────────────────────────────────────────────────────────

def _get_database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        log.error(
            "DATABASE_URL not set. Add it to %s or export it as an env var.", _ENV_PATH
        )
        sys.exit(1)
    return url


def _open_pg_connection(database_url: str) -> Any:
    """Open and return a psycopg connection with pgvector registered."""
    try:
        import psycopg
        from pgvector.psycopg import register_vector

        conn = psycopg.connect(database_url, autocommit=False)
        register_vector(conn)
        return conn
    except Exception as exc:
        log.error("Postgres connection failed: %s", exc)
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Seed: niches
# ─────────────────────────────────────────────────────────────────────────────

def seed_niches(fs_db: Any, pg_conn: Any) -> int:
    """
    Seed niches from Firestore into Postgres.
    Returns the number of rows inserted/updated.
    """
    log.info("Fetching niches from Firestore ...")
    docs = list(fs_db.collection("niches").stream())
    log.info("  Found %d niche documents.", len(docs))

    skipped: list[str] = []
    rows: list[dict[str, Any]] = []

    for doc in docs:
        d = doc.to_dict()

        niche_id = d.get("niche_id") or doc.id
        name = d.get("name")
        slug = d.get("slug")

        if not niche_id or not name or not slug:
            log.warning("Skipping niche %r: missing required field (niche_id/name/slug).", doc.id)
            skipped.append(doc.id)
            continue

        rows.append(
            {
                "niche_id": niche_id,
                "name": name,
                "slug": slug,
                "description": d.get("description"),
                "core_concept": d.get("core_concept"),
                "platform_context": d.get("platform_context"),
                "visual_characteristics": _coerce_jsonb(d.get("visual_characteristics")),
                "examples": _coerce_jsonb(d.get("examples")),
                "negative_examples": _coerce_jsonb(d.get("negative_examples")),
                "canonical_keywords": _coerce_keywords(d.get("canonical_keywords")),
                "embedding": _coerce_embedding(d.get("embedding")),
                "embedding_version": int(d.get("embedding_version") or 1),
            }
        )

    if not rows:
        log.warning("No valid niche rows to insert.")
        return 0

    upsert_sql = """
        INSERT INTO niches (
            niche_id, name, slug, description, core_concept, platform_context,
            visual_characteristics, examples, negative_examples,
            canonical_keywords, embedding, embedding_version,
            created_at, updated_at
        ) VALUES (
            %(niche_id)s, %(name)s, %(slug)s, %(description)s, %(core_concept)s,
            %(platform_context)s, %(visual_characteristics)s, %(examples)s,
            %(negative_examples)s, %(canonical_keywords)s, %(embedding)s,
            %(embedding_version)s, NOW(), NOW()
        )
        ON CONFLICT (niche_id) DO UPDATE SET
            name                  = EXCLUDED.name,
            slug                  = EXCLUDED.slug,
            description           = EXCLUDED.description,
            core_concept          = EXCLUDED.core_concept,
            platform_context      = EXCLUDED.platform_context,
            visual_characteristics = EXCLUDED.visual_characteristics,
            examples              = EXCLUDED.examples,
            negative_examples     = EXCLUDED.negative_examples,
            canonical_keywords    = EXCLUDED.canonical_keywords,
            embedding             = EXCLUDED.embedding,
            embedding_version     = EXCLUDED.embedding_version,
            updated_at            = NOW()
    """

    with pg_conn.transaction():
        cur = pg_conn.cursor()
        for row in rows:
            # Serialise JSONB fields to JSON strings for psycopg
            row_copy = dict(row)
            for field in ("visual_characteristics", "examples", "negative_examples"):
                if row_copy[field] is not None:
                    row_copy[field] = json.dumps(row_copy[field])
            cur.execute(upsert_sql, row_copy)

    count = len(rows)
    if skipped:
        log.warning("  Skipped %d niche docs: %s", len(skipped), skipped)
    log.info("Niches: %d inserted/updated.", count)
    return count


# ─────────────────────────────────────────────────────────────────────────────
# Seed: sub_niches
# ─────────────────────────────────────────────────────────────────────────────

def seed_sub_niches(fs_db: Any, pg_conn: Any) -> int:
    """
    Seed sub_niches from Firestore into Postgres.
    Returns the number of rows inserted/updated.
    """
    log.info("Fetching sub_niches from Firestore ...")
    docs = list(fs_db.collection("sub_niches").stream())
    log.info("  Found %d sub_niche documents.", len(docs))

    skipped: list[str] = []
    rows: list[dict[str, Any]] = []

    for doc in docs:
        d = doc.to_dict()

        sub_niche_id = d.get("sub_niche_id") or doc.id
        # Firestore field is 'parent_niche', schema column is 'parent_niche_id'
        parent_niche_id = d.get("parent_niche") or d.get("parent_niche_id")
        name = d.get("name")
        slug = d.get("slug")

        if not sub_niche_id or not name or not slug:
            log.warning(
                "Skipping sub_niche %r: missing required field (sub_niche_id/name/slug).",
                doc.id,
            )
            skipped.append(doc.id)
            continue

        if not parent_niche_id:
            log.warning(
                "Skipping sub_niche %r: missing parent_niche reference.", doc.id
            )
            skipped.append(doc.id)
            continue

        rows.append(
            {
                "sub_niche_id": sub_niche_id,
                "parent_niche_id": parent_niche_id,
                "name": name,
                "slug": slug,
                "description": d.get("description"),
                "visual_characteristics": _coerce_jsonb(d.get("visual_characteristics")),
                "canonical_keywords": _coerce_keywords(d.get("canonical_keywords")),
                "embedding": _coerce_embedding(d.get("embedding")),
                "embedding_version": int(d.get("embedding_version") or 1),
            }
        )

    if not rows:
        log.warning("No valid sub_niche rows to insert.")
        return 0

    upsert_sql = """
        INSERT INTO sub_niches (
            sub_niche_id, parent_niche_id, name, slug, description,
            visual_characteristics, canonical_keywords, embedding, embedding_version,
            created_at, updated_at
        ) VALUES (
            %(sub_niche_id)s, %(parent_niche_id)s, %(name)s, %(slug)s, %(description)s,
            %(visual_characteristics)s, %(canonical_keywords)s, %(embedding)s,
            %(embedding_version)s, NOW(), NOW()
        )
        ON CONFLICT (sub_niche_id) DO UPDATE SET
            parent_niche_id       = EXCLUDED.parent_niche_id,
            name                  = EXCLUDED.name,
            slug                  = EXCLUDED.slug,
            description           = EXCLUDED.description,
            visual_characteristics = EXCLUDED.visual_characteristics,
            canonical_keywords    = EXCLUDED.canonical_keywords,
            embedding             = EXCLUDED.embedding,
            embedding_version     = EXCLUDED.embedding_version,
            updated_at            = NOW()
    """

    with pg_conn.transaction():
        cur = pg_conn.cursor()
        for row in rows:
            row_copy = dict(row)
            if row_copy["visual_characteristics"] is not None:
                row_copy["visual_characteristics"] = json.dumps(
                    row_copy["visual_characteristics"]
                )
            cur.execute(upsert_sql, row_copy)

    count = len(rows)
    if skipped:
        log.warning("  Skipped %d sub_niche docs: %s", len(skipped), skipped)
    log.info("Sub-niches: %d inserted/updated.", count)
    return count


# ─────────────────────────────────────────────────────────────────────────────
# Seed: creators
# ─────────────────────────────────────────────────────────────────────────────

def seed_creators(fs_db: Any, pg_conn: Any) -> int:
    """
    Seed creators from Firestore global_creators into Postgres creators table.
    Pipeline counters and stats are RESET to defaults.
    Returns the number of rows inserted/updated.
    """
    log.info("Fetching global_creators from Firestore ...")
    docs = list(fs_db.collection("global_creators").stream())
    log.info("  Found %d creator documents.", len(docs))

    skipped: list[str] = []
    rows: list[dict[str, Any]] = []

    for doc in docs:
        d = doc.to_dict()

        creator_id = d.get("creator_id") or doc.id
        platform = _coerce_platform(d.get("platform"))

        if not creator_id:
            log.warning("Skipping creator doc %r: no creator_id.", doc.id)
            skipped.append(doc.id)
            continue

        if not platform:
            log.warning("Skipping creator %r: no platform.", creator_id)
            skipped.append(creator_id)
            continue

        # Validate platform against the CHECK constraint
        valid_platforms = {"instagram", "tiktok", "youtube", "x"}
        if platform not in valid_platforms:
            log.warning(
                "Skipping creator %r: invalid platform %r (not in %s).",
                creator_id, platform, valid_platforms,
            )
            skipped.append(creator_id)
            continue

        # Handle: use explicit field, or derive from creator_id
        handle = d.get("handle")
        if not handle:
            handle = _derive_handle(creator_id)

        # creator_name: Firestore uses 'creator_name' (primary) or 'channel_name' as fallback
        creator_name = d.get("creator_name") or d.get("channel_name")

        # date_added: string ISO in Firestore
        date_added = _parse_date_added(d.get("date_added"))

        rows.append(
            {
                "creator_id": creator_id,
                "platform": platform,
                "handle": handle,
                "creator_name": creator_name,
                "bio": d.get("bio"),
                "profile_picture_url": d.get("profile_picture_url"),
                "channel_meta": _coerce_jsonb(d.get("channel_meta")),
                "status": "active",
                "origin": d.get("origin"),
                "niche_id": d.get("niche_id"),  # may be None — that is fine
                # Counters/stats RESET to defaults
                "videos_scraped": 0,
                "videos_accepted": 0,
                "library_count": 0,
                "total_views": 0,
                "viral_hits": 0,
                "avg_views": None,
                "acceptance_rate": None,
                "filter_stats": None,
                "post_frequency": None,
                "creator_score": None,
                "date_added": date_added,
                "last_scraped_at": None,
            }
        )

    if not rows:
        log.warning("No valid creator rows to insert.")
        return 0

    upsert_sql = """
        INSERT INTO creators (
            creator_id, platform, handle, creator_name, bio, profile_picture_url,
            channel_meta, status, origin, niche_id,
            videos_scraped, videos_accepted, library_count, total_views, viral_hits,
            avg_views, acceptance_rate, filter_stats, post_frequency, creator_score,
            date_added, last_scraped_at,
            created_at, updated_at
        ) VALUES (
            %(creator_id)s, %(platform)s, %(handle)s, %(creator_name)s, %(bio)s,
            %(profile_picture_url)s, %(channel_meta)s, %(status)s, %(origin)s,
            %(niche_id)s,
            %(videos_scraped)s, %(videos_accepted)s, %(library_count)s, %(total_views)s,
            %(viral_hits)s, %(avg_views)s, %(acceptance_rate)s, %(filter_stats)s,
            %(post_frequency)s, %(creator_score)s,
            %(date_added)s, %(last_scraped_at)s,
            NOW(), NOW()
        )
        ON CONFLICT (creator_id) DO UPDATE SET
            platform              = EXCLUDED.platform,
            handle                = EXCLUDED.handle,
            creator_name          = EXCLUDED.creator_name,
            bio                   = EXCLUDED.bio,
            profile_picture_url   = EXCLUDED.profile_picture_url,
            channel_meta          = EXCLUDED.channel_meta,
            status                = EXCLUDED.status,
            origin                = EXCLUDED.origin,
            niche_id              = EXCLUDED.niche_id,
            date_added            = EXCLUDED.date_added,
            updated_at            = NOW()
    """

    with pg_conn.transaction():
        cur = pg_conn.cursor()
        for row in rows:
            row_copy = dict(row)
            if row_copy["channel_meta"] is not None:
                row_copy["channel_meta"] = json.dumps(row_copy["channel_meta"])
            if row_copy["filter_stats"] is not None:
                row_copy["filter_stats"] = json.dumps(row_copy["filter_stats"])
            cur.execute(upsert_sql, row_copy)

    count = len(rows)
    if skipped:
        log.warning("  Skipped %d creator docs: %s", len(skipped), skipped)
    log.info("Creators: %d inserted/updated.", count)
    return count


# ─────────────────────────────────────────────────────────────────────────────
# FK integrity check
# ─────────────────────────────────────────────────────────────────────────────

def check_fk_integrity(pg_conn: Any) -> None:
    """
    Warn if any creator.niche_id references a niche that does not exist.
    Does not raise — FK violations are logged as warnings only.
    """
    sql = """
        SELECT COUNT(*)
        FROM creators c
        WHERE c.niche_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM niches n WHERE n.niche_id = c.niche_id
          )
    """
    with pg_conn.cursor() as cur:
        cur.execute(sql)
        count = cur.fetchone()[0]

    if count > 0:
        log.warning(
            "FK integrity warning: %d creator(s) have a niche_id referencing a "
            "non-existent niche. These are pre-existing data issues in Firestore.",
            count,
        )
    else:
        log.info("FK integrity OK: all creator niche_id references are valid.")


# ─────────────────────────────────────────────────────────────────────────────
# Validation: row counts
# ─────────────────────────────────────────────────────────────────────────────

def log_row_counts(pg_conn: Any) -> None:
    """Log final row counts for the three seeded tables."""
    tables = ["niches", "sub_niches", "creators"]
    with pg_conn.cursor() as cur:
        for table in tables:
            cur.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 — table name is from allowlist
            count = cur.fetchone()[0]
            log.info("  %s: %d rows", table, count)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=== DoomSwipe Firestore → Postgres seed ===")

    # 1. Connect to both data stores
    database_url = _get_database_url()
    log.info("Initialising Firebase ...")
    fs_db = _init_firebase()
    log.info("Connecting to Postgres ...")
    pg_conn = _open_pg_connection(database_url)

    try:
        # 2. Seed in FK dependency order
        log.info("--- Step 1/3: niches ---")
        seed_niches(fs_db, pg_conn)

        log.info("--- Step 2/3: sub_niches ---")
        seed_sub_niches(fs_db, pg_conn)

        log.info("--- Step 3/3: creators ---")
        seed_creators(fs_db, pg_conn)

        # 3. FK integrity check
        log.info("--- FK integrity check ---")
        check_fk_integrity(pg_conn)

        # 4. Final row count summary
        log.info("=== Final row counts ===")
        log_row_counts(pg_conn)

        log.info("=== Seed complete. ===")

    finally:
        pg_conn.close()


if __name__ == "__main__":
    main()

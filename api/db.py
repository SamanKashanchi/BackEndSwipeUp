"""Postgres connection pool. Opened once on app startup, closed on shutdown.

Pool sizing rationale: Cloud Run instances are small (1-2 vCPU, low concurrency
per instance because Cloud Run scales horizontally). A pool of 1-3 connections
per instance is plenty. Supabase's session pooler accepts the connections.
"""
from __future__ import annotations

import os

from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def _configure_connection(conn) -> None:
    """Register pgvector on every pooled connection so SELECTs of vector columns
    return numpy.ndarray instead of strings."""
    register_vector(conn)


def open_pool() -> ConnectionPool:
    global _pool
    if _pool is not None:
        return _pool

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Set it in Backend/.env (local) or as a "
            "Cloud Run env var (prod)."
        )

    _pool = ConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=3,
        timeout=10,
        kwargs={"autocommit": False},
        configure=_configure_connection,
    )
    _pool.wait()
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised. Did the lifespan run?")
    return _pool

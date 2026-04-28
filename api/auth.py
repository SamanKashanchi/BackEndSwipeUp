"""Firebase ID token verification + per-account ownership check.

Token flow:
  1. Browser gets a fresh JWT via `await currentUser.getIdToken()`.
  2. Browser sends it as `Authorization: Bearer <jwt>`.
  3. We verify the signature against Google's public keys (firebase_admin
     downloads + caches them automatically).
  4. We extract the Firebase UID from the decoded token.
  5. For account-scoped endpoints we then check `accounts.user_id == uid`
     in Postgres.

On Cloud Run inside the same GCP project as Firebase Auth, no service
account key is needed — Application Default Credentials picks up the
runtime service account. Locally, run once:
    gcloud auth application-default login
"""
from __future__ import annotations

import logging
import os

import firebase_admin
from fastapi import Header, HTTPException, status
from firebase_admin import auth as firebase_auth

from db import get_pool

log = logging.getLogger(__name__)

_initialized = False


def init_firebase() -> None:
    """Initialise the firebase_admin SDK exactly once. Safe to call repeatedly."""
    global _initialized
    if _initialized:
        return
    try:
        firebase_admin.initialize_app(
            options={"projectId": os.getenv("FIREBASE_PROJECT_ID", "reel-swipe-app")}
        )
    except ValueError:
        # Already initialised by some other code path (e.g. tests).
        pass
    _initialized = True


async def verify_id_token(authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency. Returns the verified Firebase UID, or raises 401.

    Expected header: `Authorization: Bearer <id_token>`.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )

    token = authorization.split(" ", 1)[1].strip()
    try:
        decoded = firebase_auth.verify_id_token(token)
    except firebase_auth.ExpiredIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="id token expired",
        )
    except firebase_auth.InvalidIdTokenError as exc:
        log.warning("invalid id token: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid id token",
        )
    except Exception as exc:
        # Most commonly: DefaultCredentialsError when running locally without
        # `gcloud auth application-default login`. Cloud Run's runtime service
        # account provides ADC automatically, so this branch is a local-only
        # foot-gun. Always return 401 — never leak the underlying error.
        log.warning("id token verification crashed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="could not verify id token",
        )

    uid = decoded.get("uid")
    if not uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token missing uid",
        )
    return uid


def assert_account_owner(account_id: str, uid: str) -> None:
    """Raise 403 unless the given Firebase UID owns the given account.

    Source of truth is the Postgres `accounts` table. If the account doesn't
    exist in Postgres, return 404 — the cleanup path is a no-op for unknown
    accounts (e.g. legacy onboardings that never wrote to Postgres).
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM accounts WHERE account_id = %s",
                (account_id,),
            )
            row = cur.fetchone()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="account not found in postgres",
        )
    if row[0] != uid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not the account owner",
        )

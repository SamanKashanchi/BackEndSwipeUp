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
from firebase_admin import auth as firebase_auth, firestore as firebase_firestore

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

    Source of truth is **Firestore** — `users/{uid}/Accounts/{account_id}`.
    Postgres is a shadow that gets populated mid-onboarding (via the
    POST /account/{id}/niches upsert) and intentionally lags Firestore by
    a few seconds during sign-up. Checking Postgres here would 404 every
    new account before they reach the niche-confirm screen, which broke
    the Phase 2 inspirations-seeding loop.

    A simple Firestore document existence check is enough: if a doc lives
    at users/{uid}/Accounts/{account_id} then that uid owns that account.
    Firebase security rules guarantee no other uid can write into that
    path.
    """
    db = firebase_firestore.client()
    snap = db.collection("users").document(uid).collection("Accounts").document(account_id).get()
    if not snap.exists:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not the account owner",
        )

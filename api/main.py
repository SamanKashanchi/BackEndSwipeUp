"""FastAPI service for consumer-facing operations against Postgres.

First endpoint: DELETE /account/{account_id}
  Removes the Postgres row in `accounts`. ON DELETE CASCADE on
  account_summary_embeddings, account_frame_embeddings, and interactions
  takes care of the rest in a single statement.

Future endpoints (per CONTEXT.md §5.1):
  GET  /feed?account_id=X&limit=50&cursor=...
  POST /swipe
  POST /view
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

# Load Backend/.env when running locally. On Cloud Run the env vars are
# injected by the runtime so load_dotenv is a no-op.
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

from auth import assert_account_owner, init_firebase, verify_id_token
from db import close_pool, get_pool, open_pool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup: opening Postgres pool + firebase admin")
    open_pool()
    init_firebase()
    yield
    log.info("shutdown: closing Postgres pool")
    close_pool()


app = FastAPI(title="SwipeUp API", lifespan=lifespan)

# CORS — allow the live site, the Vercel previews, and local dev.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://([a-z0-9-]+\.)?getswipeup\.com|https://.*\.vercel\.app|http://localhost:\d+",
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    """Readiness probe. Returns db_configured + a quick SELECT 1 round-trip."""
    db_ok = False
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                db_ok = cur.fetchone()[0] == 1
    except Exception as exc:
        log.warning("health: db check failed: %s", exc)

    return {
        "status": "ok",
        "db_ok": db_ok,
        "project_id": os.getenv("FIREBASE_PROJECT_ID", "reel-swipe-app"),
    }


@app.delete("/account/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    account_id: str,
    uid: str = Depends(verify_id_token),
):
    """Remove the account's Postgres footprint.

    Cascades automatically:
      accounts → account_summary_embeddings (FK ON DELETE CASCADE)
              → account_frame_embeddings   (FK ON DELETE CASCADE)
              → interactions               (FK ON DELETE CASCADE)

    Firestore cleanup is the frontend's responsibility — this endpoint
    only owns the Postgres side. Auth: caller must be the account's owner.
    """
    assert_account_owner(account_id, uid)

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM accounts WHERE account_id = %s", (account_id,))
            deleted = cur.rowcount
        conn.commit()

    log.info("delete_account: account_id=%s uid=%s deleted_rows=%s", account_id, uid, deleted)
    if deleted == 0:
        # Race: ownership check passed but row vanished between SELECT and DELETE.
        # Treat as success — caller's intent (state where account doesn't exist) is satisfied.
        log.info("delete_account: account_id=%s already gone, treating as success", account_id)

    return None

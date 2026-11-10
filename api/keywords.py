"""Per-channel keywords, stored in Postgres account_keywords.

GET    /account/{account_id}/keywords           list, oldest first
POST   /account/{account_id}/keywords           add one ({"keyword": "..."})
DELETE /account/{account_id}/keywords/{keyword} remove one

A keyword is searched on the channel's own platform (accounts.platform).
The acquire job (ContentEngine refresh_keywords.py) pulls each distinct
(keyword, platform) and links the posts it finds in video_keywords; /feed
shows them. Adding a keyword nobody has pulled yet starts the job right away,
the same way adding a new creator does.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

import refresh_trigger
from auth import assert_account_owner, verify_id_token
from db import get_pool

log = logging.getLogger("swipeup.keywords")

# Same limit on every plan for now.
MAX_KEYWORDS_PER_ACCOUNT = 5
MAX_KEYWORD_LEN = 50

router = APIRouter()


def normalize_keyword(raw: str) -> str:
    """Lowercase, drop leading '#', collapse whitespace. '' if nothing is left.

    Instagram searches hashtags, which can't contain spaces, so the scraper
    joins words there; the stored keyword keeps the user's spacing."""
    s = (raw or "").strip().lstrip("#").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s[:MAX_KEYWORD_LEN].strip()


def _account_platform(cur, account_id: str) -> str:
    cur.execute("SELECT platform FROM accounts WHERE account_id = %s", (account_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="account not found")
    return row[0]


def _list(cur, account_id: str) -> list[dict]:
    cur.execute(
        "SELECT keyword, platform, created_at, last_pulled_at"
        "  FROM account_keywords"
        " WHERE account_id = %s AND active"
        " ORDER BY created_at, keyword",
        (account_id,),
    )
    return [
        {
            "keyword": r[0],
            "platform": r[1],
            "created_at": r[2].isoformat() if r[2] else None,
            "last_pulled_at": r[3].isoformat() if r[3] else None,
        }
        for r in cur.fetchall()
    ]


@router.get("/account/{account_id}/keywords")
def list_keywords(account_id: str, uid: str = Depends(verify_id_token)) -> dict:
    assert_account_owner(account_id, uid)
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            keywords = _list(cur, account_id)
    return {"keywords": keywords, "max": MAX_KEYWORDS_PER_ACCOUNT}


class AddKeywordBody(BaseModel):
    keyword: str = Field(..., min_length=1, max_length=100)


@router.post("/account/{account_id}/keywords", status_code=status.HTTP_201_CREATED)
def add_keyword(
    account_id: str,
    body: AddKeywordBody,
    uid: str = Depends(verify_id_token),
) -> dict:
    assert_account_owner(account_id, uid)
    keyword = normalize_keyword(body.keyword)
    if not keyword:
        raise HTTPException(status_code=400, detail="invalid keyword")

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            platform = _account_platform(cur, account_id)
            # Lock this account's rows so two fast adds can't both pass the cap.
            cur.execute(
                "SELECT keyword FROM account_keywords"
                " WHERE account_id = %s AND active FOR UPDATE",
                (account_id,),
            )
            current = {r[0] for r in cur.fetchall()}
            if keyword not in current and len(current) >= MAX_KEYWORDS_PER_ACCOUNT:
                raise HTTPException(
                    status_code=409,
                    detail=f"keyword limit reached ({MAX_KEYWORDS_PER_ACCOUNT})",
                )
            cur.execute(
                "INSERT INTO account_keywords (account_id, keyword, platform)"
                " VALUES (%s, %s, %s)"
                " ON CONFLICT (account_id, keyword, platform) DO UPDATE SET active = TRUE",
                (account_id, keyword, platform),
            )
            # Does anyone already have fresh posts for this search?
            cur.execute(
                "SELECT 1 FROM account_keywords"
                " WHERE keyword = %s AND platform = %s AND last_pulled_at IS NOT NULL"
                " LIMIT 1",
                (keyword, platform),
            )
            never_pulled = cur.fetchone() is None
            keywords = _list(cur, account_id)
        conn.commit()

    if never_pulled:
        refresh_trigger.request_refresh(reason=f"keyword:{platform}:{keyword}")

    log.info("add_keyword: account=%s keyword=%r platform=%s", account_id, keyword, platform)
    return {"keyword": keyword, "platform": platform, "keywords": keywords}


@router.delete(
    "/account/{account_id}/keywords/{keyword}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_keyword(account_id: str, keyword: str, uid: str = Depends(verify_id_token)):
    """Delete the row. Videos already found stay in `videos` (other accounts
    may use the same keyword) but drop out of this account's feed."""
    assert_account_owner(account_id, uid)
    kw = normalize_keyword(keyword)
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM account_keywords WHERE account_id = %s AND keyword = %s",
                (account_id, kw),
            )
        conn.commit()
    return None


__all__ = ["router", "normalize_keyword", "MAX_KEYWORDS_PER_ACCOUNT"]

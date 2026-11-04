"""Tests for the MVP creator feed's batch assembly (no DB)."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import feed


def _cand(video_id: str, creator_id: str) -> dict:
    return {
        "video_id": video_id, "niche_id": None, "creator_id": creator_id,
        "platform": "tiktok", "public_url": f"https://x/{video_id}.mp4",
        "display_url": None, "caption": "", "hashtags": [], "video_duration": 10.0,
        "views": 0, "likes": 0, "comments": 0, "shares": 0,
        "time_posted": None, "scraped_at": None,
        "creator_handle": creator_id, "creator_name": None, "creator_profile_pic": None,
        "score": None, "score_semantic": None, "score_structure": None, "score_text": None,
    }


def _pool(spec: list[tuple[str, str]]) -> list[dict]:
    n = len(spec)
    out = []
    for i, (vid, cid) in enumerate(spec):
        c = _cand(vid, cid)
        c["score"] = (n - i) / n
        out.append(c)
    return out


@pytest.fixture
def patch_db(monkeypatch):
    def _apply(pool, exists=True, gathering=()):
        monkeypatch.setattr(feed, "_account_exists", lambda _a: exists)
        monkeypatch.setattr(feed, "_fetch_candidates", lambda _a, _e, _l: list(pool))
        monkeypatch.setattr(feed, "_fetch_gathering", lambda _a: list(gathering))
    return _apply


def test_empty_pool_returns_empty_batch(patch_db):
    patch_db([])
    out = feed.build_batch("acct", {}, [], 15)
    assert out["batch"] == []
    assert out["batch_size"] == 0


def test_unknown_account_404(patch_db):
    patch_db([], exists=False)
    with pytest.raises(HTTPException) as e:
        feed.build_batch("acct", {}, [], 15)
    assert e.value.status_code == 404


def test_newest_first_when_single_creator(patch_db):
    patch_db(_pool([("v1", "a"), ("v2", "a"), ("v3", "a")]))
    out = feed.build_batch("acct", {}, [], 15)
    assert [b["video"]["video_id"] for b in out["batch"]] == ["v1", "v2", "v3"]
    assert out["session_creator_counts"] == {"a": 3}


def test_cooldown_interleaves_creators(patch_db):
    # Creator a posted the 4 newest videos; b's are older. Cooldown should
    # stop a from taking every slot back to back.
    patch_db(_pool([("a1", "a"), ("a2", "a"), ("a3", "a"), ("a4", "a"),
                    ("b1", "b"), ("b2", "b")]))
    out = feed.build_batch("acct", {}, [], 4)
    creators = [b["video"]["creator"]["creator_id"] for b in out["batch"]]
    assert creators[0] == "a"
    assert "b" in creators


def test_limit_and_no_duplicates(patch_db):
    patch_db(_pool([(f"v{i}", f"c{i % 3}") for i in range(30)]))
    out = feed.build_batch("acct", {}, [], 10)
    ids = [b["video"]["video_id"] for b in out["batch"]]
    assert len(ids) == 10
    assert len(set(ids)) == 10
    assert all(b["slot"] == feed.SLOT_LABEL for b in out["batch"])


def test_response_keeps_legacy_keys(patch_db):
    patch_db(_pool([("v1", "a")]))
    out = feed.build_batch("acct", {}, [], 15)
    for key in ("batch", "batch_size", "mix_used", "unseen_inspiration_count",
                "stage1_anchor", "rerank_applied", "fallback_slots",
                "session_creator_counts"):
        assert key in out
    assert out["batch"][0]["video"]["url"] == "https://x/v1.mp4"


def test_gathering_reported_with_empty_batch(patch_db):
    patch_db([], gathering=[{"creator_id": "tiktok_new", "platform": "tiktok", "handle": "new"}])
    out = feed.build_batch("acct", {}, [], 15)
    assert out["batch"] == []
    assert out["gathering"] == [{"creator_id": "tiktok_new", "platform": "tiktok", "handle": "new"}]


def test_gathering_empty_by_default(patch_db):
    patch_db(_pool([("v1", "a")]))
    assert feed.build_batch("acct", {}, [], 15)["gathering"] == []

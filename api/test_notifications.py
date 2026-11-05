"""Tests for the pure parts of notifications (no DB, no network)."""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import notifications as n  # noqa: E402


def test_safe_timezone_falls_back_to_utc():
    assert n.safe_timezone("America/Los_Angeles") == "America/Los_Angeles"
    assert n.safe_timezone("Not/AZone") == "UTC"
    assert n.safe_timezone(None) == "UTC"
    assert n.safe_timezone("") == "UTC"


@pytest.mark.parametrize(
    "count,handles,title,body",
    [
        (1, ["zachking"], "1 new video to swipe", "Fresh from @zachking."),
        (5, ["a", "b"], "5 new videos to swipe", "Fresh from @a and @b."),
        (9, ["a", "b", "c", "d"], "9 new videos to swipe", "Fresh from @a, @b and 2 more."),
        (3, [], "3 new videos to swipe", "Fresh from your creators."),
    ],
)
def test_new_videos_message(count, handles, title, body):
    assert n.new_videos_message(count, handles) == (title, body)


def test_nudge_due_only_in_the_local_hour_once_a_day():
    # 01:30 UTC on Nov 5 is 17:30 on Nov 4 in Los Angeles (PST, UTC-8).
    now = datetime(2026, 11, 5, 1, 30, tzinfo=timezone.utc)
    la = "America/Los_Angeles"
    assert n.nudge_due(now, la, 17, None) == date(2026, 11, 4)
    assert n.nudge_due(now, la, 18, None) is None            # wrong hour
    assert n.nudge_due(now, la, 17, date(2026, 11, 4)) is None  # already sent today
    assert n.nudge_due(now, la, 17, date(2026, 11, 3)) == date(2026, 11, 4)
    assert n.nudge_due(now, "Bad/Zone", 1, None) == date(2026, 11, 5)  # UTC fallback


def test_local_midnight_utc():
    assert n.local_midnight_utc(date(2026, 11, 4), "America/Los_Angeles") == datetime(
        2026, 11, 4, 8, 0, tzinfo=timezone.utc
    )


def test_cron_secret(monkeypatch):
    monkeypatch.setenv("NOTIFY_CRON_SECRET", "s3cret")
    n._check_cron_secret("s3cret")
    for bad in (None, "", "nope"):
        with pytest.raises(n.HTTPException):
            n._check_cron_secret(bad)
    monkeypatch.delenv("NOTIFY_CRON_SECRET")
    with pytest.raises(n.HTTPException):
        n._check_cron_secret("anything")  # unset secret never authorizes

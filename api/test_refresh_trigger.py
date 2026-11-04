"""Tests for refresh_trigger (no network)."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import refresh_trigger  # noqa: E402


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    monkeypatch.setattr(refresh_trigger, "_last_trigger", 0.0)
    monkeypatch.delenv("REFRESH_TRIGGER_ENABLED", raising=False)
    calls = []
    monkeypatch.setattr(refresh_trigger, "_run_job", lambda: calls.append(1))
    return calls


def test_triggers_then_throttles(reset, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(refresh_trigger.time, "monotonic", lambda: clock[0])
    assert refresh_trigger.request_refresh("a") is True
    clock[0] += 5
    assert refresh_trigger.request_refresh("b") is False  # onboarding burst coalesces
    clock[0] += refresh_trigger.TRIGGER_MIN_INTERVAL_S
    assert refresh_trigger.request_refresh("c") is True
    assert len(reset) == 2


def test_disabled_by_env(reset, monkeypatch):
    monkeypatch.setenv("REFRESH_TRIGGER_ENABLED", "0")
    assert refresh_trigger.request_refresh("a") is False
    assert reset == []


def test_errors_are_swallowed(monkeypatch):
    def boom():
        raise RuntimeError("403 permission denied")
    monkeypatch.setattr(refresh_trigger, "_run_job", boom)
    assert refresh_trigger.request_refresh("a") is False  # no exception


def test_job_url_from_env(monkeypatch):
    assert refresh_trigger.job_run_url() == (
        "https://run.googleapis.com/v2/projects/reel-swipe-app/locations/us-central1"
        "/jobs/orchestrator-acquire:run")
    monkeypatch.setenv("REFRESH_JOB_NAME", "other-job")
    assert refresh_trigger.job_run_url().endswith("/jobs/other-job:run")

"""Start the creator-refresh Cloud Run Job right away when a creator is added.

Without this a newly added creator waits for the scheduled acquire run (every
2h). With it, add-creator calls the Cloud Run Admin API `jobs/<name>:run`, so
the scraper starts within seconds.

The call runs inline, not in a background thread, because Cloud Run throttles
CPU once the response is sent. It is cheap (one HTTPS call, ~0.5s) and never
fails the request: any error is logged and swallowed, and the scheduled run
still picks the creator up.

Calls are throttled to one per TRIGGER_MIN_INTERVAL_S per instance. Onboarding
adds several creators back to back; the job takes longer than that to boot, so
the first trigger's run already sees all of them. Overlapping runs are safe:
refresh_creators.py claims each creator before pulling it.

Env:
  REFRESH_TRIGGER_ENABLED   "0"/"false" to turn off (default on)
  REFRESH_JOB_NAME          default orchestrator-acquire
  REFRESH_JOB_PROJECT       default reel-swipe-app
  REFRESH_JOB_REGION        default us-central1
"""
from __future__ import annotations

import logging
import os
import threading
import time

import httpx

log = logging.getLogger("swipeup.refresh_trigger")

TRIGGER_MIN_INTERVAL_S = 15.0
HTTP_TIMEOUT_S = 5.0
_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

_lock = threading.Lock()
_last_trigger = 0.0
_creds = None


def _enabled() -> bool:
    return os.environ.get("REFRESH_TRIGGER_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def job_run_url() -> str:
    project = os.environ.get("REFRESH_JOB_PROJECT", "reel-swipe-app")
    region = os.environ.get("REFRESH_JOB_REGION", "us-central1")
    job = os.environ.get("REFRESH_JOB_NAME", "orchestrator-acquire")
    return f"https://run.googleapis.com/v2/projects/{project}/locations/{region}/jobs/{job}:run"


def _access_token() -> str:
    """Token for the service's own service account (metadata server on Cloud
    Run, gcloud ADC locally). Cached; google-auth refreshes it when expired."""
    global _creds
    import google.auth
    import google.auth.transport.requests

    if _creds is None:
        _creds, _ = google.auth.default(scopes=[_SCOPE])
    if not _creds.valid:
        _creds.refresh(google.auth.transport.requests.Request())
    return _creds.token


def _run_job() -> None:
    r = httpx.post(
        job_run_url(),
        headers={"Authorization": f"Bearer {_access_token()}"},
        json={},
        timeout=HTTP_TIMEOUT_S,
    )
    r.raise_for_status()


def request_refresh(reason: str = "") -> bool:
    """Start the refresh job unless disabled or one was started in the last
    TRIGGER_MIN_INTERVAL_S. Returns True if a run was requested. Never raises."""
    global _last_trigger
    if not _enabled():
        return False
    with _lock:
        now = time.monotonic()
        if _last_trigger and now - _last_trigger < TRIGGER_MIN_INTERVAL_S:
            return False
        _last_trigger = now
    try:
        _run_job()
        log.info("refresh job started (%s)", reason)
        return True
    except Exception as exc:  # noqa: BLE001 — must never break add-creator
        log.warning("refresh job trigger failed (%s): %s: %s", reason, type(exc).__name__, exc)
        return False

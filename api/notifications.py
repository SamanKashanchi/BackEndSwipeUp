"""Web Push notifications.

Replaces the Firebase functions pushSubscribe + checkScheduledPosts. Three
kinds of notification, all sent by POST /internal/notifications/run, which
pg_cron calls every 5 minutes (migration 0032):

  scheduled posts  "Get ready!" ~30 min and "Time to post!" ~5 min before a
                   post on the user's Firestore Schedule (same rules as the old
                   checkScheduledPosts).
  new videos       N new videos from the creators the user tracks, at most one
                   every NEW_VIDEOS_COOLDOWN.
  daily nudge      once a day at the user's local nudge_hour, only if they
                   haven't swiped yet that day.

Endpoints for the app (Firebase ID token required, user from the token):
  GET    /notifications/config         VAPID public key (no auth)
  PUT    /notifications/subscription   save this device
  DELETE /notifications/subscription   forget this device
  GET    /notifications/settings       switches + device count
  PUT    /notifications/settings       change switches
  POST   /notifications/test           send a test to the user's devices

Env:
  VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY   key pair (npx web-push generate-vapid-keys)
  VAPID_SUBJECT                         default mailto:contact@getswipeup.com
  NOTIFY_CRON_SECRET                    must match the vault secret pg_cron sends
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from auth import verify_id_token
from db import get_pool

log = logging.getLogger("swipeup.notifications")

router = APIRouter()

NEW_VIDEOS_COOLDOWN = timedelta(hours=6)
PUSH_TTL_S = 60 * 60
APP_ICON = "/icons/icon-192.png"


# ── helpers ──────────────────────────────────────────────────────────────────

def _vapid_public_key() -> str:
    return os.environ.get("VAPID_PUBLIC_KEY", "").strip()


def _vapid_private_key() -> str:
    return os.environ.get("VAPID_PRIVATE_KEY", "").strip()


def _vapid_subject() -> str:
    return os.environ.get("VAPID_SUBJECT", "mailto:contact@getswipeup.com").strip()


def push_configured() -> bool:
    return bool(_vapid_public_key() and _vapid_private_key())


def safe_timezone(name: str | None) -> str:
    """An IANA zone name we can load, else UTC."""
    if not name:
        return "UTC"
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"


def new_videos_message(count: int, handles: list[str]) -> tuple[str, str]:
    """Title + body for the new-videos notification."""
    handles = [h for h in handles if h]
    shown = [f"@{h}" for h in handles[:2]]
    rest = len(handles) - len(shown)
    if not shown:
        who = "your creators"
    elif rest > 0:
        who = f"{', '.join(shown)} and {rest} more"
    else:
        who = " and ".join(shown)
    noun = "video" if count == 1 else "videos"
    return f"{count} new {noun} to swipe", f"Fresh from {who}."


def nudge_due(now_utc: datetime, tz_name: str, nudge_hour: int, last_nudge_on: date | None) -> date | None:
    """The user's local date if their daily nudge should go out now, else None."""
    local = now_utc.astimezone(ZoneInfo(safe_timezone(tz_name)))
    if local.hour != nudge_hour:
        return None
    if last_nudge_on is not None and last_nudge_on >= local.date():
        return None
    return local.date()


def local_midnight_utc(local_day: date, tz_name: str) -> datetime:
    tz = ZoneInfo(safe_timezone(tz_name))
    return datetime.combine(local_day, time(0, 0), tzinfo=tz).astimezone(timezone.utc)


def send_to_user(user_id: str, title: str, body: str, url: str, tag: str) -> tuple[int, int]:
    """Push to every device of one user. Drops devices the push service says
    are gone (404/410). Returns (sent, failed)."""
    from pywebpush import WebPushException, webpush

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = %s",
                (user_id,),
            )
            subs = cur.fetchall()

    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag, "icon": APP_ICON})
    sent = failed = 0
    gone: list[int] = []
    for sub_id, endpoint, p256dh, auth in subs:
        try:
            webpush(
                subscription_info={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
                data=payload,
                vapid_private_key=_vapid_private_key(),
                vapid_claims={"sub": _vapid_subject()},
                ttl=PUSH_TTL_S,
                headers={"Urgency": "high"},
                timeout=10,
            )
            sent += 1
        except WebPushException as exc:
            failed += 1
            code = getattr(exc.response, "status_code", None)
            if code in (404, 410):
                gone.append(sub_id)
            log.warning("push to sub %s failed: %s %s", sub_id, code, exc)
        except Exception as exc:  # network etc.: keep the device, try next run
            failed += 1
            log.warning("push to sub %s failed: %s: %s", sub_id, type(exc).__name__, exc)

    if gone:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM push_subscriptions WHERE id = ANY(%s)", (gone,))
            conn.commit()
    return sent, failed


def _ensure_settings(cur, user_id: str, tz_name: str | None = None) -> None:
    cur.execute(
        "INSERT INTO notification_settings (user_id, timezone) VALUES (%s, %s)"
        " ON CONFLICT (user_id) DO NOTHING",
        (user_id, safe_timezone(tz_name)),
    )
    if tz_name:
        cur.execute(
            "UPDATE notification_settings SET timezone = %s, updated_at = NOW()"
            " WHERE user_id = %s AND timezone <> %s",
            (safe_timezone(tz_name), user_id, safe_timezone(tz_name)),
        )


# ── app endpoints ────────────────────────────────────────────────────────────

class PushKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=512)
    auth: str = Field(min_length=1, max_length=512)


class SubscriptionBody(BaseModel):
    endpoint: str = Field(min_length=10, max_length=2048)
    keys: PushKeys
    timezone: str | None = Field(default=None, max_length=64)
    user_agent: str | None = Field(default=None, max_length=512)


class EndpointBody(BaseModel):
    endpoint: str = Field(min_length=10, max_length=2048)


class SettingsBody(BaseModel):
    new_videos: bool | None = None
    daily_nudge: bool | None = None
    nudge_hour: int | None = Field(default=None, ge=0, le=23)
    timezone: str | None = Field(default=None, max_length=64)


@router.get("/notifications/config")
def notifications_config():
    return {"vapid_public_key": _vapid_public_key(), "enabled": push_configured()}


@router.put("/notifications/subscription")
def save_subscription(body: SubscriptionBody, uid: str = Depends(verify_id_token)):
    if not body.endpoint.startswith("https://"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "endpoint must be https")
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            # One browser = one endpoint. If someone else signed in on this
            # browser before, the device now belongs to the current user.
            cur.execute(
                "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, user_agent)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (endpoint) DO UPDATE SET"
                "   user_id = EXCLUDED.user_id, p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth,"
                "   user_agent = EXCLUDED.user_agent, last_seen_at = NOW()",
                (uid, body.endpoint, body.keys.p256dh, body.keys.auth, body.user_agent),
            )
            _ensure_settings(cur, uid, body.timezone)
        conn.commit()
    return {"ok": True}


@router.delete("/notifications/subscription")
def delete_subscription(body: EndpointBody, uid: str = Depends(verify_id_token)):
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM push_subscriptions WHERE endpoint = %s AND user_id = %s",
                (body.endpoint, uid),
            )
        conn.commit()
    return {"ok": True}


def _read_settings(cur, uid: str) -> dict:
    cur.execute(
        "SELECT timezone, new_videos, daily_nudge, nudge_hour FROM notification_settings WHERE user_id = %s",
        (uid,),
    )
    row = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM push_subscriptions WHERE user_id = %s", (uid,))
    devices = cur.fetchone()[0]
    tz_name, new_videos, daily_nudge, nudge_hour = row or ("UTC", True, True, 18)
    return {
        "timezone": tz_name,
        "new_videos": new_videos,
        "daily_nudge": daily_nudge,
        "nudge_hour": nudge_hour,
        "devices": devices,
    }


@router.get("/notifications/settings")
def get_settings(uid: str = Depends(verify_id_token)):
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            return _read_settings(cur, uid)


@router.put("/notifications/settings")
def update_settings(body: SettingsBody, uid: str = Depends(verify_id_token)):
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            _ensure_settings(cur, uid, body.timezone)
            cur.execute(
                "UPDATE notification_settings SET"
                "   new_videos  = COALESCE(%s, new_videos),"
                "   daily_nudge = COALESCE(%s, daily_nudge),"
                "   nudge_hour  = COALESCE(%s, nudge_hour),"
                "   updated_at  = NOW()"
                " WHERE user_id = %s",
                (body.new_videos, body.daily_nudge, body.nudge_hour, uid),
            )
            result = _read_settings(cur, uid)
        conn.commit()
    return result


@router.post("/notifications/test")
def send_test(uid: str = Depends(verify_id_token)):
    if not push_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "push is not configured")
    sent, failed = send_to_user(uid, "SwipeUp", "Notifications are working.", "/home", "test")
    return {"sent": sent, "failed": failed}


# ── the cron run ─────────────────────────────────────────────────────────────

def _check_cron_secret(x_cron_secret: str | None) -> None:
    expected = os.environ.get("NOTIFY_CRON_SECRET", "")
    if not expected or not x_cron_secret or not hmac.compare_digest(expected, x_cron_secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad cron secret")


def run_new_videos(now: datetime) -> int:
    """One notification per user with unseen videos added since their marker,
    respecting the cooldown. Returns notifications sent."""
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.user_id,
                       COUNT(DISTINCT v.video_id)            AS n,
                       ARRAY_AGG(DISTINCT c.handle)          AS handles,
                       MAX(v.created_at)                     AS newest
                  FROM notification_settings s
                  JOIN accounts a          ON a.user_id = s.user_id
                  JOIN account_creators ac ON ac.account_id = a.account_id
                  JOIN videos v            ON v.creator_id = ac.creator_id
                                          AND v.created_at > s.new_videos_since
                  JOIN creators c          ON c.creator_id = v.creator_id
                 WHERE s.new_videos
                   AND (s.last_new_videos_at IS NULL OR s.last_new_videos_at < %s)
                   AND EXISTS (SELECT 1 FROM push_subscriptions p WHERE p.user_id = s.user_id)
                   AND NOT EXISTS (SELECT 1 FROM interactions i
                                    WHERE i.account_id = a.account_id AND i.video_id = v.video_id)
                 GROUP BY s.user_id
                """,
                (now - NEW_VIDEOS_COOLDOWN,),
            )
            rows = cur.fetchall()

    sent_total = 0
    for user_id, count, handles, newest in rows:
        # Claim first so an overlapping run can't double-send.
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE notification_settings"
                    "   SET last_new_videos_at = %s, new_videos_since = GREATEST(new_videos_since, %s)"
                    " WHERE user_id = %s AND (last_new_videos_at IS NULL OR last_new_videos_at < %s)"
                    " RETURNING user_id",
                    (now, newest, user_id, now - NEW_VIDEOS_COOLDOWN),
                )
                claimed = cur.fetchone() is not None
            conn.commit()
        if not claimed:
            continue
        title, body = new_videos_message(count, sorted(handles or []))
        sent, _ = send_to_user(user_id, title, body, "/home", "new-videos")
        sent_total += 1 if sent else 0
    return sent_total


def run_daily_nudge(now: datetime) -> int:
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.user_id, s.timezone, s.nudge_hour, s.last_nudge_on"
                "  FROM notification_settings s"
                " WHERE s.daily_nudge"
                "   AND EXISTS (SELECT 1 FROM push_subscriptions p WHERE p.user_id = s.user_id)"
            )
            candidates = cur.fetchall()

    sent_total = 0
    for user_id, tz_name, nudge_hour, last_nudge_on in candidates:
        local_day = nudge_due(now, tz_name, nudge_hour, last_nudge_on)
        if local_day is None:
            continue
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE notification_settings SET last_nudge_on = %s"
                    " WHERE user_id = %s AND (last_nudge_on IS NULL OR last_nudge_on < %s)"
                    " RETURNING user_id",
                    (local_day, user_id, local_day),
                )
                claimed = cur.fetchone() is not None
                cur.execute(
                    "SELECT EXISTS (SELECT 1 FROM interactions WHERE user_id = %s AND swiped_at >= %s)",
                    (user_id, local_midnight_utc(local_day, tz_name)),
                )
                swiped_today = cur.fetchone()[0]
            conn.commit()
        if not claimed or swiped_today:
            continue
        sent, _ = send_to_user(
            user_id, "Your feed is ready", "Take two minutes to swipe today's videos.", "/home", "daily-nudge"
        )
        sent_total += 1 if sent else 0
    return sent_total


def run_scheduled_posts(now: datetime) -> int:
    """Port of the old checkScheduledPosts: reads each subscribed user's
    Firestore schedules, flags notified30 / notified5 so each sends once."""
    from firebase_admin import firestore as fb_firestore
    from google.cloud.firestore_v1.base_query import FieldFilter

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT user_id FROM push_subscriptions")
            user_ids = [r[0] for r in cur.fetchall()]
    if not user_ids:
        return 0

    db = fb_firestore.client()
    window_start = now - timedelta(minutes=10)
    window_end = now + timedelta(minutes=35)
    sent_total = 0
    for uid in user_ids:
        for account in db.collection("users", uid, "Accounts").stream():
            posts = (
                db.collection("users", uid, "Accounts", account.id, "Schedule")
                .where(filter=FieldFilter("scheduledAt", ">=", window_start))
                .where(filter=FieldFilter("scheduledAt", "<=", window_end))
                .where(filter=FieldFilter("status", "==", "scheduled"))
                .stream()
            )
            for post in posts:
                data = post.to_dict() or {}
                scheduled = data.get("scheduledAt")
                if scheduled is None:
                    continue
                minutes_left = round((scheduled - now).total_seconds() / 60)
                name = data.get("title") or "Your video"
                if minutes_left > 10 and not data.get("notified30"):
                    flag, title, body = "notified30", "Get ready!", f'"{name}" is due in {minutes_left} min.'
                elif minutes_left <= 10 and not data.get("notified5"):
                    flag, title, body = "notified5", "Time to post!", f'"{name}" is due now. Go post it!'
                else:
                    continue
                post.reference.update({flag: True})
                sent, _ = send_to_user(uid, title, body, "/Schedule", f"post-{post.id}")
                sent_total += 1 if sent else 0
    return sent_total


@router.post("/internal/notifications/run")
def run_notifications(x_cron_secret: str | None = Header(default=None)):
    _check_cron_secret(x_cron_secret)
    if not push_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "push is not configured")
    now = datetime.now(timezone.utc)
    result = {}
    for name, job in (
        ("scheduled_posts", run_scheduled_posts),
        ("new_videos", run_new_videos),
        ("daily_nudge", run_daily_nudge),
    ):
        try:
            result[name] = job(now)
        except Exception as exc:  # one broken job must not stop the others
            log.exception("notification job %s failed", name)
            result[name] = f"error: {type(exc).__name__}"
    log.info("notifications run: %s", result)
    return result

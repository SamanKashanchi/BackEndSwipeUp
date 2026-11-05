-- ============================================================
-- 0032 — Push notifications (Web Push)
-- ============================================================
-- Replaces the Firebase functions pushSubscribe + checkScheduledPosts.
--
--   push_subscriptions      one row per browser/device that turned
--                           notifications on. Written only by swipeup-api,
--                           which takes user_id from the verified Firebase
--                           token (the old pushSubscribe trusted any uid).
--   notification_settings   per-user switches, timezone and "last sent"
--                           markers that stop repeat sends.
--
-- swipeup-api POST /internal/notifications/run sends whatever is due. Every
-- 5 minutes pg_cron calls it with the x-cron-secret header (section 3).
--
-- RLS is ON with no policies: only the API's service connection touches
-- these tables, never the browser's anon key.
-- ============================================================

-- 1. Tables ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id            BIGSERIAL PRIMARY KEY,
    user_id       TEXT        NOT NULL,
    endpoint      TEXT        NOT NULL UNIQUE,
    p256dh        TEXT        NOT NULL,
    auth          TEXT        NOT NULL,
    user_agent    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS push_subscriptions_user_idx ON push_subscriptions (user_id);

CREATE TABLE IF NOT EXISTS notification_settings (
    user_id             TEXT        PRIMARY KEY,
    timezone            TEXT        NOT NULL DEFAULT 'UTC',
    new_videos          BOOLEAN     NOT NULL DEFAULT TRUE,
    daily_nudge         BOOLEAN     NOT NULL DEFAULT TRUE,
    -- Local hour (0-23) the daily nudge goes out.
    nudge_hour          SMALLINT    NOT NULL DEFAULT 18 CHECK (nudge_hour BETWEEN 0 AND 23),
    -- "New videos" counts videos added after this. Starts at sign-up so the
    -- first alert isn't a backlog dump.
    new_videos_since    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_new_videos_at  TIMESTAMPTZ,
    last_nudge_on       DATE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE push_subscriptions    ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_settings ENABLE ROW LEVEL SECURITY;

-- Fast "new videos for this user's creators" lookups.
CREATE INDEX IF NOT EXISTS videos_creator_created_idx ON videos (creator_id, created_at DESC);

-- 2. Cron secret ------------------------------------------------------------
-- Run ONCE by hand before section 3, with the same value you set as
-- NOTIFY_CRON_SECRET on swipeup-api (do not commit the value):
--   select vault.create_secret('<random string>', 'notify_cron_secret');

-- 3. Schedule ---------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE EXTENSION IF NOT EXISTS pg_net;

SELECT cron.unschedule('swipeup-notifications')
 WHERE EXISTS (SELECT 1 FROM cron.job WHERE jobname = 'swipeup-notifications');

SELECT cron.schedule('swipeup-notifications', '*/5 * * * *', $cron$
  SELECT net.http_post(
    url     := 'https://swipeup-api-evjvpogc2q-uc.a.run.app/internal/notifications/run',
    headers := jsonb_build_object(
      'Content-Type',  'application/json',
      'x-cron-secret', (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = 'notify_cron_secret')
    ),
    body    := '{}'::jsonb,
    timeout_milliseconds := 60000
  );
$cron$);

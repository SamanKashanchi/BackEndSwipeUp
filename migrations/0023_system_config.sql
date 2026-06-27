-- ============================================================
-- 0023 — system_config: live-tunable knobs for the creator
--         pool management system (orchestrator)
-- ============================================================
-- Single key/value (JSONB) table holding every tunable for the
-- self-driving creator system. Read by the dashboard orchestrator
-- on each tick; editable from the dashboard. See CREATOR_SYSTEM.md §12.
--
-- SAFETY: orchestrator_enabled defaults FALSE and orchestrator_dry_run
-- defaults TRUE — the scheduler will compute and log what it WOULD do
-- but never spend Apify or mutate creator lifecycle until an operator
-- explicitly flips these. Nothing in this migration starts spending money.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS system_config (
  key         TEXT PRIMARY KEY,
  value       JSONB NOT NULL,
  description TEXT,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_by  TEXT
);
ALTER TABLE system_config ENABLE ROW LEVEL SECURITY;

INSERT INTO system_config (key, value, description) VALUES
  ('orchestrator_enabled',     'false', 'Master switch. When false the tick still runs the read-only janitor + demand calc but never enqueues eval/filter/scrape or mutates lifecycle.'),
  ('orchestrator_dry_run',     'true',  'When true the selector logs the scrapes it WOULD dispatch without enqueuing them (no Apify spend).'),
  ('tick_interval_minutes',    '20',    'Scheduler tick cadence (minutes).'),
  ('floor',                    '100',   'Global per-niche minimum servable videos (newcomer safety net).'),
  ('runway_days',              '7',     'Days of fresh supply buffered (demand horizon).'),
  ('assumed_daily_per_user',   '10',    'Cold-start per-user daily niche consumption (burn prior).'),
  ('burn_blend_k',             '100',   'Swipe pseudo-count; measured burn overtakes the prior past this many swipe events.'),
  ('active_user_window_days',  '30',    'Window (days) for active-user count + depletion calc.'),
  ('daily_scrape_cap',         '50',    'Global creator-scrapes/day guardrail.'),
  ('per_scrape_cap',           '30',    'Max videos pulled per creator per scrape.'),
  ('explore_fraction',         '0.6',   'Share of a niche scrape budget spent on unproven (provisional-only) creators.'),
  ('cache_freshness_hours',    '12',    'Reuse the eval Apify pull for content download within this window (URLs expire).'),
  ('reeval_ttl_days',          '30',    'Re-evaluate active creators whose evaluated_at is older than this.'),
  ('dormant_days',             '45',    'Recency (days) beyond which a creator is degraded as dormant.'),
  ('yield_floor',              '0.1',   'Minimum recent niche-yield before an active creator is degraded.'),
  ('max_failures',             '3',     'Consecutive eval/scrape failures before condemn -> rejected (unscrapable).'),
  ('per_tick_eval_cap',        '10',    'Max creators evaluated per tick.'),
  ('per_tick_filter_cap',      '25',    'Max creators filtered per tick.'),
  ('discovery_cooldown_hours', '24',    'Minimum hours between demand-driven discovery runs for the same niche.')
ON CONFLICT (key) DO NOTHING;

COMMIT;

-- ============================================================
-- 0030 — Remove the dead "maintain" job config
-- ============================================================
-- The maintain job was seeded by 0027 (job_maintain_enabled /
-- job_maintain_interval_min) but was never wired into the supervisor:
-- _ORCH_JOBS is only [evaluate, filter, plan, acquire], orchestrator.py's
-- config defaults have no job_maintain_*, and the dashboard never exposed it.
-- The rows just sit in system_config unread, misleading anyone reading the
-- config as if a maintain job exists. Now that the orchestrator is moving to
-- per-job Cloud Run Jobs, drop the dead knobs so config reflects reality.
--
-- dormant_days went with it: it was ONLY the threshold for maintain's decay
-- (active -> degraded on stalled posting), which no code ever implemented and
-- which nothing reads now that maintain is gone. Another orphaned knob.
-- ============================================================

BEGIN;

DELETE FROM system_config
 WHERE key IN ('job_maintain_enabled', 'job_maintain_interval_min', 'dormant_days');

COMMIT;

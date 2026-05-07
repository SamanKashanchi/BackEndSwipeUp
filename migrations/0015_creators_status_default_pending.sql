-- ============================================================
-- 0015 — Flip creators.status default from 'active' to 'pending'
-- ============================================================
-- Migration 0012 introduced the lifecycle (pending → evaluating →
-- active/rejected/degraded) and reset every existing row to pending,
-- but the column default was still 'active'. That meant any INSERT
-- that didn't explicitly set status would land as 'active' and
-- silently bypass the lifecycle pipeline.
--
-- Defensive: every writer also gets an explicit status='pending' in
-- the INSERT so the default isn't load-bearing — but flipping the
-- default catches any future writer that forgets.
-- ============================================================

ALTER TABLE creators ALTER COLUMN status SET DEFAULT 'pending';

-- Add per-attempt cold/hot start tracking.
--
-- "Cold" = spawn from a fresh load (level start, post-death respawn, practice
-- savestate load, hyper-play savestate load). The player has no carried state
-- from prior segments; powerups are whatever the load gave them.
--
-- "Hot" = spawn from carrying live state out of a completed prior segment.
-- Currently produced only by the reference recorder when a checkpoint arms
-- the next episode. Practice and hyper-play emit cold-only today; they
-- *could* gather hot data in the future (see plan
-- 2026-05-26-attempt-start-kind.md and BACKLOG).

ALTER TABLE attempts ADD COLUMN is_hot INTEGER NOT NULL DEFAULT 0;

-- BACKFILL
-- For REFERENCE attempts only, mark the first attempt of each episode as HOT
-- iff the *immediately preceding* attempt in the same capture_run (by id,
-- which is monotonic insertion order) was a survival from a *different*
-- episode. That signature uniquely identifies "player completed a prior
-- segment and carried state into this one."
--
-- Anything else stays cold: level starts, post-death respawns (same episode
-- as the prior died attempt), practice/hyper-play rows.

UPDATE attempts SET is_hot = 1
WHERE id IN (
  SELECT first_evt.id
  FROM (
    SELECT MIN(id) AS id, episode_id, capture_run_id
    FROM attempts
    WHERE source = 'reference'
      AND capture_run_id IS NOT NULL
    GROUP BY episode_id
  ) AS first_evt
  WHERE EXISTS (
    SELECT 1
    FROM attempts prev
    WHERE prev.capture_run_id = first_evt.capture_run_id
      AND prev.outcome = 'survived'
      AND prev.episode_id != first_evt.episode_id
      AND prev.id = (
        SELECT MAX(p2.id) FROM attempts p2
        WHERE p2.capture_run_id = first_evt.capture_run_id
          AND p2.id < first_evt.id
      )
  )
);

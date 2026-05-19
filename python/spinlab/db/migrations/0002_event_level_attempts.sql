-- Phase 0 of the segments-v07 integration: event-level attempts.
--
-- The old `attempts` shape was one row per practice EPISODE (a multi-death
-- attempt collapsed into a single record with `deaths` + `clean_tail_ms`
-- aggregate fields). The v07 segments model wants one row per
-- died-or-survived EVENT instead. This migration drops the episode-shaped
-- table and rebuilds it event-level.
--
-- Existing `attempts` data is dropped — Andrew confirmed in the spec that
-- the old rows are expendable and lossy backfill isn't worth it. Existing
-- `model_state` rows are also wiped so `rebuild_all_states` regenerates
-- from the new event rows through the legacy adapter on next startup.
--
-- Episode shape is recovered by grouping events by `episode_id`; see
-- `db/attempts.py::AttemptsMixin.get_episode_views` for the adapter.

DROP TABLE IF EXISTS attempts;

CREATE TABLE attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  segment_id TEXT NOT NULL REFERENCES segments(id),
  session_id TEXT REFERENCES sessions(id),
  capture_run_id TEXT REFERENCES capture_runs(id),
  episode_id TEXT NOT NULL,        -- groups consecutive events from one armed attempt
  outcome TEXT NOT NULL CHECK (outcome IN ('died', 'survived')),
  time_ms INTEGER NOT NULL,        -- raw wall-clock for this single event (no penalty)
  source TEXT NOT NULL DEFAULT 'practice',
  chosen_allocator TEXT,
  invalidated INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  CHECK ((session_id IS NOT NULL) <> (capture_run_id IS NOT NULL))
);

-- Episode lookups dominate: "all events for this segment in this episode,
-- in order". A composite index on (segment_id, episode_id, id) covers
-- both the segment-wide scans and the per-episode roll-ups.
CREATE INDEX idx_attempts_segment_episode
  ON attempts(segment_id, episode_id, id);
CREATE INDEX idx_attempts_session ON attempts(session_id);
CREATE INDEX idx_attempts_capture_run ON attempts(capture_run_id);

-- Wipe model state — the previous rows were integrated from episode-shaped
-- attempts that no longer exist. rebuild_all_states will regenerate from
-- the new event rows via the legacy adapter.
DELETE FROM model_state;

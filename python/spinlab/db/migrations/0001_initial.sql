-- Initial schema (A0 cleanup baseline).
--
-- All future schema changes go in numbered migration files in this directory.
-- Once shipped, a migration file is immutable — fix mistakes with a new
-- migration, never by editing the original.

CREATE TABLE IF NOT EXISTS games (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS waypoints (
  id TEXT PRIMARY KEY,
  game_id TEXT NOT NULL REFERENCES games(id),
  level_number INTEGER NOT NULL,
  endpoint_type TEXT NOT NULL,
  ordinal INTEGER NOT NULL DEFAULT 0,
  conditions_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS capture_runs (
  id TEXT PRIMARY KEY,
  game_id TEXT NOT NULL REFERENCES games(id),
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','saved')),
  active INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL DEFAULT 'live' CHECK (kind IN ('live','replay')),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capture_sessions (
  id TEXT PRIMARY KEY,
  capture_run_id TEXT NOT NULL REFERENCES capture_runs(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  end_reason TEXT,
  UNIQUE (capture_run_id, ordinal)
);

CREATE TABLE IF NOT EXISTS segments (
  id TEXT PRIMARY KEY,
  game_id TEXT NOT NULL REFERENCES games(id),
  level_number INTEGER NOT NULL,
  start_type TEXT NOT NULL,
  start_ordinal INTEGER NOT NULL DEFAULT 0,
  end_type TEXT NOT NULL,
  end_ordinal INTEGER NOT NULL DEFAULT 0,
  start_waypoint_id TEXT REFERENCES waypoints(id),
  end_waypoint_id TEXT REFERENCES waypoints(id),
  is_primary INTEGER NOT NULL DEFAULT 1,
  description TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  ordinal INTEGER NOT NULL DEFAULT 0,
  capture_run_id TEXT REFERENCES capture_runs(id),
  capture_session_id TEXT REFERENCES capture_sessions(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS waypoint_save_states (
  waypoint_id TEXT NOT NULL REFERENCES waypoints(id),
  variant_type TEXT NOT NULL,
  state_path TEXT NOT NULL,
  PRIMARY KEY (waypoint_id, variant_type)
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  game_id TEXT NOT NULL REFERENCES games(id),
  started_at TEXT NOT NULL,
  ended_at TEXT,
  segments_attempted INTEGER NOT NULL DEFAULT 0,
  segments_completed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  segment_id TEXT NOT NULL REFERENCES segments(id),
  session_id TEXT REFERENCES sessions(id),
  capture_run_id TEXT REFERENCES capture_runs(id),
  completed INTEGER NOT NULL,
  time_ms INTEGER,
  source TEXT NOT NULL DEFAULT 'practice',
  deaths INTEGER NOT NULL DEFAULT 0,
  clean_tail_ms INTEGER,
  invalidated INTEGER NOT NULL DEFAULT 0,
  chosen_allocator TEXT,
  created_at TEXT NOT NULL,
  CHECK ((session_id IS NOT NULL) <> (capture_run_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS recorded_segment_times (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  capture_session_id TEXT NOT NULL REFERENCES capture_sessions(id) ON DELETE CASCADE,
  segment_id TEXT NOT NULL,
  time_ms INTEGER NOT NULL,
  deaths INTEGER NOT NULL,
  clean_tail_ms INTEGER NOT NULL,
  recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_state (
  segment_id TEXT NOT NULL REFERENCES segments(id),
  estimator TEXT NOT NULL,
  state_json TEXT NOT NULL,
  output_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (segment_id, estimator)
);

CREATE TABLE IF NOT EXISTS allocator_config (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE INDEX IF NOT EXISTS idx_attempts_segment ON attempts(segment_id, created_at);
CREATE INDEX IF NOT EXISTS idx_attempts_session ON attempts(session_id);
CREATE INDEX IF NOT EXISTS idx_attempts_capture_run ON attempts(capture_run_id);
CREATE INDEX IF NOT EXISTS idx_capture_sessions_run ON capture_sessions(capture_run_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_recorded_segment_times_session ON recorded_segment_times(capture_session_id);
CREATE INDEX IF NOT EXISTS idx_segments_capture_session ON segments(capture_session_id);
CREATE INDEX IF NOT EXISTS idx_segments_capture_run ON segments(capture_run_id);
-- Enforces "at most one live draft per game". Replay drafts are intentionally
-- not unique-constrained: they're ephemeral and never recovered on restart.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_live_draft_per_game
  ON capture_runs(game_id)
  WHERE status = 'draft' AND kind = 'live';

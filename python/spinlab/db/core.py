"""Database core: schema, connection, transaction, shared helpers."""

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ..models import Segment

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
  id TEXT PRIMARY KEY,
  game_id TEXT NOT NULL REFERENCES games(id),
  level_number INTEGER NOT NULL,
  start_type TEXT NOT NULL,
  start_ordinal INTEGER NOT NULL DEFAULT 0,
  end_type TEXT NOT NULL,
  end_ordinal INTEGER NOT NULL DEFAULT 0,
  description TEXT DEFAULT '',
  strat_version INTEGER DEFAULT 1,
  active INTEGER DEFAULT 1,
  ordinal INTEGER,
  reference_id TEXT REFERENCES capture_runs(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segment_variants (
  segment_id TEXT NOT NULL REFERENCES segments(id),
  variant_type TEXT NOT NULL,
  state_path TEXT NOT NULL,
  is_default INTEGER DEFAULT 0,
  PRIMARY KEY (segment_id, variant_type)
);

CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  segment_id TEXT NOT NULL REFERENCES segments(id),
  session_id TEXT NOT NULL,
  completed INTEGER NOT NULL,
  time_ms INTEGER,
  goal_matched INTEGER,
  rating TEXT,
  strat_version INTEGER NOT NULL,
  source TEXT DEFAULT 'practice',
  deaths INTEGER DEFAULT 0,
  clean_tail_ms INTEGER,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id TEXT NOT NULL,
  event TEXT NOT NULL,
  level_number INTEGER NOT NULL,
  room_id INTEGER,
  goal_type TEXT,
  timestamp_ms INTEGER NOT NULL,
  session_type TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  game_id TEXT NOT NULL REFERENCES games(id),
  started_at TEXT NOT NULL,
  ended_at TEXT,
  segments_attempted INTEGER DEFAULT 0,
  segments_completed INTEGER DEFAULT 0
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

CREATE TABLE IF NOT EXISTS capture_runs (
  id TEXT PRIMARY KEY,
  game_id TEXT NOT NULL REFERENCES games(id),
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  active INTEGER DEFAULT 0,
  draft INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_attempts_segment ON attempts(segment_id, created_at);
CREATE INDEX IF NOT EXISTS idx_attempts_session ON attempts(session_id);
CREATE INDEX IF NOT EXISTS idx_transitions_game ON transitions(game_id, created_at);
"""


class DatabaseCore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        if str(db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='splits'"
        )
        if cur.fetchone():
            self.conn.executescript("""
                DROP TABLE IF EXISTS model_state;
                DROP TABLE IF EXISTS attempts;
                DROP TABLE IF EXISTS sessions;
                DROP TABLE IF EXISTS splits;
                DROP INDEX IF EXISTS idx_attempts_split;
                DROP INDEX IF EXISTS idx_attempts_session;
            """)
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        try:
            self.conn.execute("ALTER TABLE capture_runs ADD COLUMN draft INTEGER DEFAULT 0")
            self.conn.commit()
        except sqlite3.OperationalError:
            pass

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        """Context manager for grouping operations in a single transaction."""
        try:
            yield self
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # -- Games --

    def upsert_game(self, game_id: str, name: str, category: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO games (id, name, category, created_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(id) DO NOTHING",
            (game_id, name, category, now),
        )
        self.conn.commit()

    # -- Reset --

    def reset_all_data(self) -> None:
        """Delete all attempts, sessions, model state, and allocator config."""
        self.conn.execute("DELETE FROM attempts")
        self.conn.execute("DELETE FROM sessions")
        self.conn.execute("DELETE FROM model_state")
        self.conn.execute("DELETE FROM allocator_config")
        self.conn.execute("DELETE FROM transitions")
        self.conn.commit()

    def reset_game_data(self, game_id: str) -> None:
        """Delete attempts, sessions, model state for a specific game."""
        self.conn.execute(
            "DELETE FROM attempts WHERE segment_id IN"
            " (SELECT id FROM segments WHERE game_id = ?)",
            (game_id,),
        )
        self.conn.execute(
            "DELETE FROM model_state WHERE segment_id IN"
            " (SELECT id FROM segments WHERE game_id = ?)",
            (game_id,),
        )
        self.conn.execute("DELETE FROM sessions WHERE game_id = ?", (game_id,))
        self.conn.execute("DELETE FROM transitions WHERE game_id = ?", (game_id,))
        self.conn.commit()

    # -- Helpers --

    @staticmethod
    def _row_to_segment(row: sqlite3.Row) -> Segment:
        keys = row.keys()
        return Segment(
            id=row["id"],
            game_id=row["game_id"],
            level_number=row["level_number"],
            start_type=row["start_type"],
            start_ordinal=row["start_ordinal"],
            end_type=row["end_type"],
            end_ordinal=row["end_ordinal"],
            description=row["description"] or "",
            strat_version=row["strat_version"],
            active=bool(row["active"]),
            ordinal=row["ordinal"] if "ordinal" in keys else None,
            reference_id=row["reference_id"] if "reference_id" in keys else None,
        )

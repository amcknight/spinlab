"""SpinLab database layer — SQLite."""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import Split, Schedule, Attempt, Rating

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS splits (
  id TEXT PRIMARY KEY,
  game_id TEXT NOT NULL REFERENCES games(id),
  level_number INTEGER NOT NULL,
  room_id INTEGER,
  goal TEXT NOT NULL,
  description TEXT DEFAULT '',
  state_path TEXT,
  reference_time_ms INTEGER,
  strat_version INTEGER DEFAULT 1,
  active INTEGER DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule (
  split_id TEXT PRIMARY KEY REFERENCES splits(id),
  ease_factor REAL DEFAULT 2.5,
  interval_minutes REAL DEFAULT 5.0,
  repetitions INTEGER DEFAULT 0,
  next_review TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  split_id TEXT NOT NULL REFERENCES splits(id),
  session_id TEXT NOT NULL,
  completed INTEGER NOT NULL,
  time_ms INTEGER,
  goal_matched INTEGER,
  rating TEXT,
  strat_version INTEGER NOT NULL,
  source TEXT DEFAULT 'practice',
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
  splits_attempted INTEGER DEFAULT 0,
  splits_completed INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_attempts_split ON attempts(split_id, created_at);
CREATE INDEX IF NOT EXISTS idx_attempts_session ON attempts(session_id);
CREATE INDEX IF NOT EXISTS idx_schedule_next ON schedule(next_review);
CREATE INDEX IF NOT EXISTS idx_transitions_game ON transitions(game_id, created_at);
"""


class Database:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- Games --

    def upsert_game(self, game_id: str, name: str, category: str) -> None:
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO games (id, name, category, created_at) VALUES (?, ?, ?, ?)",
            (game_id, name, category, now),
        )
        self.conn.commit()

    # -- Splits --

    def upsert_split(self, split: Split) -> None:
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            """INSERT INTO splits (id, game_id, level_number, room_id, goal, description,
               state_path, reference_time_ms, strat_version, active, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 state_path=excluded.state_path,
                 reference_time_ms=excluded.reference_time_ms,
                 description=excluded.description,
                 updated_at=excluded.updated_at""",
            (split.id, split.game_id, split.level_number, split.room_id,
             split.goal, split.description, split.state_path,
             split.reference_time_ms, split.strat_version, int(split.active),
             now, now),
        )
        self.conn.commit()

    def get_active_splits(self, game_id: str) -> list[Split]:
        rows = self.conn.execute(
            "SELECT * FROM splits WHERE game_id = ? AND active = 1", (game_id,)
        ).fetchall()
        return [self._row_to_split(r) for r in rows]

    def deactivate_split(self, split_id: str) -> None:
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            "UPDATE splits SET active = 0, updated_at = ? WHERE id = ?",
            (now, split_id),
        )
        self.conn.commit()

    def increment_strat_version(self, split_id: str) -> None:
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            "UPDATE splits SET strat_version = strat_version + 1, updated_at = ? WHERE id = ?",
            (now, split_id),
        )
        self.conn.commit()

    # -- Schedule --

    def ensure_schedule(self, split_id: str) -> None:
        """Create schedule entry if it doesn't exist."""
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            """INSERT OR IGNORE INTO schedule
               (split_id, ease_factor, interval_minutes, repetitions, next_review, updated_at)
               VALUES (?, 2.5, 5.0, 0, ?, ?)""",
            (split_id, now, now),
        )
        self.conn.commit()

    def get_due_splits(self, game_id: str, now: Optional[datetime] = None) -> list[dict]:
        """Get splits due for review, most overdue first."""
        if now is None:
            now = datetime.utcnow()
        rows = self.conn.execute(
            """SELECT s.*, sch.ease_factor, sch.interval_minutes, sch.repetitions, sch.next_review
               FROM splits s
               JOIN schedule sch ON s.id = sch.split_id
               WHERE s.game_id = ? AND s.active = 1 AND sch.next_review <= ?
               ORDER BY sch.next_review ASC""",
            (game_id, now.isoformat()),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_next_due(self, game_id: str) -> Optional[dict]:
        """Get the split that will be due soonest."""
        row = self.conn.execute(
            """SELECT s.*, sch.ease_factor, sch.interval_minutes, sch.repetitions, sch.next_review
               FROM splits s
               JOIN schedule sch ON s.id = sch.split_id
               WHERE s.game_id = ? AND s.active = 1
               ORDER BY sch.next_review ASC LIMIT 1""",
            (game_id,),
        ).fetchone()
        return dict(row) if row else None

    def update_schedule(self, schedule: Schedule) -> None:
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            """UPDATE schedule SET ease_factor = ?, interval_minutes = ?,
               repetitions = ?, next_review = ?, updated_at = ?
               WHERE split_id = ?""",
            (schedule.ease_factor, schedule.interval_minutes,
             schedule.repetitions, schedule.next_review.isoformat(),
             now, schedule.split_id),
        )
        self.conn.commit()

    def reset_schedule(self, split_id: str) -> None:
        """Reset schedule to new-card state (for strat changes)."""
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            """UPDATE schedule SET ease_factor = 2.5, interval_minutes = 5.0,
               repetitions = 0, next_review = ?, updated_at = ?
               WHERE split_id = ?""",
            (now, now, split_id),
        )
        self.conn.commit()

    # -- Attempts --

    def log_attempt(self, attempt: Attempt) -> None:
        self.conn.execute(
            """INSERT INTO attempts
               (split_id, session_id, completed, time_ms, goal_matched,
                rating, strat_version, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (attempt.split_id, attempt.session_id, int(attempt.completed),
             attempt.time_ms, attempt.goal_matched,
             attempt.rating.value if attempt.rating else None,
             attempt.strat_version, attempt.source,
             attempt.created_at.isoformat()),
        )
        self.conn.commit()

    def get_split_stats(self, split_id: str, strat_version: Optional[int] = None) -> dict:
        """Get aggregate stats for a split."""
        where = "split_id = ?"
        params: list = [split_id]
        if strat_version is not None:
            where += " AND strat_version = ?"
            params.append(strat_version)

        row = self.conn.execute(
            f"""SELECT
                COUNT(*) as total_attempts,
                SUM(completed) as completions,
                AVG(CASE WHEN completed = 1 THEN time_ms END) as avg_time_ms,
                MIN(CASE WHEN completed = 1 THEN time_ms END) as best_time_ms
            FROM attempts WHERE {where}""",
            params,
        ).fetchone()
        return dict(row)

    # -- Sessions --

    def create_session(self, session_id: str, game_id: str) -> None:
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            "INSERT INTO sessions (id, game_id, started_at) VALUES (?, ?, ?)",
            (session_id, game_id, now),
        )
        self.conn.commit()

    def end_session(self, session_id: str, splits_attempted: int, splits_completed: int) -> None:
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            """UPDATE sessions SET ended_at = ?, splits_attempted = ?, splits_completed = ?
               WHERE id = ?""",
            (now, splits_attempted, splits_completed, session_id),
        )
        self.conn.commit()

    def get_current_session(self, game_id: str) -> Optional[dict]:
        """Get active session (ended_at IS NULL)."""
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE game_id = ? AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (game_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_split_attempt_count(self, split_id: str, session_id: str) -> int:
        """Count attempts on a split in a specific session."""
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM attempts "
            "WHERE split_id = ? AND session_id = ?",
            (split_id, session_id),
        ).fetchone()
        return row["cnt"]

    def get_recent_attempts(self, game_id: str, limit: int = 8) -> list[dict]:
        """Last N attempts joined with split info, most recent first."""
        rows = self.conn.execute(
            """SELECT a.*, s.goal, s.description, s.level_number,
                      s.reference_time_ms
               FROM attempts a
               JOIN splits s ON a.split_id = s.id
               WHERE s.game_id = ?
               ORDER BY a.created_at DESC
               LIMIT ?""",
            (game_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_splits_with_schedule(self, game_id: str) -> list[dict]:
        """All splits joined with schedule, ordered by level_number."""
        rows = self.conn.execute(
            """SELECT s.*, sch.ease_factor, sch.interval_minutes,
                      sch.repetitions, sch.next_review
               FROM splits s
               LEFT JOIN schedule sch ON s.id = sch.split_id
               WHERE s.game_id = ?
               ORDER BY s.level_number, s.room_id""",
            (game_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_session_history(self, game_id: str, limit: int = 10) -> list[dict]:
        """Recent sessions, most recent first."""
        rows = self.conn.execute(
            """SELECT * FROM sessions
               WHERE game_id = ?
               ORDER BY started_at DESC
               LIMIT ?""",
            (game_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- Helpers --

    @staticmethod
    def _row_to_split(row: sqlite3.Row) -> Split:
        return Split(
            id=row["id"],
            game_id=row["game_id"],
            level_number=row["level_number"],
            room_id=row["room_id"],
            goal=row["goal"],
            description=row["description"] or "",
            state_path=row["state_path"],
            reference_time_ms=row["reference_time_ms"],
            strat_version=row["strat_version"],
            active=bool(row["active"]),
        )

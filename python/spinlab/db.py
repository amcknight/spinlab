"""SpinLab database layer — SQLite."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

from .models import Split, Attempt

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
CREATE INDEX IF NOT EXISTS idx_transitions_game ON transitions(game_id, created_at);
"""


class Database:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

        # --- Migration: drop old SM-2 schedule table ---
        cur = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schedule'"
        )
        if cur.fetchone() is not None:
            self.conn.execute("DROP TABLE schedule")

        # Drop the schedule index if it exists
        self.conn.execute("DROP INDEX IF EXISTS idx_schedule_next")

        # --- New tables ---
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS model_state (
                split_id TEXT PRIMARY KEY REFERENCES splits(id),
                estimator TEXT NOT NULL,
                state_json TEXT NOT NULL,
                marginal_return REAL,
                updated_at TEXT NOT NULL
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS allocator_config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # --- Migration: add ordinal column to splits ---
        cur = self.conn.execute("PRAGMA table_info(splits)")
        col_names = [row[1] for row in cur.fetchall()]
        if "ordinal" not in col_names:
            self.conn.execute("ALTER TABLE splits ADD COLUMN ordinal INTEGER")

        # --- capture_runs table ---
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS capture_runs (
                id TEXT PRIMARY KEY,
                game_id TEXT NOT NULL REFERENCES games(id),
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                active INTEGER DEFAULT 0
            )
        """)

        # --- Migration: add reference_id to splits ---
        cur = self.conn.execute("PRAGMA table_info(splits)")
        col_names = [row[1] for row in cur.fetchall()]
        if "reference_id" not in col_names:
            self.conn.execute(
                "ALTER TABLE splits ADD COLUMN reference_id TEXT REFERENCES capture_runs(id)"
            )

        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- Games --

    def upsert_game(self, game_id: str, name: str, category: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO games (id, name, category, created_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(id) DO NOTHING",
            (game_id, name, category, now),
        )
        self.conn.commit()

    # -- Splits --

    def upsert_split(self, split: Split) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """INSERT INTO splits (id, game_id, level_number, room_id, goal, description,
               state_path, reference_time_ms, strat_version, active, ordinal, reference_id,
               created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 state_path=excluded.state_path,
                 reference_time_ms=excluded.reference_time_ms,
                 description=excluded.description,
                 ordinal=excluded.ordinal,
                 reference_id=excluded.reference_id,
                 updated_at=excluded.updated_at""",
            (split.id, split.game_id, split.level_number, split.room_id,
             split.goal, split.description, split.state_path,
             split.reference_time_ms, split.strat_version, int(split.active),
             split.ordinal, split.reference_id, now, now),
        )
        self.conn.commit()

    def get_active_splits(self, game_id: str) -> list[Split]:
        rows = self.conn.execute(
            "SELECT * FROM splits WHERE game_id = ? AND active = 1", (game_id,)
        ).fetchall()
        return [self._row_to_split(r) for r in rows]

    def deactivate_split(self, split_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "UPDATE splits SET active = 0, updated_at = ? WHERE id = ?",
            (now, split_id),
        )
        self.conn.commit()

    def increment_strat_version(self, split_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "UPDATE splits SET strat_version = strat_version + 1, updated_at = ? WHERE id = ?",
            (now, split_id),
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
             attempt.rating,
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
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO sessions (id, game_id, started_at) VALUES (?, ?, ?)",
            (session_id, game_id, now),
        )
        self.conn.commit()

    def end_session(self, session_id: str, splits_attempted: int, splits_completed: int) -> None:
        now = datetime.now(UTC).isoformat()
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

    # -- Model state --

    def save_model_state(
        self, split_id: str, estimator: str, state_json: str, marginal_return: float
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """INSERT INTO model_state (split_id, estimator, state_json, marginal_return, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(split_id) DO UPDATE SET
                 estimator=excluded.estimator,
                 state_json=excluded.state_json,
                 marginal_return=excluded.marginal_return,
                 updated_at=excluded.updated_at""",
            (split_id, estimator, state_json, marginal_return, now),
        )
        self.conn.commit()

    def load_model_state(self, split_id: str) -> dict | None:
        cur = self.conn.execute(
            "SELECT split_id, estimator, state_json, marginal_return, updated_at "
            "FROM model_state WHERE split_id = ?",
            (split_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "split_id": row[0],
            "estimator": row[1],
            "state_json": row[2],
            "marginal_return": row[3],
            "updated_at": row[4],
        }

    def load_all_model_states(self, game_id: str) -> list[dict]:
        cur = self.conn.execute(
            """SELECT m.split_id, m.estimator, m.state_json, m.marginal_return, m.updated_at
               FROM model_state m
               JOIN splits s ON m.split_id = s.id
               WHERE s.game_id = ? AND s.active = 1
               ORDER BY m.marginal_return DESC""",
            (game_id,),
        )
        cols = ["split_id", "estimator", "state_json", "marginal_return", "updated_at"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def save_allocator_config(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO allocator_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def load_allocator_config(self, key: str) -> str | None:
        cur = self.conn.execute(
            "SELECT value FROM allocator_config WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        return row[0] if row else None

    def get_all_splits_with_model(self, game_id: str) -> list[dict]:
        """Get all active splits LEFT JOIN model_state, ordered by ordinal."""
        cur = self.conn.execute(
            """SELECT s.id, s.game_id, s.level_number, s.room_id, s.goal,
                      s.description, s.strat_version, s.reference_time_ms,
                      s.state_path, s.active, s.ordinal,
                      m.estimator, m.state_json, m.marginal_return
               FROM splits s
               LEFT JOIN model_state m ON s.id = m.split_id
               WHERE s.game_id = ? AND s.active = 1
               ORDER BY s.ordinal, s.level_number, s.room_id""",
            (game_id,),
        )
        cols = [
            "id", "game_id", "level_number", "room_id", "goal",
            "description", "strat_version", "reference_time_ms",
            "state_path", "active", "ordinal",
            "estimator", "state_json", "marginal_return",
        ]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_split_attempts(self, split_id: str) -> list[dict]:
        """Get all attempts for a split, ordered by created_at."""
        cur = self.conn.execute(
            "SELECT split_id, completed, time_ms, created_at "
            "FROM attempts WHERE split_id = ? ORDER BY created_at",
            (split_id,),
        )
        cols = ["split_id", "completed", "time_ms", "created_at"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # -- Reset --

    def reset_all_data(self) -> None:
        """Delete all attempts, sessions, model state, and allocator config.

        Keeps splits and games intact so the manifest doesn't need re-import.
        """
        self.conn.execute("DELETE FROM attempts")
        self.conn.execute("DELETE FROM sessions")
        self.conn.execute("DELETE FROM model_state")
        self.conn.execute("DELETE FROM allocator_config")
        self.conn.execute("DELETE FROM transitions")
        self.conn.commit()

    def reset_game_data(self, game_id: str) -> None:
        """Delete attempts, sessions, model state for a specific game.

        Keeps splits, games, and global allocator_config intact.
        """
        self.conn.execute(
            "DELETE FROM attempts WHERE split_id IN"
            " (SELECT id FROM splits WHERE game_id = ?)",
            (game_id,),
        )
        self.conn.execute(
            "DELETE FROM model_state WHERE split_id IN"
            " (SELECT id FROM splits WHERE game_id = ?)",
            (game_id,),
        )
        self.conn.execute("DELETE FROM sessions WHERE game_id = ?", (game_id,))
        self.conn.execute("DELETE FROM transitions WHERE game_id = ?", (game_id,))
        self.conn.commit()

    # -- Capture Runs --

    def create_capture_run(self, run_id: str, game_id: str, name: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO capture_runs (id, game_id, name, created_at, active) "
            "VALUES (?, ?, ?, ?, 0)",
            (run_id, game_id, name, now),
        )
        self.conn.commit()

    def list_capture_runs(self, game_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, game_id, name, created_at, active FROM capture_runs "
            "WHERE game_id = ? ORDER BY created_at",
            (game_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_active_capture_run(self, run_id: str) -> None:
        row = self.conn.execute(
            "SELECT game_id FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not row:
            return
        game_id = row[0]
        self.conn.execute(
            "UPDATE capture_runs SET active = 0 WHERE game_id = ?", (game_id,)
        )
        self.conn.execute(
            "UPDATE capture_runs SET active = 1 WHERE id = ?", (run_id,)
        )
        self.conn.commit()

    def rename_capture_run(self, run_id: str, name: str) -> None:
        self.conn.execute(
            "UPDATE capture_runs SET name = ? WHERE id = ?", (name, run_id)
        )
        self.conn.commit()

    def delete_capture_run(self, run_id: str) -> None:
        """Soft-delete: deactivate all splits in the run, null FK, remove the record."""
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "UPDATE splits SET active = 0, reference_id = NULL, updated_at = ? "
            "WHERE reference_id = ?",
            (now, run_id),
        )
        self.conn.execute("DELETE FROM capture_runs WHERE id = ?", (run_id,))
        self.conn.commit()

    def get_splits_by_reference(self, reference_id: str) -> list[dict]:
        cur = self.conn.execute(
            """SELECT id, game_id, level_number, room_id, goal, description,
                      reference_time_ms, state_path, active, ordinal, reference_id
               FROM splits WHERE reference_id = ? AND active = 1
               ORDER BY ordinal""",
            (reference_id,),
        )
        cols = ["id", "game_id", "level_number", "room_id", "goal", "description",
                "reference_time_ms", "state_path", "active", "ordinal", "reference_id"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # -- Split editing --

    def update_split(self, split_id: str, **kwargs) -> None:
        """Partial update: pass description=, goal=, active= as kwargs."""
        allowed = {"description", "goal", "active"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        if "active" in updates:
            updates["active"] = int(updates["active"])
        now = datetime.now(UTC).isoformat()
        sets = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [now, split_id]
        self.conn.execute(
            f"UPDATE splits SET {sets}, updated_at = ? WHERE id = ?", vals
        )
        self.conn.commit()

    def soft_delete_split(self, split_id: str) -> None:
        self.update_split(split_id, active=0)

    # -- Helpers --

    @staticmethod
    def _row_to_split(row: sqlite3.Row) -> Split:
        keys = row.keys()
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
            ordinal=row["ordinal"] if "ordinal" in keys else None,
            reference_id=row["reference_id"] if "reference_id" in keys else None,
        )

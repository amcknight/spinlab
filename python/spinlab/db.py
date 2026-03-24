"""SpinLab database layer — SQLite."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

from .models import Segment, SegmentVariant, Attempt

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
  segment_id TEXT PRIMARY KEY REFERENCES segments(id),
  estimator TEXT NOT NULL,
  state_json TEXT NOT NULL,
  marginal_return REAL,
  updated_at TEXT NOT NULL
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


class Database:
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
        # Detect old split-based schema and drop it (segment refactor, no migration)
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
        # Add draft column if missing (dashboard restructure)
        try:
            self.conn.execute("ALTER TABLE capture_runs ADD COLUMN draft INTEGER DEFAULT 0")
            self.conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

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

    # -- Segments --

    def upsert_segment(self, seg: Segment) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """INSERT INTO segments (id, game_id, level_number, start_type, start_ordinal,
               end_type, end_ordinal, description, strat_version, active, ordinal,
               reference_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 description=excluded.description,
                 ordinal=excluded.ordinal,
                 reference_id=excluded.reference_id,
                 active=excluded.active,
                 updated_at=excluded.updated_at""",
            (seg.id, seg.game_id, seg.level_number, seg.start_type,
             seg.start_ordinal, seg.end_type, seg.end_ordinal,
             seg.description, seg.strat_version, int(seg.active),
             seg.ordinal, seg.reference_id, now, now),
        )
        self.conn.commit()

    def get_active_segments(self, game_id: str) -> list[Segment]:
        rows = self.conn.execute(
            "SELECT * FROM segments WHERE game_id = ? AND active = 1", (game_id,)
        ).fetchall()
        return [self._row_to_segment(r) for r in rows]

    def deactivate_segment(self, segment_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "UPDATE segments SET active = 0, updated_at = ? WHERE id = ?",
            (now, segment_id),
        )
        self.conn.commit()

    def increment_strat_version(self, segment_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "UPDATE segments SET strat_version = strat_version + 1, updated_at = ? WHERE id = ?",
            (now, segment_id),
        )
        self.conn.commit()

    # -- Segment Variants --

    def add_variant(self, v: SegmentVariant) -> None:
        self.conn.execute(
            """INSERT INTO segment_variants (segment_id, variant_type, state_path, is_default)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(segment_id, variant_type) DO UPDATE SET
                 state_path=excluded.state_path,
                 is_default=excluded.is_default""",
            (v.segment_id, v.variant_type, v.state_path, int(v.is_default)),
        )
        self.conn.commit()

    def get_variants(self, segment_id: str) -> list[SegmentVariant]:
        rows = self.conn.execute(
            "SELECT segment_id, variant_type, state_path, is_default "
            "FROM segment_variants WHERE segment_id = ?",
            (segment_id,),
        ).fetchall()
        return [
            SegmentVariant(
                segment_id=r[0], variant_type=r[1],
                state_path=r[2], is_default=bool(r[3]),
            )
            for r in rows
        ]

    def get_variant(self, segment_id: str, variant_type: str) -> SegmentVariant | None:
        row = self.conn.execute(
            "SELECT segment_id, variant_type, state_path, is_default "
            "FROM segment_variants WHERE segment_id = ? AND variant_type = ?",
            (segment_id, variant_type),
        ).fetchone()
        if row is None:
            return None
        return SegmentVariant(
            segment_id=row[0], variant_type=row[1],
            state_path=row[2], is_default=bool(row[3]),
        )

    def get_default_variant(self, segment_id: str) -> SegmentVariant | None:
        """Get default variant; falls back to any variant if none marked default."""
        row = self.conn.execute(
            "SELECT segment_id, variant_type, state_path, is_default "
            "FROM segment_variants WHERE segment_id = ? "
            "ORDER BY is_default DESC LIMIT 1",
            (segment_id,),
        ).fetchone()
        if row is None:
            return None
        return SegmentVariant(
            segment_id=row[0], variant_type=row[1],
            state_path=row[2], is_default=bool(row[3]),
        )

    # -- Attempts --

    def log_attempt(self, attempt: Attempt) -> None:
        self.conn.execute(
            """INSERT INTO attempts
               (segment_id, session_id, completed, time_ms, goal_matched,
                rating, strat_version, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (attempt.segment_id, attempt.session_id, int(attempt.completed),
             attempt.time_ms, attempt.goal_matched,
             attempt.rating,
             attempt.strat_version, attempt.source,
             attempt.created_at.isoformat()),
        )
        self.conn.commit()

    def get_segment_stats(self, segment_id: str, strat_version: Optional[int] = None) -> dict:
        """Get aggregate stats for a segment."""
        where = "segment_id = ?"
        params: list = [segment_id]
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

    def end_session(self, session_id: str, segments_attempted: int, segments_completed: int) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """UPDATE sessions SET ended_at = ?, segments_attempted = ?, segments_completed = ?
               WHERE id = ?""",
            (now, segments_attempted, segments_completed, session_id),
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

    def get_segment_attempt_count(self, segment_id: str, session_id: str) -> int:
        """Count attempts on a segment in a specific session."""
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM attempts "
            "WHERE segment_id = ? AND session_id = ?",
            (segment_id, session_id),
        ).fetchone()
        return row["cnt"]

    def get_recent_attempts(self, game_id: str, limit: int = 8) -> list[dict]:
        """Last N attempts joined with segment info, most recent first."""
        rows = self.conn.execute(
            """SELECT a.*, s.description, s.level_number,
                      s.start_type, s.start_ordinal,
                      s.end_type, s.end_ordinal
               FROM attempts a
               JOIN segments s ON a.segment_id = s.id
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
        self, segment_id: str, estimator: str, state_json: str, marginal_return: float
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """INSERT INTO model_state (segment_id, estimator, state_json, marginal_return, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(segment_id) DO UPDATE SET
                 estimator=excluded.estimator,
                 state_json=excluded.state_json,
                 marginal_return=excluded.marginal_return,
                 updated_at=excluded.updated_at""",
            (segment_id, estimator, state_json, marginal_return, now),
        )
        self.conn.commit()

    def load_model_state(self, segment_id: str) -> dict | None:
        cur = self.conn.execute(
            "SELECT segment_id, estimator, state_json, marginal_return, updated_at "
            "FROM model_state WHERE segment_id = ?",
            (segment_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "segment_id": row[0],
            "estimator": row[1],
            "state_json": row[2],
            "marginal_return": row[3],
            "updated_at": row[4],
        }

    def load_all_model_states(self, game_id: str) -> list[dict]:
        cur = self.conn.execute(
            """SELECT m.segment_id, m.estimator, m.state_json, m.marginal_return, m.updated_at
               FROM model_state m
               JOIN segments s ON m.segment_id = s.id
               WHERE s.game_id = ? AND s.active = 1
               ORDER BY m.marginal_return DESC""",
            (game_id,),
        )
        cols = ["segment_id", "estimator", "state_json", "marginal_return", "updated_at"]
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

    def get_all_segments_with_model(self, game_id: str) -> list[dict]:
        """Get all active segments LEFT JOIN model_state, with default variant state_path."""
        cur = self.conn.execute(
            """SELECT s.id, s.game_id, s.level_number, s.start_type, s.start_ordinal,
                      s.end_type, s.end_ordinal, s.description, s.strat_version,
                      s.active, s.ordinal,
                      (SELECT sv.state_path FROM segment_variants sv
                       WHERE sv.segment_id = s.id
                       ORDER BY sv.is_default DESC LIMIT 1) AS state_path,
                      m.estimator, m.state_json, m.marginal_return
               FROM segments s
               LEFT JOIN model_state m ON s.id = m.segment_id
               WHERE s.game_id = ? AND s.active = 1
               ORDER BY s.ordinal, s.level_number""",
            (game_id,),
        )
        # Use column descriptions from cursor for accurate mapping
        actual_cols = [desc[0] for desc in cur.description]
        return [dict(zip(actual_cols, row)) for row in cur.fetchall()]

    def get_segment_attempts(self, segment_id: str) -> list[dict]:
        """Get all attempts for a segment, ordered by created_at."""
        cur = self.conn.execute(
            "SELECT segment_id, completed, time_ms, created_at "
            "FROM attempts WHERE segment_id = ? ORDER BY created_at",
            (segment_id,),
        )
        cols = ["segment_id", "completed", "time_ms", "created_at"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # -- Reset --

    def reset_all_data(self) -> None:
        """Delete all attempts, sessions, model state, and allocator config.

        Keeps segments and games intact so the manifest doesn't need re-import.
        """
        self.conn.execute("DELETE FROM attempts")
        self.conn.execute("DELETE FROM sessions")
        self.conn.execute("DELETE FROM model_state")
        self.conn.execute("DELETE FROM allocator_config")
        self.conn.execute("DELETE FROM transitions")
        self.conn.commit()

    def reset_game_data(self, game_id: str) -> None:
        """Delete attempts, sessions, model state for a specific game.

        Keeps segments, games, and global allocator_config intact.
        """
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

    # -- Capture Runs --

    def create_capture_run(self, run_id: str, game_id: str, name: str, draft: bool = False) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO capture_runs (id, game_id, name, created_at, active, draft) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (run_id, game_id, name, now, 1 if draft else 0),
        )
        self.conn.commit()

    def list_capture_runs(self, game_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, game_id, name, created_at, active, draft FROM capture_runs "
            "WHERE game_id = ? AND draft = 0 ORDER BY created_at",
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
        """Soft-delete: deactivate all segments in the run, null FK, remove the record."""
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "UPDATE segments SET active = 0, reference_id = NULL, updated_at = ? "
            "WHERE reference_id = ?",
            (now, run_id),
        )
        self.conn.execute("DELETE FROM capture_runs WHERE id = ?", (run_id,))
        self.conn.commit()

    def promote_draft(self, run_id: str, name: str) -> None:
        """Promote a draft capture run to saved: rename and set draft=0."""
        self.conn.execute(
            "UPDATE capture_runs SET draft = 0, name = ? WHERE id = ?",
            (name, run_id),
        )
        self.conn.commit()

    def hard_delete_capture_run(self, run_id: str) -> None:
        """Hard delete: remove run, segments, variants, model_state, attempts."""
        seg_ids = [
            r[0] for r in self.conn.execute(
                "SELECT id FROM segments WHERE reference_id = ?", (run_id,)
            ).fetchall()
        ]
        if seg_ids:
            placeholders = ",".join("?" * len(seg_ids))
            self.conn.execute(
                f"DELETE FROM segment_variants WHERE segment_id IN ({placeholders})",
                seg_ids,
            )
            self.conn.execute(
                f"DELETE FROM model_state WHERE segment_id IN ({placeholders})",
                seg_ids,
            )
            self.conn.execute(
                f"DELETE FROM attempts WHERE segment_id IN ({placeholders})",
                seg_ids,
            )
            self.conn.execute(
                f"DELETE FROM segments WHERE reference_id = ?", (run_id,),
            )
        # Always delete the capture_run row, even if it had no segments
        self.conn.execute("DELETE FROM capture_runs WHERE id = ?", (run_id,))
        self.conn.commit()

    def get_segments_by_reference(self, reference_id: str) -> list[dict]:
        cur = self.conn.execute(
            """SELECT id, game_id, level_number, start_type, start_ordinal,
                      end_type, end_ordinal, description, active, ordinal,
                      reference_id,
                      (SELECT sv.state_path FROM segment_variants sv
                       WHERE sv.segment_id = segments.id
                       ORDER BY sv.is_default DESC LIMIT 1) AS state_path
               FROM segments WHERE reference_id = ? AND active = 1
               ORDER BY ordinal""",
            (reference_id,),
        )
        actual_cols = [desc[0] for desc in cur.description]
        return [dict(zip(actual_cols, row)) for row in cur.fetchall()]

    # -- Segment editing --

    def update_segment(self, segment_id: str, **kwargs) -> None:
        """Partial update: pass description=, active= as kwargs."""
        allowed = {"description", "active"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        if "active" in updates:
            updates["active"] = int(updates["active"])
        now = datetime.now(UTC).isoformat()
        sets = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [now, segment_id]
        self.conn.execute(
            f"UPDATE segments SET {sets}, updated_at = ? WHERE id = ?", vals
        )
        self.conn.commit()

    def soft_delete_segment(self, segment_id: str) -> None:
        self.update_segment(segment_id, active=0)

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

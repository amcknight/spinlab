"""Database core: connection, transaction, shared helpers.

Schema lives in ``migrations/NNNN_name.sql`` files; see
``spinlab.db.migrations`` for the runner.

Transactional model
-------------------
The connection runs in **autocommit mode** (``isolation_level=None``). Each
statement is its own transaction by default — matching the original "every
mixin method calls ``.commit()``" behavior, but without scattering
``self.conn.commit()`` calls through every mixin file.

When a code path needs multiple statements to commit atomically, wrap it::

    with db.transaction():
        db.do_one_thing()
        db.do_another_thing()

``transaction()`` uses ``BEGIN IMMEDIATE`` at the top level and SAVEPOINTs
when nested, so methods that wrap their own body in ``with self.transaction():``
compose cleanly under an outer caller transaction.
"""

import itertools
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ..models import Segment
from .migrations import run_migrations

_savepoint_counter = itertools.count()

# A contended writer waits this long for the WAL write lock before raising
# SQLITE_BUSY. Sized well above a normal write batch (a few statements,
# << 100ms) so routine contention just waits, yet short enough that a genuine
# deadlock still surfaces as an error rather than hanging forever.
_BUSY_TIMEOUT_MS = 5000


class DatabaseCore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._is_memory = str(db_path) == ":memory:"
        if not self._is_memory:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # File DBs: one connection per thread (no cross-thread sharing → no
        # concurrent-use corruption). :memory: DBs are per-connection, so they
        # MUST share one connection or each thread would see an empty database;
        # tests are single-threaded and never hit the concurrency bug.
        self._local = threading.local()
        self._conns_lock = threading.Lock()
        self._all_conns: list[sqlite3.Connection] = []
        self._shared_conn: sqlite3.Connection | None = None
        conn = self._new_conn()
        run_migrations(conn)  # runs once; file is shared, so all conns see the schema
        if self._is_memory:
            self._shared_conn = conn
        else:
            self._local.conn = conn

    def _configure(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        # Autocommit: lets BEGIN / COMMIT / ROLLBACK / SAVEPOINT do what they
        # say in the SQL we issue (see transaction() and run_migrations()).
        conn.isolation_level = None
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._configure(conn)
        with self._conns_lock:
            self._all_conns.append(conn)
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._is_memory:
            assert self._shared_conn is not None
            return self._shared_conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        with self._conns_lock:
            for c in self._all_conns:
                try:
                    c.close()
                except Exception:
                    pass
            self._all_conns.clear()
        self._shared_conn = None
        if hasattr(self._local, "conn"):
            del self._local.conn

    @contextmanager
    def transaction(self):
        """Group statements into a single atomic unit. Composable.

        Top-level: uses ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK``.
        Nested under an outer ``with db.transaction():``: uses ``SAVEPOINT``,
        so an inner failure rolls back the inner block but leaves the outer
        transaction free to continue or roll back itself.
        """
        if self.conn.in_transaction:
            sp = f"sp_{next(_savepoint_counter)}"
            self.conn.execute(f"SAVEPOINT {sp}")
            try:
                yield self
            except Exception:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                self.conn.execute(f"RELEASE SAVEPOINT {sp}")
                raise
            else:
                self.conn.execute(f"RELEASE SAVEPOINT {sp}")
        else:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def upsert_game(self, game_id: str, name: str, category: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO games (id, name, category, created_at, last_accessed)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET last_accessed = excluded.last_accessed",
            (game_id, name, category, now, now),
        )

    def get_recently_played_games(self, limit: int = 3) -> list[str]:
        """Return game names sorted by most-recently accessed, newest first."""
        rows = self.conn.execute(
            "SELECT name FROM games WHERE last_accessed IS NOT NULL"
            " ORDER BY last_accessed DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [row[0] for row in rows]

    def reset_all_data(self) -> None:
        """Delete all attempts, sessions, model state, and allocator config."""
        with self.transaction():
            self.conn.execute("DELETE FROM attempts")
            self.conn.execute("DELETE FROM sessions")
            self.conn.execute("DELETE FROM model_state")
            self.conn.execute("DELETE FROM allocator_config")

    def reset_game_data(self, game_id: str) -> None:
        """Delete attempts, sessions, model state for a specific game."""
        with self.transaction():
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
            active=bool(row["active"]),
            ordinal=row["ordinal"] if "ordinal" in keys else 0,
            capture_run_id=row["capture_run_id"] if "capture_run_id" in keys else None,
            start_waypoint_id=row["start_waypoint_id"] if "start_waypoint_id" in keys else None,
            end_waypoint_id=row["end_waypoint_id"] if "end_waypoint_id" in keys else None,
            is_primary=bool(row["is_primary"]) if "is_primary" in keys else True,
            capture_session_id=row["capture_session_id"] if "capture_session_id" in keys else None,
        )

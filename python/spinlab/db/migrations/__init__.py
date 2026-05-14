"""Hand-rolled forward-only SQL migration runner.

Migrations live as ``NNNN_name.sql`` files in this directory and run in
filename order. Each applied file is recorded in the ``schema_migrations``
table by filename, so subsequent runs skip it.

Discipline:
  - Files are immutable once shipped. Fix mistakes with a NEW migration file,
    never by editing an existing one — that's how migration logs diverge
    between environments.
  - Each migration runs in its own transaction. Mid-file failure rolls back
    that migration only; earlier ones stay applied.
  - Forward-only. No down migrations.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent

# NNNN_anything.sql — the NNNN must be all digits so sort order matches
# numeric order through 9999. Pad if you ever cross that threshold.
_FILENAME_PATTERN = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def _ensure_tracking_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          filename TEXT PRIMARY KEY,
          applied_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _applied_set(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


def _discover(migrations_dir: Path) -> list[Path]:
    """Return migration files in filename order, validating the name format.

    Files that don't match ``NNNN_name.sql`` raise — silently skipping them
    is how you ship a "fixed" migration that never runs.
    """
    files = sorted(p for p in migrations_dir.iterdir() if p.suffix == ".sql")
    for p in files:
        if not _FILENAME_PATTERN.match(p.name):
            raise ValueError(
                f"Migration filename {p.name!r} does not match NNNN_name.sql"
            )
    return files


def run_migrations(
    conn: sqlite3.Connection,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> list[str]:
    """Apply any unapplied migrations in ``migrations_dir``. Returns the list
    of filenames newly applied this call (empty if everything was up to date).

    Each migration runs in its own transaction: BEGIN / migration SQL /
    schema_migrations INSERT / COMMIT are all in one ``executescript()`` call.
    On failure mid-script, the runner issues ROLLBACK so partial DDL doesn't
    leak. Filenames are validated by ``_FILENAME_PATTERN`` (digits + lowercase
    + underscore + ``.sql``) so the inlined filename in the INSERT is safe.
    """
    _ensure_tracking_table(conn)
    applied = _applied_set(conn)
    newly_applied: list[str] = []

    for path in _discover(migrations_dir):
        if path.name in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        script = (
            "BEGIN IMMEDIATE;\n"
            + sql
            + f"\nINSERT INTO schema_migrations (filename, applied_at) "
              f"VALUES ('{path.name}', datetime('now'));\n"
            + "COMMIT;\n"
        )
        try:
            conn.executescript(script)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass  # transaction was already rolled back by sqlite
            logger.exception("migration failed: %s", path.name)
            raise
        logger.info("migration applied: %s", path.name)
        newly_applied.append(path.name)

    return newly_applied

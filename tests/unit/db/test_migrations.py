"""Tests for the hand-rolled migration runner."""
import sqlite3

import pytest

from spinlab.db.migrations import MIGRATIONS_DIR, run_migrations


def _fresh_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _applied(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT filename FROM schema_migrations ORDER BY filename"
    ).fetchall()]


def test_fresh_db_applies_all_migrations():
    conn = _fresh_conn()
    newly_applied = run_migrations(conn)
    assert newly_applied == sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    assert _applied(conn) == newly_applied


def test_rerun_is_noop():
    """Once applied, a second run finds nothing to do."""
    conn = _fresh_conn()
    run_migrations(conn)
    expected = _applied(conn)
    assert run_migrations(conn) == []
    assert _applied(conn) == expected  # unchanged


def test_partial_state_applies_only_unapplied(tmp_path):
    """Pre-mark 0001 as applied; runner skips it and applies 0002 only."""
    migrations_dir = tmp_path / "migs"
    migrations_dir.mkdir()
    (migrations_dir / "0001_first.sql").write_text(
        "CREATE TABLE first_table (id INTEGER);"
    )
    (migrations_dir / "0002_second.sql").write_text(
        "CREATE TABLE second_table (id INTEGER);"
    )

    conn = _fresh_conn()
    # Simulate "0001 already applied" — but skip the actual CREATE so we can
    # verify the runner doesn't try to re-run it.
    conn.execute("""CREATE TABLE schema_migrations (
        filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)""")
    conn.execute(
        "INSERT INTO schema_migrations VALUES ('0001_first.sql', datetime('now'))"
    )
    conn.commit()

    newly_applied = run_migrations(conn, migrations_dir)
    assert newly_applied == ["0002_second.sql"]

    # second_table created, first_table NOT (we lied about 0001 being applied)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "second_table" in tables
    assert "first_table" not in tables


def test_bad_filename_rejected(tmp_path):
    """Malformed filenames raise rather than silently skipping — silent skip
    is how a "fixed" migration file ships and never runs."""
    migrations_dir = tmp_path / "migs"
    migrations_dir.mkdir()
    (migrations_dir / "bogus.sql").write_text("CREATE TABLE x (id INT);")

    conn = _fresh_conn()
    with pytest.raises(ValueError, match="does not match NNNN_name.sql"):
        run_migrations(conn, migrations_dir)


def test_failing_migration_rolls_back_and_stops(tmp_path):
    """Mid-file failure rolls back that migration. Earlier ones stay applied;
    later ones don't run."""
    migrations_dir = tmp_path / "migs"
    migrations_dir.mkdir()
    (migrations_dir / "0001_ok.sql").write_text(
        "CREATE TABLE one (id INTEGER);"
    )
    (migrations_dir / "0002_broken.sql").write_text(
        "CREATE TABLE two (id INTEGER); SELECT not_a_column FROM missing_table;"
    )
    (migrations_dir / "0003_after.sql").write_text(
        "CREATE TABLE three (id INTEGER);"
    )

    conn = _fresh_conn()
    with pytest.raises(sqlite3.OperationalError):
        run_migrations(conn, migrations_dir)

    # 0001 stays applied; 0002 rolled back (no 'two' table); 0003 never ran.
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "one" in tables
    assert "two" not in tables
    assert "three" not in tables
    assert _applied(conn) == ["0001_ok.sql"]


def test_initial_migration_records_in_tracking_table():
    """End-to-end via Database(): after init, 0001_initial.sql shows as applied."""
    from spinlab.db import Database
    db = Database(":memory:")
    applied = _applied(db.conn)
    assert "0001_initial.sql" in applied

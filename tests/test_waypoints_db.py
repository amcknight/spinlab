from spinlab.db import Database


def test_waypoints_table_exists():
    db = Database(":memory:")
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(waypoints)").fetchall()}
    assert cols == {"id", "game_id", "level_number", "endpoint_type",
                    "ordinal", "conditions_json"}


def test_waypoint_save_states_table_exists():
    db = Database(":memory:")
    cols = {r[1] for r in db.conn.execute(
        "PRAGMA table_info(waypoint_save_states)").fetchall()}
    assert cols == {"waypoint_id", "variant_type", "state_path", "is_default"}


def test_segments_table_has_waypoint_columns():
    db = Database(":memory:")
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(segments)").fetchall()}
    assert "start_waypoint_id" in cols
    assert "end_waypoint_id" in cols
    assert "is_primary" in cols


def test_attempts_table_has_condition_columns():
    db = Database(":memory:")
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(attempts)").fetchall()}
    assert "observed_start_conditions" in cols
    assert "observed_end_conditions" in cols
    assert "invalidated" in cols


def test_segment_variants_table_dropped():
    db = Database(":memory:")
    row = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='segment_variants'"
    ).fetchone()
    assert row is None

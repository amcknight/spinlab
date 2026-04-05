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


from spinlab.models import Waypoint


def test_upsert_and_get_waypoint():
    db = Database(":memory:")
    db.upsert_game("g1", "Game", "any%")
    w = Waypoint.make("g1", 5, "checkpoint", 1, {"powerup": "big"})
    db.upsert_waypoint(w)
    got = db.get_waypoint(w.id)
    assert got is not None
    assert got.id == w.id
    assert got.conditions_json == w.conditions_json
    assert got.endpoint_type == "checkpoint"


def test_upsert_waypoint_idempotent():
    db = Database(":memory:")
    db.upsert_game("g1", "Game", "any%")
    w = Waypoint.make("g1", 5, "goal", 0, {"powerup": "small"})
    db.upsert_waypoint(w)
    db.upsert_waypoint(w)
    rows = db.conn.execute("SELECT COUNT(*) FROM waypoints").fetchone()
    assert rows[0] == 1


from spinlab.models import WaypointSaveState


def test_save_state_attaches_to_waypoint():
    db = Database(":memory:")
    db.upsert_game("g1", "Game", "any%")
    w = Waypoint.make("g1", 1, "checkpoint", 1, {})
    db.upsert_waypoint(w)
    db.add_save_state(WaypointSaveState(
        waypoint_id=w.id, variant_type="hot",
        state_path="/tmp/hot.mss", is_default=True))
    got = db.get_save_state(w.id, "hot")
    assert got is not None
    assert got.state_path == "/tmp/hot.mss"

def test_get_default_save_state_falls_back_to_any():
    db = Database(":memory:")
    db.upsert_game("g1", "Game", "any%")
    w = Waypoint.make("g1", 1, "checkpoint", 1, {})
    db.upsert_waypoint(w)
    db.add_save_state(WaypointSaveState(
        waypoint_id=w.id, variant_type="cold",
        state_path="/tmp/cold.mss", is_default=False))
    got = db.get_default_save_state(w.id)
    assert got is not None
    assert got.variant_type == "cold"

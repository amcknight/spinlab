from spinlab.db import Database
from spinlab.reference_capture import ReferenceCapture
from spinlab.condition_registry import ConditionRegistry, ConditionDef, Scope


def _registry():
    return ConditionRegistry(definitions=[
        ConditionDef(name="powerup", address=0x19, size=1, type="enum",
                     values={0: "small", 1: "big"}, scope=Scope.game()),
    ])


def _bootstrap_db():
    db = Database(":memory:")
    db.upsert_game("g1", "Game", "any%")
    db.create_capture_run("run1", "g1", "run 1")
    return db


def test_entrance_then_goal_creates_segment_with_waypoints():
    db = _bootstrap_db()
    cap = ReferenceCapture()
    cap.capture_run_id = "run1"
    reg = _registry()
    cap.handle_entrance({
        "level": 5, "state_path": "/tmp/start.mss",
        "conditions": {"powerup": 0},  # raw: small
    })
    cap.handle_exit(
        {"level": 5, "goal": "goal", "conditions": {"powerup": 0}},
        "g1", db, reg)
    segs = db.get_active_segments("g1")
    assert len(segs) == 1
    assert segs[0].start_waypoint_id is not None
    assert segs[0].end_waypoint_id is not None
    assert segs[0].is_primary is True
    wp = db.get_waypoint(segs[0].start_waypoint_id)
    assert wp is not None
    assert '"powerup": "small"' in wp.conditions_json


def test_same_geography_different_powerup_creates_two_segments():
    db = _bootstrap_db()
    cap = ReferenceCapture()
    cap.capture_run_id = "run1"
    reg = _registry()
    # Run 1: entered small, exited
    cap.handle_entrance({"level": 5, "state_path": "/tmp/s1.mss",
                         "conditions": {"powerup": 0}})
    cap.handle_exit({"level": 5, "goal": "goal", "conditions": {"powerup": 0}},
                    "g1", db, reg)
    # Run 2: entered big, exited
    cap.pending_start = None  # reset
    cap.handle_entrance({"level": 5, "state_path": "/tmp/s2.mss",
                         "conditions": {"powerup": 1}})
    cap.handle_exit({"level": 5, "goal": "goal", "conditions": {"powerup": 1}},
                    "g1", db, reg)
    segs = db.get_active_segments("g1")
    assert len(segs) == 2
    primary_count = sum(1 for s in segs if s.is_primary)
    assert primary_count == 1     # second segment is NOT primary


def test_save_state_attaches_to_start_waypoint():
    db = _bootstrap_db()
    cap = ReferenceCapture()
    cap.capture_run_id = "run1"
    reg = _registry()
    cap.handle_entrance({"level": 5, "state_path": "/tmp/start.mss",
                         "conditions": {"powerup": 0}})
    cap.handle_exit({"level": 5, "goal": "goal", "conditions": {"powerup": 0}},
                    "g1", db, reg)
    segs = db.get_active_segments("g1")
    ss = db.get_default_save_state(segs[0].start_waypoint_id)
    assert ss is not None
    assert ss.state_path == "/tmp/start.mss"

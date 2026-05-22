import pytest

from spinlab.capture import SegmentRecorder
from spinlab.condition_registry import ConditionRegistry
from spinlab.db import Database
from spinlab.protocol import CheckpointEvent, LevelEntranceEvent, LevelExitEvent


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_game("g1", "Game", "any%")
    d.create_capture_run("run1", "g1", "Test Run", kind="live")
    d.create_capture_session("sess1", "run1", 1)
    yield d
    d.close()


@pytest.fixture
def registry():
    return ConditionRegistry()


def _make_cap(db: Database, registry: ConditionRegistry,
              run_id: str = "run1", sess_id: str = "sess1") -> SegmentRecorder:
    cap = SegmentRecorder(db, registry)
    cap.capture_run_id = run_id
    cap.current_capture_session_id = sess_id
    return cap


def _events_for_run(db, run_id):
    """All event rows in attempts for this run, in insertion order."""
    rows = db.conn.execute(
        "SELECT segment_id, episode_id, outcome, time_ms, source "
        "FROM attempts WHERE capture_run_id = ? ORDER BY id", (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def test_clean_segment_writes_one_survived_event(db, registry):
    """Entrance at t=1000, exit at t=6000, no deaths -> one `survived` event
    with time_ms=5000 (raw wall-clock from entrance to exit)."""
    cap = _make_cap(db, registry)
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=1000, state_path="/s.mss"))
    cap.handle_exit(LevelExitEvent(level=1, goal="normal", timestamp_ms=6000), "g1")

    events = _events_for_run(db, "run1")
    assert len(events) == 1
    assert events[0]["outcome"] == "survived"
    assert events[0]["time_ms"] == 5000
    assert events[0]["source"] == "reference"


def test_segment_with_one_death_writes_died_then_survived(db, registry):
    """Entrance at t=1000, death at t=3000, spawn at t=6000, exit at t=9000
    -> 2 events sharing one episode_id:
       died with time_ms=2000 (3000-1000)
       survived with time_ms=6000 (9000-3000; includes respawn animation)."""
    cap = _make_cap(db, registry)
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=1000, state_path="/s.mss"))
    cap.handle_death(timestamp_ms=3000)
    cap.handle_spawn_timing(timestamp_ms=6000)
    cap.handle_exit(LevelExitEvent(level=1, goal="normal", timestamp_ms=9000), "g1")

    events = _events_for_run(db, "run1")
    assert len(events) == 2
    assert events[0]["outcome"] == "died"
    assert events[0]["time_ms"] == 2000
    assert events[1]["outcome"] == "survived"
    assert events[1]["time_ms"] == 6000
    assert events[0]["episode_id"] == events[1]["episode_id"]


def test_two_deaths_in_segment_write_three_events(db, registry):
    """Two deaths and a clean tail produce died/died/survived events sharing
    one episode_id; each time_ms is the wall-clock delta since the previous
    event (or since the segment start, for the first death)."""
    cap = _make_cap(db, registry)
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=1000, state_path="/s.mss"))
    cap.handle_death(timestamp_ms=2000)
    cap.handle_death(timestamp_ms=3000)
    cap.handle_spawn_timing(timestamp_ms=4000)
    cap.handle_exit(LevelExitEvent(level=1, goal="normal", timestamp_ms=6000), "g1")

    events = _events_for_run(db, "run1")
    assert [e["outcome"] for e in events] == ["died", "died", "survived"]
    assert [e["time_ms"] for e in events] == [1000, 1000, 3000]
    assert len({e["episode_id"] for e in events}) == 1, \
        "all events of one segment share one episode_id"


def test_checkpoint_closes_segment_and_starts_new_episode(db, registry):
    """Entrance -> checkpoint -> exit produces two segments. Each segment's
    closing event is `survived`. The two segments have distinct episode_ids
    (one fresh episode per segment-pass)."""
    cap = _make_cap(db, registry)
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=1000, state_path="/s.mss"))
    cap.handle_checkpoint(
        CheckpointEvent(level_num=1, cp_ordinal=1, timestamp_ms=4000),
        "g1",
    )
    cap.handle_exit(LevelExitEvent(level=1, goal="normal", timestamp_ms=7000), "g1")

    events = _events_for_run(db, "run1")
    assert len(events) == 2
    assert all(e["outcome"] == "survived" for e in events)
    assert events[0]["time_ms"] == 3000
    assert events[1]["time_ms"] == 3000
    assert events[0]["episode_id"] != events[1]["episode_id"]
    assert events[0]["segment_id"] != events[1]["segment_id"]


def test_abort_drops_in_flight_segment(db, registry):
    """LevelExitEvent with goal='abort' drops the pending segment without
    writing any event rows or creating a segment row."""
    cap = _make_cap(db, registry)
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=1000, state_path="/s.mss"))
    cap.handle_death(timestamp_ms=2000)
    cap.handle_exit(LevelExitEvent(level=1, goal="abort", timestamp_ms=3000), "g1")

    events = _events_for_run(db, "run1")
    assert events == []
    seg_count = db.conn.execute(
        "SELECT COUNT(*) FROM segments WHERE capture_run_id = 'run1'",
    ).fetchone()[0]
    assert seg_count == 0


def test_clear_drops_in_flight_buffer(db, registry):
    """clear() drops the in-flight segment's buffered events. Events from
    previously-closed segments stay in attempts (clear is per-session
    in-memory only, not a DB rollback)."""
    cap = _make_cap(db, registry)
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=0, state_path="/s.mss"))
    cap.handle_exit(LevelExitEvent(level=1, goal="normal", timestamp_ms=5000), "g1")

    # One segment closed -> one event in attempts.
    assert len(_events_for_run(db, "run1")) == 1

    # Start a second segment but clear before closing -> no second event.
    cap.handle_entrance(LevelEntranceEvent(level=2, timestamp_ms=6000, state_path="/s2.mss"))
    cap.handle_death(timestamp_ms=7000)
    cap.clear()

    # First segment's event still present; second segment's buffered events lost.
    events = _events_for_run(db, "run1")
    assert len(events) == 1


async def test_handle_spawn_event_does_not_emit_event_row(db, registry):
    """SpawnEvent is used by the recorder for save-state and detector wiring,
    but is NOT itself an event row in attempts. Regression guard for the
    multi-session work where spawn timing was conflated with event emission."""
    from tests.conftest import FakeEmuBackend

    from spinlab.capture.reference import ReferenceController
    from spinlab.protocol import (
        DeathEvent,
        LevelEntranceEvent,
        LevelExitEvent,
        SpawnEvent,
    )

    ctl = ReferenceController(db, FakeEmuBackend(connected=True))
    ctl.recorder.capture_run_id = "run1"
    ctl.recorder.current_capture_session_id = "sess1"

    await ctl.handle_entrance(LevelEntranceEvent(
        level=1, state_path=None, timestamp_ms=1000, conditions={},
    ))
    ctl.handle_death(DeathEvent(timestamp_ms=2000))
    ctl.handle_spawn(SpawnEvent(
        level_num=1, state_path=None,
        is_cold_cp=False, cp_ordinal=None,
        timestamp_ms=3000, conditions={},
    ), game_id="g1")
    ctl.handle_exit(LevelExitEvent(
        level=1, goal="normal", timestamp_ms=5000, conditions={},
    ), game_id="g1")

    events = _events_for_run(db, "run1")
    # 2 events: died + survived. The spawn is NOT a row in attempts.
    assert [e["outcome"] for e in events] == ["died", "survived"]
    # Died at t=2000 (delta from entrance at t=1000) = 1000ms.
    # Survived at t=5000 (delta from death at t=2000) = 3000ms.
    assert [e["time_ms"] for e in events] == [1000, 3000]

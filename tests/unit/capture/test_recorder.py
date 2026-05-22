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


def _get_attempts(db: Database, source: str = "reference") -> list[dict]:
    """Return all attempt event rows for a given source, ordered by id."""
    rows = db.conn.execute(
        "SELECT * FROM attempts WHERE source = ? ORDER BY id",
        (source,),
    ).fetchall()
    return [dict(r) for r in rows]


def test_clean_segment_timing(db, registry):
    """Entrance at t=1000, exit at t=6000, no deaths → one SURVIVED event, time_ms=5000."""
    cap = _make_cap(db, registry)
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=1000, state_path="/s.mss"))
    cap.handle_exit(LevelExitEvent(level=1, goal="normal", timestamp_ms=6000), "g1")

    events = _get_attempts(db)
    assert len(events) == 1
    assert events[0]["outcome"] == "survived"
    assert events[0]["time_ms"] == 5000


def test_segment_with_deaths_timing(db, registry):
    """Entrance at t=1000, death at t=3000, spawn at t=6000, exit at t=9000
    → one DIED event (time_ms=2000) + one SURVIVED event (time_ms=3000)."""
    cap = _make_cap(db, registry)
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=1000, state_path="/s.mss"))
    cap.handle_death(timestamp_ms=3000)
    cap.handle_spawn_timing(timestamp_ms=6000)
    cap.handle_exit(LevelExitEvent(level=1, goal="normal", timestamp_ms=9000), "g1")

    events = _get_attempts(db)
    assert len(events) == 2
    assert events[0]["outcome"] == "died"
    assert events[0]["time_ms"] == 2000      # 3000 - 1000
    assert events[1]["outcome"] == "survived"
    assert events[1]["time_ms"] == 6000      # 9000 - 3000 (last_event_ms after death)


def test_checkpoint_splits_timing(db, registry):
    """Entrance at t=1000, checkpoint at t=4000, exit at t=7000
    → two segments, each with one SURVIVED event of 3000ms."""
    cap = _make_cap(db, registry)
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=1000, state_path="/s.mss"))
    cap.handle_checkpoint(
        CheckpointEvent(level_num=1, cp_ordinal=1, timestamp_ms=4000),
        "g1",
    )
    cap.handle_exit(LevelExitEvent(level=1, goal="normal", timestamp_ms=7000), "g1")

    events = _get_attempts(db)
    assert len(events) == 2
    assert all(e["outcome"] == "survived" for e in events)
    assert events[0]["time_ms"] == 3000   # 4000 - 1000
    assert events[1]["time_ms"] == 3000   # 7000 - 4000


def test_clear_resets_per_session_state(db, registry):
    """After clear(), a new segment starts fresh with zero deaths and correct event timing.
    Rows from before clear() still exist in the DB — clear is per-session in-memory only."""
    cap = _make_cap(db, registry)
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=0, state_path="/s.mss"))
    cap.handle_exit(LevelExitEvent(level=1, goal="normal", timestamp_ms=5000), "g1")

    # Confirm one event row exists before clear
    count_before = db.conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE source = 'reference'",
    ).fetchone()[0]
    assert count_before == 1

    cap.clear()

    # After clear, rows from before still exist (clear is NOT a DB rollback)
    count_after = db.conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE source = 'reference'",
    ).fetchone()[0]
    assert count_after == 1

    # After clear, start a new segment — must set ids again since clear() resets them
    cap.capture_run_id = "run1"
    cap.current_capture_session_id = "sess1"
    cap.handle_entrance(LevelEntranceEvent(level=2, timestamp_ms=10000, state_path="/s2.mss"))
    cap.handle_exit(LevelExitEvent(level=2, goal="normal", timestamp_ms=15000), "g1")

    events = _get_attempts(db)
    assert len(events) == 2
    # The new segment's event: SURVIVED, time_ms=5000, deaths=0
    new_event = events[-1]
    assert new_event["outcome"] == "survived"
    assert new_event["time_ms"] == 5000     # 15000 - 10000


def test_abort_exit_no_timing(db, registry):
    """Abort goal → no attempt rows inserted."""
    cap = _make_cap(db, registry)
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=1000, state_path="/s.mss"))
    cap.handle_exit(LevelExitEvent(level=1, goal="abort", timestamp_ms=5000), "g1")

    count = db.conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE source = 'reference'",
    ).fetchone()[0]
    assert count == 0


def test_death_via_handle_death_increments_counter(db, registry):
    """Two deaths during a segment → two DIED event rows + one SURVIVED."""
    cap = _make_cap(db, registry)
    cap.handle_entrance(LevelEntranceEvent(
        level=1, state_path="/s.mss",
        conditions={}, timestamp_ms=1000,
    ))
    cap.handle_death(timestamp_ms=2000)
    cap.handle_death(timestamp_ms=3000)
    cap.handle_spawn_timing(timestamp_ms=4000)
    cap.handle_exit(LevelExitEvent(level=1, goal="normal", timestamp_ms=6000), "g1")

    events = _get_attempts(db)
    assert len(events) == 3
    died_events = [e for e in events if e["outcome"] == "died"]
    assert len(died_events) == 2


async def test_handle_spawn_event_propagates_timestamp_ms(db, registry):
    """ReferenceController.handle_spawn must pass event.timestamp_ms through to
    the recorder's _last_spawn_ms, otherwise clean_tail_ms is always == time_ms
    for any segment with deaths. Regression test for the multi-session work."""
    from tests.conftest import FakeEmuBackend

    from spinlab.capture.reference import ReferenceController
    from spinlab.protocol import (
        DeathEvent,
        LevelEntranceEvent,
        LevelExitEvent,
        SpawnEvent,
    )
    # The module-level `db` fixture already pre-creates game="g1", run="run1",
    # session="sess1" — reuse those rather than building a parallel fixture.
    # connected=True so handle_entrance's save_state call hits FakeEmuBackend
    # (no-op recorder; we're testing timing, not the save itself).
    ctl = ReferenceController(db, FakeEmuBackend(connected=True))
    ctl.recorder.capture_run_id = "run1"
    ctl.recorder.current_capture_session_id = "sess1"

    await ctl.handle_entrance(LevelEntranceEvent(
        level=1, state_path=None, timestamp_ms=1000, conditions={},
    ))
    ctl.handle_death(DeathEvent())
    ctl.handle_spawn(SpawnEvent(
        level_num=1, state_path=None,
        is_cold_cp=False, cp_ordinal=None,
        timestamp_ms=3000, conditions={},
    ), game_id="g1")
    ctl.handle_exit(LevelExitEvent(
        level=1, goal="normal", timestamp_ms=5000, conditions={},
    ), game_id="g1")

    # Post-refactor: events land in `attempts` (not recorded_segment_times).
    # Entrance at t=1000; DeathEvent has no timestamp so it is dropped (None path).
    # Spawn timing at t=3000 sets _last_spawn_ms but does NOT advance _last_event_ms.
    # Exit at t=5000 → SURVIVED event with time_ms = 5000 - 1000 = 4000.
    events = _get_attempts(db)
    assert len(events) == 1
    assert events[0]["outcome"] == "survived"
    assert events[0]["time_ms"] == 4000   # 5000 - 1000 (death had no timestamp)

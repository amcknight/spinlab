import pytest

from spinlab.capture import SegmentRecorder
from spinlab.condition_registry import ConditionRegistry
from spinlab.db import Database
from spinlab.protocol import CheckpointEvent, LevelEntranceEvent, LevelExitEvent


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_game("g1", "Game", "any%")
    d.create_capture_run("run1", "g1", "Test Run", draft=True)
    d.create_capture_session("sess1", "run1", 1)
    yield d
    d.close()


@pytest.fixture
def registry():
    return ConditionRegistry()


def _make_cap(run_id: str = "run1", sess_id: str = "sess1") -> SegmentRecorder:
    cap = SegmentRecorder()
    cap.capture_run_id = run_id
    cap.current_capture_session_id = sess_id
    return cap


def test_clean_segment_timing(db, registry):
    """Entrance at t=1000, exit at t=6000, no deaths → time_ms=5000, deaths=0, clean_tail_ms=5000."""
    cap = _make_cap()
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=1000, state_path="/s.mss"))
    cap.handle_exit(LevelExitEvent(level=1, goal="goal", timestamp_ms=6000), "g1", db, registry)

    times = db.conn.execute(
        "SELECT time_ms, deaths, clean_tail_ms FROM recorded_segment_times "
        "WHERE capture_session_id = ? ORDER BY id",
        ("sess1",),
    ).fetchall()
    assert len(times) == 1
    assert times[0][0] == 5000
    assert times[0][1] == 0
    assert times[0][2] == 5000


def test_segment_with_deaths_timing(db, registry):
    """Entrance at t=1000, death at t=3000, spawn at t=6000, exit at t=9000
    → time_ms=8000, deaths=1, clean_tail_ms=3000."""
    cap = _make_cap()
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=1000, state_path="/s.mss"))
    cap.handle_death(timestamp_ms=3000)
    cap.handle_spawn_timing(timestamp_ms=6000)
    cap.handle_exit(LevelExitEvent(level=1, goal="goal", timestamp_ms=9000), "g1", db, registry)

    times = db.conn.execute(
        "SELECT time_ms, deaths, clean_tail_ms FROM recorded_segment_times "
        "WHERE capture_session_id = ? ORDER BY id",
        ("sess1",),
    ).fetchall()
    assert len(times) == 1
    assert times[0][0] == 8000
    assert times[0][1] == 1
    assert times[0][2] == 3000


def test_checkpoint_splits_timing(db, registry):
    """Entrance at t=1000, checkpoint at t=4000, exit at t=7000 → two segments, each 3000ms."""
    cap = _make_cap()
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=1000, state_path="/s.mss"))
    cap.handle_checkpoint(
        CheckpointEvent(level_num=1, cp_ordinal=1, timestamp_ms=4000),
        "g1", db, registry,
    )
    cap.handle_exit(LevelExitEvent(level=1, goal="goal", timestamp_ms=7000), "g1", db, registry)

    times = db.conn.execute(
        "SELECT time_ms FROM recorded_segment_times "
        "WHERE capture_session_id = ? ORDER BY id",
        ("sess1",),
    ).fetchall()
    assert len(times) == 2
    assert times[0][0] == 3000
    assert times[1][0] == 3000


def test_clear_resets_per_session_state(db, registry):
    """After clear(), a new segment starts fresh with zero deaths and correct clean_tail.
    Rows from before clear() still exist in the DB — clear is per-session in-memory only."""
    cap = _make_cap()
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=0, state_path="/s.mss"))
    cap.handle_exit(LevelExitEvent(level=1, goal="goal", timestamp_ms=5000), "g1", db, registry)

    # Confirm one row exists before clear
    count_before = db.conn.execute(
        "SELECT COUNT(*) FROM recorded_segment_times WHERE capture_session_id = ?",
        ("sess1",),
    ).fetchone()[0]
    assert count_before == 1

    cap.clear()

    # After clear, rows from before still exist (clear is NOT a DB rollback)
    count_after = db.conn.execute(
        "SELECT COUNT(*) FROM recorded_segment_times WHERE capture_session_id = ?",
        ("sess1",),
    ).fetchone()[0]
    assert count_after == 1

    # After clear, start a new segment — must set ids again since clear() resets them
    cap.capture_run_id = "run1"
    cap.current_capture_session_id = "sess1"
    cap.handle_entrance(LevelEntranceEvent(level=2, timestamp_ms=10000, state_path="/s2.mss"))
    cap.handle_exit(LevelExitEvent(level=2, goal="goal", timestamp_ms=15000), "g1", db, registry)

    new_times = db.conn.execute(
        "SELECT time_ms, deaths, clean_tail_ms FROM recorded_segment_times "
        "WHERE capture_session_id = ? ORDER BY id DESC LIMIT 1",
        ("sess1",),
    ).fetchall()
    assert len(new_times) == 1
    assert new_times[0][1] == 0           # deaths = 0
    assert new_times[0][2] == 5000        # clean_tail_ms = 5000 (no deaths)


def test_abort_exit_no_timing(db, registry):
    """Abort goal → no timing rows inserted."""
    cap = _make_cap()
    cap.handle_entrance(LevelEntranceEvent(level=1, timestamp_ms=1000, state_path="/s.mss"))
    cap.handle_exit(LevelExitEvent(level=1, goal="abort", timestamp_ms=5000), "g1", db, registry)

    count = db.conn.execute(
        "SELECT COUNT(*) FROM recorded_segment_times WHERE capture_session_id = ?",
        ("sess1",),
    ).fetchone()[0]
    assert count == 0


def test_death_via_handle_death_increments_counter(db, registry):
    """Two deaths during a segment are reflected in the recorded segment time."""
    cap = _make_cap()
    cap.handle_entrance(LevelEntranceEvent(
        level=1, state_path="/s.mss",
        conditions={}, timestamp_ms=1000,
    ))
    cap.handle_death(timestamp_ms=2000)
    cap.handle_death(timestamp_ms=3000)
    cap.handle_spawn_timing(timestamp_ms=4000)
    cap.handle_exit(LevelExitEvent(level=1, goal="goal", timestamp_ms=6000), "g1", db, registry)

    times = db.conn.execute(
        "SELECT deaths FROM recorded_segment_times WHERE capture_session_id = ?",
        ("sess1",),
    ).fetchall()
    assert len(times) == 1
    assert times[0][0] == 2


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
        level_num=1, state_captured=False, state_path=None,
        is_cold_cp=False, cp_ordinal=None,
        timestamp_ms=3000, conditions={},
    ), game_id="g1")
    ctl.handle_exit(LevelExitEvent(
        level=1, goal="exit", timestamp_ms=5000, conditions={},
    ), game_id="g1")

    rows = db.drain_recorded_segment_times_for_run("run1")
    assert len(rows) == 1
    row = rows[0]
    assert row["deaths"] == 1
    assert row["time_ms"] == 4000        # 5000 - 1000
    assert row["clean_tail_ms"] == 2000  # 5000 - 3000 (NOT 4000)

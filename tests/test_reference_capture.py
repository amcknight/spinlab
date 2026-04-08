import pytest
from unittest.mock import MagicMock
from spinlab.reference_capture import ReferenceCapture, RefSegmentTime
from spinlab.condition_registry import ConditionRegistry


@pytest.fixture
def db():
    mock = MagicMock()
    mock.upsert_waypoint = MagicMock()
    mock.upsert_segment = MagicMock()
    mock.add_save_state = MagicMock()
    mock.conn = MagicMock()
    mock.conn.execute = MagicMock(
        return_value=MagicMock(fetchone=MagicMock(return_value=None))
    )
    return mock


@pytest.fixture
def registry():
    return ConditionRegistry()


def _make_cap(run_id: str = "run1") -> ReferenceCapture:
    cap = ReferenceCapture()
    cap.capture_run_id = run_id
    return cap


def test_clean_segment_timing(db, registry):
    """Entrance at t=1000, exit at t=6000, no deaths → time_ms=5000, deaths=0, clean_tail_ms=5000."""
    cap = _make_cap()
    cap.handle_entrance({"level": 1, "timestamp_ms": 1000, "state_path": "/s.mss"})
    cap.handle_exit({"level": 1, "goal": "goal", "timestamp_ms": 6000}, "g1", db, registry)

    assert len(cap.segment_times) == 1
    st = cap.segment_times[0]
    assert st.time_ms == 5000
    assert st.deaths == 0
    assert st.clean_tail_ms == 5000


def test_segment_with_deaths_timing(db, registry):
    """Entrance at t=1000, death at t=3000, spawn at t=6000, exit at t=9000
    → time_ms=8000, deaths=1, clean_tail_ms=3000."""
    cap = _make_cap()
    cap.handle_entrance({"level": 1, "timestamp_ms": 1000, "state_path": "/s.mss"})
    cap.handle_death(timestamp_ms=3000)
    cap.handle_spawn_timing(timestamp_ms=6000)
    cap.handle_exit({"level": 1, "goal": "goal", "timestamp_ms": 9000}, "g1", db, registry)

    assert len(cap.segment_times) == 1
    st = cap.segment_times[0]
    assert st.time_ms == 8000
    assert st.deaths == 1
    assert st.clean_tail_ms == 3000


def test_checkpoint_splits_timing(db, registry):
    """Entrance at t=1000, checkpoint at t=4000, exit at t=7000 → two segments, each 3000ms."""
    cap = _make_cap()
    cap.handle_entrance({"level": 1, "timestamp_ms": 1000, "state_path": "/s.mss"})
    cap.handle_checkpoint(
        {"level_num": 1, "cp_ordinal": 1, "timestamp_ms": 4000},
        "g1", db, registry,
    )
    cap.handle_exit({"level": 1, "goal": "goal", "timestamp_ms": 7000}, "g1", db, registry)

    assert len(cap.segment_times) == 2
    assert cap.segment_times[0].time_ms == 3000
    assert cap.segment_times[1].time_ms == 3000


def test_clear_resets_segment_times(db, registry):
    """After timing accumulates, clear() empties segment_times."""
    cap = _make_cap()
    cap.handle_entrance({"level": 1, "timestamp_ms": 0, "state_path": "/s.mss"})
    cap.handle_exit({"level": 1, "goal": "goal", "timestamp_ms": 5000}, "g1", db, registry)
    assert len(cap.segment_times) == 1

    cap.clear()
    assert cap.segment_times == []
    assert cap._deaths_in_segment == 0
    assert cap._last_spawn_ms is None


def test_abort_exit_no_timing(db, registry):
    """Abort goal → no segment times recorded."""
    cap = _make_cap()
    cap.handle_entrance({"level": 1, "timestamp_ms": 1000, "state_path": "/s.mss"})
    cap.handle_exit({"level": 1, "goal": "abort", "timestamp_ms": 5000}, "g1", db, registry)

    assert cap.segment_times == []


def test_death_via_handle_death_increments_counter(db, registry):
    """handle_death increments _deaths_in_segment."""
    cap = ReferenceCapture()
    cap.capture_run_id = "run1"
    cap.handle_entrance({
        "level": 1, "state_path": "/s.mss",
        "conditions": {}, "timestamp_ms": 1000,
    })
    cap.handle_death(timestamp_ms=2000)
    cap.handle_death(timestamp_ms=3000)
    assert cap._deaths_in_segment == 2

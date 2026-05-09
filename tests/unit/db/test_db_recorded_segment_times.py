"""Tests for RecordedSegmentTimesMixin."""
import pytest

from spinlab.db import Database


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_game("smw", "Super Mario World", "any%")
    d.create_capture_run("run_1", "smw", "Test Run", draft=True)
    d.create_capture_session("sess_1", "run_1", 1)
    d.create_capture_session("sess_2", "run_1", 2)
    yield d
    d.close()


def test_add_and_drain_recorded_segment_times(db):
    db.add_recorded_segment_time("sess_1", "seg_a", time_ms=1000, deaths=0, clean_tail_ms=1000)
    db.add_recorded_segment_time("sess_2", "seg_b", time_ms=2000, deaths=1, clean_tail_ms=500)
    drained = db.drain_recorded_segment_times_for_run("run_1")
    assert len(drained) == 2
    by_seg = {r["segment_id"]: r for r in drained}
    assert by_seg["seg_a"]["time_ms"] == 1000
    assert by_seg["seg_a"]["deaths"] == 0
    assert by_seg["seg_a"]["clean_tail_ms"] == 1000
    assert by_seg["seg_b"]["time_ms"] == 2000
    assert by_seg["seg_b"]["deaths"] == 1
    assert by_seg["seg_b"]["clean_tail_ms"] == 500
    # Drain deletes
    rows = db.conn.execute("SELECT COUNT(*) FROM recorded_segment_times").fetchone()
    assert rows[0] == 0


def test_drain_only_pulls_from_specified_run(db):
    # Non-draft so it doesn't collide with run_1 under the
    # one-paused-run-per-game unique index. Drain filters by run_id and
    # doesn't care about draft state.
    db.create_capture_run("run_other", "smw", "Other", draft=False)
    db.create_capture_session("sess_other", "run_other", 1)
    db.add_recorded_segment_time("sess_1", "seg_x", time_ms=100, deaths=0, clean_tail_ms=100)
    db.add_recorded_segment_time("sess_other", "seg_y", time_ms=200, deaths=0, clean_tail_ms=200)
    drained = db.drain_recorded_segment_times_for_run("run_1")
    assert len(drained) == 1
    assert drained[0]["segment_id"] == "seg_x"
    rows = db.conn.execute("SELECT COUNT(*) FROM recorded_segment_times").fetchone()
    assert rows[0] == 1

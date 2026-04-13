"""Tests for reference_seeding.seed_reference_attempts."""
import pytest
from spinlab.db import Database
from spinlab.models import Attempt, AttemptSource, Segment, Waypoint
from spinlab.reference_capture import RefSegmentTime
from spinlab.reference_seeding import seed_reference_attempts


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("g", "Game", "any%")
    d.create_capture_run("run1", "g", "Test Run")
    return d


def _make_segment(db, seg_id, game_id="g", level=1, ref_id="run1"):
    wp_s = Waypoint.make(game_id, level, "entrance", 0, {})
    wp_e = Waypoint.make(game_id, level, "goal", 0, {})
    db.upsert_waypoint(wp_s)
    db.upsert_waypoint(wp_e)
    seg = Segment(
        id=seg_id, game_id=game_id, level_number=level,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        reference_id=ref_id,
        start_waypoint_id=wp_s.id, end_waypoint_id=wp_e.id,
    )
    db.upsert_segment(seg)
    return seg


def test_seed_attempts_inserted(db):
    """Two segments seeded → both appear in attempts table with correct values."""
    _make_segment(db, "seg1", level=1)
    _make_segment(db, "seg2", level=2)

    times = [
        RefSegmentTime(segment_id="seg1", time_ms=5000, deaths=0, clean_tail_ms=5000),
        RefSegmentTime(segment_id="seg2", time_ms=8000, deaths=1, clean_tail_ms=3000),
    ]
    seed_reference_attempts(db, "run1", times)

    rows1 = db.get_segment_attempts("seg1")
    assert len(rows1) == 1
    assert rows1[0]["time_ms"] == 5000
    assert rows1[0]["deaths"] == 0
    assert rows1[0]["clean_tail_ms"] == 5000
    assert rows1[0]["completed"] == 1

    rows2 = db.get_segment_attempts("seg2")
    assert len(rows2) == 1
    assert rows2[0]["time_ms"] == 8000
    assert rows2[0]["deaths"] == 1
    assert rows2[0]["clean_tail_ms"] == 3000
    assert rows2[0]["completed"] == 1


def test_seed_attempts_source_is_reference(db):
    """Seeded attempts have source='reference'."""
    _make_segment(db, "seg1", level=1)

    times = [RefSegmentTime(segment_id="seg1", time_ms=4000, deaths=0, clean_tail_ms=4000)]
    seed_reference_attempts(db, "run1", times)

    row = db.conn.execute(
        "SELECT source FROM attempts WHERE segment_id = 'seg1'"
    ).fetchone()
    assert row is not None
    assert row["source"] == "reference"


def test_seed_with_empty_times(db):
    """Empty segment_times list → 0 attempts inserted."""
    seed_reference_attempts(db, "run1", [])

    row = db.conn.execute("SELECT COUNT(*) as cnt FROM attempts").fetchone()
    assert row["cnt"] == 0


def test_seed_returns_count(db):
    """Return value equals number of RefSegmentTime objects passed in."""
    _make_segment(db, "seg1", level=1)
    _make_segment(db, "seg2", level=2)
    _make_segment(db, "seg3", level=3)

    times = [
        RefSegmentTime(segment_id="seg1", time_ms=1000, deaths=0, clean_tail_ms=1000),
        RefSegmentTime(segment_id="seg2", time_ms=2000, deaths=0, clean_tail_ms=2000),
        RefSegmentTime(segment_id="seg3", time_ms=3000, deaths=0, clean_tail_ms=3000),
    ]
    count = seed_reference_attempts(db, "run1", times)
    assert count == 3


def test_draft_save_seeds_and_rebuilds(db):
    """Full flow: DraftManager.save() triggers seeding + estimator rebuild."""
    from unittest.mock import MagicMock
    from spinlab.draft_manager import DraftManager
    from spinlab.reference_capture import RefSegmentTime

    db.create_capture_run("run1_draft", "g", "Draft", draft=True)
    _make_segment(db, "seg1_draft", ref_id="run1_draft")

    times = [RefSegmentTime(segment_id="seg1_draft", time_ms=5000, deaths=0, clean_tail_ms=5000)]

    dm = DraftManager()
    dm.enter_draft("run1_draft", 1)

    mock_scheduler = MagicMock()
    result = dm.save(db, "Saved Run", segment_times=times, scheduler=mock_scheduler)

    assert result.status.value == "ok"

    # Verify attempt was inserted
    attempts = db.get_segment_attempts("seg1_draft")
    assert len(attempts) == 1
    assert attempts[0]["time_ms"] == 5000

    # Verify rebuild was called
    mock_scheduler.rebuild_all_states.assert_called_once()


def test_draft_save_without_times_skips_seeding(db):
    """DraftManager.save() without segment_times doesn't seed or rebuild."""
    from unittest.mock import MagicMock
    from spinlab.draft_manager import DraftManager

    db.create_capture_run("run2", "g", "Draft2", draft=True)

    dm = DraftManager()
    dm.enter_draft("run2", 0)

    mock_scheduler = MagicMock()
    result = dm.save(db, "No Times", segment_times=None, scheduler=mock_scheduler)

    assert result.status.value == "ok"
    mock_scheduler.rebuild_all_states.assert_not_called()

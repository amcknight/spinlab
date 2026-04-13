"""Tests for draft reference lifecycle in SessionManager, and reference seeding."""
import pytest
from unittest.mock import MagicMock

from spinlab.db import Database
from spinlab.models import Mode, Segment, Status, Waypoint
from spinlab.session_manager import SessionManager
from spinlab.capture import DraftManager, RecordedSegmentTime
from spinlab.capture.draft import _seed_reference_attempts as seed_reference_attempts


def make_sm(mock_db, mock_tcp, tmp_path):
    sm = SessionManager(db=mock_db, tcp=mock_tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
    sm.game_id = "abcdef0123456789"
    sm.game_name = "Test Game"
    return sm


class TestStopReferenceCreatesDraft:
    async def test_stop_reference_enters_draft_state(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        await sm.start_reference()
        run_id = sm.ref_capture.capture_run_id
        sm.ref_capture.segments_count = 5

        await sm.stop_reference()
        assert sm.mode == Mode.IDLE
        assert sm.draft.run_id == run_id
        assert sm.draft.segments_count == 5


class TestReplayCreatesDraft:
    async def test_replay_finished_enters_draft_state(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.mode = Mode.REPLAY
        sm.ref_capture.capture_run_id = "replay_abc"
        sm.ref_capture.segments_count = 8

        await sm.route_event({"event": "replay_finished", "frames_played": 5000})
        assert sm.mode == Mode.IDLE
        assert sm.draft.run_id == "replay_abc"
        assert sm.draft.segments_count == 8

    async def test_replay_error_with_segments_enters_draft(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.mode = Mode.REPLAY
        sm.ref_capture.capture_run_id = "replay_abc"
        sm.ref_capture.segments_count = 3

        await sm.route_event({"event": "replay_error", "message": "fail"})
        assert sm.draft.run_id == "replay_abc"
        assert sm.draft.segments_count == 3

    async def test_replay_error_no_segments_auto_discards(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.mode = Mode.REPLAY
        sm.ref_capture.capture_run_id = "replay_abc"
        sm.ref_capture.segments_count = 0

        await sm.route_event({"event": "replay_error", "message": "fail"})
        assert sm.draft.run_id is None
        mock_db.hard_delete_capture_run.assert_called_once_with("replay_abc")


class TestDraftGuards:
    async def test_start_reference_blocked_by_draft(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.draft.run_id = "draft_pending"
        result = await sm.start_reference()
        assert result.status == Status.DRAFT_PENDING

    async def test_start_replay_blocked_by_draft(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.draft.run_id = "draft_pending"
        result = await sm.start_replay("/data/test.spinrec")
        assert result.status == Status.DRAFT_PENDING

    async def test_start_practice_blocked_by_draft(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.draft.run_id = "draft_pending"
        result = await sm.start_practice()
        assert result.status == Status.DRAFT_PENDING


class TestSaveAndDiscard:
    async def test_save_draft(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.draft.run_id = "live_abc"
        sm.draft.segments_count = 5

        result = await sm.save_draft("My Run")
        assert result.status == Status.OK
        assert sm.draft.run_id is None
        assert sm.draft.segments_count == 0
        mock_db.promote_draft.assert_called_once_with("live_abc", "My Run")
        mock_db.set_active_capture_run.assert_called_once_with("live_abc")

    async def test_discard_draft(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.draft.run_id = "live_abc"
        sm.draft.segments_count = 5

        result = await sm.discard_draft()
        assert result.status == Status.OK
        assert sm.draft.run_id is None
        assert sm.draft.segments_count == 0
        mock_db.hard_delete_capture_run.assert_called_once_with("live_abc")

    async def test_save_no_draft_returns_error(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        result = await sm.save_draft("Name")
        assert result.status == Status.NO_DRAFT


class TestStopReplayDraft:
    async def test_stop_replay_with_segments_enters_draft(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.mode = Mode.REPLAY
        sm.ref_capture.capture_run_id = "replay_abc"
        sm.ref_capture.segments_count = 5

        result = await sm.stop_replay()
        assert result.status == Status.STOPPED
        assert sm.draft.run_id == "replay_abc"
        assert sm.draft.segments_count == 5

    async def test_stop_replay_no_segments_auto_discards(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.mode = Mode.REPLAY
        sm.ref_capture.capture_run_id = "replay_abc"
        sm.ref_capture.segments_count = 0

        result = await sm.stop_replay()
        assert result.status == Status.STOPPED
        assert sm.draft.run_id is None
        mock_db.hard_delete_capture_run.assert_called_once_with("replay_abc")


class TestOnDisconnectDraft:
    def test_disconnect_with_segments_enters_draft(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.mode = Mode.REFERENCE
        sm.ref_capture.capture_run_id = "live_abc"
        sm.ref_capture.segments_count = 3

        sm.on_disconnect()
        assert sm.draft.run_id == "live_abc"
        assert sm.draft.segments_count == 3

    def test_disconnect_no_segments_auto_discards(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.mode = Mode.REFERENCE
        sm.ref_capture.capture_run_id = "live_abc"
        sm.ref_capture.segments_count = 0

        sm.on_disconnect()
        assert sm.draft.run_id is None
        mock_db.hard_delete_capture_run.assert_called_once_with("live_abc")

    def test_disconnect_no_ref_state_is_noop(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.mode = Mode.IDLE

        sm.on_disconnect()
        assert sm.draft.run_id is None
        mock_db.hard_delete_capture_run.assert_not_called()


class TestGetStateDraft:
    def test_get_state_includes_draft(self, mock_db, mock_tcp, tmp_path):
        sm = make_sm(mock_db, mock_tcp, tmp_path)
        sm.draft.run_id = "live_abc"
        sm.draft.segments_count = 12
        state = sm.get_state()
        assert state["draft"] == {"run_id": "live_abc", "segments_captured": 12}


# ---------------------------------------------------------------------------
# Tests for _seed_reference_attempts (from test_reference_seeding.py)
# ---------------------------------------------------------------------------

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
        RecordedSegmentTime(segment_id="seg1", time_ms=5000, deaths=0, clean_tail_ms=5000),
        RecordedSegmentTime(segment_id="seg2", time_ms=8000, deaths=1, clean_tail_ms=3000),
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

    times = [RecordedSegmentTime(segment_id="seg1", time_ms=4000, deaths=0, clean_tail_ms=4000)]
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
        RecordedSegmentTime(segment_id="seg1", time_ms=1000, deaths=0, clean_tail_ms=1000),
        RecordedSegmentTime(segment_id="seg2", time_ms=2000, deaths=0, clean_tail_ms=2000),
        RecordedSegmentTime(segment_id="seg3", time_ms=3000, deaths=0, clean_tail_ms=3000),
    ]
    count = seed_reference_attempts(db, "run1", times)
    assert count == 3


def test_draft_save_seeds_and_rebuilds(db):
    """Full flow: DraftManager.save() triggers seeding + estimator rebuild."""
    db.create_capture_run("run1_draft", "g", "Draft", draft=True)
    _make_segment(db, "seg1_draft", ref_id="run1_draft")

    times = [RecordedSegmentTime(segment_id="seg1_draft", time_ms=5000, deaths=0, clean_tail_ms=5000)]

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
    db.create_capture_run("run2", "g", "Draft2", draft=True)

    dm = DraftManager()
    dm.enter_draft("run2", 0)

    mock_scheduler = MagicMock()
    result = dm.save(db, "No Times", segment_times=None, scheduler=mock_scheduler)

    assert result.status.value == "ok"
    mock_scheduler.rebuild_all_states.assert_not_called()


def test_save_draft_seeds_attempts_and_rebuilds_model(db):
    """Reference run segment times become first-attempt practice data after save.

    Regression for the seeding bug diagnosed in the 2026-04-13 cleanup pass spec
    (section 2). Events are constructed from the actual protocol dataclasses so
    the test is faithful to what the recorder receives at runtime. If
    LevelEntranceEvent / LevelExitEvent don't carry the timestamps the recorder
    needs to build RecordedSegmentTime, this test will catch it.
    """
    from dataclasses import asdict

    from spinlab.capture import SegmentRecorder
    from spinlab.condition_registry import ConditionRegistry
    from spinlab.models import AttemptSource
    from spinlab.protocol import LevelEntranceEvent, LevelExitEvent

    recorder = SegmentRecorder()
    registry = ConditionRegistry()

    run_id = "run1"
    recorder.capture_run_id = run_id

    entrance = asdict(LevelEntranceEvent(level=1, timestamp_ms=0))
    recorder.handle_entrance(entrance)

    # In production the exit fires ~12s after entrance; the recorder relies on
    # a timestamp difference to compute time_ms. If the event dataclass doesn't
    # expose timestamp_ms, the recorder silently drops the segment timing.
    exit_event = asdict(LevelExitEvent(level=1, goal="exit", timestamp_ms=12345))
    recorder.handle_exit(exit_event, game_id="g", db=db, registry=registry)

    assert len(recorder.segment_times) == 1, (
        "recorder must produce a RecordedSegmentTime from entrance+exit — "
        "if this fails, LevelEntranceEvent or LevelExitEvent is missing timestamp_ms"
    )
    assert recorder.segment_times[0].time_ms == 12345

    draft = DraftManager()
    draft.enter_draft(run_id, recorder.segments_count)

    scheduler = MagicMock()
    result = draft.save(
        db, name="Regression run",
        segment_times=recorder.segment_times,
        scheduler=scheduler,
    )

    assert result.status.name == "OK"
    rows = db.conn.execute(
        "SELECT source FROM attempts WHERE session_id = ?", (run_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == AttemptSource.REFERENCE.value
    scheduler.rebuild_all_states.assert_called_once()

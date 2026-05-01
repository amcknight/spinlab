"""Multi-session reference run lifecycle tests."""
import pytest
import pytest_asyncio

from spinlab.capture import ReferenceController
from spinlab.db import Database
from spinlab.errors import RunPendingError, SessionDeleteAfterFinalizeError
from spinlab.models import Mode, Status

from tests.conftest import FakeTcpManager


@pytest.fixture
def db(tmp_path):
    d = Database(":memory:")
    d.upsert_game("smw", "Super Mario World", "any%")
    yield d
    d.close()


@pytest.fixture
def tcp():
    return FakeTcpManager(connected=True)


@pytest.fixture
def controller(db, tcp):
    return ReferenceController(db, tcp)


@pytest_asyncio.fixture
async def started_session(controller, db, tmp_path):
    """A controller in RECORDING with an open session under a fresh run."""
    result = await controller.start_reference(
        Mode.IDLE, "smw", tmp_path, run_name="Test Run",
    )
    assert result.new_mode == Mode.REFERENCE
    return controller


# --- Single-session save_and_finish path ---

@pytest.mark.asyncio
async def test_save_and_finish_seeds_attempts_and_finalizes(started_session, db):
    sess_id = started_session.recorder.current_capture_session_id
    run_id = started_session.recorder.capture_run_id
    db.add_recorded_segment_time(sess_id, "seg_x", time_ms=1500, deaths=0, clean_tail_ms=1500)
    _make_minimal_segment(db, run_id, sess_id, "seg_x")

    result = await started_session.save_and_finish_run(Mode.REFERENCE, name="My Run")

    assert result.status == Status.OK
    assert result.new_mode == Mode.IDLE
    row = db.conn.execute("SELECT draft, name FROM capture_runs WHERE id = ?", (run_id,)).fetchone()
    assert row[0] == 0
    assert row[1] == "My Run"
    attempts = db.conn.execute(
        "SELECT segment_id, time_ms FROM attempts WHERE segment_id = 'seg_x'"
    ).fetchall()
    assert [(r[0], r[1]) for r in attempts] == [("seg_x", 1500)]
    rows = db.conn.execute(
        "SELECT COUNT(*) FROM recorded_segment_times "
        "WHERE capture_session_id = ?", (sess_id,)
    ).fetchone()
    assert rows[0] == 0


# --- Multi-session: stop then resume ---

@pytest.mark.asyncio
async def test_stop_session_pauses_run(started_session, db):
    run_id = started_session.recorder.capture_run_id
    result = await started_session.stop_reference(Mode.REFERENCE)
    assert result.new_mode == Mode.IDLE
    assert started_session.paused_run_id == run_id
    sessions = db.list_capture_sessions_for_run(run_id)
    assert len(sessions) == 1
    assert sessions[0]["end_reason"] == "stopped"
    draft = db.conn.execute(
        "SELECT draft FROM capture_runs WHERE id = ?", (run_id,)
    ).fetchone()[0]
    assert draft == 1


@pytest.mark.asyncio
async def test_resume_creates_new_session_under_same_run(started_session, db, tmp_path):
    run_id = started_session.recorder.capture_run_id
    await started_session.stop_reference(Mode.REFERENCE)

    result = await started_session.resume_reference(Mode.IDLE, "smw", tmp_path)
    assert result.new_mode == Mode.REFERENCE
    assert started_session.recorder.capture_run_id == run_id
    assert started_session.paused_run_id is None
    sessions = db.list_capture_sessions_for_run(run_id)
    assert [s["ordinal"] for s in sessions] == [1, 2]
    assert sessions[1]["ended_at"] is None


# --- Discard ---

@pytest.mark.asyncio
async def test_discard_run_hard_deletes_everything(started_session, db):
    run_id = started_session.recorder.capture_run_id
    sess_id = started_session.recorder.current_capture_session_id
    db.add_recorded_segment_time(sess_id, "seg_x", time_ms=100, deaths=0, clean_tail_ms=100)
    await started_session.stop_reference(Mode.REFERENCE)

    result = await started_session.discard_run()
    assert result.status == Status.OK
    assert started_session.paused_run_id is None
    assert db.list_capture_sessions_for_run(run_id) == []
    rows = db.conn.execute("SELECT COUNT(*) FROM capture_runs WHERE id = ?", (run_id,)).fetchone()
    assert rows[0] == 0


# --- Delete session ---

@pytest.mark.asyncio
async def test_delete_capture_session_while_paused(started_session, db, tmp_path):
    run_id = started_session.recorder.capture_run_id
    sess_id = started_session.recorder.current_capture_session_id
    await started_session.stop_reference(Mode.REFERENCE)
    await started_session.resume_reference(Mode.IDLE, "smw", tmp_path)
    sess_2 = started_session.recorder.current_capture_session_id
    await started_session.stop_reference(Mode.REFERENCE)
    result = await started_session.delete_capture_session(sess_id)
    assert result.status == Status.OK
    sessions = db.list_capture_sessions_for_run(run_id)
    assert {s["id"] for s in sessions} == {sess_2}


@pytest.mark.asyncio
async def test_delete_capture_session_after_finalize_rejected(started_session, db):
    run_id = started_session.recorder.capture_run_id
    sess_id = started_session.recorder.current_capture_session_id
    await started_session.save_and_finish_run(Mode.REFERENCE, name="Done")
    with pytest.raises(SessionDeleteAfterFinalizeError):
        await started_session.delete_capture_session(sess_id)


# --- One paused run per game ---

@pytest.mark.asyncio
async def test_start_reference_rejects_when_paused_run_exists(started_session, tmp_path):
    await started_session.stop_reference(Mode.REFERENCE)
    with pytest.raises(RunPendingError):
        await started_session.start_reference(
            Mode.IDLE, "smw", tmp_path, run_name="Other",
        )


# --- Disconnect ---

@pytest.mark.asyncio
async def test_disconnect_pauses_run(started_session, db):
    run_id = started_session.recorder.capture_run_id
    started_session.handle_disconnect()
    assert started_session.paused_run_id == run_id
    sessions = db.list_capture_sessions_for_run(run_id)
    assert sessions[0]["end_reason"] == "disconnected"


# --- Helpers ---

def _make_minimal_segment(db, run_id, sess_id, seg_id):
    """Insert a minimal valid segment row for FK referential integrity."""
    from spinlab.models import EndpointType, Segment, Waypoint
    wp_a = Waypoint.make("smw", 1, EndpointType.ENTRANCE, 0, {})
    wp_b = Waypoint.make("smw", 1, EndpointType.GOAL, 0, {})
    db.upsert_waypoint(wp_a)
    db.upsert_waypoint(wp_b)
    seg = Segment(
        id=seg_id, game_id="smw", level_number=1,
        start_type=EndpointType.ENTRANCE, start_ordinal=0,
        end_type=EndpointType.GOAL, end_ordinal=0,
        start_waypoint_id=wp_a.id, end_waypoint_id=wp_b.id,
        reference_id=run_id, capture_session_id=sess_id,
    )
    db.upsert_segment(seg)

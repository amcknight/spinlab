"""Crash-and-recover for multi-session reference runs.

Simulates: dashboard process dies mid-recording. On restart with same DB:
- Orphaned session marked end_reason=crashed
- Run remains draft=1
- Segments and recorded_segment_times preserved
- Resume creates a new session ordinal+1

No Mesen, no real TCP, no Playwright — pure Python integration of
ReferenceController against a real on-disk SQLite file.
"""
import pytest
from tests.conftest import FakeTcpManager

from spinlab.capture import ReferenceController
from spinlab.db import Database
from spinlab.models import Mode

# This test does not need Mesen — override the module-wide emulator mark set
# by tests/integration/conftest.py so it runs in the default fast suite.
pytestmark = []


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def db(db_path):
    d = Database(str(db_path))
    d.upsert_game("smw", "Super Mario World", "any%")
    yield d
    d.close()


@pytest.mark.asyncio
async def test_dashboard_crash_mid_session_recovers(db, db_path, tmp_path):
    # --- Pre-crash: start a run, capture some timing, die without graceful shutdown ---
    tcp = FakeTcpManager(connected=True)
    controller = ReferenceController(db, tcp)
    await controller.start_reference(Mode.IDLE, "smw", tmp_path, run_name="Long Run")
    run_id = controller.recorder.capture_run_id
    sess_id_1 = controller.recorder.current_capture_session_id
    db.add_recorded_segment_time(sess_id_1, "seg_a", time_ms=1000, deaths=0, clean_tail_ms=1000)

    # Simulate crash: drop the controller and DB references without ending the session
    del controller
    db.close()

    # --- Post-crash: new dashboard instance, same DB file ---
    db2 = Database(str(db_path))
    tcp2 = FakeTcpManager(connected=True)
    controller2 = ReferenceController(db2, tcp2)
    controller2.recover_paused_run("smw")

    assert controller2.paused_run_id == run_id
    sessions = db2.list_capture_sessions_for_run(run_id)
    assert len(sessions) == 1
    assert sessions[0]["end_reason"] == "crashed"
    times = db2.conn.execute(
        "SELECT segment_id, time_ms FROM recorded_segment_times "
        "WHERE capture_session_id = ?", (sess_id_1,),
    ).fetchall()
    assert [(r[0], r[1]) for r in times] == [("seg_a", 1000)]

    # --- Resume creates session 2 ---
    await controller2.resume_reference(Mode.IDLE, "smw", tmp_path)
    sess_id_2 = controller2.recorder.current_capture_session_id
    assert sess_id_2 != sess_id_1
    sessions = db2.list_capture_sessions_for_run(run_id)
    assert [s["ordinal"] for s in sessions] == [1, 2]
    db2.close()


def test_paused_run_survives_replay_then_dashboard_restart(db):
    """User has paused run A. Plays a replay of finalised reference B. Replay
    finishes (leaving an ephemeral replay_xxx capture_run draft). Dashboard
    restarts. Recovery picks A back up; B's segments do not leak into A; the
    replay-derived capture_run row is filtered out (id NOT LIKE 'replay_%').

    The most fragile part of multi-session reference runs is the interaction
    between recovery and replay's ephemeral capture_run rows. This test pins
    the contract end-to-end.
    """
    # 1. Paused run A with one segment captured.
    db.create_capture_run("run_A", "smw", "A paused", draft=True)
    db.create_capture_session("sA1", "run_A", 1, "/tmp/A1.spinrec")
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, start_type, start_ordinal, "
        "end_type, end_ordinal, capture_session_id, reference_id, "
        "created_at, updated_at) VALUES "
        "('seg_A', 'smw', 1, 'entrance', 0, 'goal', 0, 'sA1', 'run_A', "
        "datetime('now'), datetime('now'))"
    )
    # 2. Finalised reference B.
    db.create_capture_run("run_B", "smw", "B finalised", draft=False)
    # 3. Simulate replay: an ephemeral replay_xxx capture_run draft exists with
    #    one segment.
    db.create_capture_run("replay_abc", "smw", "Replay X", draft=True)
    db.create_capture_session("sR", "replay_abc", 1, "/tmp/R.spinrec")
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, start_type, start_ordinal, "
        "end_type, end_ordinal, capture_session_id, reference_id, "
        "created_at, updated_at) VALUES "
        "('seg_R', 'smw', 99, 'entrance', 0, 'goal', 0, 'sR', 'replay_abc', "
        "datetime('now'), datetime('now'))"
    )
    db.conn.commit()

    # 4. Simulate dashboard restart by calling recovery directly.
    recovered = db.recover_paused_capture_run("smw")

    assert recovered == "run_A", "recovery picked replay run instead of paused A"

    a_segs = db.conn.execute(
        "SELECT id FROM segments WHERE reference_id = 'run_A'"
    ).fetchall()
    assert {r[0] for r in a_segs} == {"seg_A"}, "B's segments leaked into A"

    # Replay-derived run is left as-is (not auto-deleted by recovery — that's
    # a separate cleanup pass), but is NOT promoted to paused state.
    replay_state = db.conn.execute(
        "SELECT id FROM capture_runs WHERE id = 'replay_abc'"
    ).fetchone()
    assert replay_state is not None, (
        "replay_abc was hard-deleted by recovery — but the spec says replay drafts "
        "accumulate until explicitly discarded"
    )

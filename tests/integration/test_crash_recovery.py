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
import pytest_asyncio
from pathlib import Path

from spinlab.capture import ReferenceController
from spinlab.db import Database
from spinlab.models import Mode

from tests.conftest import FakeTcpManager

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

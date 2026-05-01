"""Tests for CaptureSessionsMixin."""
import pytest
from spinlab.db import Database


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_game("smw", "Super Mario World", "any%")
    d.create_capture_run("run_1", "smw", "Test Run", draft=True)
    yield d
    d.close()


def test_create_and_get_capture_session(db):
    db.create_capture_session(
        session_id="sess_1", capture_run_id="run_1",
        ordinal=1, spinrec_path="/tmp/sess_1.spinrec",
    )
    sess = db.get_capture_session("sess_1")
    assert sess is not None
    assert sess["id"] == "sess_1"
    assert sess["capture_run_id"] == "run_1"
    assert sess["ordinal"] == 1
    assert sess["spinrec_path"] == "/tmp/sess_1.spinrec"
    assert sess["ended_at"] is None
    assert sess["end_reason"] is None


def test_get_capture_session_missing_returns_none(db):
    assert db.get_capture_session("nonexistent") is None

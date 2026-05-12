"""Tests for the attempt invalidation flow.

Uses a real SQLite Database + FakeEmuBackend so assertions hit the
actual `attempts.invalidated` column rather than mock call counts.
"""
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from tests.conftest import FakeEmuBackend, make_seg_with_state

from spinlab.db import Database
from spinlab.models import Attempt, AttemptSource
from spinlab.protocol import AttemptInvalidatedEvent
from spinlab.session_manager import SessionManager


@pytest.fixture
def db(tmp_path):
    """Real Database with one game + one segment ready to receive attempts."""
    d = Database(tmp_path / "inv.db")
    d.upsert_game("g1", "Test Game", "any%")
    state_file = tmp_path / "hot.mss"
    state_file.write_bytes(b"")
    seg = make_seg_with_state(d, "g1", 1, "entrance", "goal", state_file)
    d._seg_id = seg.id
    return d


@pytest.fixture
def emu():
    return FakeEmuBackend(connected=True)


def make_sm(db, emu):
    return SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")


def _log_attempt(db: Database, segment_id: str, session_id: str) -> int:
    """Insert an attempt for the segment + parent_id; return its id."""
    attempt = Attempt(
        segment_id=segment_id,
        parent_id=session_id,
        completed=True,
        time_ms=5000,
        deaths=0,
        clean_tail_ms=5000,
        source=AttemptSource.PRACTICE,
        created_at=datetime.now(UTC),
    )
    return db.log_attempt(attempt)


def _is_invalidated(db: Database, attempt_id: int) -> bool:
    row = db.conn.execute(
        "SELECT invalidated FROM attempts WHERE id = ?", (attempt_id,),
    ).fetchone()
    return bool(row[0])


class TestAttemptInvalidatedEvent:
    async def test_marks_last_attempt_as_invalidated(self, db, emu):
        """attempt_invalidated event flips the `invalidated` flag on the
        most recently logged attempt for the active practice session."""
        sm = make_sm(db, emu)

        fake_session = MagicMock()
        fake_session.session_id = "sess1"
        sm.practice_session = fake_session

        first_id = _log_attempt(db, db._seg_id, "sess1")
        last_id = _log_attempt(db, db._seg_id, "sess1")

        assert _is_invalidated(db, first_id) is False
        assert _is_invalidated(db, last_id) is False

        await sm.route_event(AttemptInvalidatedEvent())

        # Only the most recent attempt for this session flips.
        assert _is_invalidated(db, first_id) is False
        assert _is_invalidated(db, last_id) is True

    async def test_only_invalidates_attempts_in_active_session(self, db, emu):
        """Attempts from a different parent_id are untouched."""
        sm = make_sm(db, emu)

        fake_session = MagicMock()
        fake_session.session_id = "sess_active"
        sm.practice_session = fake_session

        other_id = _log_attempt(db, db._seg_id, "sess_other")
        active_id = _log_attempt(db, db._seg_id, "sess_active")

        await sm.route_event(AttemptInvalidatedEvent())

        assert _is_invalidated(db, other_id) is False
        assert _is_invalidated(db, active_id) is True

    async def test_no_op_when_no_practice_session(self, db, emu):
        """Without an active practice session, attempts are left alone."""
        sm = make_sm(db, emu)
        sm.practice_session = None

        attempt_id = _log_attempt(db, db._seg_id, "sess1")

        await sm.route_event(AttemptInvalidatedEvent())

        assert _is_invalidated(db, attempt_id) is False

    async def test_no_op_when_no_attempts_yet(self, db, emu):
        """Empty session — no exception, no DB writes."""
        sm = make_sm(db, emu)

        fake_session = MagicMock()
        fake_session.session_id = "sess_empty"
        sm.practice_session = fake_session

        # No attempts logged for sess_empty.
        await sm.route_event(AttemptInvalidatedEvent())

        # And the row count for that session is still zero.
        count = db.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE parent_id = ?", ("sess_empty",),
        ).fetchone()[0]
        assert count == 0

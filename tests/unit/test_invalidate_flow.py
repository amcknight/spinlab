# tests/test_invalidate_flow.py
"""Tests for the attempt invalidation flow."""
from unittest.mock import MagicMock

from spinlab.protocol import AttemptInvalidatedEvent
from spinlab.session_manager import SessionManager


def make_sm(mock_db, mock_tcp, **kwargs):
    defaults = dict(db=mock_db, tcp=mock_tcp, rom_dir=None, default_category="any%")
    defaults.update(kwargs)
    return SessionManager(**defaults)


class TestAttemptInvalidatedEvent:
    async def test_marks_last_attempt_as_invalidated(self, mock_db, mock_tcp):
        """attempt_invalidated event marks the most recent attempt invalidated."""
        sm = make_sm(mock_db, mock_tcp)

        # Simulate a live practice session with a known session_id.
        fake_session = MagicMock()
        fake_session.session_id = "sess1"
        sm.practice_session = fake_session

        mock_db.get_last_practice_attempt.return_value = 42

        await sm.route_event(AttemptInvalidatedEvent())

        mock_db.get_last_practice_attempt.assert_called_once_with(session_id="sess1")
        mock_db.set_attempt_invalidated.assert_called_once_with(42, True)

    async def test_no_op_when_no_practice_session(self, mock_db, mock_tcp):
        """attempt_invalidated is silently ignored when no practice session is active."""
        sm = make_sm(mock_db, mock_tcp)
        sm.practice_session = None

        await sm.route_event(AttemptInvalidatedEvent())

        mock_db.get_last_practice_attempt.assert_not_called()
        mock_db.set_attempt_invalidated.assert_not_called()

    async def test_no_op_when_no_attempts_yet(self, mock_db, mock_tcp):
        """attempt_invalidated is silently ignored when the session has no attempts."""
        sm = make_sm(mock_db, mock_tcp)

        fake_session = MagicMock()
        fake_session.session_id = "sess_empty"
        sm.practice_session = fake_session

        mock_db.get_last_practice_attempt.return_value = None

        await sm.route_event(AttemptInvalidatedEvent())

        mock_db.get_last_practice_attempt.assert_called_once_with(session_id="sess_empty")
        mock_db.set_attempt_invalidated.assert_not_called()

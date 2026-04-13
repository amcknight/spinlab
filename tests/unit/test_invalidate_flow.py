# tests/test_invalidate_flow.py
"""Tests for the attempt invalidation flow: Lua combo → Python event handler."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from spinlab.protocol import SetInvalidateComboCmd
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

        await sm.route_event({"event": "attempt_invalidated"})

        mock_db.get_last_practice_attempt.assert_called_once_with(session_id="sess1")
        mock_db.set_attempt_invalidated.assert_called_once_with(42, True)

    async def test_no_op_when_no_practice_session(self, mock_db, mock_tcp):
        """attempt_invalidated is silently ignored when no practice session is active."""
        sm = make_sm(mock_db, mock_tcp)
        sm.practice_session = None

        await sm.route_event({"event": "attempt_invalidated"})

        mock_db.get_last_practice_attempt.assert_not_called()
        mock_db.set_attempt_invalidated.assert_not_called()

    async def test_no_op_when_no_attempts_yet(self, mock_db, mock_tcp):
        """attempt_invalidated is silently ignored when the session has no attempts."""
        sm = make_sm(mock_db, mock_tcp)

        fake_session = MagicMock()
        fake_session.session_id = "sess_empty"
        sm.practice_session = fake_session

        mock_db.get_last_practice_attempt.return_value = None

        await sm.route_event({"event": "attempt_invalidated"})

        mock_db.get_last_practice_attempt.assert_called_once_with(session_id="sess_empty")
        mock_db.set_attempt_invalidated.assert_not_called()


class TestSetInvalidateCombo:
    async def test_combo_pushed_to_lua_on_install(self, mock_db, mock_tcp, monkeypatch):
        """_install_condition_registry pushes set_invalidate_combo to Lua when connected."""
        sm = make_sm(mock_db, mock_tcp, invalidate_combo=["L", "Select"])
        mock_tcp.is_connected = True

        import spinlab.condition_registry as cr_mod
        monkeypatch.setattr(
            cr_mod,
            "load_registry_for_game",
            lambda gid, games_root=None: cr_mod.ConditionRegistry(definitions=[]),
        )

        await sm._install_condition_registry("anygame")

        sent_cmds = [c[0][0] for c in mock_tcp.send_command.call_args_list]
        combo_cmds = [c for c in sent_cmds if isinstance(c, SetInvalidateComboCmd)]
        assert len(combo_cmds) == 1
        assert combo_cmds[0].combo == ["L", "Select"]

    async def test_combo_not_pushed_when_disconnected(self, mock_db, mock_tcp, monkeypatch):
        """_install_condition_registry skips TCP push when not connected."""
        sm = make_sm(mock_db, mock_tcp, invalidate_combo=["L", "Select"])
        mock_tcp.is_connected = False

        import spinlab.condition_registry as cr_mod
        monkeypatch.setattr(
            cr_mod,
            "load_registry_for_game",
            lambda gid, games_root=None: cr_mod.ConditionRegistry(definitions=[]),
        )

        await sm._install_condition_registry("anygame")

        mock_tcp.send_command.assert_not_called()

    async def test_custom_combo_reflected_in_push(self, mock_db, mock_tcp, monkeypatch):
        """A non-default invalidate_combo is sent verbatim to Lua."""
        sm = make_sm(mock_db, mock_tcp, invalidate_combo=["R", "Start"])
        mock_tcp.is_connected = True

        import spinlab.condition_registry as cr_mod
        monkeypatch.setattr(
            cr_mod,
            "load_registry_for_game",
            lambda gid, games_root=None: cr_mod.ConditionRegistry(definitions=[]),
        )

        await sm._install_condition_registry("anygame")

        sent_cmds = [c[0][0] for c in mock_tcp.send_command.call_args_list]
        combo_cmds = [c for c in sent_cmds if isinstance(c, SetInvalidateComboCmd)]
        assert len(combo_cmds) == 1
        assert combo_cmds[0].combo == ["R", "Start"]

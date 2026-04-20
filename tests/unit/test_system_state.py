"""Tests for SystemState — single source of truth for system mode."""
from spinlab.models import Mode
from spinlab.system_state import SystemState


class TestSystemStateDefaults:
    def test_defaults_to_idle_with_no_game(self):
        state = SystemState()
        assert state.mode == Mode.IDLE
        assert state.game_id is None
        assert state.game_name is None

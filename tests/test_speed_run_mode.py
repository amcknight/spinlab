"""Tests for Speed Run mode enum and transitions."""
import pytest
from spinlab.models import Mode, transition_mode


def test_speed_run_mode_exists():
    assert Mode.SPEED_RUN.value == "speed_run"


def test_idle_to_speed_run_legal():
    assert transition_mode(Mode.IDLE, Mode.SPEED_RUN) == Mode.SPEED_RUN


def test_speed_run_to_idle_legal():
    assert transition_mode(Mode.SPEED_RUN, Mode.IDLE) == Mode.IDLE


def test_speed_run_to_practice_illegal():
    with pytest.raises(ValueError):
        transition_mode(Mode.SPEED_RUN, Mode.PRACTICE)

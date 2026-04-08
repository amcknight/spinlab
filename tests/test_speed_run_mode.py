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


from spinlab.protocol import (
    SpeedRunLoadCmd, SpeedRunStopCmd,
    SpeedRunCheckpointEvent, SpeedRunDeathEvent, SpeedRunCompleteEvent,
    parse_event, serialize_command,
)


def test_speed_run_load_cmd_serializes():
    cmd = SpeedRunLoadCmd(
        id="seg1",
        state_path="/entrance.mss",
        description="Level 1",
        checkpoints=[
            {"ordinal": 1, "state_path": "/cp1.mss"},
            {"ordinal": 2, "state_path": "/cp2.mss"},
        ],
        expected_time_ms=45000,
        auto_advance_delay_ms=1000,
    )
    s = serialize_command(cmd)
    assert '"event": "speed_run_load"' in s or '"event":"speed_run_load"' in s


def test_speed_run_stop_cmd_serializes():
    cmd = SpeedRunStopCmd()
    s = serialize_command(cmd)
    assert "speed_run_stop" in s


def test_parse_speed_run_checkpoint_event():
    raw = {"event": "speed_run_checkpoint", "ordinal": 1, "elapsed_ms": 12340, "split_ms": 12340}
    evt = parse_event(raw)
    assert isinstance(evt, SpeedRunCheckpointEvent)
    assert evt.ordinal == 1
    assert evt.split_ms == 12340


def test_parse_speed_run_death_event():
    raw = {"event": "speed_run_death", "elapsed_ms": 5230, "split_ms": 5230}
    evt = parse_event(raw)
    assert isinstance(evt, SpeedRunDeathEvent)
    assert evt.split_ms == 5230


def test_parse_speed_run_complete_event():
    raw = {"event": "speed_run_complete", "elapsed_ms": 45600, "split_ms": 12000}
    evt = parse_event(raw)
    assert isinstance(evt, SpeedRunCompleteEvent)
    assert evt.elapsed_ms == 45600

"""ControllerMenuDetector — R arms a command menu; X dispatches pause."""
from spinlab.protocol import ControllerCommandEvent, ControllerMenuArmedEvent
from spinlab.retroarch.menu_detector import (
    ARM_THRESHOLD_FRAMES,
    BUTTON_R,
    BUTTON_X,
    ControllerMenuDetector,
)
from spinlab.retroarch.snapshot import MemorySnapshot


def _snap(controller_held: int = 0) -> MemorySnapshot:
    return MemorySnapshot(
        game_mode=0, level_num=0, room_num=0, level_start=0, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0,
        cp_entrance=0, controller_held=controller_held,
    )


def _run(detector, snaps):
    out = []
    for s in snaps:
        out.extend(detector.step(s))
    return out


def test_r_held_below_threshold_does_not_arm():
    d = ControllerMenuDetector()
    events = _run(d, [_snap(BUTTON_R)] * (ARM_THRESHOLD_FRAMES - 1))
    assert events == []


def test_r_held_past_threshold_arms():
    d = ControllerMenuDetector()
    events = _run(d, [_snap(BUTTON_R)] * ARM_THRESHOLD_FRAMES)
    armed = [e for e in events if isinstance(e, ControllerMenuArmedEvent)]
    assert len(armed) == 1 and armed[0].armed is True


def test_armed_then_x_press_emits_pause():
    d = ControllerMenuDetector()
    snaps = [_snap(BUTTON_R)] * ARM_THRESHOLD_FRAMES + [_snap(BUTTON_R | BUTTON_X)]
    events = _run(d, snaps)
    cmds = [e for e in events if isinstance(e, ControllerCommandEvent)]
    assert len(cmds) == 1 and cmds[0].command == "pause"


def test_x_does_not_refire_while_held():
    d = ControllerMenuDetector()
    snaps = (
        [_snap(BUTTON_R)] * ARM_THRESHOLD_FRAMES
        + [_snap(BUTTON_R | BUTTON_X)] * 5
    )
    events = _run(d, snaps)
    cmds = [e for e in events if isinstance(e, ControllerCommandEvent)]
    assert len(cmds) == 1


def test_release_r_disarms():
    d = ControllerMenuDetector()
    snaps = [_snap(BUTTON_R)] * ARM_THRESHOLD_FRAMES + [_snap(0)]
    events = _run(d, snaps)
    armed = [e for e in events if isinstance(e, ControllerMenuArmedEvent)]
    assert [e.armed for e in armed] == [True, False]


def test_lone_x_without_r_does_nothing():
    d = ControllerMenuDetector()
    events = _run(d, [_snap(BUTTON_X)] * ARM_THRESHOLD_FRAMES)
    assert events == []

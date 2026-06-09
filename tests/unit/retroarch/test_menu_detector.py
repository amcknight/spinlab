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


def test_rx_chord_at_arm_does_not_fire_until_fresh_press():
    """R+X held together: arms but does NOT dispatch on the arm frame.
    Pause fires only after X is released and pressed again while armed."""
    d = ControllerMenuDetector()
    # R+X held continuously through and past the arm threshold.
    chord = [_snap(BUTTON_R | BUTTON_X)] * (ARM_THRESHOLD_FRAMES + 3)
    events = _run(d, chord)
    assert [e for e in events if isinstance(e, ControllerCommandEvent)] == []
    # Release X (keep R held), then press X again -> one fresh dispatch.
    events = _run(d, [_snap(BUTTON_R), _snap(BUTTON_R | BUTTON_X)])
    cmds = [e for e in events if isinstance(e, ControllerCommandEvent)]
    assert len(cmds) == 1 and cmds[0].command == "pause"


def test_release_r_while_x_held_disarms_without_dispatch():
    """Letting go of R while X is still down disarms cleanly — no command."""
    d = ControllerMenuDetector()
    snaps = (
        [_snap(BUTTON_R)] * ARM_THRESHOLD_FRAMES  # arm with R only
        + [_snap(BUTTON_X)]                        # R released, X still held
    )
    events = _run(d, snaps)
    assert [e for e in events if isinstance(e, ControllerCommandEvent)] == []
    armed = [e for e in events if isinstance(e, ControllerMenuArmedEvent)]
    assert [e.armed for e in armed] == [True, False]


def test_disarm_then_rearm_dispatches_again():
    """A full arm -> dispatch -> disarm -> re-arm -> dispatch cycle works twice."""
    d = ControllerMenuDetector()
    cycle = (
        [_snap(BUTTON_R)] * ARM_THRESHOLD_FRAMES + [_snap(BUTTON_R | BUTTON_X)]  # fire 1
        + [_snap(0)]                                                            # disarm
        + [_snap(BUTTON_R)] * ARM_THRESHOLD_FRAMES + [_snap(BUTTON_R | BUTTON_X)]  # fire 2
    )
    events = _run(d, cycle)
    cmds = [e for e in events if isinstance(e, ControllerCommandEvent)]
    assert len(cmds) == 2 and all(c.command == "pause" for c in cmds)

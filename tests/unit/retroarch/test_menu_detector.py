"""ControllerMenuDetector — R is a held modifier; X pressed after R = pause."""
from spinlab.protocol import ControllerCommandEvent, ControllerMenuArmedEvent
from spinlab.retroarch.menu_detector import (
    BUTTON_LEFT,
    BUTTON_R,
    BUTTON_RIGHT,
    BUTTON_X,
    BUTTON_Y,
    HELD1,
    HELD2,
    ControllerMenuDetector,
)
from spinlab.retroarch.snapshot import MemorySnapshot


def _snap(controller_held: int = 0, controller_held_1: int = 0) -> MemorySnapshot:
    return MemorySnapshot(
        game_mode=0, level_num=0, room_num=0, level_start=0, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0,
        cp_entrance=0, controller_held=controller_held,
        controller_held_1=controller_held_1,
    )


def _run(detector, snaps):
    out = []
    for s in snaps:
        out.extend(detector.step(s))
    return out


def _cmds(events):
    return [e for e in events if isinstance(e, ControllerCommandEvent)]


def _armed(events):
    return [e.armed for e in events if isinstance(e, ControllerMenuArmedEvent)]


def test_r_opens_menu_immediately():
    d = ControllerMenuDetector()
    events = _run(d, [_snap(BUTTON_R)])
    assert _armed(events) == [True]


def test_x_pressed_after_r_pauses():
    d = ControllerMenuDetector()
    events = _run(d, [_snap(BUTTON_R), _snap(BUTTON_R | BUTTON_X)])
    cmds = _cmds(events)
    assert len(cmds) == 1 and cmds[0].command == "pause"


def test_x_already_held_when_r_pressed_does_not_fire():
    """Holding X (e.g. run) and then tapping R must NOT pause — X wasn't pressed
    *after* R. It's seeded as already-held."""
    d = ControllerMenuDetector()
    events = _run(d, [_snap(BUTTON_X), _snap(BUTTON_R | BUTTON_X)])
    assert _cmds(events) == []


def test_simultaneous_rx_does_not_fire_but_fresh_x_does():
    """R+X arriving on the same frame is seeded (no fire); releasing X and
    pressing it again while R is held fires once."""
    d = ControllerMenuDetector()
    events = _run(d, [
        _snap(BUTTON_R | BUTTON_X),  # same-frame chord -> seeded, no fire
        _snap(BUTTON_R),             # X released
        _snap(BUTTON_R | BUTTON_X),  # fresh X press -> fire
    ])
    cmds = _cmds(events)
    assert len(cmds) == 1 and cmds[0].command == "pause"


def test_x_does_not_refire_while_held():
    d = ControllerMenuDetector()
    events = _run(d, [_snap(BUTTON_R)] + [_snap(BUTTON_R | BUTTON_X)] * 5)
    assert len(_cmds(events)) == 1


def test_release_r_closes_menu():
    d = ControllerMenuDetector()
    events = _run(d, [_snap(BUTTON_R), _snap(0)])
    assert _armed(events) == [True, False]


def test_lone_x_without_r_does_nothing():
    d = ControllerMenuDetector()
    events = _run(d, [_snap(BUTTON_X), _snap(BUTTON_X)])
    assert events == []


def test_release_r_while_x_held_closes_without_dispatch():
    d = ControllerMenuDetector()
    events = _run(d, [_snap(BUTTON_R), _snap(BUTTON_X)])  # R down, then R up / X held
    assert _cmds(events) == []
    assert _armed(events) == [True, False]


def test_reopen_dispatches_again():
    """open -> X (fire) -> close -> reopen -> X (fire) works twice."""
    d = ControllerMenuDetector()
    events = _run(d, [
        _snap(BUTTON_R), _snap(BUTTON_R | BUTTON_X),  # fire 1
        _snap(0),                                     # close
        _snap(BUTTON_R), _snap(BUTTON_R | BUTTON_X),  # fire 2
    ])
    cmds = _cmds(events)
    assert len(cmds) == 2 and all(c.command == "pause" for c in cmds)


# A registry with one command on the $15 (HELD1) byte, to prove the (byte,bit)
# mechanism reads the second byte. Mirrors how Phase 2 will register Y commands.
_HELD1_REGISTRY = {(HELD1, 0x40): "toggle_test"}


def test_command_on_held1_byte_dispatches():
    """A command bound to a $15 bit fires when that bit is pressed after R."""
    d = ControllerMenuDetector(commands=_HELD1_REGISTRY)
    events = _run(d, [
        _snap(controller_held=BUTTON_R),                          # R down -> menu open
        _snap(controller_held=BUTTON_R, controller_held_1=0x40),  # Y pressed after
    ])
    cmds = _cmds(events)
    assert len(cmds) == 1 and cmds[0].command == "toggle_test"


def test_held1_command_already_held_at_open_is_seeded():
    """Y already held when R goes down does NOT fire (seed), same as X."""
    d = ControllerMenuDetector(commands=_HELD1_REGISTRY)
    events = _run(d, [
        _snap(controller_held_1=0x40),                            # Y held, no R
        _snap(controller_held=BUTTON_R, controller_held_1=0x40),  # R down, Y already held
    ])
    assert _cmds(events) == []


def test_pause_still_dispatches_with_default_registry():
    """The default registry (pause on $17) is unchanged by the generalization."""
    d = ControllerMenuDetector()
    events = _run(d, [_snap(controller_held=BUTTON_R),
                      _snap(controller_held=BUTTON_R | BUTTON_X)])
    cmds = _cmds(events)
    assert len(cmds) == 1 and cmds[0].command == "pause"


def test_y_after_r_dispatches_toggle_practice_default_registry():
    """R held, then Y pressed -> toggle_practice (Y is a real $15 command now)."""
    d = ControllerMenuDetector()
    events = _run(d, [
        _snap(controller_held=BUTTON_R),                              # R down -> menu open
        _snap(controller_held=BUTTON_R, controller_held_1=BUTTON_Y),  # Y pressed after
    ])
    cmds = _cmds(events)
    assert len(cmds) == 1 and cmds[0].command == "toggle_practice"


def test_right_after_r_dispatches_next_segment():
    d = ControllerMenuDetector()
    events = _run(d, [
        _snap(controller_held=BUTTON_R),
        _snap(controller_held=BUTTON_R, controller_held_1=BUTTON_RIGHT),
    ])
    cmds = _cmds(events)
    assert len(cmds) == 1 and cmds[0].command == "next_segment"


def test_left_after_r_dispatches_prev_segment():
    d = ControllerMenuDetector()
    events = _run(d, [
        _snap(controller_held=BUTTON_R),
        _snap(controller_held=BUTTON_R, controller_held_1=BUTTON_LEFT),
    ])
    cmds = _cmds(events)
    assert len(cmds) == 1 and cmds[0].command == "prev_segment"

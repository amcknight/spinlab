"""ControllerMenuDetector — button-ID state machine (gamepad-driven)."""
from spinlab.gamepad.menu_detector import ControllerMenuDetector
from spinlab.protocol import ControllerCommandEvent, ControllerMenuArmedEvent

# Arbitrary button ids for the tests — opaque integers, not SNES bits.
MOD = 8
PAUSE = 9
PRACTICE = 11

_MAPPING = {PAUSE: "pause", PRACTICE: "toggle_practice"}


def _det() -> ControllerMenuDetector:
    return ControllerMenuDetector(modifier=MOD, commands=_MAPPING)


def _run(det, frames):
    out = []
    for held in frames:
        out.extend(det.step(set(held)))
    return out


def _cmds(events):
    return [e.command for e in events if isinstance(e, ControllerCommandEvent)]


def _armed(events):
    return [e.armed for e in events if isinstance(e, ControllerMenuArmedEvent)]


def test_modifier_opens_menu_immediately():
    assert _armed(_run(_det(), [{MOD}])) == [True]


def test_command_pressed_after_modifier_fires():
    events = _run(_det(), [{MOD}, {MOD, PAUSE}])
    assert _cmds(events) == ["pause"]


def test_command_already_held_at_open_is_seeded_no_fire():
    # PAUSE held when MOD goes down must NOT fire (seeded as already-seen).
    events = _run(_det(), [{PAUSE}, {MOD, PAUSE}])
    assert _cmds(events) == []


def test_simultaneous_chord_is_seeded_then_fresh_press_fires():
    events = _run(_det(), [{MOD, PAUSE}, {MOD}, {MOD, PAUSE}])
    assert _cmds(events) == ["pause"]


def test_command_does_not_refire_while_held():
    events = _run(_det(), [{MOD}] + [{MOD, PAUSE}] * 5)
    assert _cmds(events) == ["pause"]


def test_release_modifier_closes_menu():
    assert _armed(_run(_det(), [{MOD}, set()])) == [True, False]


def test_lone_command_without_modifier_does_nothing():
    assert _run(_det(), [{PAUSE}, {PAUSE}]) == []


def test_two_commands_fire_in_sorted_order_same_frame():
    events = _run(_det(), [{MOD}, {MOD, PAUSE, PRACTICE}])
    # 9 (pause) sorts before 11 (toggle_practice).
    assert _cmds(events) == ["pause", "toggle_practice"]


def test_reset_clears_state():
    det = _det()
    det.step({MOD})
    det.reset()
    # After reset, MOD held again must re-emit armed=True (was open before reset).
    assert _armed(det.step({MOD})) == [True]

"""GamepadMenuLoop poll/forward logic + build_gamepad_loop config binding."""
import pytest

from spinlab.config import GamepadConfig
from spinlab.gamepad.loop import GamepadMenuLoop, build_gamepad_loop
from spinlab.gamepad.menu_detector import ControllerMenuDetector
from spinlab.protocol import ControllerCommandEvent, ControllerMenuArmedEvent

MOD = 8
PAUSE = 9


class _FakeSource:
    def __init__(self, frames):
        self._frames = list(frames)
    def pressed(self):
        return set(self._frames.pop(0)) if self._frames else set()


class _RecordingLoop:
    """Stand-in for an asyncio loop: runs the callback inline and records it."""
    def __init__(self):
        self.events = []
    def call_soon_threadsafe(self, fn, arg):
        fn(arg)
        self.events.append(arg)


def _loop_with(frames):
    det = ControllerMenuDetector(modifier=MOD, commands={PAUSE: "pause"})
    return GamepadMenuLoop(source=_FakeSource(frames), detector=det)


def test_start_before_bind_raises():
    """start() must refuse to spawn the thread until bind() supplied the loop
    and callback — otherwise the daemon would forward onto a None loop."""
    gl = _loop_with([])
    with pytest.raises(RuntimeError):
        gl.start()


def test_poll_once_forwards_arm_then_command():
    rec = _RecordingLoop()
    received = []
    gl = _loop_with([{MOD}, {MOD, PAUSE}])
    gl.bind(loop=rec, on_event=received.append)

    gl._poll_once()  # MOD down -> armed
    gl._poll_once()  # PAUSE after MOD -> command

    armed = [e.armed for e in received if isinstance(e, ControllerMenuArmedEvent)]
    cmds = [e.command for e in received if isinstance(e, ControllerCommandEvent)]
    assert armed == [True]
    assert cmds == ["pause"]


def test_build_returns_none_when_disabled():
    assert build_gamepad_loop(GamepadConfig(enabled=False)) is None


def test_build_raises_on_enabled_without_modifier():
    gp = GamepadConfig(enabled=True, modifier=None, buttons={"pause": 9})
    with pytest.raises(ValueError, match="modifier"):
        build_gamepad_loop(gp)


def test_build_raises_on_unknown_verb():
    gp = GamepadConfig(enabled=True, modifier=8, buttons={"explode": 9})
    with pytest.raises(ValueError, match="explode"):
        build_gamepad_loop(gp)


def test_build_constructs_loop_with_inverted_mapping():
    gp = GamepadConfig(
        enabled=True, modifier=8,
        buttons={"pause": 9, "toggle_practice": 11},
    )
    gl = build_gamepad_loop(gp)
    assert isinstance(gl, GamepadMenuLoop)
    # The detector must map button-id -> verb (inverse of config's verb -> id).
    assert gl._detector._commands == {9: "pause", 11: "toggle_practice"}

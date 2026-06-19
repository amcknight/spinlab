"""GamepadButtonSource — the pygame wrapper's degradation guard.

Real button reads need a physical pad and are smoke-tested manually. Here we
only prove that a missing pygame / failed init degrades to an empty set and an
`available is False` flag, never a crash.
"""
from spinlab.gamepad import source as source_mod
from spinlab.gamepad.source import GamepadButtonSource


def test_missing_pygame_degrades_to_inactive(monkeypatch):
    def _boom():
        raise ImportError("pygame not installed")
    monkeypatch.setattr(source_mod, "_import_pygame", _boom)

    src = GamepadButtonSource(device_index=0)
    assert src.available is False
    assert src.pressed() == set()


def test_init_failure_degrades_to_inactive(monkeypatch):
    class _FakePygame:
        class error(Exception):
            pass
        class joystick:
            @staticmethod
            def init():
                raise RuntimeError("no joystick subsystem")
        @staticmethod
        def init():
            pass
    monkeypatch.setattr(source_mod, "_import_pygame", lambda: _FakePygame)

    src = GamepadButtonSource(device_index=0)
    assert src.available is False
    assert src.pressed() == set()

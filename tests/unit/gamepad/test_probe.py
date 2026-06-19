"""gamepad-probe device enumeration (the non-interactive, testable part)."""
from spinlab.gamepad import probe as probe_mod
from spinlab.gamepad.probe import list_devices


def test_list_devices_returns_names_in_index_order(monkeypatch):
    class _Joy:
        def __init__(self, i):
            self._i = i
        def init(self):
            pass
        def get_name(self):
            return f"Pad{self._i}"
    class _FakePygame:
        class joystick:
            @staticmethod
            def init():
                pass
            @staticmethod
            def get_count():
                return 2
            Joystick = _Joy
        @staticmethod
        def init():
            pass
    monkeypatch.setattr(probe_mod, "_import_pygame", lambda: _FakePygame)

    assert list_devices() == ["Pad0", "Pad1"]


def test_list_devices_empty_when_pygame_missing(monkeypatch):
    def _boom():
        raise ImportError("no pygame")
    monkeypatch.setattr(probe_mod, "_import_pygame", _boom)
    assert list_devices() == []

"""`spinlab gamepad-probe` — print button indices as they are pressed so the
user can fill in config.yaml's `gamepad.buttons`. The pygame button index of a
physical button varies per controller and mode (X-input vs D-input) and is not
guessable; this is the tool that maps them. Interactive; run from a terminal."""
from __future__ import annotations

import time

# Re-export the import seam so tests can monkeypatch enumeration without a pad.
from spinlab.gamepad.source import _import_pygame

# How often the probe samples the pad while waiting for presses. Matches the
# menu loop's 60 Hz so what you see is what the menu will read.
_PROBE_POLL_PERIOD_SEC = 1.0 / 60.0


def list_devices() -> list[str]:
    """Names of connected joysticks, index-ordered. Empty if pygame is missing
    or no pad is present."""
    try:
        pygame = _import_pygame()
        pygame.init()
        pygame.joystick.init()
        names = []
        for i in range(pygame.joystick.get_count()):
            joy = pygame.joystick.Joystick(i)
            joy.init()
            names.append(joy.get_name())
        return names
    except Exception:
        return []


def run_probe(device_index: int = 0) -> int:
    """Interactive: print each button index on its rising edge until Ctrl-C."""
    devices = list_devices()
    if not devices:
        print("No gamepad detected (or pygame not installed: pip install -e '.[gamepad]').")
        return 1
    # Validate the index BEFORE printing the list so the "<- probing" marker is
    # only attached when it actually points at a real device.
    if device_index >= len(devices):
        print(f"No device at index {device_index} (detected {len(devices)}).")
        return 1
    print("Detected devices:")
    for i, name in enumerate(devices):
        marker = " <- probing" if i == device_index else ""
        print(f"  [{i}] {name}{marker}")

    prev: set[int] = set()
    try:
        pygame = _import_pygame()
        joy = pygame.joystick.Joystick(device_index)
        joy.init()
        print(
            f"\nProbing '{joy.get_name()}' ({joy.get_numbuttons()} buttons). "
            "Press buttons to see their indices; Ctrl-C to quit.\n"
        )
        while True:
            pygame.event.pump()
            now = {i for i in range(joy.get_numbuttons()) if joy.get_button(i)}
            for i in sorted(now - prev):
                print(f"  button {i} pressed")
            prev = now
            time.sleep(_PROBE_POLL_PERIOD_SEC)
    except KeyboardInterrupt:
        print("\nDone.")
        return 0
    except Exception as exc:
        # e.g. the pad is unplugged mid-probe; degrade gracefully, no traceback.
        print(f"\nProbe ended: {exc}")
        return 1

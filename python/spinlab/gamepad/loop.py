"""GamepadMenuLoop — a daemon thread that polls the gamepad and forwards menu
events into the asyncio event loop.

SDL's event pump prefers a single owning thread, so the poll runs on a dedicated
daemon thread (off the async loop). Each emitted event is marshalled back onto
the loop with ``call_soon_threadsafe`` so it reaches SessionManager.route_event
on the loop thread, exactly like poller-emitted events.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from spinlab.config import GamepadConfig
from spinlab.gamepad.menu_detector import MENU_VERBS, ControllerMenuDetector
from spinlab.gamepad.source import ButtonSource, GamepadButtonSource

logger = logging.getLogger(__name__)

# Poll the pad once per frame at 60 Hz — fast enough that a tap between two
# polls is never missed at human press durations (the prior WRAM poller ran at
# the same rate), cheap enough to leave a core idle. Matches the detector's
# rising-edge gesture, which assumes per-frame granularity.
GAMEPAD_POLL_PERIOD_SEC = 1.0 / 60.0

# How long stop() waits for the daemon thread to drain before giving up. The
# thread checks the stop flag every poll period, so two periods is ample; we
# never block shutdown on a wedged SDL read.
_STOP_JOIN_TIMEOUT_SEC = 0.5


class GamepadMenuLoop:
    def __init__(
        self,
        *,
        source: ButtonSource,
        detector: ControllerMenuDetector,
        period_sec: float = GAMEPAD_POLL_PERIOD_SEC,
    ) -> None:
        self._source = source
        self._detector = detector
        self._period = period_sec
        self._loop = None
        self._on_event: Callable[[object], None] | None = None
        self._thread: threading.Thread | None = None
        self._stopped = False

    def bind(self, *, loop, on_event: Callable[[object], None]) -> None:
        """Capture the running asyncio loop and the enqueue callback. Called
        once, from the loop thread, before start()."""
        self._loop = loop
        self._on_event = on_event

    def start(self) -> None:
        if self._loop is None or self._on_event is None:
            raise RuntimeError("GamepadMenuLoop.start() before bind()")
        self._stopped = False
        self._thread = threading.Thread(
            target=self._run, name="gamepad-menu", daemon=True,
        )
        self._thread.start()
        logger.info("gamepad menu loop started")

    def stop(self) -> None:
        self._stopped = True
        if self._thread is not None:
            self._thread.join(timeout=_STOP_JOIN_TIMEOUT_SEC)
            self._thread = None

    def _poll_once(self) -> None:
        pressed = self._source.pressed()
        for event in self._detector.step(pressed):
            # Marshal onto the asyncio loop thread; bind() guarantees both set.
            self._loop.call_soon_threadsafe(self._on_event, event)

    def _run(self) -> None:
        while not self._stopped:
            try:
                self._poll_once()
            except Exception as exc:
                # Never let the daemon die on a transient read hiccup.
                logger.warning("gamepad poll iteration failed: %s", exc)
            time.sleep(self._period)


def build_gamepad_loop(gp: GamepadConfig) -> GamepadMenuLoop | None:
    """Construct a GamepadMenuLoop from config, or None when disabled.

    Fails loud on a misconfigured-but-enabled section: a missing modifier or an
    unknown verb is an operator error, not something to silently swallow. The
    only graceful degradation (missing pygame / no pad) happens inside
    GamepadButtonSource, so a returned loop may still be inactive at runtime.
    """
    if not gp.enabled:
        return None
    if gp.modifier is None:
        raise ValueError("gamepad.enabled is true but gamepad.modifier is unset")
    unknown = set(gp.buttons) - MENU_VERBS
    if unknown:
        raise ValueError(
            f"gamepad.buttons has unknown verb(s): {sorted(unknown)}; "
            f"valid verbs are {sorted(MENU_VERBS)}"
        )
    # Config maps verb -> button id; the detector wants button id -> verb.
    commands = {button_id: verb for verb, button_id in gp.buttons.items()}
    source = GamepadButtonSource(device_index=gp.device_index)
    detector = ControllerMenuDetector(modifier=gp.modifier, commands=commands)
    return GamepadMenuLoop(source=source, detector=detector)

"""ButtonSource — the menu detector's input. A real pad (pygame) in production,
a fake in tests. The pygame wrapper guards every entry point: missing pygame or
no controller degrades to an empty held-set so the menu is simply inactive,
never a crash."""
from __future__ import annotations

import logging
import os
from typing import Protocol

from spinlab.gamepad.menu_detector import ButtonId

logger = logging.getLogger(__name__)


class ButtonSource(Protocol):
    """Returns the set of physical buttons held this poll."""

    def pressed(self) -> set[ButtonId]:
        ...


def _import_pygame():
    """Import pygame, configuring SDL for a windowless joystick-only run.

    Isolated into a module function so tests can monkeypatch it to simulate a
    missing/failing pygame without touching the real import machinery. The dummy
    video driver lets the joystick subsystem init on a headless box; on a normal
    desktop it is harmless (we never open a window).
    """
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    import pygame  # type: ignore[import-not-found]  # optional [gamepad] extra
    return pygame


class GamepadButtonSource:
    """pygame-backed ButtonSource. Opens one joystick by index and reports its
    held buttons each poll. Any import/init/read failure flips ``available`` to
    False and returns an empty set."""

    def __init__(self, device_index: int = 0) -> None:
        self._device_index = device_index
        self._pygame = None
        self._joystick = None
        self._available = False
        self._init()

    @property
    def available(self) -> bool:
        return self._available

    def _init(self) -> None:
        try:
            pygame = _import_pygame()
            pygame.init()
            pygame.joystick.init()
            if pygame.joystick.get_count() <= self._device_index:
                logger.warning(
                    "gamepad: no joystick at index %d (count=%d); menu inactive",
                    self._device_index, pygame.joystick.get_count(),
                )
                return
            joystick = pygame.joystick.Joystick(self._device_index)
            joystick.init()
            self._pygame = pygame
            self._joystick = joystick
            self._available = True
            logger.info(
                "gamepad: opened joystick %d (%s, %d buttons)",
                self._device_index, joystick.get_name(), joystick.get_numbuttons(),
            )
        except Exception as exc:  # ImportError, pygame.error, anything
            logger.warning("gamepad: init failed (%s); menu inactive", exc)
            self._available = False

    def pressed(self) -> set[ButtonId]:
        if not self._available or self._joystick is None or self._pygame is None:
            return set()
        try:
            # SDL needs its event queue pumped before get_button reflects the
            # current hardware state.
            self._pygame.event.pump()
            n = self._joystick.get_numbuttons()
            return {i for i in range(n) if self._joystick.get_button(i)}
        except Exception as exc:
            logger.warning("gamepad: read failed (%s); menu inactive", exc)
            self._available = False
            return set()

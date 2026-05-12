"""MovieController — owns RA movie record/playback lifecycle.

Extracted from RetroArchOrchestrator. Holds the cross-call state
(_active_recording, _active_playback, _fast_forwarding) that the four
movie command handlers previously kept inline on the orchestrator.

The fast-forward toggle requires symmetric pairing: NCI's FAST_FORWARD is
a flip with no state query, so any path that toggles ON must toggle OFF
in the stop path. That contract is enforced here.

Construction order note: this controller emits events via an injected
callback. Since the typical caller (build_orchestrator) needs orch's
event queue, and orch needs the controller in its constructor, the
``set_event_callback`` setter allows late binding after both objects
exist. The constructor accepts a placeholder callback (e.g. a no-op
lambda) for the brief window before binding.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from spinlab.errors import BackendNotImplementedError
from spinlab.protocol import (
    SPEED_UNCAPPED,
    ReplayErrorEvent,
    ReplayFinishedEvent,
    ReplayStartedEvent,
)
from spinlab.retroarch.raclient import (
    MoviePlayback,
    MoviePlaybackError,
    MovieRecording,
    RAClient,
    RAClientError,
)

logger = logging.getLogger(__name__)


class MovieController:
    def __init__(
        self,
        raclient: RAClient,
        enable: bool,
        on_event: Callable[[object], None],
    ) -> None:
        self._raclient = raclient
        self._enable = enable
        self._on_event = on_event
        self._active_recording: MovieRecording | None = None
        self._active_playback: MoviePlayback | None = None
        self._fast_forwarding: bool = False

    def set_event_callback(self, on_event: Callable[[object], None]) -> None:
        """Rebind the event callback. Used by build_orchestrator to bind
        ``orch.events.put_nowait`` after both objects exist (see module
        docstring)."""
        self._on_event = on_event

    @property
    def enabled(self) -> bool:
        return self._enable

    @property
    def is_recording(self) -> bool:
        return self._active_recording is not None

    @property
    def is_playing(self) -> bool:
        return self._active_playback is not None

    async def start_recording(self, path: Path) -> None:
        """Start movie recording. No-op when disabled. Non-fatal on RAClientError."""
        if not self._enable:
            logger.info("Reference recording started (movies disabled)")
            return
        try:
            self._active_recording = await self._raclient.record_movie(path)
            logger.info("Movie recording started: %s", path)
        except RAClientError as exc:
            logger.warning("Movie recording failed to start: %s", exc)

    async def stop_recording(self) -> None:
        """Stop movie recording. No-op if nothing active. Non-fatal on RAClientError."""
        if self._active_recording is None:
            logger.info("Reference recording stopped (no movie recorder active)")
            return
        try:
            stopped_path = await self._active_recording.stop()
            logger.info("Movie recording stopped: %s", stopped_path)
        except RAClientError as exc:
            logger.warning("Movie recording failed to stop: %s", exc)
        finally:
            self._active_recording = None

    async def start_playback(self, path: Path, speed: int) -> None:
        """Start movie playback. Raises BackendNotImplementedError when disabled.

        On RAClient errors, emits ReplayErrorEvent rather than raising.

        If ``speed == SPEED_UNCAPPED``, toggles RA into fast-forward; the
        matching toggle-off happens in ``stop_playback``.
        """
        if not self._enable:
            logger.warning("MovieController: start_playback rejected — movies disabled")
            raise BackendNotImplementedError()

        try:
            self._active_playback = await self._raclient.play_movie(path)
        except MoviePlaybackError as exc:
            logger.error("Movie replay verification failed: %s", exc)
            self._on_event(ReplayErrorEvent(message=str(exc)))
            return
        except RAClientError as exc:
            logger.error("Movie replay failed: %s", exc)
            self._on_event(ReplayErrorEvent(message=str(exc)))
            return

        if speed == SPEED_UNCAPPED:
            await asyncio.to_thread(self._raclient.fast_forward_toggle)
            self._fast_forwarding = True

        self._on_event(ReplayStartedEvent(
            path=str(path),
            frame_count=self._active_playback.frame_count,
        ))
        logger.info(
            "Movie replay started: %s (frames=%d, fast_forward=%s)",
            path, self._active_playback.frame_count, self._fast_forwarding,
        )

    async def stop_playback(self) -> None:
        """Stop playback. Idempotent. Emits ReplayFinishedEvent on a real stop.

        Symmetric fast-forward toggle: if start_playback toggled ON, this
        toggles OFF.
        """
        if self._active_playback is None:
            return
        try:
            await self._active_playback.stop()
        except RAClientError as exc:
            logger.warning("Movie replay failed to stop: %s", exc)
        finally:
            self._active_playback = None
            if self._fast_forwarding:
                await asyncio.to_thread(self._raclient.fast_forward_toggle)
                self._fast_forwarding = False
        self._on_event(ReplayFinishedEvent())
        logger.info("Movie replay stopped")

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

from spinlab import log
from spinlab.errors import BackendNotImplementedError
from spinlab.protocol import (
    SPEED_UNCAPPED,
    MovieEvent,
    ReplayErrorEvent,
    ReplayFinishedEvent,
    ReplayStartedEvent,
)
from spinlab.retroarch.movie_io import (
    MovieIOError,
    MoviePlayback,
    MoviePlaybackError,
    MovieRecording,
    RAMovieIO,
)
from spinlab.retroarch.raclient import RAClient, RAClientError

logger = logging.getLogger(__name__)

# Auto-end-of-replay detection. RA gives no NCI event when a movie finishes; we
# poll its log for the end marker, then halt. 0.5s is frequent enough that the
# replay finalizes promptly without busy-spinning the log file.
REPLAY_END_POLL_SEC = 0.5
# After the marker appears, wait this long before halting so the 60Hz poller can
# sample RA's paused final frame and flush the closing transition — otherwise a
# fast-forward replay can lose its last segment. ~2s ≈ 120 ticks of headroom.
REPLAY_END_SETTLE_SEC = 2.0


class MovieController:
    def __init__(
        self,
        movie_io: RAMovieIO,
        raclient: RAClient,
        enable: bool,
        on_event: Callable[[MovieEvent], None],
        on_replay_started: Callable[[], None] | None = None,
    ) -> None:
        self._movie_io = movie_io
        self._raclient = raclient
        self._enable = enable
        self._on_event = on_event
        # Called after `play_movie` succeeds, before the ReplayStartedEvent
        # is emitted. Lets the orchestrator forward the signal into the
        # poller's detector so it can synthesize the entrance edge that
        # PLAY_REPLAY's savestate load otherwise bypasses. See
        # `detector.mark_replay_entrance` for the why.
        self._on_replay_started: Callable[[], None] = on_replay_started or (lambda: None)
        self._active_recording: MovieRecording | None = None
        self._active_playback: MoviePlayback | None = None
        self._fast_forwarding: bool = False
        # Background task that watches RA's log for the end-of-playback marker
        # and auto-finalizes the replay. None when no replay is being watched.
        self._end_watch_task: asyncio.Task[None] | None = None

    def set_event_callback(self, on_event: Callable[[MovieEvent], None]) -> None:
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
            self._active_recording = await self._movie_io.record_movie(path)
            logger.info("Movie recording started: %s", path)
        except RAClientError as exc:
            logger.warning("Movie recording failed to start: %s", exc)

    async def stop_recording(self) -> None:
        """Stop movie recording. No-op if nothing active. Non-fatal on RAClientError / MovieIOError."""
        if self._active_recording is None:
            logger.info("Reference recording stopped (no movie recorder active)")
            return
        try:
            stopped_path = await self._active_recording.stop()
            logger.info("Movie recording stopped: %s", stopped_path)
        except (RAClientError, MovieIOError) as exc:
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

        # Mark the pending replay entrance BEFORE play_movie awaits. play_movie
        # fires PLAY_REPLAY, which loads the replay's savestate AND bumps
        # state_version; the poller resyncs on its next tick and arms the
        # entrance. The mark must be set before that bump, so it goes here. The
        # entrance then fires regardless of the brief level_start=1 splash (see
        # detector.mark_replay_entrance). Safe if play_movie fails: the mark is
        # a no-op until a state-load resync, and the next legitimate level
        # entrance consumes it as a single combined event.
        self._on_replay_started()

        # Ensure RA is running before PLAY_REPLAY. A replay started while RA is
        # paused (e.g. RA auto-paused when a prior movie ended) loads the
        # embedded state but never advances a frame, so the WRAM-advance verify
        # in play_movie fails — the "loads state, starts, then stops" bug.
        await self._raclient.resume_if_paused()

        try:
            self._active_playback = await self._movie_io.play_movie(path)
        except MoviePlaybackError as exc:
            log.error(
                logger, "movie replay verification failed",
                exc=exc, replay_path=str(path),
            )
            self._on_event(ReplayErrorEvent(message=str(exc)))
            return
        except RAClientError as exc:
            log.error(
                logger, "movie replay failed",
                exc=exc, replay_path=str(path),
            )
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

        # Auto-finalize when the movie finishes. RA emits no NCI event for the
        # natural end of playback, so watch its log; without this a finished
        # replay sits forever with fast-forward still on and the run unfinalized.
        # Skipped when no log anchor is available (then manual stop is the only
        # path, as before).
        anchor = self._movie_io.replay_log_anchor()
        if anchor is not None:
            self._end_watch_task = asyncio.create_task(self._watch_for_replay_end(anchor))

    async def _watch_for_replay_end(self, anchor: tuple[Path, int] | None) -> None:
        """Poll RA's log for the end-of-playback marker, then auto-finalize.

        Cancelled by ``stop_playback`` when the user halts first; otherwise it
        detects the natural end, settles briefly so the closing segment is
        captured, and runs the normal stop path (FF off + ReplayFinishedEvent).
        """
        try:
            while not await asyncio.to_thread(
                self._movie_io.playback_ended_since, anchor
            ):
                await asyncio.sleep(REPLAY_END_POLL_SEC)
            await asyncio.sleep(REPLAY_END_SETTLE_SEC)
        except asyncio.CancelledError:
            return
        # Clear our own handle before stopping so stop_playback doesn't try to
        # cancel the task that's currently running it.
        self._end_watch_task = None
        logger.info("Movie replay ended (RA log marker) — auto-finalizing")
        await self.stop_playback()

    async def stop_playback(self) -> None:
        """Stop playback. Idempotent. Emits ReplayFinishedEvent on a real stop.

        Symmetric fast-forward toggle: if start_playback toggled ON, this
        toggles OFF.
        """
        # Cancel the end-of-playback watcher (no-op if we're being called BY it,
        # which clears the handle first).
        watch_task = self._end_watch_task
        self._end_watch_task = None
        if watch_task is not None and watch_task is not asyncio.current_task():
            watch_task.cancel()

        if self._active_playback is None:
            return
        try:
            await self._active_playback.stop()
        except RAClientError as exc:
            log.warn(
                logger, "movie replay failed to stop",
                exc=exc, replay_path=str(self._active_playback.path),
            )
        finally:
            self._active_playback = None
            if self._fast_forwarding:
                await asyncio.to_thread(self._raclient.fast_forward_toggle)
                self._fast_forwarding = False
        # RA auto-pauses when a movie finishes; leave it running so the next
        # replay starts from a live emulator (a replay should not end paused).
        await self._raclient.resume_if_paused()
        self._on_event(ReplayFinishedEvent())
        logger.info("Movie replay stopped")

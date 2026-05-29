"""Poller — async loop that drives transition detection at ~60Hz.

Reads MemorySnapshots via the NCI transport, runs them through the
TransitionDetector + ColdFillSpawnDetector, and emits typed protocol events
through a caller-supplied callback.

State-load handling: the Poller checks ``state_version()`` once per tick.
When the version increments (RAClient.load_state was called since the last
tick), it treats the current snapshot as a fresh prev and skips detection
for that tick.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from spinlab import log
from spinlab.condition_registry import ConditionRegistry
from spinlab.protocol import PollerEvent
from spinlab.retroarch.cold_fill_detector import ColdFillSpawnDetector
from spinlab.retroarch.detector import TransitionDetector
from spinlab.retroarch.nci import NCIClient
from spinlab.retroarch.snapshot import MemorySnapshot

logger = logging.getLogger(__name__)

DEFAULT_PERIOD_SEC = 1.0 / 60.0  # one frame at 60 Hz

# After this many consecutive read failures (~1 second at 60 Hz), force a
# socket reconnect. Prevents the BlockingIOError[WinError 10035] loop where
# _read_failing suppresses log spam but the socket never recovers.
_READ_RECONNECT_FAILURE_THRESHOLD = 60


@dataclass
class PollerDeps:
    client: NCIClient
    read_snapshot: Callable[[NCIClient], MemorySnapshot]
    on_event: Callable[[PollerEvent], None]
    state_path_for: Callable[[PollerEvent], str | None] | None = None
    conditions_registry: ConditionRegistry | None = None
    # Returns RAClient's monotonic state_version. The Poller compares against
    # the last seen value each tick; an increment means "RA just reloaded, the
    # next snapshot is the new prev".
    state_version: Callable[[], int] = lambda: 0


class Poller:
    def __init__(
        self,
        deps: PollerDeps,
        period_sec: float = DEFAULT_PERIOD_SEC,
        detector: TransitionDetector | None = None,
        cold_fill: ColdFillSpawnDetector | None = None,
    ) -> None:
        self._deps = deps
        self._period = period_sec
        self._stopped = False
        self._detector = detector if detector is not None else TransitionDetector()
        self._cold_fill = cold_fill if cold_fill is not None else ColdFillSpawnDetector()
        self._start_ms = time.perf_counter() * 1000
        self._last_seen_state_version = deps.state_version()
        # Number of successful RAM reads completed (excludes polls that raised
        # an exception). Used by tests to verify throughput during playback.
        self.poll_count: int = 0
        # Transition-log state: log once on entering failure, once on recovery.
        # The poller runs at 60Hz, so per-frame exception logging would spam the
        # log; these flags collapse a persistent fault into one entry + one
        # "recovered" entry per fault episode.
        self._read_failing: bool = False
        self._read_fail_count: int = 0
        self._conditions_failing: bool = False
        self._detector_failing: bool = False
        self._cold_fill_failing: bool = False

    def _stamp_state_path(self, ev: PollerEvent) -> PollerEvent:
        if self._deps.state_path_for is None:
            return ev
        path = self._deps.state_path_for(ev)
        if not path:
            return ev
        if not hasattr(ev, "state_path"):
            return ev
        return dataclasses.replace(ev, state_path=path)

    def _stamp_conditions(self, ev: PollerEvent) -> PollerEvent:
        reg = self._deps.conditions_registry
        if reg is None:
            return ev
        try:
            values = reg.read_all(self._deps.client)
        except Exception as exc:
            if not self._conditions_failing:
                log.warn(logger, "poller condition read failed", exc=exc)
                self._conditions_failing = True
            return ev
        if self._conditions_failing:
            log.info(logger, "poller condition read recovered")
            self._conditions_failing = False
        if not values:
            return ev
        return dataclasses.replace(ev, conditions=values)

    def stop(self) -> None:
        self._stopped = True

    def activate_cold_fill(self, segment_id: str) -> None:
        """Put the cold-fill detector into active mode for ``segment_id``.

        Separate from state-version handling: state_version says "RA just
        reloaded; resync prev". Cold-fill activation says "we're now in
        cold-fill detection mode for this specific segment" — independent
        signals that often happen together but mean different things.
        """
        self._cold_fill.activate(segment_id)

    def mark_replay_entrance(self) -> None:
        """Forward to the embedded detector. Called by MovieController before
        PLAY_REPLAY so the detector arms a synthesized entrance at the
        state-load resync (PLAY_REPLAY bumps state_version). See
        ``TransitionDetector.mark_replay_entrance``.
        """
        self._detector.mark_replay_entrance()

    async def run(self) -> None:
        while not self._stopped:
            try:
                snap = self._deps.read_snapshot(self._deps.client)
            except Exception as exc:
                self._read_fail_count += 1
                if not self._read_failing:
                    log.warn(logger, "poller read failed", exc=exc)
                    self._read_failing = True
                if self._read_fail_count >= _READ_RECONNECT_FAILURE_THRESHOLD:
                    log.warn(
                        logger, "poller read stuck; reconnecting NCI socket",
                        fail_count=self._read_fail_count,
                    )
                    self._deps.client.close()
                    self._read_fail_count = 0
                await asyncio.sleep(self._period)
                continue
            if self._read_failing:
                log.info(logger, "poller read recovered")
                self._read_failing = False
            self._read_fail_count = 0

            self.poll_count += 1
            ts = int(time.perf_counter() * 1000 - self._start_ms)

            cur_ver = self._deps.state_version()
            if cur_ver != self._last_seen_state_version:
                # RA reloaded since last tick; treat this snapshot as the new
                # prev and skip detection for this tick — otherwise the diff
                # against the pre-load prev would emit phantom edges.
                armed_replay_entrance = self._detector.resync_after_state_load(snap)
                self._cold_fill.resync_after_state_load(snap)
                self._last_seen_state_version = cur_ver
                if armed_replay_entrance:
                    # Replay start: PLAY_REPLAY bumped state_version and this
                    # resync armed the synthesized entrance. Log it so a
                    # replay-fixture failure's diagnostic ring buffer shows the
                    # replay-start signal actually reached the detector.
                    log.info(
                        logger, "poller resync armed replay entrance",
                        state_version=cur_ver,
                    )
                await asyncio.sleep(self._period)
                continue

            try:
                events = list(self._detector.step(snap, timestamp_ms=ts))
            except Exception as exc:
                if not self._detector_failing:
                    log.error(logger, "detector.step raised", exc=exc)
                    self._detector_failing = True
                events = []
            else:
                if self._detector_failing:
                    log.info(logger, "detector.step recovered")
                    self._detector_failing = False

            for event in events:
                event = self._stamp_state_path(event)
                event = self._stamp_conditions(event)
                try:
                    self._deps.on_event(event)
                except Exception as exc:
                    log.error(
                        logger, "poller event handler raised",
                        exc=exc, event_type=type(event).__name__,
                    )

            try:
                cf_event = self._cold_fill.step(snap, timestamp_ms=ts)
            except Exception as exc:
                if not self._cold_fill_failing:
                    log.error(logger, "cold_fill.step raised", exc=exc)
                    self._cold_fill_failing = True
                cf_event = None
            else:
                if self._cold_fill_failing:
                    log.info(logger, "cold_fill.step recovered")
                    self._cold_fill_failing = False
            if cf_event is not None:
                cf_event = self._stamp_state_path(cf_event)
                cf_event = self._stamp_conditions(cf_event)
                try:
                    self._deps.on_event(cf_event)
                except Exception as exc:
                    log.error(
                        logger, "poller event handler raised",
                        exc=exc, event_type=type(cf_event).__name__,
                    )

            await asyncio.sleep(self._period)

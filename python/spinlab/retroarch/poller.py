"""Poller — async loop that drives transition detection at ~60Hz.

Reads MemorySnapshots via the NCI transport, runs them through the
TransitionDetector + ColdFillSpawnDetector, and emits typed protocol events
through a caller-supplied callback.

State-load handling: the Poller checks ``state_version()`` once per tick.
When the version increments (RAClient.load_state was called since the last
tick), it treats the current snapshot as a fresh prev and skips detection
for that tick — replaces the old ``mark_state_loaded`` side-band call.
"""
from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from spinlab.condition_registry import ConditionRegistry
from spinlab.retroarch.cold_fill import ColdFillSpawnDetector
from spinlab.retroarch.detector import TransitionDetector
from spinlab.retroarch.nci import NCIClient
from spinlab.retroarch.snapshot import MemorySnapshot

DEFAULT_PERIOD_SEC = 1.0 / 60.0  # one frame at 60 Hz


@dataclass
class PollerDeps:
    client: NCIClient
    read_snapshot: Callable[[NCIClient], MemorySnapshot]
    on_event: Callable[[Any], None]
    state_path_for: Callable[[Any], str | None] | None = None
    conditions_registry: ConditionRegistry | None = None
    # Returns RAClient's monotonic state_version. The Poller compares against
    # the last seen value each tick; an increment means "RA just reloaded, the
    # next snapshot is the new prev" — replaces the old mark_state_loaded flag.
    state_version: Callable[[], int] = lambda: 0


class Poller:
    def __init__(self, deps: PollerDeps, period_sec: float = DEFAULT_PERIOD_SEC) -> None:
        self._deps = deps
        self._period = period_sec
        self._stopped = False
        self._detector = TransitionDetector()
        self._cold_fill = ColdFillSpawnDetector()
        self._start_ms = time.perf_counter() * 1000
        self._last_seen_state_version = deps.state_version()
        # Number of successful RAM reads completed (excludes polls that raised
        # an exception). Used by tests to verify throughput during playback.
        self.poll_count: int = 0

    def _stamp_state_path(self, ev: Any) -> Any:
        if self._deps.state_path_for is None:
            return ev
        path = self._deps.state_path_for(ev)
        if not path:
            return ev
        if not hasattr(ev, "state_path"):
            return ev
        return dataclasses.replace(ev, state_path=path)

    def _stamp_conditions(self, ev: Any) -> Any:
        reg = self._deps.conditions_registry
        if reg is None:
            return ev
        try:
            values = reg.read_all(self._deps.client)
        except Exception:
            return ev
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

    async def run(self) -> None:
        while not self._stopped:
            try:
                snap = self._deps.read_snapshot(self._deps.client)
            except Exception:
                await asyncio.sleep(self._period)
                continue

            self.poll_count += 1
            ts = int(time.perf_counter() * 1000 - self._start_ms)

            cur_ver = self._deps.state_version()
            if cur_ver != self._last_seen_state_version:
                # RA reloaded since last tick; treat this snapshot as the new
                # prev and skip detection for this tick — otherwise the diff
                # against the pre-load prev would emit phantom edges.
                self._detector.resync_after_state_load(snap)
                self._cold_fill.resync_after_state_load(snap)
                self._last_seen_state_version = cur_ver
                await asyncio.sleep(self._period)
                continue

            for event in self._detector.step(snap, timestamp_ms=ts):
                event = self._stamp_state_path(event)
                event = self._stamp_conditions(event)
                self._deps.on_event(event)

            cf_event = self._cold_fill.step(snap, timestamp_ms=ts)
            if cf_event is not None:
                cf_event = self._stamp_state_path(cf_event)
                cf_event = self._stamp_conditions(cf_event)
                self._deps.on_event(cf_event)

            await asyncio.sleep(self._period)

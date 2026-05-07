"""Poller — async loop that drives transition detection at ~60Hz.

Architecture:
  - NCIClient (Phase B) — owns the UDP socket.
  - read_snapshot (Phase C task 2) — builds a MemorySnapshot.
  - TransitionDetector (task 7) — converts (prev, curr) -> events.
  - ColdFillTracker (task 8) — separate mode, optional.
  - on_event callback — receives every emitted TransitionEvent.

Caller responsibilities:
  - Build PollerDeps with the right client/snapshot fn/event callback.
  - await poller.run() in an asyncio task.
  - poller.stop() to clean shutdown.
  - poller.mark_state_loaded() before the next poll if a save state was just
    loaded — replaces prev with the post-load snapshot to suppress phantom
    edge events.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from spinlab.retroarch.cold_fill import ColdFillTracker
from spinlab.retroarch.detector import TransitionDetector
from spinlab.retroarch.events import TransitionEvent
from spinlab.retroarch.nci import NCIClient
from spinlab.retroarch.snapshot import MemorySnapshot

DEFAULT_PERIOD_SEC = 1.0 / 60.0  # one frame at 60 Hz


@dataclass
class PollerDeps:
    client: NCIClient
    read_snapshot: Callable[[NCIClient], MemorySnapshot]
    on_event: Callable[[TransitionEvent], None]


class Poller:
    def __init__(self, deps: PollerDeps, period_sec: float = DEFAULT_PERIOD_SEC) -> None:
        self._deps = deps
        self._period = period_sec
        self._stopped = False
        self._state_just_loaded = False
        self._seeded = False  # True once the detector has a prev snapshot
        self._detector = TransitionDetector()
        self._cold_fill = ColdFillTracker()
        self._start_ms = time.perf_counter() * 1000

    def mark_state_loaded(self) -> None:
        """Tell the poller the next snapshot replaces prev (suppress phantom edges).

        The resync is deferred until after the first snapshot has seeded _prev,
        so the post-load read (not the seed read) becomes the new baseline. This
        ensures that mark_state_loaded() called before the very first poll still
        suppresses phantom edges on the first meaningful (prev, curr) comparison.
        """
        self._state_just_loaded = True

    def stop(self) -> None:
        self._stopped = True

    def activate_cold_fill(self, segment_id: str) -> None:
        self._cold_fill.activate(segment_id)

    async def run(self) -> None:
        while not self._stopped:
            try:
                snap = self._deps.read_snapshot(self._deps.client)
            except Exception:
                # Don't kill the loop on transient NCI errors. Caller wires up
                # a logger via the on_event callback path or a separate hook
                # if visibility is needed; bare bones for Phase C.
                await asyncio.sleep(self._period)
                continue

            ts = int(time.perf_counter() * 1000 - self._start_ms)

            # Resync only once prev has been seeded: the snapshot consumed here
            # becomes the post-load baseline, so the following step compares
            # (post-load, next) rather than (pre-load, post-load).
            if self._state_just_loaded and self._seeded:
                self._detector.resync_after_state_load(snap)
                self._state_just_loaded = False
                await asyncio.sleep(self._period)
                continue

            for event in self._detector.step(snap, timestamp_ms=ts):
                self._deps.on_event(event)
            self._seeded = True

            cf_event = self._cold_fill.step(snap, timestamp_ms=ts)
            if cf_event is not None:
                self._deps.on_event(cf_event)

            await asyncio.sleep(self._period)

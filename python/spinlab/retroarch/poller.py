"""Poller — async loop that drives transition detection at ~60Hz.

Architecture:
  - NCIClient (Phase B) — owns the UDP socket.
  - read_snapshot (Phase C task 2) — builds a MemorySnapshot.
  - TransitionDetector (task 7) — converts (prev, curr) -> events.
  - ColdFillSpawnDetector — separate mode, optional.
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
    # Event payload is a protocol dataclass instance (LevelEntranceEvent /
    # DeathEvent / etc.). Typed Any here because the Union of every emitted
    # type is verbose and the consumer pattern is isinstance-dispatch.
    on_event: Callable[[Any], None]
    state_path_for: Callable[[Any], str | None] | None = None
    conditions_registry: ConditionRegistry | None = None


class Poller:
    def __init__(self, deps: PollerDeps, period_sec: float = DEFAULT_PERIOD_SEC) -> None:
        self._deps = deps
        self._period = period_sec
        self._stopped = False
        self._state_just_loaded = False
        self._detector = TransitionDetector()
        self._cold_fill = ColdFillSpawnDetector()
        self._start_ms = time.perf_counter() * 1000
        # Number of successful RAM reads completed (excludes polls that raised
        # an exception). Used by tests to verify throughput during playback.
        self.poll_count: int = 0

    def _stamp_state_path(self, ev: Any) -> Any:
        """Apply state_path_for resolver if configured. Returns event with stamped path."""
        if self._deps.state_path_for is None:
            return ev
        path = self._deps.state_path_for(ev)
        if not path:
            return ev
        if not hasattr(ev, "state_path"):
            return ev
        return dataclasses.replace(ev, state_path=path)

    def _stamp_conditions(self, ev: Any) -> Any:
        """Apply conditions_registry if configured. Returns event with populated conditions."""
        reg = self._deps.conditions_registry
        if reg is None:
            return ev
        try:
            values = reg.read_all(self._deps.client)
        except Exception:
            return ev  # don't kill the loop on transient NCI errors
        if not values:
            return ev
        return dataclasses.replace(ev, conditions=values)

    def mark_state_loaded(self) -> None:
        """Tell the poller the next snapshot replaces prev (suppress phantom edges)."""
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

            self.poll_count += 1
            ts = int(time.perf_counter() * 1000 - self._start_ms)

            if self._state_just_loaded:
                self._detector.resync_after_state_load(snap)
                # ColdFillSpawnDetector also needs prev_* synced to the just-
                # loaded snapshot — otherwise its activate() leaves prev=0 and
                # the first post-load poll sees a phantom 0->non-zero edge on
                # exit_mode (or anim) that fires died_via_exit / died_sprite
                # before the player has touched anything.
                self._cold_fill.resync_after_state_load(snap)
                self._state_just_loaded = False
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

"""Practice and hyper-play attempt-timing state machines.

Emulator-agnostic: consumes typed protocol events (Death / LevelExit /
Checkpoint / Spawn) and produces typed result events. The RA backend
forwards each event to ``observe_event()`` to drive the state machine
forward. When an attempt completes (or the auto-advance delay elapses),
the configured callback receives a typed ``AttemptResultEvent``
(practice) or a ``HyperPlay*Event`` (hyper play).

State summary (PracticeTiming):
  IDLE    — not armed; observe_event/tick are no-ops.
  PLAYING — armed; watching for finish / death / abort events.
  RESULT  — finish detected; waiting ``auto_advance_delay_ms`` before
             emitting the result and returning to IDLE.

Deaths increment a penalty counter — they do NOT auto-fail an attempt.
The only failure path is a manual ``disarm()`` or a ``LevelExitEvent``
whose ``goal`` field equals ``"abort"``.

HyperPlayTiming handles the single-complete-attempt shape: arm → play
(deaths + checkpoints) → complete.
"""
from __future__ import annotations

import math
import time
import uuid
from collections.abc import Callable
from enum import Enum

from spinlab.protocol import (
    AttemptResultEvent,
    CheckpointEvent,
    DeathEvent,
    EventAttemptEmission,
    HyperPlayCheckpointEvent,
    HyperPlayCompleteEvent,
    HyperPlayDeathEvent,
    LevelExitEvent,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _default_now_ms() -> int:
    """Monotonic clock in milliseconds — suitable for elapsed measurements."""
    return int(time.monotonic() * 1000)


# ---------------------------------------------------------------------------
# PracticeTiming
# ---------------------------------------------------------------------------

class _PracticeState(Enum):
    IDLE = "idle"
    PLAYING = "playing"
    RESULT = "result"


class PracticeTiming:
    """State machine that converts an event stream into an AttemptResultEvent.

    Drives:
      - .arm(...)              — start a new attempt (resets any prior state)
      - .observe_event(...)    — feed in typed protocol events
      - .tick(now_ms)          — drive the auto-advance timer; returns True if
                                 a result was emitted this call
      - .disarm()              — abort current attempt without emitting
      - .is_armed -> bool

    The orchestrator calls .tick() periodically. When tick fires the result,
    ``on_attempt_result`` receives the AttemptResultEvent.
    """

    def __init__(self, *, now_ms: Callable[[], int] | None = None) -> None:
        self._now: Callable[[], int] = now_ms or _default_now_ms
        self._state = _PracticeState.IDLE

        # Armed-attempt fields — reset by _reset().
        self._segment_id: str = ""
        self._end_type: str = ""
        self._death_penalty_ms: int = 0
        self._auto_advance_delay_ms: int = 0
        self._start_ms: int = 0
        self._deaths: int = 0
        self._last_death_ms: int = 0

        # Per-event emission tracking.
        # episode_id groups events from a single armed attempt; minted in
        # arm() and carried verbatim into every EventAttemptEmission fired
        # before the next arm().
        # last_event_ms is the wall-clock time of the previous event (or
        # start_ms before the first event) so event time_ms is just the
        # delta — no penalty math, that's the legacy adapter's job.
        self._episode_id: str = ""
        self._last_event_ms: int = 0

        # Result-phase fields.
        self._elapsed_ms: int = 0
        self._completed: bool = False
        self._result_start_ms: int = 0

        self._on_attempt_result: Callable[[AttemptResultEvent], None] | None = None
        self._on_event_attempt: Callable[[EventAttemptEmission], None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_armed(self) -> bool:
        return self._state != _PracticeState.IDLE

    def arm(
        self,
        *,
        segment_id: str,
        end_type: str,
        death_penalty_ms: int,
        auto_advance_delay_ms: int,
        on_attempt_result: Callable[[AttemptResultEvent], None],
        on_event_attempt: Callable[[EventAttemptEmission], None] | None = None,
    ) -> None:
        """Start (or restart) an attempt. Resets all prior state without emitting."""
        self._reset()
        self._segment_id = segment_id
        self._end_type = end_type
        self._death_penalty_ms = death_penalty_ms
        self._auto_advance_delay_ms = auto_advance_delay_ms
        self._on_attempt_result = on_attempt_result
        self._on_event_attempt = on_event_attempt
        self._start_ms = self._now()
        self._episode_id = uuid.uuid4().hex
        self._last_event_ms = self._start_ms
        self._state = _PracticeState.PLAYING

    def observe_event(self, event: object) -> None:
        """Feed an incoming event into the state machine.

        Only events received while PLAYING affect state; all others are
        silently ignored.
        """
        if self._state != _PracticeState.PLAYING:
            return

        if isinstance(event, DeathEvent):
            # Deaths never auto-fail — accumulate penalty counter only.
            now = self._now()
            self._deaths += 1
            self._last_death_ms = now
            self._emit_event_attempt("died", now)
            return

        if self._end_type == "checkpoint":
            # Checkpoint end-type: only a checkpoint event triggers completion;
            # level_exit events are ignored entirely.
            if isinstance(event, CheckpointEvent):
                self._emit_event_attempt("survived", self._now())
                self._enter_result(completed=True)
        else:
            # Goal / exit end-type: level_exit drives completion.
            # goal == "abort" → failed; any other goal → completed.
            if isinstance(event, LevelExitEvent):
                completed = event.goal != "abort"
                if completed:
                    # Aborts get no event row — they record a DeathEvent earlier
                    # (the abort follows a death) or simply end without one (a
                    # disarm-style stop). Either way the episode has no
                    # 'survived' close.
                    self._emit_event_attempt("survived", self._now())
                self._enter_result(completed=completed)

    def tick(self, now_ms: int | None = None) -> bool:
        """Advance the auto-advance timer. Returns True exactly once when the
        result event is emitted."""
        if self._state != _PracticeState.RESULT:
            return False
        now = now_ms if now_ms is not None else self._now()
        if (now - self._result_start_ms) < self._auto_advance_delay_ms:
            return False
        self._emit_result()
        return True

    def disarm(self) -> None:
        """Abort the current attempt silently; no event is emitted."""
        self._reset()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit_event_attempt(self, outcome: str, now_ms: int) -> None:
        """Fire ``on_event_attempt`` for one died/survived event.

        ``time_ms`` is the wall-clock delta since the previous event in this
        episode (or since arm() for the first one) — no penalty math, just
        raw elapsed. The legacy adapter folds the per-death penalty back in
        when rolling events up; v07 wants the clean number.
        """
        cb = self._on_event_attempt
        if cb is None:
            return
        time_ms = now_ms - self._last_event_ms
        self._last_event_ms = now_ms
        cb(EventAttemptEmission(
            segment_id=self._segment_id,
            episode_id=self._episode_id,
            outcome=outcome,  # type: ignore[arg-type]
            time_ms=time_ms,
            timestamp_ms=now_ms,
        ))

    def _enter_result(self, *, completed: bool) -> None:
        now = self._now()
        penalty = self._death_penalty_ms * self._deaths
        self._elapsed_ms = (now - self._start_ms) + penalty
        self._completed = completed
        self._result_start_ms = now
        self._state = _PracticeState.RESULT

    def _emit_result(self) -> None:
        clean_tail_ms: int | None
        if self._completed and self._deaths == 0:
            # No deaths: clean tail is the full elapsed time.
            clean_tail_ms = int(math.floor(self._elapsed_ms))
        elif self._completed and self._deaths > 0 and self._last_death_ms > 0:
            # Completed with deaths: clean tail is wall-clock time from the
            # last death to the finish (result_start_ms is set at finish).
            clean_tail_ms = int(math.floor(self._result_start_ms - self._last_death_ms))
        else:
            # Failed attempt (abort) or degenerate state: no clean tail.
            clean_tail_ms = None

        result = AttemptResultEvent(
            segment_id=self._segment_id,
            completed=self._completed,
            time_ms=int(math.floor(self._elapsed_ms)),
            deaths=self._deaths,
            clean_tail_ms=clean_tail_ms,
        )
        cb = self._on_attempt_result
        # Reset before calling the callback so re-entrant arm() calls work
        # cleanly if the callback immediately starts the next attempt.
        self._reset()
        if cb is not None:
            cb(result)

    def _reset(self) -> None:
        self._state = _PracticeState.IDLE
        self._segment_id = ""
        self._end_type = ""
        self._death_penalty_ms = 0
        self._auto_advance_delay_ms = 0
        self._start_ms = 0
        self._deaths = 0
        self._last_death_ms = 0
        self._episode_id = ""
        self._last_event_ms = 0
        self._elapsed_ms = 0
        self._completed = False
        self._result_start_ms = 0
        self._on_attempt_result = None
        self._on_event_attempt = None


# ---------------------------------------------------------------------------
# HyperPlayTiming
# ---------------------------------------------------------------------------

class _HyperPlayState(Enum):
    IDLE = "idle"
    PLAYING = "playing"
    DYING = "dying"
    RESULT = "result"


HyperPlayEmittedEvent = HyperPlayCheckpointEvent | HyperPlayDeathEvent | HyperPlayCompleteEvent


class HyperPlayTiming:
    """State machine for a full speed-run attempt.

    Handles: arm → play (deaths emitted immediately; checkpoint splits emitted
    when hit) → complete (after auto_advance_delay_ms in RESULT state).

    Death handling: on death the machine enters a DYING blackout state for
    ``death_delay_ms``; the blackout duration is then added back to
    ``start_ms`` so it doesn't count toward elapsed time. Split timers reset
    at each checkpoint (or after respawn).

    Known gap: early-finish detection (orb/key mid-level) is not implemented;
    ``LevelExitEvent`` is the only finish signal.
    """

    def __init__(self, *, now_ms: Callable[[], int] | None = None) -> None:
        self._now: Callable[[], int] = now_ms or _default_now_ms
        self._state = _HyperPlayState.IDLE

        self._segment_id: str = ""
        self._checkpoints: list = []
        self._cp_index: int = 0          # next checkpoint index (0-based)
        self._on_event: Callable[[HyperPlayEmittedEvent], None] | None = None

        # Timing fields.
        self._start_ms: int = 0
        self._split_ms: int = 0          # split start time (reset at respawn/cp)
        self._death_delay_ms: int = 0
        self._auto_advance_delay_ms: int = 0
        self._death_started_ms: int = 0

        # Result-phase fields.
        self._elapsed_ms: int = 0
        self._result_start_ms: int = 0
        self._result_split_ms: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_armed(self) -> bool:
        return self._state != _HyperPlayState.IDLE

    def arm(
        self,
        *,
        segment_id: str,
        checkpoints: list,
        on_event: Callable[[HyperPlayEmittedEvent], None] | None = None,
        death_delay_ms: int = 1500,
        auto_advance_delay_ms: int = 1000,
    ) -> None:
        """Arm a speed-run attempt."""
        self._reset()
        self._segment_id = segment_id
        self._checkpoints = list(checkpoints)
        self._on_event = on_event
        self._death_delay_ms = death_delay_ms
        self._auto_advance_delay_ms = auto_advance_delay_ms
        now = self._now()
        self._start_ms = now
        self._split_ms = now
        self._state = _HyperPlayState.PLAYING

    def observe_event(self, event: object) -> None:
        """Feed an incoming event into the speed-run state machine."""
        if self._state != _HyperPlayState.PLAYING:
            # DYING advances in tick(); IDLE/RESULT ignore events.
            return

        if isinstance(event, DeathEvent):
            now = self._now()
            elapsed = now - self._start_ms
            split = now - self._split_ms
            self._dispatch(HyperPlayDeathEvent(
                elapsed_ms=int(math.floor(elapsed)),
                split_ms=int(math.floor(split)),
            ))
            self._state = _HyperPlayState.DYING
            self._death_started_ms = now

        elif isinstance(event, CheckpointEvent):
            now = self._now()
            elapsed = now - self._start_ms
            split = now - self._split_ms
            if self._cp_index < len(self._checkpoints):
                cp = self._checkpoints[self._cp_index]
                ordinal = cp.get("ordinal", self._cp_index + 1) if isinstance(cp, dict) else self._cp_index + 1
                self._dispatch(HyperPlayCheckpointEvent(
                    ordinal=ordinal,
                    elapsed_ms=int(math.floor(elapsed)),
                    split_ms=int(math.floor(split)),
                ))
                self._cp_index += 1

        elif isinstance(event, LevelExitEvent):
            if event.goal != "abort":
                now = self._now()
                self._elapsed_ms = now - self._start_ms
                self._result_split_ms = int(math.floor(now - self._split_ms))
                self._result_start_ms = now
                self._state = _HyperPlayState.RESULT

    def tick(self, now_ms: int | None = None) -> bool:
        """Advance timers. Returns True when the complete event is emitted."""
        now = now_ms if now_ms is not None else self._now()

        if self._state == _HyperPlayState.DYING:
            if (now - self._death_started_ms) >= self._death_delay_ms:
                # Blackout elapsed; bump start_ms so dead-time is excluded.
                blackout = now - self._death_started_ms
                self._start_ms += blackout
                self._split_ms = now
                self._state = _HyperPlayState.PLAYING
            return False

        if self._state == _HyperPlayState.RESULT:
            if (now - self._result_start_ms) >= self._auto_advance_delay_ms:
                self._dispatch(HyperPlayCompleteEvent(
                    elapsed_ms=int(math.floor(self._elapsed_ms)),
                    split_ms=self._result_split_ms,
                ))
                self._reset()
                return True

        return False

    def disarm(self) -> None:
        """Abort the current speed run silently; no event is emitted."""
        self._reset()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _dispatch(self, event: HyperPlayEmittedEvent) -> None:
        if self._on_event is not None:
            self._on_event(event)

    def _reset(self) -> None:
        self._state = _HyperPlayState.IDLE
        self._segment_id = ""
        self._checkpoints = []
        self._cp_index = 0
        self._on_event = None
        self._start_ms = 0
        self._split_ms = 0
        self._death_delay_ms = 0
        self._auto_advance_delay_ms = 0
        self._death_started_ms = 0
        self._elapsed_ms = 0
        self._result_start_ms = 0
        self._result_split_ms = 0

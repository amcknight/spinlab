"""Practice and speed-run attempt-timing state machines.

Replaces lua/spinlab.lua's ``handle_practice`` (lines 695-772) and
``handle_speed_run`` (line 778+). Phase C's poller emits Death / LevelExit /
Checkpoint / Spawn events as dicts; the orchestrator calls
``observe_event()`` to drive these state machines forward. When an attempt
completes (or the auto-advance delay elapses), the configured callback
receives a JSON-compatible dict shaped for ``protocol.AttemptResultEvent``.

State summary (PracticeTiming):
  IDLE    — not armed; observe_event/tick are no-ops.
  PLAYING — armed; watching for finish / death / abort events.
  RESULT  — finish detected; waiting ``auto_advance_delay_ms`` before
             emitting the result and returning to IDLE.

Key invariant (parity with Lua):
  Deaths increment a penalty counter — they do NOT auto-fail an attempt.
  The only failure path is a manual ``disarm()`` call or a
  ``level_exit`` event whose ``goal`` field equals ``"abort"``.

SpeedRunTiming is a minimal port of ``handle_speed_run``. It handles the
single-complete-attempt shape: arm → play (deaths + checkpoints) → complete.
Cross-verify the full timing arithmetic against ``lua/spinlab.lua``
``handle_speed_run`` (line ~778) during the smoke-test gate; refinements
belong in a follow-up commit.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from enum import Enum


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
    """State machine that converts a Phase C event stream into a protocol.AttemptResultEvent dict.

    Drives:
      - .arm(...)              — start a new attempt (resets any prior state)
      - .observe_event(...)    — feed in adapter-converted JSON-dict events
      - .tick(now_ms)          — drive the auto-advance timer; returns True if
                                 an event was emitted this call
      - .disarm()              — abort current attempt without emitting
      - .is_armed -> bool

    The orchestrator calls .tick() periodically (e.g. once per poll cycle).
    When tick fires the attempt_result, ``on_attempt_result`` receives the
    JSON dict.
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

        # Result-phase fields.
        self._elapsed_ms: int = 0
        self._completed: bool = False
        self._result_start_ms: int = 0

        self._on_attempt_result: Callable[[dict], None] | None = None

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
        on_attempt_result: Callable[[dict], None],
    ) -> None:
        """Start (or restart) an attempt. Resets all prior state without emitting."""
        self._reset()
        self._segment_id = segment_id
        self._end_type = end_type
        self._death_penalty_ms = death_penalty_ms
        self._auto_advance_delay_ms = auto_advance_delay_ms
        self._on_attempt_result = on_attempt_result
        self._start_ms = self._now()
        self._state = _PracticeState.PLAYING

    def observe_event(self, event_dict: dict) -> None:
        """Feed an incoming event into the state machine.

        Only events received while PLAYING affect state; all others are
        silently ignored, matching the Lua observe-only semantics.
        """
        if self._state != _PracticeState.PLAYING:
            return

        ev_name = event_dict.get("event")

        if ev_name == "death":
            # Deaths never auto-fail — accumulate penalty counter only.
            self._deaths += 1
            self._last_death_ms = self._now()
            return

        if self._end_type == "checkpoint":
            # Checkpoint end-type: only a checkpoint event triggers completion;
            # level_exit events are ignored entirely.
            if ev_name == "checkpoint":
                self._enter_result(completed=True)
        else:
            # Goal / exit end-type: level_exit drives completion.
            # goal == "abort" → failed; any other goal → completed.
            if ev_name == "level_exit":
                goal = event_dict.get("goal", "abort")
                self._enter_result(completed=(goal != "abort"))

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

        result: dict = {
            "event": "attempt_result",
            "segment_id": self._segment_id,
            "completed": self._completed,
            "time_ms": int(math.floor(self._elapsed_ms)),
            "deaths": self._deaths,
            "clean_tail_ms": clean_tail_ms,
        }
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
        self._elapsed_ms = 0
        self._completed = False
        self._result_start_ms = 0
        self._on_attempt_result = None


# ---------------------------------------------------------------------------
# SpeedRunTiming
# ---------------------------------------------------------------------------

class _SpeedRunState(Enum):
    IDLE = "idle"
    PLAYING = "playing"
    DYING = "dying"
    RESULT = "result"


class SpeedRunTiming:
    """Minimal port of Lua's ``handle_speed_run`` (lua/spinlab.lua ~line 778).

    Handles: arm → play (deaths emitted immediately; checkpoint splits emitted
    when hit) → complete (after auto_advance_delay_ms in RESULT state).

    Death handling follows the Lua model: on death the machine enters a
    DYING blackout state for ``death_delay_ms``; the blackout duration is
    then added back to ``start_ms`` so it doesn't count toward elapsed time.
    Split timers reset at each checkpoint (or after respawn).

    Known gaps vs. Lua (flag for smoke gate):
      - ``detect_finish`` early-finish logic (orb/key mid-level) is not
        implemented — treat ``level_exit`` as the only finish signal during
        Phase F-live smoke testing.
      - Checkpoint state-path / respawn-path tracking uses the checkpoint
        list directly; verify the index-1 (Lua) vs index-0 (Python) mapping
        is correct for your segment config during smoke testing.

    Minimal port — verify timing arithmetic against Lua during smoke-test
    gate; refinements belong in a follow-up commit.
    """

    def __init__(self, *, now_ms: Callable[[], int] | None = None) -> None:
        self._now: Callable[[], int] = now_ms or _default_now_ms
        self._state = _SpeedRunState.IDLE

        self._segment_id: str = ""
        self._checkpoints: list = []
        self._cp_index: int = 0          # next checkpoint index (0-based)
        self._on_event: Callable[[dict], None] | None = None

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
        return self._state != _SpeedRunState.IDLE

    def arm(
        self,
        *,
        segment_id: str,
        checkpoints: list,
        on_event: Callable[[dict], None] | None = None,
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
        self._state = _SpeedRunState.PLAYING

    def observe_event(self, event_dict: dict) -> None:
        """Feed an incoming event into the speed-run state machine."""
        ev_name = event_dict.get("event")

        if self._state == _SpeedRunState.PLAYING:
            if ev_name == "death":
                now = self._now()
                elapsed = now - self._start_ms
                split = now - self._split_ms
                self._dispatch({
                    "event": "speed_run_death",
                    "elapsed_ms": int(math.floor(elapsed)),
                    "split_ms": int(math.floor(split)),
                })
                self._state = _SpeedRunState.DYING
                self._death_started_ms = now

            elif ev_name == "checkpoint":
                now = self._now()
                elapsed = now - self._start_ms
                split = now - self._split_ms
                if self._cp_index < len(self._checkpoints):
                    cp = self._checkpoints[self._cp_index]
                    ordinal = cp.get("ordinal", self._cp_index + 1) if isinstance(cp, dict) else self._cp_index + 1
                    self._dispatch({
                        "event": "speed_run_checkpoint",
                        "ordinal": ordinal,
                        "elapsed_ms": int(math.floor(elapsed)),
                        "split_ms": int(math.floor(split)),
                    })
                    self._cp_index += 1

            elif ev_name == "level_exit":
                goal = event_dict.get("goal", "abort")
                if goal != "abort":
                    now = self._now()
                    self._elapsed_ms = now - self._start_ms
                    self._result_split_ms = int(math.floor(now - self._split_ms))
                    self._result_start_ms = now
                    self._state = _SpeedRunState.RESULT

        elif self._state == _SpeedRunState.DYING:
            # Dying state advances in tick(); observe_event is a no-op here.
            pass

    def tick(self, now_ms: int | None = None) -> bool:
        """Advance timers. Returns True when the complete event is emitted."""
        now = now_ms if now_ms is not None else self._now()

        if self._state == _SpeedRunState.DYING:
            if (now - self._death_started_ms) >= self._death_delay_ms:
                # Blackout elapsed; bump start_ms so dead-time is excluded.
                blackout = now - self._death_started_ms
                self._start_ms += blackout
                self._split_ms = now
                self._state = _SpeedRunState.PLAYING
            return False

        if self._state == _SpeedRunState.RESULT:
            if (now - self._result_start_ms) >= self._auto_advance_delay_ms:
                self._dispatch({
                    "event": "speed_run_complete",
                    "elapsed_ms": int(math.floor(self._elapsed_ms)),
                    "split_ms": self._result_split_ms,
                })
                self._reset()
                return True

        return False

    def disarm(self) -> None:
        """Abort the current speed run silently; no event is emitted."""
        self._reset()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _dispatch(self, event: dict) -> None:
        if self._on_event is not None:
            self._on_event(event)

    def _reset(self) -> None:
        self._state = _SpeedRunState.IDLE
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

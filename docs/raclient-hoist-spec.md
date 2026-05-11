# RAClient hoist — Phase 1 spec

**Status:** approved, not yet implemented
**Date:** 2026-05-11
**Scope:** Architectural cleanup, post-Mesen→RA migration. First phase of a
larger cleanup pass; later phases (test infra rebuild, SessionManager split,
naming) depend on this landing first.

## Context

The Mesen+Lua → RetroArch migration is complete (Phase G shipped 2026-05-08).
Along the way the codebase accumulated RA-specific knowledge in many layers:

- NCI socket calls scattered across `orchestrator.py`, `state_io.py`, `movie.py`
- Mtime-poll-for-confirmation patterns duplicated in two modules with different
  retry constants
- RA-quirk knowledge embedded in inline comments (double-tap RESET timing,
  replay-slot log scraping, hot-swap basename handling, BSV+SAVE_STATE
  constraints)
- Three separate `_FakeNCI` implementations across test files

The shape is fundamentally wrong: there is no single chokepoint where
"interacting with RetroArch" lives. RA gunk leaks into SessionManager-adjacent
code, into capture controllers, into the orchestrator's command dispatch.

This spec covers Phase 1 of the cleanup: hoist one `RAClient` façade that owns
every RA-specific operation, and let everything above it depend on a single,
narrow surface.

## Problem statement

We need:

1. **One chokepoint** for all RetroArch interaction
2. **Future SessionManager refactoring** (Phase 3) to be tractable — currently
   blocked by SessionManager depending on six different RA-adjacent modules
3. **One shared test fake** instead of three `_FakeNCI` variants
4. **Agent-debuggable logs** so that a future Claude reading
   `spinlab.log` can reconstruct what RA was asked to do and how it responded,
   without seeing the screen

## Goals

- Introduce `RAClient`, a single async façade owning all RA operations
- Keep the existing `EmuBackend` Protocol unchanged (already the right contract)
- Slim `RetroArchOrchestrator` to its actual job: command dispatch + tick loop
  + event routing
- Move SpinLab-specific path resolution out of `retroarch/` entirely
- Replace the `poller.mark_state_loaded()` side-band with a polled
  `state_version` counter
- Establish a logging convention with agent-debuggability as an explicit design
  goal
- Replace three `_FakeNCI` test scaffolds with one `FakeRAClient`

## Non-goals

These are explicitly deferred to later phases:

- **SessionManager split** (Phase 3 — A5/A6)
- **Renames** (Phase 5 — D2/D4/D6)
- **`retroarch/` directory rename** (Phase 4? — flagged below; the directory
  becomes misleading after Phase 1 because most of its contents are no longer
  RA-specific)
- **ReplayProgressEvent cleanup** (Phase 0 — debris sweep, separate commit)
- **Real-async UDP** via `asyncio.DatagramProtocol` — `to_thread` overhead is
  sub-millisecond and below noise floor; do not bother

## Current shape

| Module | Lines | RA gunk it contains |
|---|---|---|
| [retroarch/nci.py](../python/spinlab/retroarch/nci.py) | 295 | UDP transport, raw NCI commands. Already a real class. |
| [retroarch/state_io.py](../python/spinlab/retroarch/state_io.py) | 357 | **Two concerns conflated:** (a) SpinLab path resolution `segment_id ↔ Path`, (b) RA-side save/load mechanics with mtime poll, slot file cleanup, basename hot-swap |
| [retroarch/movie.py](../python/spinlab/retroarch/movie.py) | 259 | Movie file staging, mtime poll for replay file stability, file copy-out |
| [retroarch/orchestrator.py](../python/spinlab/retroarch/orchestrator.py) | 621 | **Five concerns mixed:** EmuBackend impl, RA hotkey quirks (double-tap RESET), replay-slot log scraping, verify-playback-by-WRAM, build factory with movie-dir resolution |
| [retroarch/poller.py](../python/spinlab/retroarch/poller.py) | ~140 | RAM polling via NCIClient — RA-coupled but logically a SpinLab event-detection loop |

## Target shape

### RAClient owns

- The UDP socket (NCIClient becomes internal — kept in `nci.py`, no longer
  imported outside `raclient.py`)
- Save-state mechanics (`SAVE_STATE` + mtime poll + file move)
- Load-state mechanics (file copy → slot → `LOAD_STATE_SLOT`)
- Slot file cleanup
- Movie record (start, stop with file copy-out)
- Movie play (stage at slot, verify by WRAM tick)
- Replay-slot resolution (log scrape)
- Hotkey-press quirks (RESET ×2, slot-minus debounce pacing)
- RA log dir auto-resolution (currently in `build_orchestrator`)
- Game basename auto-detection on connect (from `GET_STATUS`)
- Monotonic `state_version` counter (bumps on every successful `load_state`)

### Orchestrator keeps

- `EmuBackend` Protocol implementation
- Command dispatch table mapping protocol cmd types to handlers
- `_tick_loop` driving `practice_timing.tick()` / `speed_run_timing.tick()`
- `on_poller_event` routing into timing modules + queue
- Connection lifecycle of background tasks (poller, tick)
- Disconnect-warning suppression
- Conditions registry wiring (`_on_set_conditions`)

### Moves out of `retroarch/` entirely

- **SpinLab path resolution** (`state_path_for`, `resolve_event_path`,
  `segment_id_for_event`) → new module `python/spinlab/state_paths.py` at
  the top level. This is SpinLab data-model logic, not RA logic.

### Stays in `retroarch/` (unchanged in Phase 1)

- `Poller`, `TransitionDetector`, `ColdFillSpawnDetector`, `snapshot.py`,
  `predicates.py`, `transition_state.py`, `timing.py`, `addresses.py`,
  `ConditionRegistry`, `exceptions.py`, `responses.py` — these depend on
  `RAClient.read_ram` but are SpinLab event-detection logic. They will likely
  move out of `retroarch/` in a later phase since the directory name becomes
  misleading once `RAClient` owns the actual RA-specific code; **out of scope
  for Phase 1**.

## RAClient interface

Single file: `python/spinlab/retroarch/raclient.py`. Target size ~460 lines.
If it crosses ~700, split internally — but do not pre-split.

```python
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

@dataclass(frozen=True)
class ConnectInfo:
    rom_filename: str
    system: str
    crc32: str

@dataclass(frozen=True)
class StatusInfo:
    state: str            # "PLAYING" / "PAUSED" / "CONTENTLESS" / ...
    rom_filename: str | None
    system: str | None
    crc32: str | None

@dataclass
class MovieRecording:
    path: Path
    async def stop(self) -> Path: ...   # halts RA, copies file out

@dataclass
class MoviePlayback:
    path: Path
    frame_count: int
    async def stop(self) -> None: ...

class RAHotkey(StrEnum):
    RESET = "RESET"
    PAUSE_TOGGLE = "PAUSE_TOGGLE"
    FRAME_ADVANCE = "FRAMEADVANCE"
    SAVE_STATE = "SAVE_STATE"
    RECORD_REPLAY = "RECORD_REPLAY"
    HALT_REPLAY = "HALT_REPLAY"
    PLAY_REPLAY = "PLAY_REPLAY"
    REPLAY_SLOT_MINUS = "REPLAY_SLOT_MINUS"
    REPLAY_SLOT_PLUS = "REPLAY_SLOT_PLUS"

class RAClient:
    """High-level RetroArch client.

    Encapsulates RA's network protocol, state-file mechanics, movie I/O, and
    the hotkey/log/timing quirks of RA 1.22.x. The rest of SpinLab depends
    only on this surface.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        ra_savestate_dir: Path,
        ra_log_dir: Path | None,
        ra_movie_dir: Path | None = None,
    ) -> None: ...

    # --- Lifecycle ---
    async def connect(self, timeout: float = 5.0) -> ConnectInfo:
        """Probe NCI + GET_STATUS. Caches game_basename internally.

        Raises NotReachableError if NCI doesn't respond within `timeout`.
        """
    async def disconnect(self) -> None: ...

    @property
    def is_connected(self) -> bool: ...

    @property
    def game_basename(self) -> str | None:
        """ROM basename from last GET_STATUS. None until connect() succeeds."""

    @property
    def state_version(self) -> int:
        """Monotonic counter; increments on every successful load_state().
        Poller reads this each tick; change → resync detectors."""

    # --- Status & memory ---
    async def get_status(self) -> StatusInfo: ...
    async def read_ram(self, addr: int, length: int) -> bytes: ...
    async def write_ram(self, addr: int, data: bytes) -> None: ...

    # --- Save states ---
    async def save_state(self, dest_path: Path) -> Path:
        """SAVE_STATE → mtime-poll for confirmation → move to dest_path.

        Returns the final dest_path on success. Raises StateSaveTimeoutError
        if RA's slot file does not appear/change within budget.
        """

    async def load_state(self, src_path: Path) -> None:
        """Copy src_path into RA's reserved slot, fire LOAD_STATE_SLOT.

        Increments state_version on success. Caller does NOT need to notify
        the poller separately — poller polls state_version.

        Raises StateLoadError on filesystem failure or RA refusal.
        """

    # --- Movie record / play ---
    async def record_movie(self, dest_path: Path) -> MovieRecording:
        """Fire RECORD_REPLAY. Returns a handle whose .stop() halts RA and
        copies the file out to dest_path.
        """

    async def play_movie(self, src_path: Path) -> MoviePlayback:
        """Stage src_path at RA's current runtime slot, fire PLAY_REPLAY,
        verify by sampling WRAM advances. Returns a handle with frame_count
        (read from sibling .json metadata if present, else 0).

        Raises MoviePlaybackError if RA refused the file (no WRAM advance
        within verification window).
        """

    # --- Hotkeys ---
    async def press(self, key: RAHotkey, *, taps: int = 1) -> None:
        """Press a hotkey N times with the right inter-tap spacing for that
        key. Spacing comes from an internal HotkeyProfile table.
        """

    async def reset(self) -> None:
        """Convenience: press(RAHotkey.RESET, taps=2). RA's RESET requires
        a two-press anti-accident gate.
        """
```

### Hotkey profile table

Centralizes the magic numbers currently scattered as comments
([orchestrator.py:67](../python/spinlab/retroarch/orchestrator.py#L67),
[orchestrator.py:40-48](../python/spinlab/retroarch/orchestrator.py#L40-L48)):

```python
@dataclass(frozen=True)
class HotkeyProfile:
    min_tap_gap_sec: float
    """Spacing between repeated presses to satisfy RA's input-layer quirks."""

# RA's input layer debounces hotkeys at ~6Hz (167ms between accepts).
# RESET additionally requires the second press inside its anti-accident
# confirmation window; 300ms is comfortably inside both bounds.
_HOTKEY_PROFILES: dict[RAHotkey, HotkeyProfile] = {
    RAHotkey.RESET: HotkeyProfile(min_tap_gap_sec=0.3),
    RAHotkey.REPLAY_SLOT_MINUS: HotkeyProfile(min_tap_gap_sec=0.18),
    RAHotkey.REPLAY_SLOT_PLUS: HotkeyProfile(min_tap_gap_sec=0.18),
    # All others use a sensible default.
}
_DEFAULT_HOTKEY_PROFILE = HotkeyProfile(min_tap_gap_sec=0.05)
```

## State-version counter design (fixes #9)

Today the orchestrator manually calls `poller.mark_state_loaded()` after
`state_io.load_state_from_path()` ([orchestrator.py:232](../python/spinlab/retroarch/orchestrator.py#L232)).
Forget the call → phantom edges from stale snapshots. The poller has to be
told.

New design: `RAClient` maintains a private `_state_version: int` field.
`load_state()` increments it on success. The poller reads `state_version` each
tick and tracks a `_last_seen_state_version`; when they differ, it calls
`resync_after_state_load()` on both detectors.

Concrete change:

```python
# In RAClient
async def load_state(self, src_path: Path) -> None:
    # ... file copy + LOAD_STATE_SLOT ...
    self._state_version += 1

@property
def state_version(self) -> int:
    return self._state_version

# In Poller (one new line in step loop)
def _tick(self) -> None:
    v = self._raclient.state_version
    if v != self._last_seen_state_version:
        self._transition_detector.resync_after_state_load()
        self._cold_fill_detector.resync_after_state_load()
        self._last_seen_state_version = v
    # ... existing snapshot read + step ...
```

`Poller.mark_state_loaded()` and `Poller.activate_cold_fill()`'s coupling to
state-load go away (the activate call still exists for cold-fill activation
specifically; only the load-time resync becomes automatic).

## Logging convention

**Design goal:** a future agent reading `spinlab.log` should be able to
reconstruct what RA was asked to do and how it responded, time-correlated with
the gameplay events that triggered it, **without seeing the screen.**

Levels:

| Level | What gets logged | Example |
|---|---|---|
| INFO | High-level ops with structural context | `save_state segment=2-2:entrance path=…/2-2_entrance.state took=120ms` |
| INFO | State transitions (connect, disconnect) | `connected rom="Toothpaste.smc" crc32=abc123` |
| WARNING | RA misbehaved (timeout, refused load) | `save_state mtime_poll_timeout segment=2-2:entrance budget_ms=1000` |
| WARNING | Recoverable structural issue | `replay_slot log_unavailable falling_back_to=0` |
| ERROR | Unrecoverable | `connect failed_permanently attempts=5` |
| DEBUG | Per-command NCI traffic (gated; off by default) | `nci_send "READ_CORE_RAM 13bf 1" reply="READ_CORE_RAM 13bf 02"` |

Division of responsibility:

- **Callers** (orchestrator, capture controllers, SessionManager) log their
  **decisions**: "entering practice mode for segment X", "starting reference
  capture", "user requested reset".
- **RAClient** logs its **actions and outcomes**: "save_state X done in 120ms",
  "play_movie X verification failed", "reset double-tapped".
- **No double-logging** — if RAClient logs an action, the caller doesn't also
  log it. The caller logs the *reason* for the action; RAClient logs the
  *execution*.

Format: keyword=value pairs for grep-ability. No emoji, no decorative prefixes.
Timestamps come from the existing log format.

## Implementation plan

Two commits. No incremental-green-tests discipline — Andrew confirmed no data
or users to protect; tests will be broken at intermediate states.

### Commit 1: RAClient hoist + tests rewritten

- Create `python/spinlab/retroarch/raclient.py` with the full interface above
- Create `python/spinlab/state_paths.py` (moves `state_path_for`,
  `resolve_event_path`, `segment_id_for_event` out of `retroarch/state_io.py`)
- Delete `python/spinlab/retroarch/state_io.py` (save/load mechanics absorbed
  into RAClient; path resolution moved)
- Delete or thin `python/spinlab/retroarch/movie.py` (record/play mechanics
  absorbed)
- Slim `python/spinlab/retroarch/orchestrator.py` from 621 → ~300 lines:
  - Construct `RAClient` instead of `NCIClient` + `StateIO` + `MoviePlayer` +
    `MovieRecorder`
  - Replace `_state_io.load_state_from_path()` calls with
    `_raclient.load_state()` (and drop adjacent `mark_state_loaded()` calls)
  - Replace `_state_io.save_segment_state()` calls with
    `_raclient.save_state(path)` where path comes from `state_paths` resolver
  - Move replay-slot resolution out (now inside RAClient.play_movie)
  - Move WRAM-tick verification out (now inside RAClient.play_movie)
  - Move RESET double-tap out (now `await raclient.reset()`)
  - Simplify `build_orchestrator`: no movie-dir derivation, no log-dir lookup
    — RAClient does both internally from constructor params
- Update `Poller` to read `raclient.state_version` instead of receiving
  `mark_state_loaded()` calls
- Create `tests/fakes/__init__.py` and `tests/fakes/raclient.py` with one
  `FakeRAClient` implementation
- Delete `tests/unit/retroarch/test_orchestrator.py` and
  `tests/unit/test_retroarch_orchestrator.py`; replace with one consolidated
  test file using `FakeRAClient`
- Delete the three `_FakeNCI` definitions in test files; consumers use
  `FakeRAClient` instead
- Update remaining tests that fake `NCIClient` or `StateIO` directly to use
  `FakeRAClient`
- Establish logging convention in RAClient as defined above

### Commit 2: Wart fixes that come along for the ride

These are independent of RAClient but easy to bundle once the structure is right:

- Delete dead `is_core_running` ([nci.py:252-271](../python/spinlab/retroarch/nci.py#L252-L271))
  and its docstring cross-references at lines 166, 189, 192
- Delete `ReplayProgressEvent` field, handler, and state-builder wiring
  (B1 — no emitter exists)
- Delete `save_segment_state`'s unused `Path` return value (B6)
- Strip phase-N comments ("Phase B", "Phase D", "Phase 0", "Phase E") across
  the codebase
- Delete pycache files for removed test modules
- Compress `vite.py` module docstring; move detail to a doc

## Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| RAClient grows past 700 lines | medium | Pre-committed: split internally if it crosses the threshold. Estimated landing size is ~460. |
| State-version counter race in poller | low | Poller is single-threaded; reads `state_version` once per tick. `RAClient._state_version` is bumped in `load_state` which runs on a worker thread, but a stale read by the poller just defers resync by one tick (~16ms) — harmless. |
| Test rewrite reveals coverage gaps | medium | Expected; surface them in commit 1 PR description. Coverage gaps that can't be addressed during commit 1 get filed as follow-ups. |
| Replay-slot resolution behaves differently inside RAClient | low | Logic moves verbatim; only the caller changes. Existing test cases for slot parsing transfer to new location. |
| Movie record/play file-stability polling diverges from current behavior | medium | Both polling implementations get unified inside RAClient. Pick the more robust (movie.py's two-phase: find file, then wait for mtime+size stability). |

## Open questions resolved before locking

These were debated and resolved during design; recording for future reference.

- **Paths in, segment_ids stay on the caller side.** RAClient never imports
  SpinLab data model.
- **Single file, not a package, for RAClient.** Pre-splitting recreates the
  scattered-RA-knowledge problem we're fixing.
- **Full async surface, sync UDP underneath.** `asyncio.DatagramProtocol` is
  fiddly and gains nothing measurable.
- **No async context managers for movies.** Lifecycles span HTTP requests;
  handles + explicit `.stop()` is the only shape that fits.
- **`NCIClient` keeps its name** (matches RA's own docs), becomes internal
  by convention.
- **Build `FakeRAClient` in this phase**, don't leave test cleanup for later.
- **`state_version` counter** replaces `mark_state_loaded()` side-band.
- **Return types:** `save_state → Path`, `load_state → None`, movies →
  real handle dataclasses, `connect → ConnectInfo`. No `SaveResult`/`LoadResult`
  ceremony.

## Open questions to revisit during implementation

These aren't blockers but flag for during-implementation decisions:

- **Exception hierarchy.** Today: `NCIError`, `NCITimeout`, `NCIProtocolError`.
  Adding: `StateSaveTimeoutError`, `StateLoadError`, `MoviePlaybackError`,
  `NotReachableError`. Should they all derive from a single `RAClientError`?
  Lean: yes, for catch-all callers; resolve when writing the code.
- **Hotkey profile defaults.** `_DEFAULT_HOTKEY_PROFILE` uses 50ms gap. May
  need tuning if any default hotkey turns out to need the 6Hz debounce.
  Empirical question — pick a safe value, log it if it ever bites.
- **`RAClient.write_ram` confirmation.** Today fire-and-forget. Worth a
  follow-up read? Probably not by default; document the contract.
- **Path-resolution module location.** `python/spinlab/state_paths.py` (top
  level) vs `python/spinlab/capture/state_paths.py`. Lean: top level — used
  by both capture and routes.

## Things flagged for later phases

Captured here so they don't get lost:

- **`retroarch/` directory rename.** After Phase 1, most modules in
  `retroarch/` are no longer RA-specific (snapshot.py, detector.py,
  cold_fill.py, predicates.py, transition_state.py, addresses.py,
  timing.py). Directory becomes misleading. Probable later move:
  `spinlab/detection/` for the event-detection modules, leave only
  `RAClient`, `NCIClient`, `Poller` in `retroarch/`.
- **SessionManager split (A5/A6).** Phase 3. Becomes tractable once
  RAClient is the only RA dependency.
- **D2/D4 renames.** Phase 5. After structure settles.
- **Test file consolidation beyond the orchestrator tests** (C7 — StateIO
  tests split across 5 files). Some of this happens naturally when StateIO
  is deleted; remaining work is small.

## Success criteria

Phase 1 lands when:

- `RAClient` exists and is the sole RA-specific dependency for everything
  above it
- `EmuBackend` Protocol is unchanged
- `tests/fakes/raclient.py` provides `FakeRAClient`
- The two duplicate orchestrator test files are gone; one consolidated test
  file remains
- Three `_FakeNCI` test scaffolds are gone
- `python/spinlab/retroarch/state_io.py` is deleted
- `python/spinlab/state_paths.py` exists at top level
- `python/spinlab/retroarch/orchestrator.py` is under 350 lines
- `python -m pytest` passes (full suite, per CLAUDE.md)
- `npx pyright python/` produces no new errors
- Wart-fix commit (commit 2) lands behind it

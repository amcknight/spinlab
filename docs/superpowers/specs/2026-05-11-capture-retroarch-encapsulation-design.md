# Capture + RetroArch Encapsulation Pass

## Goal

Shrink the two largest behavioral files (`capture/reference.py` at 660 lines and `retroarch/orchestrator.py` at 475 lines) by extracting clusters that already have natural boundaries into focused units. Each extraction has a clear public surface, independent testability, and removes a chunk of state or logic from a file that has grown to do too much.

## Why now

The post-migration audit ([2026-05-10](../../../memory/project_post_migration_audit_2026_05_10.md), all seven passes complete) cleared the structural debt from the Lua/Mesen removal. What remains is the "files that grew during migration and never got slimmed" problem: `reference.py` and `orchestrator.py` each carry 4+ distinct concerns that could be understood and tested in isolation.

The user's framing: "encapsulation is usually the biggest win for bringing both clarity and testability." Each extraction in this pass produces a testable seam where there isn't one today — every new file gets its own focused unit test, including for code paths only exercised today through integration tests (movie record/replay) or via large blob tests on the orchestrator (atomic finalize-run SQL).

## Out of scope

- **Splitting `raclient.py`** (761 lines, 35% coverage). The audit flagged it as a "split after tests" item — restructuring a low-coverage file without test scaffolding is too risky. Sequenced after the dedicated RAClient unit-test pass.
- **Splitting `session_manager.py`.** The audit references a planned SessionManager split as a separate phase tied to the RAClient hoist follow-up. Not bundled here.
- **Moving `SegmentRecorder` or `ColdFillController`.** Both are already focused — 220 and 141 lines, single-responsibility.
- **The smaller retroarch/ files** (`exceptions.py`, `responses.py`, `addresses.py`, `transition_state.py`, `snapshot.py`, `predicates.py`, `poller.py`, `detector.py`, `cold_fill_detector.py`, `nci.py`). All already focused, all under 280 lines.
- **Behavior changes.** This is a pure refactor pass. Each commit must preserve current behavior; new tests document the existing contract rather than changing it.

## Architecture

```
python/spinlab/capture/
├── __init__.py
├── recorder.py        unchanged (220 lines — SegmentRecorder)
├── cold_fill.py       unchanged (141 lines — ColdFillController)
├── reference.py       SLIMMED to ~520 lines
├── fill_gap.py        NEW (~55 lines — FillGapController)
└── finalizer.py       NEW (~110 lines — atomic_save_and_finish_run)

python/spinlab/retroarch/
├── orchestrator.py    SLIMMED to ~310 lines
├── movies.py          NEW (~95 lines — MovieController)
├── wiring.py          NEW (~95 lines — build_orchestrator factory)
└── (all other files unchanged)
```

Net delta: +5 files, two largest files lose ~30% lines each, four new focused units each under 120 lines.

## Components

### `capture/finalizer.py` — `atomic_save_and_finish_run`

Module-level function (not a class). Extracts the 100-line atomic SQL block currently inlined in `ReferenceController.save_and_finish_run` (reference.py:368-446).

```python
def atomic_save_and_finish_run(
    db: Database,
    run_id: str,
    session_id: str | None,
    name: str,
) -> list[Attempt]:
    """End session + drain timing rows + promote draft + activate + seed attempts, atomically.

    Inlines all five mutations inside an explicit BEGIN IMMEDIATE because the
    individual db helper methods each commit() internally. Either every step
    succeeds, or rollback leaves every row exactly as it was. Returns the
    seeded Attempt objects so the caller can log them.

    Raises whatever sqlite3 raises on rollback; caller responsible for
    surfacing the right ActionResult.
    """
```

**Why a function, not a class.** This is a single atomic operation, not a stateful unit. A class wrapping one method adds ceremony without clarity. A future relocation to `Database` (e.g., `db.atomic_save_and_finish_run`) becomes a mechanical move.

**Tests.** New `tests/unit/capture/test_finalizer.py`:
- Happy path: commit succeeds, all 5 mutations applied, returns expected Attempts.
- Rollback path: inject failure mid-transaction (e.g., monkeypatch one of the db helpers to raise), assert no partial state.
- Drain idempotence: re-running the function on an already-drained run should be a no-op for the timing-rows section (test the empty-list branch).

Currently untested directly — the rollback path has no integration coverage at all.

### `capture/fill_gap.py` — `FillGapController`

Owns the fill-gap state (`fill_gap_segment_id`, `_fill_gap_waypoint_id`) and the two methods that read/write it. Sits alongside `ColdFillController` in `capture/`.

```python
class FillGapController:
    def __init__(self, db: Database, emu: EmuBackend) -> None: ...

    @property
    def is_active(self) -> bool: ...
    @property
    def segment_id(self) -> str | None: ...

    async def start(self, segment_id: str) -> ActionResult:
        # Resolve segment's hot save state; raise NoHotVariantError if missing.
        # Send FillGapLoadCmd. Set internal state.

    def handle_spawn(self, event: SpawnEvent) -> bool:
        # If state_path present and we have an active segment, persist the
        # cold save state to the start waypoint and clear state. Return True
        # if consumed; False if not active or event has no state_path.

    def clear(self) -> None:
        # Reset state without persistence (called on disconnect / error).
```

**Why extract.** Today, fill-gap state is mixed into `ReferenceController` fields and methods alongside reference-recording state. They share no invariants — `fill_gap_segment_id` and `paused_run_id` can both be set; they don't interact. The fill-gap flow has its own simple state machine (`start → handle_spawn → clear`) that's obscured by being a few methods in a class that also owns the reference-recording invariants.

**SessionManager change.** Grows `self.fill_gap: FillGapController` alongside `self.cold_fill: ColdFillController`. The `_handle_spawn` dispatch in `session_manager.py` already branches; the fill-gap branch calls `self.fill_gap.handle_spawn(event)` instead of `self.capture.handle_fill_gap_spawn(event)`.

**Tests.** New `tests/unit/capture/test_fill_gap.py`:
- `start` raises `NoHotVariantError` when segment has no hot variant.
- `start` raises `NotConnectedError` when emu disconnected.
- `start` sends `FillGapLoadCmd` and sets `is_active=True`.
- `handle_spawn` with `state_path=None` returns False, leaves state alone.
- `handle_spawn` with no `_fill_gap_waypoint_id` returns False.
- `handle_spawn` happy path persists `WaypointSaveState` and clears state.
- `clear` resets state.

Currently exercised only via `tests/unit/test_session_manager.py::TestFillGap` (one integration-style test). New tests give direct coverage.

### `retroarch/movies.py` — `MovieController`

Owns the movie record/playback state currently in `RetroArchOrchestrator` (`_active_recording`, `_active_playback`, `_fast_forwarding`). Exposes 4 methods; orchestrator's 4 movie command handlers become 1-line delegations.

```python
class MovieController:
    def __init__(
        self,
        raclient: RAClient,
        enable: bool,
        on_event: Callable[[object], None],
    ) -> None: ...

    @property
    def is_recording(self) -> bool: ...
    @property
    def is_playing(self) -> bool: ...

    async def start_recording(self, path: Path) -> None:
        # No-op if not enabled. Non-fatal on RAClientError (log + return).

    async def stop_recording(self) -> None:
        # No-op if nothing active.

    async def start_playback(self, path: Path, speed: int) -> None:
        # Raises BackendNotImplementedError if disabled.
        # Emits ReplayStartedEvent or ReplayErrorEvent via on_event.
        # If speed == SPEED_UNCAPPED, toggles RA into fast-forward.

    async def stop_playback(self) -> None:
        # Idempotent. Symmetric fast-forward toggle on the way out.
        # Emits ReplayFinishedEvent.
```

**Why extract.** The movie handlers carry their own cross-call state (`_fast_forwarding` is a flip-flop with no NCI query, so symmetric toggling is the only safe API). That state has nothing to do with command dispatch, the tick loop, or the EmuBackend protocol surface. Pulling it out makes the orchestrator's remaining handlers uniformly stateless one-liners.

**Tests.** New `tests/unit/retroarch/test_movies.py`:
- `start_recording` no-op when `enable=False`.
- `start_recording` non-fatal on `RAClientError` (logs, returns, no exception).
- `stop_recording` no-op when nothing active.
- `start_playback` raises `BackendNotImplementedError` when `enable=False`.
- `start_playback` happy path emits `ReplayStartedEvent` with `frame_count`.
- `start_playback` with `SPEED_UNCAPPED` calls `fast_forward_toggle`.
- `start_playback` with `speed=0` (default) does not toggle.
- `start_playback` MoviePlaybackError emits `ReplayErrorEvent`, does not raise.
- `stop_playback` symmetric `fast_forward_toggle` when fast-forwarding was on.
- `stop_playback` no second toggle when fast-forwarding was off.
- `stop_playback` idempotent — second call is a no-op.

The fast-forward symmetric-toggle contract is currently only exercised by the replay-fixture integration test.

### `retroarch/wiring.py` — `build_orchestrator`

Pure mechanical move. The factory at orchestrator.py:381-475 becomes the entirety of `wiring.py`. The only caller (`dashboard.py`'s import path) updates from `spinlab.retroarch.orchestrator import build_orchestrator` to `spinlab.retroarch.wiring import build_orchestrator`.

**Why extract.** The factory is config-parsing + path-resolution + dependency-injection wiring — none of which is part of the EmuBackend protocol implementation. Hidden inside `orchestrator.py`, it makes the file look like it owns config concerns it doesn't actually own. Moving it makes `orchestrator.py` purely about implementing the protocol.

**Tests.** Existing tests in `tests/unit/retroarch/test_orchestrator.py` that exercise `build_orchestrator` get their import paths updated. No new tests; the factory's behavior is unchanged.

## Sequencing

Four commits, each green and independently revertable. Order minimizes interdependency:

1. **`capture/finalizer.py`** — extract `atomic_save_and_finish_run` + new unit test. ReferenceController.save_and_finish_run becomes ~15 lines (validate state, call function, rebuild scheduler, transition to idle). No SessionManager change. No public API change.

2. **`capture/fill_gap.py`** — extract `FillGapController` + new unit test. SessionManager grows `self.fill_gap`; `_handle_spawn` dispatch updated to call `self.fill_gap.handle_spawn` first (returns True → consumed). ReferenceController loses 2 fields + 2 methods.

3. **`retroarch/movies.py`** — extract `MovieController` + new unit test. Orchestrator constructor takes a `movies: MovieController` parameter; `_on_reference_start`, `_on_reference_stop`, `_on_replay`, `_on_replay_stop` become 1-line delegations. `build_orchestrator` constructs MovieController and wires it in.

4. **`retroarch/wiring.py`** — move `build_orchestrator` function verbatim. Update `dashboard.py` import. Update test import paths in `tests/unit/retroarch/test_orchestrator.py` (and any other consumers).

Each commit runs the full suite green before the next begins.

## Testability deltas

| Extraction | New direct unit test | Previously covered by |
|---|---|---|
| `atomic_save_and_finish_run` | Commit + rollback paths | No direct test; integration-only via save-and-finish dashboard flow |
| `FillGapController` | 7 focused cases (start errors, start happy path, handle_spawn branches) | 1 integration-style test in `test_session_manager.py::TestFillGap` |
| `MovieController` | 11 focused cases (enable gating, fast-forward toggle symmetry, error paths, idempotency) | Replay-fixture integration test only |
| `build_orchestrator` | Existing tests retained | (unchanged) |

The 3 newly-testable units total ~20 new unit tests. None of them require the emulator; all run in the fast suite.

## Risk

- **`FillGapController` extraction touches SessionManager dispatch.** This is the highest-risk change because it shifts where a routing decision happens. Mitigation: the existing `test_session_manager.py::TestFillGap` test stays green throughout (it tests the user-visible end-to-end behavior, not the internal dispatch shape).
- **`MovieController` extraction is the largest by line-count moved.** Mitigation: split the commit's body into "introduce MovieController class" and "rewire orchestrator handlers" — actually land both in the same commit but with the two halves clearly visible in the diff.
- **`save_and_finish_run` is the most subtle code being relocated.** It's a transaction with rollback semantics; the extracted function must preserve those exactly. Mitigation: write the unit test for the rollback path *before* extraction — verifies the test catches the failure mode, then verifies the extracted code still passes.

## Definition of done

- All four extractions landed as separate commits on main.
- Full pytest suite (`python -m pytest`) green after each commit.
- Each new file under 120 lines.
- Each new file has its own focused unit test file.
- `reference.py` slimmed to ~520 lines; `orchestrator.py` slimmed to ~310 lines.
- No behavior changes observable from the dashboard or via API.
- Memory note added pointing at this spec for future-me asking "what got encapsulated when."

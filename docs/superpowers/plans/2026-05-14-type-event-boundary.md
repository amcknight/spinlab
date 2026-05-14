# CF1' — Type the Event Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Thread precise types through the protocol-event boundary so Pyright verifies the conditions/event shape end-to-end and downstream `# type: ignore` candidates can be re-examined.

**Architecture:** Events carry **raw** condition values (`dict[str, int]` from `ConditionRegistry.read_all`), not decoded ones. Decoding happens transiently inside `SegmentRecorder._close_segment` via `condition_registry.decode()` before persistence. Type the protocol dataclasses to spell this out, then propagate precise types through the poller/movies callbacks and the `PendingStart` carrier, then narrow `decode`'s return.

**Tech Stack:** Python 3.11+ with `from __future__ import annotations`, frozen dataclasses, Pyright (run via `npx pyright python/`).

---

## File Structure

**Modified files (no new files needed):**

- `python/spinlab/protocol.py` — Add a `PollerEvent` union type alias; tighten `conditions: dict` → `dict[str, int]` on 5 events; add a `MovieEvent` union for the movie-controller callback surface.
- `python/spinlab/retroarch/poller.py` — Replace `Callable[[Any], None]` with `Callable[[PollerEvent], None]`; tighten `_stamp_state_path` / `_stamp_conditions` signatures from `Any → Any` to `PollerEvent → PollerEvent`; tighten `state_path_for` to `Callable[[PollerEvent], str | None]`.
- `python/spinlab/retroarch/movies.py` — Replace `Callable[[object], None]` with `Callable[[MovieEvent], None]`.
- `python/spinlab/capture/recorder.py` — `PendingStart.raw_conditions: dict` → `dict[str, int]`.
- `python/spinlab/condition_registry.py` — `decode() -> dict[str, Any]` → `dict[str, str | bool]`; update the `# Used by capture-side decoding` docstring to spell out the value union.
- `python/spinlab/state_builder.py` (if it consumes `conditions`) — narrow as needed.

**Investigated but not necessarily modified (stretch):**

- `python/spinlab/db/model_state.py:69,80,95` and `python/spinlab/db/attempts.py:96` — `# type: ignore` on `dict(zip(cols, row))` patterns. These are about SQLite-row TypedDict conformance, not condition typing; document the finding and either remove or leave intact based on what Pyright actually says.

---

## Test Approach

This work is pure type-annotation tightening. Behavioral tests already cover the event-stamping path (`tests/unit/retroarch/test_poller.py`), the recorder (`tests/unit/capture/test_recorder.py`), and the registry decode (`tests/unit/test_condition_registry.py`). **The primary verification per task is `npx pyright python/<changed-file>` reporting 0 new errors**, then full `python -m pytest` to confirm no runtime regression.

For each task:
1. Make the type change.
2. Run `npx pyright python/<file>` — must be clean (or fail with exactly the call sites we expect to update in the same task).
3. Run scoped pytest for the affected module.
4. Commit.

The whole-suite `python -m pytest` + whole-tree `npx pyright python/` is the gate before the final merge commit (Task 9).

---

### Task 1: Pin baseline state — Pyright + pytest both clean before any change

**Files:** none (baseline check)

- [ ] **Step 1: Capture baseline Pyright counts**

Run:
```bash
npx pyright python/ 2>&1 | tail -5
```
Expected: a line like `N errors, M warnings, K informations` — record the numbers in a scratch note. **The plan succeeds only if all later runs match or improve these numbers.** Existing errors are tracked; we just must not introduce new ones.

- [ ] **Step 2: Capture baseline full pytest**

Run:
```bash
python -m pytest -q
```
Expected: all tests pass (emulator tests may skip if RA isn't running; that's fine). Record pass/skip counts.

- [ ] **Step 3: Confirm git state**

Run:
```bash
git status && git log --oneline -3
```
Expected: clean working tree, currently on `improve/type-event-boundary` at commit `f040571` (the `/improve` scan) or its descendant.

- [ ] **Step 4: No commit — this is observation only**

---

### Task 2: Tighten `conditions: dict` → `dict[str, int]` on the 5 poller events

**Files:**
- Modify: `python/spinlab/protocol.py:34,43,49,55,69`

**Why `dict[str, int]` and not `dict[str, str | bool]`:** the poller stamps events from `ConditionRegistry.read_all()`, which is declared `dict[str, int]`. Decoded conditions (str | bool) only exist transiently inside `SegmentRecorder._close_segment`. Spelling the union into the event field would lie about what's actually there at the wire.

- [ ] **Step 1: Edit the 5 dataclasses**

```python
# python/spinlab/protocol.py — change in all five places

@dataclass(frozen=True)
class LevelEntranceEvent:
    level: int = 0
    room: int = 0
    frame: int = 0
    state_path: str | None = None
    timestamp_ms: int = 0
    conditions: dict[str, int] = field(default_factory=dict)

@dataclass(frozen=True)
class CheckpointEvent:
    level_num: int = 0
    cp_ordinal: int = 1
    cp_type: str = ""
    state_path: str | None = None
    timestamp_ms: int = 0
    conditions: dict[str, int] = field(default_factory=dict)

@dataclass(frozen=True)
class DeathEvent:
    level_num: int = 0
    timestamp_ms: int = 0
    conditions: dict[str, int] = field(default_factory=dict)

@dataclass(frozen=True)
class SpawnEvent:
    level_num: int = 0
    state_path: str | None = None
    conditions: dict[str, int] = field(default_factory=dict)
    is_cold_cp: bool = False
    cp_ordinal: int | None = None
    segment_id: str = ""
    timestamp_ms: int = 0

@dataclass(frozen=True)
class LevelExitEvent:
    level: int = 0
    room: int = 0
    goal: str = "abort"
    elapsed_ms: int = 0
    frame: int = 0
    timestamp_ms: int = 0
    conditions: dict[str, int] = field(default_factory=dict)
```

- [ ] **Step 2: Pyright after the protocol edit**

Run:
```bash
npx pyright python/spinlab/protocol.py
```
Expected: clean (the dataclasses themselves only have local references).

- [ ] **Step 3: Pyright across the consumers — find broken sites**

Run:
```bash
npx pyright python/spinlab/retroarch/poller.py python/spinlab/capture/recorder.py python/spinlab/state_builder.py python/spinlab/session_manager.py 2>&1 | tail -30
```
Expected: any NEW errors here are spots where downstream code passes a non-`dict[str, int]` into a `conditions=` slot. Most likely zero — the poller already builds `dict[str, int]` from `read_all`. Record any errors; they'll be fixed in subsequent tasks (do NOT silence with `# type: ignore` yet).

- [ ] **Step 4: Run protocol + recorder tests**

Run:
```bash
python -m pytest tests/unit/test_models.py tests/unit/capture/test_recorder.py tests/unit/retroarch/test_poller.py -q
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/protocol.py
git commit -m "$(cat <<'EOF'
protocol: tighten event conditions to dict[str, int]

Five frozen event dataclasses (LevelEntrance, Checkpoint, Death, Spawn,
LevelExit) carried `conditions: dict` with no value type. The poller
stamps these from `ConditionRegistry.read_all` which returns
`dict[str, int]`, so spell that out. Decoding to `dict[str, str | bool]`
happens transiently in `SegmentRecorder._close_segment` — not at the
event boundary.

Step 1 of CF1' (type event boundary).
EOF
)"
```

---

### Task 3: Add `PollerEvent` and `MovieEvent` union aliases

**Files:**
- Modify: `python/spinlab/protocol.py` (add aliases at the bottom of the events section, before the Commands section)

Union aliases give the poller and movie controller callbacks something concrete to point at. The names should be discoverable: `PollerEvent` for everything the poller emits; `MovieEvent` for the replay-lifecycle events the `MovieController` emits.

- [ ] **Step 1: Read the existing event list to confirm membership**

```bash
grep -n "^class \|^@dataclass" python/spinlab/protocol.py | head -40
```
Verify the events: `RomInfoEvent`, `GameContextEvent`, `LevelEntranceEvent`, `CheckpointEvent`, `DeathEvent`, `SpawnEvent`, `LevelExitEvent`, `AttemptResultEvent`, `ReplayStartedEvent`, `ReplayFinishedEvent`, `ReplayErrorEvent`, `AttemptInvalidatedEvent`, `SpeedRunCheckpointEvent`, `SpeedRunDeathEvent`, `SpeedRunCompleteEvent`.

- [ ] **Step 2: Edit `protocol.py` — add union aliases after the last event class and before the `# Commands` divider comment**

Insert (preserving the existing divider afterwards):

```python
# ---------------------------------------------------------------------------
# Event unions
#
# PollerEvent: events the Poller can emit (memory-driven transitions + the
# infrastructure events stamped onto the same stream by the orchestrator).
# MovieEvent: events the MovieController emits during replay playback.
# Used as the parameter type of callback signatures so call sites stop
# defaulting to `Any`.
# ---------------------------------------------------------------------------

PollerEvent = (
    RomInfoEvent
    | GameContextEvent
    | LevelEntranceEvent
    | CheckpointEvent
    | DeathEvent
    | SpawnEvent
    | LevelExitEvent
)

MovieEvent = ReplayStartedEvent | ReplayFinishedEvent | ReplayErrorEvent
```

Place these immediately before the `# Commands (SessionManager → backend)` divider near line 113.

- [ ] **Step 3: Pyright on protocol.py**

Run:
```bash
npx pyright python/spinlab/protocol.py
```
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/protocol.py
git commit -m "$(cat <<'EOF'
protocol: add PollerEvent and MovieEvent union aliases

Names something concrete for the poller and movie-controller callbacks
to point at. Tasks 4-5 will swap `Callable[[Any], None]` /
`Callable[[object], None]` for `Callable[[PollerEvent], None]` /
`Callable[[MovieEvent], None]`.

Step 2 of CF1'.
EOF
)"
```

---

### Task 4: Type the Poller's callback surface

**Files:**
- Modify: `python/spinlab/retroarch/poller.py:38,39,69,79`

`PollerDeps.on_event` and `state_path_for` are the public callback shapes; `_stamp_state_path` and `_stamp_conditions` are the internal helpers. All four take `Any` today.

- [ ] **Step 1: Read the current `PollerDeps` definition for context**

```bash
sed -n '34,46p' python/spinlab/retroarch/poller.py
```

- [ ] **Step 2: Edit the import block**

At the top of `python/spinlab/retroarch/poller.py`, the existing import:

```python
from spinlab.condition_registry import ConditionRegistry
from spinlab.retroarch.cold_fill_detector import ColdFillSpawnDetector
from spinlab.retroarch.detector import TransitionDetector
```

needs an extra line for the union type:

```python
from spinlab.condition_registry import ConditionRegistry
from spinlab.protocol import PollerEvent
from spinlab.retroarch.cold_fill_detector import ColdFillSpawnDetector
from spinlab.retroarch.detector import TransitionDetector
```

- [ ] **Step 3: Edit `PollerDeps`**

Replace the existing `PollerDeps` dataclass with:

```python
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
```

- [ ] **Step 4: Tighten `_stamp_state_path` and `_stamp_conditions` signatures**

Replace:

```python
    def _stamp_state_path(self, ev: Any) -> Any:
```

with:

```python
    def _stamp_state_path(self, ev: PollerEvent) -> PollerEvent:
```

and:

```python
    def _stamp_conditions(self, ev: Any) -> Any:
```

with:

```python
    def _stamp_conditions(self, ev: PollerEvent) -> PollerEvent:
```

Note: `dataclasses.replace(ev, conditions=values)` returns the same dataclass type, so the return type lines up. Pyright handles `dataclasses.replace` correctly for the union members.

- [ ] **Step 5: Remove the now-unused `Any` import if applicable**

```bash
grep -n "from typing" python/spinlab/retroarch/poller.py
```

If `Any` is the only thing imported from `typing` and no longer used, remove the line (or the `Any` from the import list). Use:

```bash
npx pyright python/spinlab/retroarch/poller.py 2>&1 | grep "is not accessed"
```

to confirm before removing.

- [ ] **Step 6: Pyright on poller.py**

Run:
```bash
npx pyright python/spinlab/retroarch/poller.py
```
Expected: clean. If Pyright complains that `dataclasses.replace(ev, ...)` returns a wider type than `PollerEvent`, that's a known Pyright wrinkle — it should still typecheck because every member of the union is a frozen dataclass with the same `state_path` / `conditions` field. If it does complain, cast at the return: `return cast(PollerEvent, dataclasses.replace(ev, ...))` — but try without the cast first.

- [ ] **Step 7: Pyright on call sites**

Run:
```bash
npx pyright python/spinlab/retroarch/wiring.py python/spinlab/retroarch/orchestrator.py 2>&1 | tail -20
```
Expected: clean. `wiring.py:99` (`on_event=lambda ev: None`) should still type-check because `lambda ev: None` accepts any input. `wiring.py:100` (`state_path_for=state_paths.resolve_event`) requires `StatePathResolver.resolve_event` to accept a `PollerEvent`. If Pyright complains there, that resolver currently takes `Any`; tighten it in this same task (it's one signature).

- [ ] **Step 8: Run poller tests**

```bash
python -m pytest tests/unit/retroarch/test_poller.py tests/unit/retroarch/test_build_orchestrator.py -q
```
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add python/spinlab/retroarch/poller.py
# Add any other files modified for call-site fixes:
# git add python/spinlab/retroarch/<file>.py
git commit -m "$(cat <<'EOF'
poller: type the event callback surface with PollerEvent

PollerDeps.on_event, .state_path_for, and the _stamp_* helpers all took
`Any`. Use the new `PollerEvent` union from protocol.py so call sites
get concrete typing instead of defaulting to Any.

Step 3 of CF1'.
EOF
)"
```

---

### Task 5: Type the MovieController callback

**Files:**
- Modify: `python/spinlab/retroarch/movies.py:51,61`

- [ ] **Step 1: Edit the import**

Add `MovieEvent` to the existing `protocol` import in `movies.py`:

```bash
grep -n "from spinlab.protocol" python/spinlab/retroarch/movies.py
```

Add `MovieEvent` to the existing import list (or add a new `from spinlab.protocol import MovieEvent` line if there isn't already a protocol import).

- [ ] **Step 2: Change the constructor and rebinder signatures**

Replace:

```python
    def __init__(
        self,
        movie_io: RAMovieIO,
        raclient: RAClient,
        enable: bool,
        on_event: Callable[[object], None],
    ) -> None:
```

with:

```python
    def __init__(
        self,
        movie_io: RAMovieIO,
        raclient: RAClient,
        enable: bool,
        on_event: Callable[[MovieEvent], None],
    ) -> None:
```

and replace:

```python
    def set_event_callback(self, on_event: Callable[[object], None]) -> None:
```

with:

```python
    def set_event_callback(self, on_event: Callable[[MovieEvent], None]) -> None:
```

- [ ] **Step 3: Verify the existing emit sites still typecheck**

The body of `MovieController` emits `ReplayStartedEvent`, `ReplayFinishedEvent`, and `ReplayErrorEvent` — each is a member of `MovieEvent`, so `self._on_event(some_event)` should still work without changes.

Run:
```bash
npx pyright python/spinlab/retroarch/movies.py
```
Expected: clean.

- [ ] **Step 4: Check the binder at wiring**

```bash
npx pyright python/spinlab/retroarch/wiring.py python/spinlab/retroarch/orchestrator.py 2>&1 | tail -10
```
Expected: clean. `wiring.py:110` (`on_event=lambda ev: None`) and the `set_event_callback` rebind at `orchestrator.__init__` should both type-check because `orch.events.put_nowait` accepts any object.

- [ ] **Step 5: Movies tests**

```bash
python -m pytest tests/unit/retroarch/test_movies.py tests/unit/retroarch/test_movie_io.py -q
```
Expected: all pass. If `tests/unit/retroarch/test_movies.py` doesn't exist, check what movie-controller tests are present with `ls tests/unit/retroarch/ | grep -i movie`.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/retroarch/movies.py
git commit -m "$(cat <<'EOF'
movies: type MovieController callback with MovieEvent

Replaces Callable[[object], None] with Callable[[MovieEvent], None] where
MovieEvent = ReplayStarted | ReplayFinished | ReplayError. Concrete
signature documents what the callback actually receives.

Step 4 of CF1'.
EOF
)"
```

---

### Task 6: Type `PendingStart.raw_conditions`

**Files:**
- Modify: `python/spinlab/capture/recorder.py:36`

- [ ] **Step 1: Edit the dataclass**

Replace:

```python
@dataclass
class PendingStart:
    """Buffered start-of-segment state for pairing with the next endpoint."""
    type: EndpointType     # ENTRANCE or CHECKPOINT
    ordinal: int
    state_path: str | None
    timestamp_ms: int
    level_num: int
    raw_conditions: dict
```

with:

```python
@dataclass
class PendingStart:
    """Buffered start-of-segment state for pairing with the next endpoint."""
    type: EndpointType     # ENTRANCE or CHECKPOINT
    ordinal: int
    state_path: str | None
    timestamp_ms: int
    level_num: int
    raw_conditions: dict[str, int]
```

- [ ] **Step 2: Verify the callers**

`SegmentRecorder.handle_entrance` constructs `PendingStart(... raw_conditions=event.conditions)` — `event.conditions` is now `dict[str, int]` after Task 2, so this lines up.

`SegmentRecorder._close_segment` passes `start.raw_conditions` to `condition_registry.decode(...)` which already declares `raw: dict[str, int]` (see condition_registry.py:159). Lines up.

- [ ] **Step 3: Pyright on recorder.py**

```bash
npx pyright python/spinlab/capture/recorder.py
```
Expected: clean.

- [ ] **Step 4: Recorder tests**

```bash
python -m pytest tests/unit/capture/test_recorder.py tests/unit/capture/test_capture_with_conditions.py tests/unit/capture/test_multi_session.py -q
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/capture/recorder.py
git commit -m "$(cat <<'EOF'
recorder: type PendingStart.raw_conditions as dict[str, int]

Matches the upstream event.conditions and the registry.decode parameter
both being dict[str, int]. Closes the last untyped condition site.

Step 5 of CF1'.
EOF
)"
```

---

### Task 7: Narrow `condition_registry.decode` return type

**Files:**
- Modify: `python/spinlab/condition_registry.py:159-180`

The function decodes enum codes to strings and bool codes to bools, so the return is `dict[str, str | bool]`, not `dict[str, Any]`.

- [ ] **Step 1: Edit `decode`**

Replace:

```python
    def decode(self, raw: dict[str, int], level: int) -> dict[str, Any]:
        """Decode raw memory values into logical conditions, filtering to in-scope."""
        result: dict[str, Any] = {}
        for d in self.in_scope(level):
            if d.name not in raw:
                continue
            v = raw[d.name]
            if d.type == "enum":
                if d.values is None:
                    raise ValueError(
                        f"enum condition '{d.name}' requires a 'values' map but got None"
                    )
                if v not in d.values:
                    raise ValueError(
                        f"unknown value {v} for enum condition '{d.name}'; known: {sorted(d.values.keys())}"
                    )
                result[d.name] = d.values[v]
            elif d.type == "bool":
                result[d.name] = bool(v)
            else:
                raise ValueError(f"unknown condition type: {d.type}")
        return result
```

with:

```python
    def decode(self, raw: dict[str, int], level: int) -> dict[str, str | bool]:
        """Decode raw memory values into logical conditions, filtering to in-scope.

        Returns enum conditions as their string label (from ``ConditionDef.values``)
        and bool conditions as Python bools. Unknown ``type`` values raise.
        """
        result: dict[str, str | bool] = {}
        for d in self.in_scope(level):
            if d.name not in raw:
                continue
            v = raw[d.name]
            if d.type == "enum":
                if d.values is None:
                    raise ValueError(
                        f"enum condition '{d.name}' requires a 'values' map but got None"
                    )
                if v not in d.values:
                    raise ValueError(
                        f"unknown value {v} for enum condition '{d.name}'; known: {sorted(d.values.keys())}"
                    )
                result[d.name] = d.values[v]
            elif d.type == "bool":
                result[d.name] = bool(v)
            else:
                raise ValueError(f"unknown condition type: {d.type}")
        return result
```

- [ ] **Step 2: Remove now-unused `Any` import if applicable**

```bash
grep -n "from typing import" python/spinlab/condition_registry.py
grep -n "\bAny\b" python/spinlab/condition_registry.py
```

If `Any` is no longer used elsewhere in the file, remove it from the imports.

- [ ] **Step 3: Check downstream consumers of `decode`**

```bash
grep -rn "\.decode(" python/spinlab/ | grep -i condition
```

Most likely callers: `SegmentRecorder._close_segment`, `Waypoint.make` (which takes the decoded conditions to canonicalize them), and possibly state_builder. If a caller stores the result into a variable typed `dict[str, Any]`, that's still valid (covariant assignment); if a caller stores it into `dict[str, int]`, Pyright will error and that caller is wrong.

- [ ] **Step 4: Pyright**

```bash
npx pyright python/spinlab/condition_registry.py python/spinlab/capture/recorder.py python/spinlab/models.py python/spinlab/state_builder.py 2>&1 | tail -20
```
Expected: clean.

- [ ] **Step 5: Condition registry tests + recorder tests**

```bash
python -m pytest tests/unit/test_condition_registry.py tests/unit/capture/test_recorder.py tests/unit/capture/test_capture_with_conditions.py -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/condition_registry.py
git commit -m "$(cat <<'EOF'
condition_registry: type decode() return as dict[str, str | bool]

Enum conditions decode to their string label; bool conditions to Python
bools. The previous dict[str, Any] return overstated what decode actually
produces; narrowing it lets call sites (Waypoint.make canonicalization,
SegmentRecorder._close_segment) stop carrying Any forward.

Step 6 of CF1'.
EOF
)"
```

---

### Task 8: Investigate db-layer `# type: ignore` removal — STRETCH

**Files:**
- Investigate: `python/spinlab/db/model_state.py:69,80,95`
- Investigate: `python/spinlab/db/attempts.py:96`

The scan claimed these `# type: ignore` comments would lift after Tasks 2-7. On re-reading they're about `dict(zip(cols, row))` patterns that produce `dict[Hashable, Any]` and don't satisfy a TypedDict return type — that's an SQLite-row typing problem, not a conditions problem. So they likely **won't** lift just from CF1'. Confirm and document.

- [ ] **Step 1: Read each `# type: ignore` site to confirm the cause**

```bash
sed -n '60,100p' python/spinlab/db/model_state.py
sed -n '85,100p' python/spinlab/db/attempts.py
```

- [ ] **Step 2: Try removing each ignore one at a time and re-run Pyright**

For each site, temporarily delete `# type: ignore[...]` and run:

```bash
npx pyright python/spinlab/db/model_state.py 2>&1 | tail -10
```

If Pyright is now silent on that line, the ignore was load-bearing for an upstream problem CF1' just fixed — keep it removed. If Pyright still errors with the same message about `dict(zip(...))` not matching the TypedDict, restore the ignore and note it in this task's commit message.

- [ ] **Step 3: Document findings**

If any ignores lifted, commit the removals:

```bash
git add python/spinlab/db/model_state.py python/spinlab/db/attempts.py
git commit -m "$(cat <<'EOF'
db: drop now-redundant # type: ignore comments

The CF1' typing pass made these unnecessary — Pyright now accepts the
return types without suppression.
EOF
)"
```

If none lifted, write a single follow-up note to memory or to the scan file noting the misdiagnosis — these are unrelated SQLite-row TypedDict issues and would need a separate `Row` adapter to fix properly. **Do not commit a no-op change.**

---

### Task 9: Final verification — full pytest + Pyright across the tree

**Files:** none (gate task)

- [ ] **Step 1: Full pytest**

```bash
python -m pytest -q
```
Expected: pass counts equal to (or better than) Task 1's baseline. If anything regressed, bisect the offending task and fix in a new commit on the same branch — do not amend prior commits.

- [ ] **Step 2: Full Pyright sweep**

```bash
npx pyright python/ 2>&1 | tail -5
```
Expected: error/warning count equal to (or fewer than) Task 1's baseline. The same rule applies — no new errors. If any task introduced one and Task 8 didn't catch it, fix in a new commit.

- [ ] **Step 3: Confirm the branch graph is clean**

```bash
git log --oneline improve/type-event-boundary ^main
```
Expected: roughly Tasks 2-7 (and optionally Task 8) as individual commits on top of the `/improve` scan commits.

- [ ] **Step 4: Hand off to merge**

Stop here. Do **not** merge to main or open a PR — that's the user's call. Output a one-line summary: "CF1' implementation complete; N commits on `improve/type-event-boundary` ready for review/merge."

---

## Self-Review

**Spec coverage:**
- ✅ Task 2 covers protocol.py:34,43,49,55,69 (conditions typing).
- ✅ Task 4 covers poller.py:38 (`on_event: Callable[[Any], None]`).
- ✅ Task 5 covers movies.py:51 (`on_event: Callable[[object], None]`).
- ✅ Task 6 covers recorder.py:36 (PendingStart.raw_conditions).
- ✅ Task 7 covers condition_registry.py:159 (decode return).
- ✅ Task 8 covers the db-layer `# type: ignore` claim — but reframes it as an investigation because the claim looks wrong on re-reading.
- ✅ Task 1 and Task 9 cover the "Pyright + full pytest gate" constraint from CLAUDE.md.

**Placeholder scan:** none of "TBD", "implement later", "appropriate error handling", "similar to Task N" present. Each code-change step shows the actual code.

**Type consistency:** `PollerEvent` and `MovieEvent` are defined once in Task 3 and referenced verbatim in Tasks 4 and 5. `dict[str, int]` is consistent across Tasks 2 (events), 6 (PendingStart), and matches the existing `read_all` and `decode` signatures.

**Cross-task dependencies:**
- Task 3 must precede Tasks 4 and 5 (defines the unions).
- Task 2 should precede Task 6 (PendingStart consumes event.conditions; if Task 6 runs first, Pyright will flag the assignment).
- Task 7 is independent but should land before Task 8's investigation, since Task 8 expects the upstream chain to be fully typed.
- Tasks 1 and 9 bookend.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-14-type-event-boundary.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?

# Event Pipeline Collapse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the two parallel event vocabularies (internal `spinlab.retroarch.events` + wire-shape `spinlab.protocol`) into a single rich shape used by both backends. Delete `event_adapter.py` and the lossy field-dropping translation it performs.

**Architecture:** The wire-shape dataclasses in `spinlab.protocol` become the single canonical event vocabulary. They grow the rich fields the RA detector currently has (`room`, `frame`, `elapsed_ms`, `segment_id`, `cp_type`, `level_num`). The RA poller and detectors construct protocol classes directly. The RA orchestrator converts typed event → dict via `dataclasses.asdict()` — no manual field-drop. State-path sentinel becomes `None` (was `""`). Mesen's parse-from-JSON path is unaffected since `parse_event` already filters unknown fields.

**Tech Stack:** Python 3.11+, dataclasses, pytest. No new deps.

---

## File Structure

**Modified:**
- `python/spinlab/protocol.py` — extend event classes with the rich fields. State-path fields become `str | None = None` (already nullable on most; only normalize the few outliers).
- `python/spinlab/retroarch/detector.py` — emit `protocol.*Event` instead of `retroarch.events.*`.
- `python/spinlab/retroarch/cold_fill.py` — emit `protocol.SpawnEvent`.
- `python/spinlab/retroarch/poller.py` — `PollerDeps.on_event` and `PollerDeps.state_path_for` retyped to use `protocol` events. `_stamp_state_path` and `_stamp_conditions` operate on protocol events. The `read_snapshot` / detector wiring stays the same.
- `python/spinlab/retroarch/orchestrator.py` — `on_poller_event` consumes `protocol.*Event`, converts via `dataclasses.asdict`, drops the import of `event_adapter`. `to_rom_info_dict` inlined as a private helper.
- `python/spinlab/retroarch/state_io.py` — `segment_id_for_event` and `resolve_event_path` accept `protocol` events.
- All tests under `tests/unit/retroarch/` that import from `spinlab.retroarch.events` switch to `spinlab.protocol`.

**Deleted:**
- `python/spinlab/retroarch/events.py`
- `python/spinlab/retroarch/event_adapter.py`
- `tests/unit/retroarch/test_events.py` (covered by protocol tests)
- `tests/unit/retroarch/test_event_adapter.py` (no adapter to test)

**Note on `parse_event`:** Mesen continues to feed JSON dicts. `parse_event` keeps its `valid_fields` filter so older Lua emits without the new fields still work — those fields just take their dataclass defaults.

---

## Task 1: Extend `protocol.py` event classes with rich fields

**Files:**
- Modify: `python/spinlab/protocol.py`
- Test: `tests/unit/test_protocol.py` (new, if not present — otherwise extend)

The protocol events need to carry every field the internal detector currently emits, so that downstream consumers (recorder, capture controllers) can use them. Field-by-field reconciliation:

| Class                  | Internal RA fields                                                            | Add to protocol class                          |
|------------------------|-------------------------------------------------------------------------------|------------------------------------------------|
| `LevelEntranceEvent`   | `level, room, frame, state_path, timestamp_ms, conditions`                    | `room: int = 0`, `frame: int = 0`              |
| `LevelExitEvent`       | `level, room, goal, elapsed_ms, frame, timestamp_ms, conditions`              | `room: int = 0`, `elapsed_ms: int = 0`, `frame: int = 0` |
| `CheckpointEvent`      | `level_num, cp_type, cp_ordinal, state_path, timestamp_ms, conditions`        | `cp_type: str = ""`                            |
| `SpawnEvent`           | `level_num, is_cold_cp, cp_ordinal, state_captured, state_path, segment_id, timestamp_ms, conditions` | `segment_id: str = ""`     |
| `DeathEvent`           | `level_num, timestamp_ms, conditions`                                         | `level_num: int = 0`, `timestamp_ms: int = 0`, `conditions: dict = field(default_factory=dict)` |

`state_path` already typed `str | None` on the protocol classes — no change needed.

- [ ] **Step 1: Write failing tests for new fields**

```python
# tests/unit/test_protocol.py — append to existing file
from spinlab.protocol import (
    CheckpointEvent,
    DeathEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    SpawnEvent,
    parse_event,
)


def test_level_entrance_event_carries_room_and_frame():
    ev = LevelEntranceEvent(level=5, room=2, frame=120)
    assert ev.room == 2
    assert ev.frame == 120


def test_level_exit_event_carries_room_elapsed_frame():
    ev = LevelExitEvent(level=5, room=1, goal="goal", elapsed_ms=12345, frame=600)
    assert ev.room == 1
    assert ev.elapsed_ms == 12345
    assert ev.frame == 600


def test_checkpoint_event_carries_cp_type():
    ev = CheckpointEvent(level_num=5, cp_ordinal=1, cp_type="midway")
    assert ev.cp_type == "midway"


def test_spawn_event_carries_segment_id():
    ev = SpawnEvent(level_num=5, segment_id="seg_abc", is_cold_cp=True)
    assert ev.segment_id == "seg_abc"


def test_death_event_carries_level_num_and_timestamp():
    ev = DeathEvent(level_num=7, timestamp_ms=999)
    assert ev.level_num == 7
    assert ev.timestamp_ms == 999


def test_parse_event_tolerates_unknown_extras():
    """Forward-compat: a Lua message with an unknown field still parses."""
    raw = {"event": "death", "level_num": 5, "spurious_field": 999}
    ev = parse_event(raw)
    assert isinstance(ev, DeathEvent)
    assert ev.level_num == 5


def test_parse_event_populates_new_fields():
    raw = {
        "event": "level_exit", "level": 5, "room": 1,
        "goal": "goal", "elapsed_ms": 12345, "frame": 600,
        "timestamp_ms": 1000,
    }
    ev = parse_event(raw)
    assert isinstance(ev, LevelExitEvent)
    assert ev.room == 1
    assert ev.elapsed_ms == 12345
    assert ev.frame == 600
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/unit/test_protocol.py -v`
Expected: FAIL on the new field accesses (AttributeError or TypeError).

- [ ] **Step 3: Add fields to protocol classes**

Edit `python/spinlab/protocol.py`:

```python
@dataclass
class LevelEntranceEvent:
    event: str = "level_entrance"
    level: int = 0
    room: int = 0
    frame: int = 0
    state_path: str | None = None
    timestamp_ms: int = 0
    conditions: dict = field(default_factory=dict)


@dataclass
class CheckpointEvent:
    event: str = "checkpoint"
    level_num: int = 0
    cp_ordinal: int = 1
    cp_type: str = ""
    state_path: str | None = None
    timestamp_ms: int = 0
    conditions: dict = field(default_factory=dict)


@dataclass
class DeathEvent:
    event: str = "death"
    level_num: int = 0
    timestamp_ms: int = 0
    conditions: dict = field(default_factory=dict)


@dataclass
class SpawnEvent:
    event: str = "spawn"
    level_num: int = 0
    state_captured: bool = False
    state_path: str | None = None
    conditions: dict = field(default_factory=dict)
    is_cold_cp: bool = False
    cp_ordinal: int | None = None
    segment_id: str = ""
    timestamp_ms: int = 0


@dataclass
class LevelExitEvent:
    event: str = "level_exit"
    level: int = 0
    room: int = 0
    goal: str = "abort"
    elapsed_ms: int = 0
    frame: int = 0
    timestamp_ms: int = 0
    conditions: dict = field(default_factory=dict)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/unit/test_protocol.py -v`
Expected: PASS.

- [ ] **Step 5: Run full suite to check for regressions**

Run: `python -m pytest --tb=short -q`
Expected: 898 passed, 3 skipped (or new test count after step 1).

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/protocol.py tests/unit/test_protocol.py
git commit -m "feat(protocol): extend event classes with rich detector fields"
```

---

## Task 2: Re-point the detector at protocol events

**Files:**
- Modify: `python/spinlab/retroarch/detector.py`
- Modify: `python/spinlab/retroarch/transition_state.py` (no changes; just verify imports)
- Test: `tests/unit/retroarch/test_detector.py`

The detector is the primary producer. Switch its imports and constructor calls from `retroarch.events` to `spinlab.protocol`. The `TransitionEvent` base class in `retroarch.events` is gone after Task 5, so we use `Union[LevelEntranceEvent, ...]` as the return-element type, or simply `object` for the list. Pick `object` for now to avoid a long Union; the orchestrator's `on_poller_event` already does isinstance dispatch.

- [ ] **Step 1: Write a failing test that asserts protocol-class output**

Edit `tests/unit/retroarch/test_detector.py` — add a test (or modify an existing one) so it imports from `spinlab.protocol`:

```python
from spinlab.protocol import (
    CheckpointEvent,
    DeathEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    SpawnEvent,
)
```

And asserts `isinstance(ev, LevelEntranceEvent)` etc. against the existing test fixtures. The existing tests use `retroarch.events.*` — flip those imports.

- [ ] **Step 2: Run failing test**

Run: `pytest tests/unit/retroarch/test_detector.py -v`
Expected: FAIL — detector still emits `retroarch.events.*`, isinstance check against protocol class fails.

- [ ] **Step 3: Update detector to construct protocol events**

Edit `python/spinlab/retroarch/detector.py`:

```python
"""TransitionDetector — stateful, pure-logic event emitter."""
from __future__ import annotations

from spinlab.protocol import (
    CheckpointEvent,
    DeathEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    SpawnEvent,
)
from spinlab.retroarch.predicates import (
    LEVEL_START_ACTIVE,
    PLAYER_ANIM_DEAD,
    check_checkpoint_hit,
    goal_type,
    is_death_frame,
    is_exit_frame,
)
from spinlab.retroarch.snapshot import MemorySnapshot
from spinlab.retroarch.transition_state import TransitionState

FPS = 60.0  # SMW NTSC; close enough for elapsed-ms math

# Events the detector emits. Listed for downstream type narrowing.
_EmittedEvent = (
    LevelEntranceEvent | DeathEvent | CheckpointEvent | LevelExitEvent | SpawnEvent
)


class TransitionDetector:
    """Per-frame transition emitter. Stateful but pure (no IO)."""

    # ... __init__/reset/resync_after_state_load unchanged ...

    def step(self, curr: MemorySnapshot, timestamp_ms: int) -> list[_EmittedEvent]:
        self._frame_counter += 1
        events: list[_EmittedEvent] = []
        prev = self._prev
        if prev is None:
            self._prev = curr
            return events

        if is_death_frame(prev, curr) and not self._state.died_flag:
            events.append(DeathEvent(timestamp_ms=timestamp_ms, level_num=curr.level_num))
            self._state.died_flag = True

        cp_type = check_checkpoint_hit(prev, curr, self._state)
        if cp_type is not None:
            self._state.cp_ordinal += 1
            self._cp_acquired = True
            self._state.first_cp_entrance = 0
            events.append(CheckpointEvent(
                timestamp_ms=timestamp_ms,
                level_num=curr.level_num,
                cp_type=cp_type,
                cp_ordinal=self._state.cp_ordinal,
            ))

        self._exit_this_frame = is_exit_frame(prev, curr)
        if self._exit_this_frame:
            elapsed = int((self._frame_counter - self._level_start_frame) / FPS * 1000)
            events.append(LevelExitEvent(
                timestamp_ms=timestamp_ms,
                level=curr.level_num,
                room=curr.room_num,
                goal=goal_type(curr),
                elapsed_ms=elapsed,
                frame=self._frame_counter,
            ))

        edge_spawn = curr.level_start == LEVEL_START_ACTIVE and prev.level_start == 0
        fast_retry = (
            self._state.died_flag
            and curr.level_start == LEVEL_START_ACTIVE
            and curr.player_anim != PLAYER_ANIM_DEAD
            and prev.player_anim == PLAYER_ANIM_DEAD
        )
        if (edge_spawn or fast_retry) and not self._exit_this_frame:
            if self._state.died_flag:
                was_cp = self._cp_acquired
                if was_cp:
                    self._cp_acquired = False
                events.append(SpawnEvent(
                    timestamp_ms=timestamp_ms,
                    level_num=curr.level_num,
                    is_cold_cp=was_cp,
                    cp_ordinal=self._state.cp_ordinal,
                    state_captured=was_cp,
                ))
                self._state.died_flag = False
            else:
                self._state.cp_ordinal = 0
                self._cp_acquired = False
                self._state.first_cp_entrance = curr.cp_entrance
                self._level_start_frame = self._frame_counter
                events.append(LevelEntranceEvent(
                    timestamp_ms=timestamp_ms,
                    level=curr.level_num,
                    room=curr.room_num,
                    frame=self._frame_counter,
                ))

        self._prev = curr
        return events
```

- [ ] **Step 4: Verify detector tests pass**

Run: `pytest tests/unit/retroarch/test_detector.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/detector.py tests/unit/retroarch/test_detector.py
git commit -m "refactor(retroarch): detector emits protocol events directly"
```

---

## Task 3: Re-point ColdFillSpawnDetector at protocol events

**Files:**
- Modify: `python/spinlab/retroarch/cold_fill.py`
- Test: `tests/unit/retroarch/test_cold_fill.py`

- [ ] **Step 1: Update test imports + isinstance assertions**

In `tests/unit/retroarch/test_cold_fill.py`:

```python
from spinlab.condition_registry import ConditionRegistry  # already done
from spinlab.protocol import SpawnEvent
from spinlab.retroarch.cold_fill import ColdFillSpawnDetector
from spinlab.retroarch.snapshot import MemorySnapshot
```

Replace `Spawn` references with `SpawnEvent`.

- [ ] **Step 2: Run failing tests**

Run: `pytest tests/unit/retroarch/test_cold_fill.py -v`
Expected: FAIL on import / isinstance.

- [ ] **Step 3: Update cold_fill.py imports**

Edit `python/spinlab/retroarch/cold_fill.py`:

```python
from spinlab.protocol import SpawnEvent
from spinlab.retroarch import addresses as a
from spinlab.retroarch.predicates import (
    FANFARE_ACTIVE,
    LEVEL_START_ACTIVE,
    PLAYER_ANIM_DEAD,
)
from spinlab.retroarch.snapshot import MemorySnapshot


class ColdFillSpawnDetector:
    # ... unchanged init/state ...

    def step(self, curr: MemorySnapshot, timestamp_ms: int) -> SpawnEvent | None:
        # ... unchanged logic, but the emitted Spawn(...) becomes SpawnEvent(...) ...
        if edge_spawn or fast_retry or playable:
            emitted = SpawnEvent(
                timestamp_ms=timestamp_ms,
                level_num=curr.level_num,
                is_cold_cp=True,
                cp_ordinal=0,
                state_captured=True,
                segment_id=self._segment_id or "",
            )
            ...
```

- [ ] **Step 4: Run tests to verify**

Run: `pytest tests/unit/retroarch/test_cold_fill.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/cold_fill.py tests/unit/retroarch/test_cold_fill.py
git commit -m "refactor(retroarch): cold-fill detector emits protocol SpawnEvent"
```

---

## Task 4: Update Poller, StateIO, and tests to consume protocol events

**Files:**
- Modify: `python/spinlab/retroarch/poller.py`
- Modify: `python/spinlab/retroarch/state_io.py`
- Test: `tests/unit/retroarch/test_poller.py`, `test_poller_state_path.py`, `test_poller_conditions.py`, `test_state_io_resolver.py`, `test_state_io_with_poller.py`

`Poller` and `StateIO` reference `TransitionEvent` (the abstract base from `retroarch.events`). With that file gone, retype to use `Any` or a Union. The poller's `_stamp_state_path` calls `dataclasses.replace(ev, state_path=path)`; this works on any frozen dataclass with a `state_path` field — protocol classes are non-frozen but `dataclasses.replace` still works (returns a new instance). Tests using protocol events confirm.

- [ ] **Step 1: Retype `PollerDeps` and `StateIO.segment_id_for_event` / `resolve_event_path`**

Edit `python/spinlab/retroarch/poller.py`:

```python
from typing import Any

# at top, replace:
from spinlab.retroarch.events import TransitionEvent
# with: (no import — use Any below for the typing hooks)


@dataclass
class PollerDeps:
    client: NCIClient
    read_snapshot: Callable[[NCIClient], MemorySnapshot]
    on_event: Callable[[Any], None]
    state_path_for: Callable[[Any], str] | None = None
    conditions_registry: ConditionRegistry | None = None
```

Edit `python/spinlab/retroarch/state_io.py`:

```python
from typing import Any

# replace:
from spinlab.retroarch.events import (
    Checkpoint,
    LevelEntrance,
    Spawn,
    TransitionEvent,
)
# with:
from spinlab.protocol import (
    CheckpointEvent,
    LevelEntranceEvent,
    SpawnEvent,
)


# Update isinstance branches:
def segment_id_for_event(self, event: Any) -> str | None:
    if isinstance(event, LevelEntranceEvent):
        return f"entrance_{event.level}_{event.room}"
    if isinstance(event, CheckpointEvent):
        return f"cp_{event.level_num}_{event.cp_ordinal}_hot"
    if isinstance(event, SpawnEvent) and event.segment_id:
        return event.segment_id
    return None
```

- [ ] **Step 2: Update tests that import from `spinlab.retroarch.events`**

Across `tests/unit/retroarch/test_poller*.py`, `test_state_io_*.py`:

```python
# replace:
from spinlab.retroarch.events import LevelEntrance, ...
# with:
from spinlab.protocol import LevelEntranceEvent, ...
```

Adjust constructor names and isinstance assertions.

- [ ] **Step 3: Run tests to verify**

Run: `pytest tests/unit/retroarch/ -v`
Expected: tests for `events.py` and `event_adapter.py` still failing (they will be deleted in Task 5); rest passing.

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/retroarch/poller.py python/spinlab/retroarch/state_io.py tests/unit/retroarch/
git commit -m "refactor(retroarch): poller and state_io consume protocol events"
```

---

## Task 5: Replace `event_adapter` with `dataclasses.asdict` in orchestrator; delete old modules

**Files:**
- Modify: `python/spinlab/retroarch/orchestrator.py`
- Delete: `python/spinlab/retroarch/events.py`
- Delete: `python/spinlab/retroarch/event_adapter.py`
- Delete: `tests/unit/retroarch/test_events.py`
- Delete: `tests/unit/retroarch/test_event_adapter.py`

The orchestrator currently does:

```python
from spinlab.retroarch.event_adapter import to_protocol_dict, to_rom_info_dict

# in on_poller_event:
d = to_protocol_dict(ev)
self.events.put_nowait(d)
```

Replace with:

```python
import dataclasses
# ...
d = dataclasses.asdict(ev)
self.events.put_nowait(d)
```

`asdict` already produces `{"event": "level_entrance", "level": 5, ...}` because the type-tag field defaults to its tag string. Inline `to_rom_info_dict` as a private helper.

- [ ] **Step 1: Inline `to_rom_info_dict` and replace `to_protocol_dict`**

Edit `python/spinlab/retroarch/orchestrator.py`:

```python
import dataclasses
# remove:
# from spinlab.retroarch.event_adapter import to_protocol_dict, to_rom_info_dict

# add at module scope:
def _rom_info_dict(status) -> dict:
    """Build a `rom_info` JSON dict from NCI's GET_STATUS reply."""
    return {"event": "rom_info", "filename": status.game or ""}


# in connect():
self.events.put_nowait(_rom_info_dict(status))


# in on_poller_event():
try:
    d = dataclasses.asdict(ev)
except TypeError:
    logger.warning(
        "RetroArchOrchestrator: dropping unknown event type %r",
        type(ev).__name__,
    )
    return
self._practice_timing.observe_event(d)
self._speed_run_timing.observe_event(d)
self.events.put_nowait(d)
```

- [ ] **Step 2: Delete the now-dead modules**

```bash
rm python/spinlab/retroarch/events.py
rm python/spinlab/retroarch/event_adapter.py
rm tests/unit/retroarch/test_events.py
rm tests/unit/retroarch/test_event_adapter.py
```

- [ ] **Step 3: Update remaining imports**

`tests/unit/retroarch/test_orchestrator.py`, `test_orchestrator_publishes_events.py`, and any others still importing from `spinlab.retroarch.events`:

```python
# replace:
from spinlab.retroarch.events import Death, LevelExit
# with:
from spinlab.protocol import DeathEvent, LevelExitEvent
```

- [ ] **Step 4: Run full suite**

Run: `python -m pytest --tb=short -q`
Expected: PASS (count drops by ~10 due to deleted tests).

- [ ] **Step 5: Type-check changed files**

Run: `npx pyright python/spinlab/retroarch/orchestrator.py python/spinlab/retroarch/poller.py python/spinlab/retroarch/state_io.py python/spinlab/retroarch/detector.py python/spinlab/retroarch/cold_fill.py python/spinlab/protocol.py`
Expected: 0 errors.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(retroarch): drop event_adapter and internal events module

Use dataclasses.asdict on protocol classes directly; the manual
adapter was lossy (dropped room, elapsed_ms, frame, segment_id)
and is no longer needed now that protocol events carry the rich
detector fields."
```

---

## Task 6: Normalize `state_path` empty-string sentinel to `None`

**Files:**
- Modify: `python/spinlab/retroarch/state_io.py:resolve_event_path`
- Modify: `python/spinlab/retroarch/poller.py:_stamp_state_path` (if it has any "" handling)

Today `resolve_event_path` returns `""` when no path applies. The poller checks truthiness, so `None` works equivalently. Make it explicit.

- [ ] **Step 1: Update `resolve_event_path` to return `str | None`**

```python
def resolve_event_path(self, event) -> str | None:
    seg_id = self.segment_id_for_event(event)
    return str(self.state_path_for(seg_id)) if seg_id else None
```

- [ ] **Step 2: Update the `PollerDeps.state_path_for` annotation**

```python
state_path_for: Callable[[Any], str | None] | None = None
```

And `_stamp_state_path`:

```python
def _stamp_state_path(self, ev):
    if self._deps.state_path_for is None:
        return ev
    path = self._deps.state_path_for(ev)
    if not path:
        return ev
    if not hasattr(ev, "state_path"):
        return ev
    return dataclasses.replace(ev, state_path=path)
```

(Already truthiness-tolerant.)

- [ ] **Step 3: Update tests that asserted on `""`**

In `tests/unit/retroarch/test_state_io_resolver.py` (if it asserts `""`), change to `None`.

- [ ] **Step 4: Run full suite**

Run: `python -m pytest --tb=short -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/state_io.py python/spinlab/retroarch/poller.py tests/unit/retroarch/test_state_io_resolver.py
git commit -m "refactor(retroarch): use None sentinel for unresolved state paths"
```

---

---

## Task 7: Sync the live docs

**Files:**
- Modify: `docs/retroarch-migration/architecture.md`

The architecture doc lists `events.py` and `event_adapter.py` in its file
inventory and references them throughout. Both files are gone after Task 5.

- [ ] **Step 1: Update file inventory + event-flow text**

In the file-tree section, remove the lines for `events.py` and
`event_adapter.py`. Where the prose mentions `to_protocol_dict` /
`TransitionEvent`, replace with the new "protocol classes are the single
event vocabulary; orchestrator converts to dict via
`dataclasses.asdict`" wording.

- [ ] **Step 2: Run full suite one more time**

Run: `python -m pytest --tb=short -q`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add docs/retroarch-migration/architecture.md
git commit -m "docs(retroarch): drop event_adapter from architecture inventory"
```

---

## Self-Review

- **Spec coverage:**
  - Collapse two event vocabularies → Tasks 1, 2, 3, 4, 5.
  - Drop `event_adapter` round-trip → Task 5.
  - State-path `""` → `None` → Task 6.
  - Unlock dropped fields for downstream consumers → Task 1 adds them; consumers in Plan B will use them.
  - parse_event silent drop becomes moot → Task 1's tolerance test confirms it still works.

- **Type consistency:** All new fields and method signatures cross-reference. `protocol` event names are stable (`LevelEntranceEvent` not `LevelEntrance`).

- **Risks:**
  - Changing `state_path: str = ""` → `str | None = None` in already-built protocol classes — verified those fields were already `str | None = None` for most events; only normalize the `None` default everywhere.
  - `dataclasses.asdict` deep-copies nested dicts (`conditions`) — same behavior as the manual adapter's `dict(event.conditions)`.
  - `parse_event`'s `valid_fields` filter handles new fields cleanly: dicts from older Lua emits without the new fields just take dataclass defaults.

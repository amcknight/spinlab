# Architecture Improvements: Structural Boundaries + System-Level Cleanup

**Date:** 2026-04-05
**Scope:** Python backend structural boundaries, TCP protocol typing, API contract formalization

## Motivation

SpinLab has grown organically through feature additions (conditions, cold-fill, fill-gap, replay, invalidation). The individual pieces are well-built, but the boundaries between them have blurred:

- System state is scattered across SessionManager, CaptureController, and PracticeSession with no single source of truth.
- CaptureController owns 4 independent state machines (reference, replay, fill-gap, cold-fill) plus condition registry and draft management.
- The TCP protocol mixes JSON objects with colon-delimited strings and has no schema on either end.
- The Python→Frontend API contract is enforced by convention (untyped dicts that must match TypeScript interfaces).
- SessionManager imports `fastapi.HTTPException` in domain logic.

None of these are bugs. They're structural debt that makes every future feature touch more files and carry more risk.

## Section 1: Unified SystemState

### Problem

Mode lives on SessionManager. Draft state lives on CaptureController.draft. Cold-fill state is 4 fields on CaptureController. Practice session is on SessionManager. StateBuilder reaches deep into all three to assemble snapshots.

### Design

Extract a `SystemState` dataclass that is the single source of truth for "what is the system doing right now":

```python
@dataclass
class CaptureState:
    run_id: str
    rec_path: str | None = None
    segments_count: int = 0

@dataclass
class DraftState:
    run_id: str
    segment_count: int

@dataclass
class ColdFillState:
    current_segment_id: str
    current_num: int
    total: int
    segment_label: str

@dataclass
class FillGapState:
    segment_id: str
    waypoint_id: str

@dataclass
class PracticeState:
    session_id: str
    started_at: str
    current_segment_id: str | None = None
    segments_attempted: int = 0
    segments_completed: int = 0

@dataclass
class SystemState:
    mode: Mode = Mode.IDLE
    game_id: str | None = None
    game_name: str | None = None
    capture: CaptureState | None = None
    draft: DraftState | None = None
    cold_fill: ColdFillState | None = None
    fill_gap: FillGapState | None = None
    practice: PracticeState | None = None
```

SessionManager owns the `SystemState` instance. Controllers read/update their sub-state through it. StateBuilder reads from it instead of reaching into multiple objects.

### What changes

- The backward-compatible properties on SessionManager (lines 79-95) go away.
- `StateBuilder.build()` reads from `SystemState` fields instead of `session.capture.ref_capture.capture_run_id`.
- Mode transitions and sub-state assignment happen in one place.

### File impact

- New: `python/spinlab/system_state.py`
- Modified: `session_manager.py`, `state_builder.py`, `capture_controller.py`

---

## Section 2: CaptureController Decomposition

### Problem

CaptureController manages 4 independent flows (reference capture, replay, fill-gap, cold-fill) plus owns `ConditionRegistry` and `DraftManager`. The cold-fill state machine alone is ~80 lines with its own queue. Fill-gap uses `getattr(self, "_fill_gap_waypoint_id", None)` — a code smell from bolting state onto an object that doesn't naturally own it.

### Design

Split into focused controllers:

- **`CaptureController`** — reference + replay capture only (they share `ReferenceCapture` machinery). Keeps `DraftManager` since drafts are a capture output. Drops from ~318 to ~180 lines.
- **`ColdFillController`** — extracted. Owns queue, current segment, total count. Clear lifecycle: `start()` -> `handle_spawn()` -> done.
- **Fill-gap stays on CaptureController.** At ~40 lines with only two methods it doesn't earn its own file. Once cold-fill is extracted and the class is smaller, fill-gap fits naturally alongside the reference/replay logic it shares state with.
- **`ConditionRegistry`** ownership moves to SessionManager (game-level context, not capture-specific).

### Interaction with SystemState

Each controller updates the corresponding typed sub-state on `SystemState`. Controllers do not set mode directly — they return `ActionResult`, and SessionManager applies the transition. This is already the pattern; decomposition just enforces it.

### File impact

- New: `python/spinlab/cold_fill_controller.py`
- Modified: `capture_controller.py` (shrinks), `session_manager.py` (wires new controller)

---

## Section 3: Typed TCP Protocol

### Problem

The Lua-Python protocol has no schema:

- **Python -> Lua:** Mix of JSON objects (`{"event": "reference_start", "path": "..."}`) and colon-delimited strings (`set_conditions:{...}`, `practice_load:{...}`).
- **Lua -> Python:** Always JSON, but the shape per event is implicit. A typo in an event name silently drops in `route_event()`.
- Adding a new command or event requires touching 4+ files with no single place that defines the contract.

### Design

A message catalog in one Python file — every command and every event as a dataclass:

```python
# python/spinlab/protocol.py

# --- Python -> Lua commands ---

@dataclass
class ReferenceStartCmd:
    event: Literal["reference_start"] = "reference_start"
    path: str = ""

@dataclass
class SetConditionsCmd:
    event: Literal["set_conditions"] = "set_conditions"
    definitions: list[dict] = field(default_factory=list)

# --- Lua -> Python events ---

@dataclass
class SpawnEvent:
    event: Literal["spawn"] = "spawn"
    level_number: int = 0
    state_captured: bool = False
    state_path: str | None = None
    conditions: dict | None = None

@dataclass
class DeathEvent:
    event: Literal["death"] = "death"

# ... every message in the system
```

**Changes to TcpManager:**
- `send()` accepts a protocol dataclass, serializes via `dataclasses.asdict()` + `json.dumps()`. The colon-delimited format dies.
- New `parse_event(raw: dict) -> ProtocolEvent` function deserializes incoming JSON into the matching event dataclass. Unknown events raise instead of silently dropping.

**Changes to SessionManager:**
- `route_event()` receives typed event objects. Handlers take typed parameters (`_handle_spawn(self, event: SpawnEvent)`) instead of raw dicts.
- The dispatch table keys on the dataclass type or its `event` literal.

### Lua side

Lua's outbound messages (events) are already JSON — no change needed there. However, the Lua command dispatcher currently parses incoming commands with string matching for the colon-delimited format. When Python switches to sending pure JSON for all commands, the Lua-side command dispatcher needs a corresponding update to `json.decode()` all incoming messages and dispatch on the `"event"` field. This is a small change (~20 lines in the TCP receive handler in `spinlab.lua`).

### File impact

- New: `python/spinlab/protocol.py`
- Modified: `tcp_manager.py` (typed send), `session_manager.py` (typed dispatch), all handlers that read `event.get("field")`

---

## Section 4: SessionManager Decoupling

### Problem

- `_require_game()` imports and raises `fastapi.HTTPException` — the coordination layer makes HTTP decisions.
- Every action method repeats the same 3-step pattern: delegate, apply mode transition, notify SSE.

### Design

**`_apply_result()` helper absorbs boilerplate:**

```python
async def _apply_result(self, result: ActionResult) -> ActionResult:
    if result.new_mode is not None:
        self.state.mode = result.new_mode
    await self._notify_sse()
    return result
```

Every action becomes: `return await self._apply_result(await self.capture.start_reference(...))`.

**Optional:** `_require_game()` could raise a domain error instead of `HTTPException`, with the route layer translating. Low priority — it's one callsite and the current approach works.

### File impact

- Modified: `session_manager.py`

---

## Dependency Order

Sections should be implemented in this order due to dependencies:

1. **Section 1 (SystemState)** — foundation everything else reads from
2. **Section 2 (Controller decomposition)** — depends on SystemState for sub-state ownership
3. **Section 4 (SessionManager decoupling)** — small, cleans up SessionManager before protocol changes touch it
4. **Section 3 (Typed TCP protocol)** — independent of 1-2 but cleaner after SessionManager is decoupled

## Out of Scope

- Lua file splitting or internal restructuring
- Pydantic response models / TypeScript codegen (revisit when sync bugs become a real problem)
- SSE delta events (follow-up)
- Event bus / pub-sub (not needed; 1:1 dispatch is correct for current architecture)
- Database mixin pattern changes (working fine at current scale)
- FillGapController extraction (stays on CaptureController; reassess if it grows)

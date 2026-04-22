# Typed Event Pipeline

Eliminate the dict round-trip in the TCP event pipeline. Events arrive typed
from `parse_event()` but are immediately converted to raw dicts via
`dataclasses.asdict()` in SessionManager before being passed to downstream
consumers. This loses all type safety — field access becomes string-key
lookups, typos silently return `None`, and refactoring requires grepping
string literals instead of following type references.

This spec covers passing typed protocol events all the way through every
consumer, and replacing the one significant piece of untyped internal state
(`SegmentRecorder.pending_start: dict`) with a typed dataclass.

## Scope

- Remove all 12 `dataclasses.asdict()` calls on event paths in
  `session_manager.py`
- Update every downstream handler to accept the concrete protocol event type
- Replace `SegmentRecorder.pending_start: dict | None` with a
  `PendingStart` dataclass
- Split `SpeedRunSession.receive_event` into three typed methods
- Update tests to construct typed events instead of raw dicts

## Non-goals

- Schema migrations (not needed)
- SSE state rebuild performance (separate concern)
- Async DB layer (separate concern)
- Changing the protocol module's dataclass hierarchy (no base classes needed)

## Design

### SessionManager handler changes

Every `_handle_*` method stops calling `dataclasses.asdict()` and passes the
typed event directly to its delegate. The handler signatures already receive
typed events from the dispatch table — the only change is removing the
conversion before forwarding.

`_handle_spawn` currently creates a local `event_dict` shared by the
cold_fill and capture paths. Both paths accept `SpawnEvent` directly instead.

### Capture pipeline (ReferenceController + SegmentRecorder)

**ReferenceController** methods change from `event: dict` to concrete types:

| Method | Old signature | New event type |
|--------|--------------|----------------|
| `handle_entrance` | `event: dict` | `LevelEntranceEvent` |
| `handle_checkpoint` | `event: dict` | `CheckpointEvent` |
| `handle_death` | `event: dict` | `DeathEvent` |
| `handle_spawn` | `event: dict` | `SpawnEvent` |
| `handle_exit` | `event: dict` | `LevelExitEvent` |
| `handle_rec_saved` | `event: dict` | `RecSavedEvent` |

Internally these forward to SegmentRecorder, which gets the same signature
changes. All `event.get("field")` / `event["field"]` calls become
`event.field` attribute access.

**`PendingStart` dataclass** replaces `pending_start: dict | None` in
SegmentRecorder. Currently a hand-built dict with keys `type`, `ordinal`,
`state_path`, `timestamp_ms`, `level_num`, `raw_conditions`. Becomes:

```python
@dataclass
class PendingStart:
    type: str              # "entrance" or "checkpoint"
    ordinal: int
    state_path: str | None
    timestamp_ms: int
    level_num: int
    raw_conditions: dict
```

Defined in `recorder.py`, private to the module. `_close_segment` and all
other consumers change from `start["type"]` to `start.type`, etc.

### ColdFillController

`handle_spawn(event: dict)` becomes `handle_spawn(event: SpawnEvent)`.
Changes `event.get("state_captured")` to `event.state_captured`,
`event["state_path"]` to `event.state_path`.

### PracticeSession

`receive_result(event: dict)` becomes `receive_result(event: AttemptResultEvent)`.
`_result_data: dict | None` becomes `_result_data: AttemptResultEvent | None`.
`_process_result` changes from `result["segment_id"]` to `result.segment_id`, etc.

The guard `self._result_data.get("event") == "attempt_result"` in `run_one`
becomes a simple `self._result_data is not None` check — the type already
guarantees it's an `AttemptResultEvent`.

### SpeedRunSession

`receive_event(event: dict)` splits into three methods:

```python
def receive_checkpoint(self, event: SpeedRunCheckpointEvent) -> None
def receive_death(self, event: SpeedRunDeathEvent) -> None
def receive_complete(self, event: SpeedRunCompleteEvent) -> None
```

A named type alias in `speed_run.py`:

```python
SpeedRunEvent = SpeedRunCheckpointEvent | SpeedRunDeathEvent | SpeedRunCompleteEvent
```

`_event_queue` type changes from `asyncio.Queue[dict]` to
`asyncio.Queue[SpeedRunEvent]`. The `run_one` loop switches from
`event.get("event")` string matching to `isinstance` checks.

SessionManager's three `_handle_speed_run_*` methods each call the
corresponding `receive_*` method directly.

### Test updates

Tests that call handler methods directly currently construct raw dicts:

```python
recorder.handle_entrance({"level": 1, "state_path": "/tmp/foo.mss", ...})
```

These change to construct typed events:

```python
recorder.handle_entrance(LevelEntranceEvent(level=1, state_path="/tmp/foo.mss", ...))
```

Protocol dataclasses have defaults on every field, so tests only specify the
fields they care about. This is a quality improvement — tests now document
which fields matter for each scenario, and typos in field names become
immediate errors instead of silent `None` values.

## Files touched

- `python/spinlab/session_manager.py` — remove `asdict()` calls, pass typed events
- `python/spinlab/capture/reference.py` — typed method signatures
- `python/spinlab/capture/recorder.py` — typed method signatures, add `PendingStart`
- `python/spinlab/capture/cold_fill.py` — typed `handle_spawn`
- `python/spinlab/practice.py` — typed `receive_result`, typed `_result_data`
- `python/spinlab/speed_run.py` — split into 3 methods, add `SpeedRunEvent`, typed queue
- Test files that construct events for these methods

## Dead code surfaced

`ReferenceController.handle_death` currently reads `timestamp_ms` from the
event dict, but `DeathEvent` has no such field — the extraction always
returns `None`. The typed signature makes this visible as dead code. The fix
is to remove the extraction and pass `timestamp_ms=None` directly (or remove
the parameter from `recorder.handle_death` if it serves no purpose). If Lua
ever adds `timestamp_ms` to death events, it should be added to `DeathEvent`
at that point.

## Behavioral changes

None. Every event already arrives typed from `parse_event()` and the same
fields are accessed. This removes the dict round-trip without changing
semantics.

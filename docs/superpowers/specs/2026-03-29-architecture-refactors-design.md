# Spec: Architecture Refactors

## Overview

Structural improvements to reduce coupling, improve clarity, and add missing safety nets. These build on top of the fail-loud hardening (which should land first).

## Refactor 1: Extract SegmentLoader from Scheduler

**Problem:** `Scheduler._load_segments_with_model()` (scheduler.py ~lines 70-135) does segment loading, model state deserialization, gold computation, and variant path checking. This is ~65 lines of data hydration mixed into the scheduling logic.

**Fix:** Extract a `SegmentLoader` class that owns the query and hydration.

```
python/spinlab/segment_loader.py
```

**Responsibilities:**
- `load_practicable(game_id) -> list[SegmentWithModel]` — loads segments with model outputs, filters to those with valid state_path on disk
- `load_all(game_id) -> list[SegmentWithModel]` — same without filtering (for dashboard display)
- Owns deserialization of model states and gold computation

**Scheduler changes:** Replace `self._load_segments_with_model()` with `self.loader.load_practicable()`. Scheduler becomes: loader → allocator → done.

**SessionManager changes:** `_build_practice_state()` can use the loader directly instead of duplicating hydration logic.

---

## Refactor 2: Standardize error signaling — status dicts out, exceptions in

**Problem:** Some methods return `{"status": "not_connected"}`, others raise `HTTPException`. `_check_result()` in dashboard.py maps status strings to HTTP codes. This is two error systems.

**Fix:** Internal methods (SessionManager, CaptureController) raise domain exceptions. Dashboard layer catches and maps to HTTP.

**New exceptions** (in `models.py` or a new `errors.py`):

```python
class SpinLabError(Exception):
    """Base for all domain errors."""

class NotConnectedError(SpinLabError):
    """TCP connection to Lua not available."""

class NoGameLoadedError(SpinLabError):
    """Operation requires a game to be loaded."""

class ModeConflictError(SpinLabError):
    """Operation not valid in current mode."""

class DraftPendingError(SpinLabError):
    """A draft must be saved or discarded first."""

class NotFoundError(SpinLabError):
    """Requested resource doesn't exist."""
```

**Dashboard mapping:** Replace `_check_result()` with an exception handler:

```python
@app.exception_handler(SpinLabError)
async def handle_domain_error(request, exc):
    code = {
        NotConnectedError: 503,
        NoGameLoadedError: 409,
        ModeConflictError: 409,
        DraftPendingError: 409,
        NotFoundError: 404,
    }.get(type(exc), 500)
    raise HTTPException(status_code=code, detail=str(exc))
```

**Migration:** Each method that currently returns `{"status": "error_name"}` gets changed to `raise ErrorType(message)`. Methods that return success dicts keep returning dicts.

---

## Refactor 3: Add DB indexes

**Problem:** Several frequent queries do full table scans.

**Fix:** Add indexes in `db/core.py` schema initialization:

```sql
CREATE INDEX IF NOT EXISTS idx_segments_game_active ON segments(game_id, active);
CREATE INDEX IF NOT EXISTS idx_model_state_segment ON model_state(segment_id);
CREATE INDEX IF NOT EXISTS idx_attempts_segment ON attempts(segment_id, created_at);
CREATE INDEX IF NOT EXISTS idx_capture_runs_game ON capture_runs(game_id, draft);
CREATE INDEX IF NOT EXISTS idx_variants_segment ON segment_variants(segment_id);
```

These cover:
- `get_active_segments(game_id)` — segments by game
- `load_all_model_states_for_game()` — model state join
- `get_segment_attempts()` — attempts per segment
- `get_draft_runs()` / `list_capture_runs()` — capture runs by game
- Variant lookups in segment loading

**Risk:** None. Indexes are additive; existing queries work unchanged. SQLite creates them on startup.

---

## Refactor 4: Add TCP message correlation

**Problem:** No way to match a `practice_load` command to its `attempt_result` response. If Lua processes events out of order or sends a stale result, there's no detection.

**Fix:** Add a `seq` field to commands and responses.

**Python side (tcp_manager.py):**

```python
self._seq = 0

async def send(self, message: str) -> int:
    """Send message, return sequence number."""
    self._seq += 1
    # ... existing send logic ...
    return self._seq
```

**Practice side (practice.py):**

```python
cmd_dict["seq"] = self.tcp.next_seq()
await self.tcp.send(f"practice_load:{json.dumps(cmd_dict)}")
# ... wait for result ...
if result.get("seq") != cmd_dict["seq"]:
    raise ValueError(f"Sequence mismatch: sent {cmd_dict['seq']}, got {result.get('seq')}")
```

**Lua side:** Echo back the `seq` field in `attempt_result`.

**Scope:** Only add to `practice_load` → `attempt_result` initially. Other command/response pairs can adopt later.

---

## Refactor 5: Cache state in SessionManager.get_state()

**Problem:** `get_state()` reloads scheduler config, all segments, all model states, and recent attempts on every call. This fires on every SSE broadcast.

**Fix:** Add a dirty flag. Mark dirty on events that change state (attempt logged, segment created, mode change). Only rebuild the parts that changed.

```python
class SessionManager:
    def __init__(self, ...):
        self._cached_state: dict | None = None
        self._state_dirty = True

    def _invalidate_state(self):
        self._state_dirty = True

    def get_state(self) -> dict:
        if not self._state_dirty and self._cached_state is not None:
            return self._cached_state
        self._cached_state = self._build_state()
        self._state_dirty = False
        return self._cached_state
```

Call `_invalidate_state()` in: `_handle_game_context()` (game switch), `start_practice()`, `stop_practice()`, `start_reference()`, `stop_reference()`, `_process_result()` (attempt logged), `_handle_level_entrance()` / `_handle_checkpoint()` / `_handle_exit()` (segment created), allocator/estimator change. Do NOT invalidate on every `route_event()` — that defeats the cache. Only invalidate on events that change the state shape.

---

## Refactor 6: Validate config at startup

**Problem:** Config values are accessed with `.get(key, default)` scattered across cli.py and dashboard.py. Missing required keys (like `data.dir`) crash at a random later point. Optional keys silently default.

**Fix:** Add a `config.py` module that validates and exposes typed config:

```python
@dataclass
class NetworkConfig:
    host: str = "127.0.0.1"
    port: int = 15482
    dashboard_port: int = 15483

@dataclass
class DataConfig:
    dir: str  # required — no default

@dataclass
class SpinLabConfig:
    network: NetworkConfig
    data: DataConfig
    rom_dir: Path | None
    category: str

def load_config(path: Path) -> SpinLabConfig:
    """Load and validate config. Raises on missing required fields."""
```

All `.get()` chains in cli.py and dashboard.py get replaced with `config.network.port`, etc. Missing required fields raise at startup with a clear error message.

---

## Refactor 7: Lua event schema validation

**Problem:** TCP events from Lua are unvalidated dicts. A typo in an event name (e.g., `level_entrace` instead of `level_entrance`) means the event is silently ignored by the dispatcher.

**Fix:** Add a lightweight validator in `session_manager.py`:

```python
KNOWN_EVENTS = {
    "rom_info", "game_context", "level_entrance", "checkpoint",
    "death", "spawn", "level_exit", "attempt_result",
    "rec_saved", "replay_started", "replay_progress",
    "replay_finished", "replay_error",
}

async def route_event(self, event: dict) -> None:
    event_type = event.get("event")
    if event_type not in KNOWN_EVENTS:
        logger.error("Unknown event type from Lua: %r", event)
        return
    # ... existing dispatch ...
```

Unknown events log at ERROR instead of being silently skipped. This catches typos on the Lua side immediately.

---

## Execution Order

These refactors have some dependencies:

1. **Indexes** (Refactor 3) — independent, zero risk, do first
2. **Config validation** (Refactor 6) — independent, do early
3. **Lua event validation** (Refactor 7) — independent, small
4. **Error signaling** (Refactor 2) — touches many files, do before SegmentLoader
5. **SegmentLoader** (Refactor 1) — benefits from clean error types
6. **State caching** (Refactor 5) — do after SegmentLoader (loader changes state shape)
7. **TCP correlation** (Refactor 4) — touches Lua + Python, do last

## Testing

- **Refactor 1:** Existing scheduler tests adapted. New unit tests for SegmentLoader.
- **Refactor 2:** Existing dashboard tests updated to expect HTTP exceptions. New tests for domain exceptions.
- **Refactor 3:** No new tests. Existing queries verified by existing tests.
- **Refactor 4:** New test for sequence mismatch detection. Integration test for round-trip.
- **Refactor 5:** Test that get_state() returns cached value when not dirty. Test invalidation triggers.
- **Refactor 6:** Tests for valid config, missing required field, default values.
- **Refactor 7:** Test that unknown event type logs ERROR.

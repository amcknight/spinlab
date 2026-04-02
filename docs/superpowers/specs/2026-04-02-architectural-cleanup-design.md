# Architectural Cleanup — Design Spec

**Date:** 2026-04-02
**Scope:** One branch, four phases. Type safety, structural refactors, Lua dedup, test improvements.
**Frontend:** Deferred to a separate initiative.

---

## Phase 1: Pure Additions (zero-risk)

New types and dead code removal. Nothing is wired in yet — existing code continues to work unchanged.

### 1a. New Enums (in `models.py`)

```python
class EndpointType(StrEnum):
    """Segment start/end types."""
    ENTRANCE = "entrance"
    CHECKPOINT = "checkpoint"
    GOAL = "goal"

class EventType(StrEnum):
    """TCP event names from Lua. Python-side only — Lua stays as strings."""
    ROM_INFO = "rom_info"
    GAME_CONTEXT = "game_context"
    LEVEL_ENTRANCE = "level_entrance"
    CHECKPOINT = "checkpoint"
    DEATH = "death"
    SPAWN = "spawn"
    LEVEL_EXIT = "level_exit"
    ATTEMPT_RESULT = "attempt_result"
    REC_SAVED = "rec_saved"
    REPLAY_STARTED = "replay_started"
    REPLAY_PROGRESS = "replay_progress"
    REPLAY_FINISHED = "replay_finished"
    REPLAY_ERROR = "replay_error"

class Status(StrEnum):
    """Result status values shared between controllers and dashboard."""
    OK = "ok"
    STARTED = "started"
    STOPPED = "stopped"
    NOT_CONNECTED = "not_connected"
    DRAFT_PENDING = "draft_pending"
    PRACTICE_ACTIVE = "practice_active"
    REFERENCE_ACTIVE = "reference_active"
    ALREADY_RUNNING = "already_running"
    ALREADY_REPLAYING = "already_replaying"
    NOT_IN_REFERENCE = "not_in_reference"
    NOT_REPLAYING = "not_replaying"
    NOT_RUNNING = "not_running"
    NO_DRAFT = "no_draft"
    NO_HOT_VARIANT = "no_hot_variant"

class AttemptSource(StrEnum):
    PRACTICE = "practice"
    REPLAY = "replay"
```

### 1b. ActionResult Dataclass (in `models.py`)

Replaces untyped result dicts with `new_mode` side-channel:

```python
@dataclass
class ActionResult:
    status: Status
    new_mode: Mode | None = None
    session_id: str | None = None

    def to_response(self) -> dict:
        """API-safe dict — strips internal fields like new_mode."""
        d: dict = {"status": self.status.value}
        if self.session_id is not None:
            d["session_id"] = self.session_id
        return d
```

### 1c. Typed Config (`config.py`, new file)

```python
@dataclass
class NetworkConfig:
    host: str = "127.0.0.1"
    port: int = 15482
    dashboard_port: int = 15483

@dataclass
class EmulatorConfig:
    path: Path | None = None
    lua_script: Path | None = None

@dataclass
class AppConfig:
    network: NetworkConfig
    emulator: EmulatorConfig
    data_dir: Path
    rom_dir: Path | None
    category: str = "any%"

    @classmethod
    def from_yaml(cls, path: Path) -> "AppConfig":
        """Parse config.yaml into typed config. Crashes loud on missing keys."""
        ...
```

### 1d. Typed DB Row Returns (TypedDicts in DB modules)

Each DB module defines TypedDicts for its query shapes:

**`db/segments.py`:**
```python
class SegmentRow(TypedDict):
    id: str
    game_id: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int
    description: str
    strat_version: int
    state_path: str | None
    active: int
    ordinal: int | None
    reference_id: str | None
```

**`db/attempts.py`:**
```python
class AttemptRow(TypedDict):
    segment_id: str
    time_ms: int | None
    completed: int
    deaths: int
    clean_tail_ms: int | None
    created_at: str
```

**`db/model_state.py`:**
```python
class ModelStateRow(TypedDict):
    segment_id: str
    estimator: str
    state_json: str | None
    output_json: str | None
```

**`db/sessions.py`:**
```python
class SessionRow(TypedDict):
    id: str
    game_id: str
    started_at: str
    ended_at: str | None
    segments_attempted: int
    segments_completed: int
```

**`db/capture_runs.py`:**
```python
class CaptureRunRow(TypedDict):
    id: str
    game_id: str
    name: str
    created_at: str
    is_active: int

class ReferenceSegmentRow(TypedDict):
    id: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int
    description: str
    state_path: str | None
    ordinal: int | None
```

Additional TypedDicts added as needed during implementation — the above covers the main query shapes.

### 1e. Dead Code Removal

- Delete `Attempt.rating` field
- Delete `Attempt.goal_matched` field
- Drop `rating TEXT` and `goal_matched INTEGER` from DB schema
- Remove from `_expected_columns` in `db/core.py`
- Remove from `INSERT` in `db/attempts.py`
- Delete `TransitionEvent` class (replaced by `EventType`)
- Delete V1 backward-compat path in `ModelOutput.from_dict()`
- Delete stale `"splits"` migration logic in `_init_schema()` if no longer needed

### 1f. Test Factories (`tests/factories.py`, new file)

```python
def make_attempt_record(
    time_ms: int, completed: bool, deaths: int = 0,
    clean_tail_ms: int | None = None,
) -> AttemptRecord:
    """Consolidates _attempt() helpers from 5+ test files."""
    ...

def make_incomplete() -> AttemptRecord:
    """Incomplete attempt (death, no completion)."""
    ...

def make_segment_with_model(
    segment_id: str, ms_per_attempt: float = 0.0, ...
) -> SegmentWithModel:
    """Consolidates _make_segment() from allocator tests."""
    ...
```

---

## Phase 2: Wire In New Types (mechanical swaps)

### 2a. Model Field Types

- `Segment.start_type: str` → `EndpointType`
- `Segment.end_type: str` → `EndpointType`
- `Attempt.source: str` → `AttemptSource`
- `SegmentCommand.to_dict()` → `dataclasses.asdict()`

### 2b. DB Layer Returns

All DB methods updated to return the TypedDicts from Phase 1d. SQL unchanged — just annotate return types and ensure dict construction matches.

### 2c. Config Swap

- `cli.py`: `yaml.safe_load()` → `AppConfig.from_yaml()`
- `create_app()` signature: `config: dict | None` → `config: AppConfig`
- All `.get()` chains → attribute access
- `app.state.config` stores `AppConfig`

### 2d. Status Enum and ActionResult

- `CaptureController` methods return `ActionResult` instead of `dict`
- `SessionManager` methods return `ActionResult`
- `_check_result()` accepts `ActionResult`, maps `Status` → HTTP codes
- `new_mode` accessed as `result.new_mode` instead of `result.pop("new_mode")`

### 2e. EventType in Dispatch

- `SessionManager._event_handlers` keys: `str` → `EventType`
- `route_event()` validates with `EventType(evt_type)`, catches `ValueError`
- Unknown events → `logger.warning("Unknown event type: %r", evt_type)`

### 2f. TCP Non-JSON Logging

In `tcp_manager.py._read_loop()`:
- Known non-JSON responses (`pong`, `ok:queued`) → `logger.debug()`
- Unknown non-JSON → `logger.warning("Unexpected non-JSON from Lua: %r", text)`

---

## Phase 3: Structural Refactors

### 3a. Split Dashboard into Routers

**New file structure:**
```
python/spinlab/
  dashboard.py          # create_app(), lifespan, middleware, mounts routers (~50 lines)
  routes/
    __init__.py
    practice.py         # start/stop practice
    reference.py        # start/stop/list/create/rename/delete references, drafts
    model.py            # model state, allocator weights, estimator switching/params
    segments.py         # list/update/delete segments, fill-gap
    system.py           # state, SSE, ROMs, emulator launch, reset, shutdown, manifest import
    _deps.py            # Depends() helpers: get_session, get_db, get_config
```

**Dependency injection pattern:**
```python
# routes/_deps.py
from fastapi import Request
from spinlab.session_manager import SessionManager
from spinlab.db import Database
from spinlab.config import AppConfig

def get_session(request: Request) -> SessionManager:
    return request.app.state.session

def get_db(request: Request) -> Database:
    return request.app.state.db

def get_config(request: Request) -> AppConfig:
    return request.app.state.config
```

`_check_result()` stays in `dashboard.py` as a shared utility imported by routers.

### 3b. Extract StateBuilder

**New class in `state_builder.py`:**
```python
class StateBuilder:
    """Assembles API/SSE state snapshots. Pure view-model construction."""

    def __init__(self, db: Database):
        self.db = db

    def build(self, session: SessionManager) -> dict:
        """Full state snapshot — everything get_state() and _build_practice_state() do now."""
        ...
```

SessionManager delegates: `get_state()` calls `StateBuilder(self.db).build(self)`.

Also moves the inline model-view dict construction from the `/api/model` endpoint into a `build_model_view()` method on StateBuilder.

### 3c. CaptureController Dependency Injection

Constructor receives `db` and `tcp`:

```python
class CaptureController:
    def __init__(self, db: Database, tcp: TcpManager):
        self.db = db
        self.tcp = tcp
```

Method signatures lose `db` and `tcp` args. `game_id` remains per-call since it changes on game switch. `mode` remains per-call since SessionManager owns mode state.

SessionManager creates `CaptureController(db, tcp)` in `__init__`.

### 3d. Estimator/Allocator Registration Cleanup

**`estimators/__init__.py`** adds explicit discovery:
```python
def _register_all():
    from . import kalman, rolling_mean
    try:
        from . import exp_decay
    except ImportError:
        pass

_register_all()
```

**`allocators/__init__.py`** does the same for allocators:
```python
def _register_all():
    from . import greedy, random, round_robin

_register_all()
```

Remove the `# noqa: F401` side-effect imports from `scheduler.py`.

---

## Phase 4: Cross-Component & Tests

### 4a. Lua Address Map Dedup

**New file `lua/addresses.lua`:**
```lua
-- addresses.lua — Single source of truth for SNES memory addresses.
-- Both spinlab.lua and poke_engine.lua load this via dofile().
ADDR_GAME_MODE     = 0x0100
ADDR_LEVEL_NUM     = 0x13BF
ADDR_ROOM_NUM      = 0x010B
ADDR_LEVEL_START   = 0x1935
ADDR_PLAYER_ANIM   = 0x0071
ADDR_EXIT_MODE     = 0x0DD5
ADDR_IO            = 0x1DFB
ADDR_FANFARE       = 0x0906
ADDR_BOSS_DEFEAT   = 0x13C6
ADDR_MIDWAY        = 0x13CE
ADDR_CP_ENTRANCE   = 0x1B403
```

**`spinlab.lua` and `poke_engine.lua`:** Replace local address constants with `dofile(script_dir .. "addresses.lua")`.

**`tests/integration/addresses.py`:** Parse `addresses.lua` at import time:
```python
import re
from pathlib import Path

_LUA = Path(__file__).parents[2] / "lua" / "addresses.lua"
ADDR_MAP = {}
for line in _LUA.read_text().splitlines():
    m = re.match(r"(ADDR_\w+)\s*=\s*(0x[0-9a-fA-F]+)", line)
    if m:
        ADDR_MAP[m.group(1)] = int(m.group(2), 16)
```

Three copies → one source of truth.

### 4b. Test Factory Wiring

Update all test files to import from `tests/factories.py`:
- Replace local `_attempt()` in: `test_kalman.py`, `test_exp_decay.py`, `test_rolling_mean.py`, `test_estimator_sanity.py`, `test_estimator_params.py`
- Replace local `_make_segment()` in: `test_allocators.py`, `test_mix_allocator.py`
- Replace local `_incomplete()` everywhere
- Delete all local copies

### 4c. Test Behavior Focus

- **Kalman state tests:** Rewrite assertions on `state.mu` to assert through `model_output()` instead
- **Dashboard mock tests:** Replace `session.save_draft = AsyncMock(return_value=...)` patterns with real `SessionManager` + real `Database`, only mock TCP
- **Drift info test:** Test that drift info is consumed correctly by the UI/allocator path, not that it returns a specific dict shape

### 4d. Conftest Improvements

Move to `tests/conftest.py`:
- `_sync_switch()` helper
- Parametrized `all_estimators` fixture (auto-discovers via `list_estimators()`)
- Shared `client` fixture: real DB + real SessionManager + mocked TCP

---

## Out of Scope

- Frontend framework migration (deferred to separate initiative)
- Protocol versioning (C3 — low priority)
- Estimator instance caching in Scheduler (E1)
- `pick_next()` performance / scaling (E2)
- ParamDef constraint validation (E4 — ParamDef is too new and changing)
- `AttemptRecord.created_at` str→datetime (D5 — low impact, can do later)

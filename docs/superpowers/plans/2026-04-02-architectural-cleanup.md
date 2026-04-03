# Architectural Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve type safety, split oversized modules, deduplicate code, and strengthen tests across the SpinLab codebase.

**Architecture:** Four phases — (1) add new types and delete dead code, (2) wire types into existing code, (3) structural file splits and responsibility moves, (4) Lua dedup and test improvements. One branch, all phases.

**Tech Stack:** Python 3.11+ (StrEnum, TypedDict), FastAPI (APIRouter, Depends), Lua (dofile), pytest

**Spec:** `docs/superpowers/specs/2026-04-02-architectural-cleanup-design.md`

---

## File Map

### New files
- `python/spinlab/config.py` — AppConfig dataclass, parsed from YAML
- `python/spinlab/routes/__init__.py` — empty
- `python/spinlab/routes/_deps.py` — FastAPI Depends helpers
- `python/spinlab/routes/practice.py` — practice start/stop endpoints
- `python/spinlab/routes/reference.py` — reference CRUD, draft, replay endpoints
- `python/spinlab/routes/model.py` — model state, allocator weights, estimator endpoints
- `python/spinlab/routes/segments.py` — segment CRUD, fill-gap endpoints
- `python/spinlab/routes/system.py` — state, SSE, ROMs, emulator, reset, shutdown
- `python/spinlab/state_builder.py` — StateBuilder for API/SSE snapshots
- `lua/addresses.lua` — single source of truth for SNES memory addresses
- `tests/factories.py` — shared test data factories

### Modified files
- `python/spinlab/models.py` — add enums (EndpointType, EventType, Status, AttemptSource, ActionResult), delete dead fields
- `python/spinlab/db/core.py` — remove rating/goal_matched from schema and _expected_columns, clean stale migrations
- `python/spinlab/db/segments.py` — add SegmentRow TypedDict, annotate returns
- `python/spinlab/db/attempts.py` — add AttemptRow TypedDict, remove rating/goal_matched from INSERT, annotate returns
- `python/spinlab/db/sessions.py` — add SessionRow TypedDict, annotate returns
- `python/spinlab/db/model_state.py` — add ModelStateRow TypedDict, annotate returns
- `python/spinlab/db/capture_runs.py` — add CaptureRunRow/ReferenceSegmentRow TypedDicts, annotate returns
- `python/spinlab/dashboard.py` — slim to create_app + router mounts, use AppConfig
- `python/spinlab/session_manager.py` — use ActionResult, EventType, delegate state building
- `python/spinlab/capture_controller.py` — return ActionResult, inject db/tcp at init
- `python/spinlab/draft_manager.py` — return ActionResult
- `python/spinlab/practice.py` — use AttemptSource enum
- `python/spinlab/cli.py` — use AppConfig.from_yaml()
- `python/spinlab/tcp_manager.py` — log non-JSON lines
- `python/spinlab/scheduler.py` — remove side-effect imports
- `python/spinlab/estimators/__init__.py` — add _register_all()
- `python/spinlab/allocators/__init__.py` — add _register_all()
- `lua/spinlab.lua` — replace address constants with dofile()
- `lua/poke_engine.lua` — replace ADDR_MAP with dofile()
- `tests/integration/addresses.py` — parse lua/addresses.lua at import
- `tests/conftest.py` — add shared fixtures
- Multiple test files — use factories, improve behavior focus

---

## Phase 1: Pure Additions & Dead Code Removal

### Task 1: Add StrEnums to models.py

**Files:**
- Modify: `python/spinlab/models.py:1-7` (imports), `:35-41` (TransitionEvent)
- Test: `tests/test_models_enums.py` (new)

- [ ] **Step 1: Write tests for new enums**

Create `tests/test_models_enums.py`:

```python
"""Tests for new StrEnum types in models."""
from spinlab.models import EndpointType, EventType, Status, AttemptSource


class TestEndpointType:
    def test_values(self):
        assert EndpointType.ENTRANCE == "entrance"
        assert EndpointType.CHECKPOINT == "checkpoint"
        assert EndpointType.GOAL == "goal"

    def test_from_string(self):
        assert EndpointType("entrance") == EndpointType.ENTRANCE


class TestEventType:
    def test_all_tcp_events_present(self):
        names = {e.value for e in EventType}
        assert "rom_info" in names
        assert "death" in names
        assert "attempt_result" in names
        assert "replay_finished" in names

    def test_from_string(self):
        assert EventType("death") == EventType.DEATH


class TestStatus:
    def test_success_statuses(self):
        assert Status.OK == "ok"
        assert Status.STARTED == "started"
        assert Status.STOPPED == "stopped"

    def test_error_statuses(self):
        assert Status.NOT_CONNECTED == "not_connected"
        assert Status.DRAFT_PENDING == "draft_pending"


class TestAttemptSource:
    def test_values(self):
        assert AttemptSource.PRACTICE == "practice"
        assert AttemptSource.REPLAY == "replay"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_models_enums.py -v`
Expected: ImportError — names don't exist yet

- [ ] **Step 3: Add enums to models.py**

At the top of `python/spinlab/models.py`, add `StrEnum` import and the four new enums. Delete `TransitionEvent`:

```python
"""SpinLab data models."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Optional


class Mode(Enum):
    IDLE = "idle"
    REFERENCE = "reference"
    PRACTICE = "practice"
    REPLAY = "replay"
    FILL_GAP = "fill_gap"
    COLD_FILL = "cold_fill"


_LEGAL_TRANSITIONS: dict[Mode, set[Mode]] = {
    Mode.IDLE: {Mode.REFERENCE, Mode.PRACTICE, Mode.FILL_GAP, Mode.COLD_FILL},
    Mode.REFERENCE: {Mode.IDLE, Mode.REPLAY},
    Mode.PRACTICE: {Mode.IDLE},
    Mode.REPLAY: {Mode.IDLE},
    Mode.FILL_GAP: {Mode.IDLE},
    Mode.COLD_FILL: {Mode.IDLE},
}


def transition_mode(current: Mode, target: Mode) -> Mode:
    """Validate and return the target mode, or raise ValueError."""
    if target not in _LEGAL_TRANSITIONS.get(current, set()):
        raise ValueError(f"Illegal mode transition: {current.value} -> {target.value}")
    return target


class EndpointType(StrEnum):
    """Segment start/end types."""
    ENTRANCE = "entrance"
    CHECKPOINT = "checkpoint"
    GOAL = "goal"


class EventType(StrEnum):
    """TCP event names from Lua. Python-side only."""
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
    NO_GAPS = "no_gaps"
    SHUTTING_DOWN = "shutting_down"


class AttemptSource(StrEnum):
    """Where an attempt originated."""
    PRACTICE = "practice"
    REPLAY = "replay"
```

Note: Delete the old `TransitionEvent` class (lines 35-41) entirely — it is replaced by `EventType`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_models_enums.py -v`
Expected: All PASS

- [ ] **Step 5: Run full test suite to check nothing broke**

Run: `python -m pytest tests/ -x -q`
Expected: All pass. If any tests imported `TransitionEvent`, fix those imports.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/models.py tests/test_models_enums.py
git commit -m "feat: add EndpointType, EventType, Status, AttemptSource enums; delete TransitionEvent"
```

---

### Task 2: Add ActionResult dataclass

**Files:**
- Modify: `python/spinlab/models.py` (append)
- Test: `tests/test_models_enums.py` (extend)

- [ ] **Step 1: Write test for ActionResult**

Append to `tests/test_models_enums.py`:

```python
from spinlab.models import ActionResult, Mode


class TestActionResult:
    def test_to_response_basic(self):
        r = ActionResult(status=Status.OK)
        assert r.to_response() == {"status": "ok"}

    def test_to_response_with_session_id(self):
        r = ActionResult(status=Status.STARTED, session_id="abc123")
        assert r.to_response() == {"status": "started", "session_id": "abc123"}

    def test_to_response_strips_new_mode(self):
        r = ActionResult(status=Status.STARTED, new_mode=Mode.REFERENCE)
        resp = r.to_response()
        assert "new_mode" not in resp
        assert resp == {"status": "started"}

    def test_new_mode_accessible(self):
        r = ActionResult(status=Status.OK, new_mode=Mode.PRACTICE)
        assert r.new_mode == Mode.PRACTICE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_models_enums.py::TestActionResult -v`
Expected: ImportError

- [ ] **Step 3: Add ActionResult to models.py**

Append to `python/spinlab/models.py` (before the `Segment` class):

```python
@dataclass
class ActionResult:
    """Typed result from controller actions. Replaces untyped status dicts."""
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

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_models_enums.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/models.py tests/test_models_enums.py
git commit -m "feat: add ActionResult dataclass for typed controller returns"
```

---

### Task 3: Delete dead fields (rating, goal_matched, V1 compat)

**Files:**
- Modify: `python/spinlab/models.py:74-85` (Attempt), `:154-174` (ModelOutput.from_dict V1)
- Modify: `python/spinlab/db/core.py:43-55` (schema), `:149-155` (_expected_columns)
- Modify: `python/spinlab/db/attempts.py:15-28` (INSERT)
- Test: existing tests

- [ ] **Step 1: Remove rating and goal_matched from Attempt dataclass**

In `python/spinlab/models.py`, change the Attempt class to remove `goal_matched` and `rating`:

```python
@dataclass
class Attempt:
    segment_id: str
    session_id: str
    completed: bool
    time_ms: int | None = None
    strat_version: int = 1
    source: str = "practice"
    deaths: int = 0
    clean_tail_ms: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
```

- [ ] **Step 2: Remove V1 backward-compat from ModelOutput.from_dict**

In `python/spinlab/models.py`, simplify `ModelOutput.from_dict()`:

```python
    @classmethod
    def from_dict(cls, d: dict) -> "ModelOutput":
        return cls(
            total=Estimate.from_dict(d["total"]),
            clean=Estimate.from_dict(d["clean"]),
        )
```

- [ ] **Step 3: Update DB schema — remove rating/goal_matched columns**

In `python/spinlab/db/core.py`, update the `attempts` table in SCHEMA to remove `goal_matched INTEGER,` and `rating TEXT,` lines. Update `_expected_columns` to remove them:

```python
    @staticmethod
    def _expected_columns(table: str) -> set[str]:
        return {
            "model_state": {"segment_id", "estimator", "state_json", "output_json", "updated_at"},
            "attempts": {"id", "segment_id", "session_id", "completed", "time_ms",
                         "strat_version", "source",
                         "deaths", "clean_tail_ms", "created_at"},
        }.get(table, set())
```

- [ ] **Step 4: Update INSERT statement in attempts.py**

In `python/spinlab/db/attempts.py`, update `log_attempt`:

```python
    def log_attempt(self, attempt: Attempt) -> None:
        self.conn.execute(
            """INSERT INTO attempts
               (segment_id, session_id, completed, time_ms,
                strat_version, source, deaths, clean_tail_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (attempt.segment_id, attempt.session_id, int(attempt.completed),
             attempt.time_ms,
             attempt.strat_version, attempt.source,
             attempt.deaths, attempt.clean_tail_ms,
             attempt.created_at.isoformat()),
        )
        self.conn.commit()
```

- [ ] **Step 5: Clean up stale migration logic in core.py if "splits" is dead**

In `python/spinlab/db/core.py` `_init_schema()`, keep the `stale_tables = ["splits"]` logic but note it's minimal. Remove the `"splits"` entry only if you're confident no DB has that table anymore. Since all data is pre-prod, delete the entire stale_tables approach:

Replace `_init_schema` with:

```python
    def _init_schema(self) -> None:
        # Drop tables whose schema has changed (pre-prod, no data worth migrating)
        for table in ["splits", "model_state", "attempts"]:
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if cols and cols != self._expected_columns(table):
                self.conn.execute(f"DROP TABLE IF EXISTS {table}")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        for migration in [
            "ALTER TABLE capture_runs ADD COLUMN draft INTEGER DEFAULT 0",
        ]:
            try:
                self.conn.execute(migration)
                self.conn.commit()
            except sqlite3.OperationalError:
                pass
        self.conn.execute("""
            UPDATE segments SET reference_id = (
                SELECT cr.id FROM capture_runs cr
                WHERE cr.game_id = segments.game_id
                ORDER BY cr.created_at ASC LIMIT 1
            ) WHERE reference_id IS NULL
        """)
        self.conn.commit()
```

- [ ] **Step 6: Run full test suite**

Run: `python -m pytest tests/ -x -q`
Expected: All pass. Fix any tests that reference `rating` or `goal_matched`.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/models.py python/spinlab/db/core.py python/spinlab/db/attempts.py
git commit -m "chore: delete dead fields (rating, goal_matched, V1 ModelOutput compat)"
```

---

### Task 4: Add TypedDicts for DB row returns

**Files:**
- Modify: `python/spinlab/db/segments.py` (add SegmentRow)
- Modify: `python/spinlab/db/attempts.py` (add AttemptRow)
- Modify: `python/spinlab/db/sessions.py` (add SessionRow)
- Modify: `python/spinlab/db/model_state.py` (add ModelStateRow, GoldRow)
- Modify: `python/spinlab/db/capture_runs.py` (add CaptureRunRow, ReferenceSegmentRow)

- [ ] **Step 1: Add SegmentRow to segments.py**

At top of `python/spinlab/db/segments.py`, add:

```python
from typing import TypedDict


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
    active: int
    ordinal: int | None
    state_path: str | None
```

Update `get_all_segments_with_model` return type annotation from `list[dict]` to `list[SegmentRow]`. The actual return logic (`dict(zip(actual_cols, row))`) is unchanged.

Also update `segments_missing_cold` — add a `MissingColdRow` TypedDict:

```python
class MissingColdRow(TypedDict):
    segment_id: str
    hot_state_path: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int
    description: str
```

Change return type of `segments_missing_cold` to `list[MissingColdRow]`.

- [ ] **Step 2: Add AttemptRow to attempts.py**

At top of `python/spinlab/db/attempts.py`, add:

```python
from typing import TypedDict


class AttemptRow(TypedDict):
    segment_id: str
    completed: int
    time_ms: int | None
    deaths: int
    clean_tail_ms: int | None
    created_at: str


class RecentAttemptRow(TypedDict, total=False):
    """Attempt joined with segment info — has all AttemptRow fields plus segment fields."""
    id: int
    segment_id: str
    session_id: str
    completed: int
    time_ms: int | None
    strat_version: int
    source: str
    deaths: int
    clean_tail_ms: int | None
    created_at: str
    description: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int
```

Update return types: `get_segment_attempts` → `list[AttemptRow]`, `get_recent_attempts` → `list[RecentAttemptRow]`, `get_all_attempts_by_segment` → `dict[str, list[AttemptRow]]`.

- [ ] **Step 3: Add SessionRow to sessions.py**

At top of `python/spinlab/db/sessions.py`, add:

```python
from typing import TypedDict


class SessionRow(TypedDict):
    id: str
    game_id: str
    started_at: str
    ended_at: str | None
    segments_attempted: int
    segments_completed: int
```

Update return types: `get_current_session` → `SessionRow | None`, `get_session_history` → `list[SessionRow]`.

- [ ] **Step 4: Add ModelStateRow to model_state.py**

At top of `python/spinlab/db/model_state.py`, add:

```python
from typing import TypedDict


class ModelStateRow(TypedDict):
    segment_id: str
    estimator: str
    state_json: str | None
    output_json: str | None
    updated_at: str


class GoldRow(TypedDict):
    gold_ms: int | None
    clean_gold_ms: int | None
```

Update return types: `load_model_state` → `ModelStateRow | None`, `load_all_model_states_for_segment` → `list[ModelStateRow]`, `load_all_model_states` → `list[ModelStateRow]`, `load_all_model_states_for_game` → `dict[str, list[ModelStateRow]]`, `compute_golds` → `dict[str, GoldRow]`.

- [ ] **Step 5: Add CaptureRunRow to capture_runs.py**

At top of `python/spinlab/db/capture_runs.py`, add:

```python
from typing import TypedDict


class CaptureRunRow(TypedDict):
    id: str
    game_id: str
    name: str
    created_at: str
    active: int
    draft: int


class ReferenceSegmentRow(TypedDict):
    id: str
    game_id: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int
    description: str
    active: int
    ordinal: int | None
    reference_id: str | None
    state_path: str | None
```

Update return types: `list_capture_runs` → `list[CaptureRunRow]`, `get_segments_by_reference` → `list[ReferenceSegmentRow]`.

- [ ] **Step 6: Run full test suite**

Run: `python -m pytest tests/ -x -q`
Expected: All pass — TypedDicts are structural, no runtime change.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/db/
git commit -m "feat: add TypedDict annotations for all DB row returns"
```

---

### Task 5: Add AppConfig dataclass

**Files:**
- Create: `python/spinlab/config.py`
- Test: `tests/test_config.py` (new)

- [ ] **Step 1: Write tests for AppConfig**

Create `tests/test_config.py`:

```python
"""Tests for AppConfig loading."""
from pathlib import Path

import pytest
import yaml

from spinlab.config import AppConfig


class TestAppConfig:
    def test_from_yaml_minimal(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "data": {"dir": "data"},
            "network": {"host": "127.0.0.1", "port": 15482, "dashboard_port": 15483},
        }))
        cfg = AppConfig.from_yaml(config_file)
        assert cfg.data_dir == Path("data")
        assert cfg.network.host == "127.0.0.1"
        assert cfg.network.port == 15482

    def test_from_yaml_full(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "data": {"dir": "/data"},
            "network": {"host": "0.0.0.0", "port": 9999, "dashboard_port": 8080},
            "rom": {"dir": "/roms"},
            "emulator": {"path": "/emu", "lua_script": "script.lua"},
            "game": {"category": "100%"},
        }))
        cfg = AppConfig.from_yaml(config_file)
        assert cfg.rom_dir == Path("/roms")
        assert cfg.emulator.path == Path("/emu")
        assert cfg.category == "100%"

    def test_from_yaml_defaults(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "data": {"dir": "data"},
        }))
        cfg = AppConfig.from_yaml(config_file)
        assert cfg.network.host == "127.0.0.1"
        assert cfg.network.port == 15482
        assert cfg.rom_dir is None
        assert cfg.category == "any%"

    def test_from_yaml_missing_data_dir_crashes(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"network": {}}))
        with pytest.raises(KeyError):
            AppConfig.from_yaml(config_file)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: ImportError

- [ ] **Step 3: Create config.py**

Create `python/spinlab/config.py`:

```python
"""Typed configuration — parsed once at startup from YAML."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


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
        """Parse config.yaml into typed config. Crashes loud on missing required keys."""
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        net = raw.get("network", {})
        emu = raw.get("emulator", {})
        rom_dir_str = raw.get("rom", {}).get("dir")

        emu_path = emu.get("path")
        lua_script = emu.get("lua_script")

        return cls(
            network=NetworkConfig(
                host=net.get("host", "127.0.0.1"),
                port=net.get("port", 15482),
                dashboard_port=net.get("dashboard_port", 15483),
            ),
            emulator=EmulatorConfig(
                path=Path(emu_path) if emu_path else None,
                lua_script=Path(lua_script) if lua_script else None,
            ),
            data_dir=Path(raw["data"]["dir"]),
            rom_dir=Path(rom_dir_str) if rom_dir_str else None,
            category=raw.get("game", {}).get("category", "any%"),
        )
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_config.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/config.py tests/test_config.py
git commit -m "feat: add AppConfig dataclass for typed YAML config"
```

---

### Task 6: Create test factories

**Files:**
- Create: `tests/factories.py`

- [ ] **Step 1: Create factories.py**

Create `tests/factories.py`:

```python
"""Shared test data factories — consolidates duplicated helpers."""
from __future__ import annotations

from spinlab.models import AttemptRecord, Estimate, ModelOutput
from spinlab.allocators import SegmentWithModel


def make_attempt_record(
    time_ms: int,
    completed: bool,
    deaths: int = 0,
    clean_tail_ms: int | None = None,
    created_at: str = "2026-01-01T00:00:00",
) -> AttemptRecord:
    """Create an AttemptRecord for testing."""
    return AttemptRecord(
        time_ms=time_ms if completed else None,
        completed=completed,
        deaths=deaths,
        clean_tail_ms=clean_tail_ms,
        created_at=created_at,
    )


def make_incomplete(
    deaths: int = 1,
    created_at: str = "2026-01-01T00:00:00",
) -> AttemptRecord:
    """Create an incomplete (death) attempt."""
    return AttemptRecord(
        time_ms=None, completed=False, deaths=deaths,
        clean_tail_ms=None, created_at=created_at,
    )


def make_segment_with_model(
    segment_id: str,
    ms_per_attempt: float = 0.0,
    expected_ms: float = 10000.0,
    floor_ms: float | None = None,
    state_path: str | None = "/fake/state.mss",
    n_completed: int = 5,
    n_attempts: int = 5,
    selected_model: str = "kalman",
    level_number: int = 105,
    start_type: str = "entrance",
    start_ordinal: int = 0,
    end_type: str = "goal",
    end_ordinal: int = 0,
) -> SegmentWithModel:
    """Create a SegmentWithModel for allocator testing."""
    out = ModelOutput(
        total=Estimate(
            expected_ms=expected_ms,
            ms_per_attempt=ms_per_attempt,
            floor_ms=floor_ms,
        ),
        clean=Estimate(),
    )
    return SegmentWithModel(
        segment_id=segment_id,
        game_id="test_game",
        level_number=level_number,
        start_type=start_type,
        start_ordinal=start_ordinal,
        end_type=end_type,
        end_ordinal=end_ordinal,
        description=f"Segment {segment_id}",
        strat_version=1,
        state_path=state_path,
        active=True,
        model_outputs={selected_model: out},
        selected_model=selected_model,
        n_completed=n_completed,
        n_attempts=n_attempts,
    )
```

- [ ] **Step 2: Verify import works**

Run: `python -c "from tests.factories import make_attempt_record, make_incomplete, make_segment_with_model; print('OK')"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add tests/factories.py
git commit -m "feat: add shared test factories for AttemptRecord and SegmentWithModel"
```

---

## Phase 2: Wire In New Types

### Task 7: Wire EndpointType into Segment and AttemptSource into Attempt

**Files:**
- Modify: `python/spinlab/models.py` (Segment, Attempt, SegmentCommand)
- Test: existing tests

- [ ] **Step 1: Update Segment field types**

In `python/spinlab/models.py`, change:
- `Segment.start_type: str` → `start_type: EndpointType`
- `Segment.end_type: str` → `end_type: EndpointType`
- Add comment: `# 'entrance', 'checkpoint'` → remove, enum is self-documenting
- `Attempt.source: str = "practice"` → `source: AttemptSource = AttemptSource.PRACTICE`

- [ ] **Step 2: Replace SegmentCommand.to_dict() with dataclasses.asdict()**

In `python/spinlab/models.py`:

```python
import dataclasses

@dataclass
class SegmentCommand:
    """Sent from orchestrator to Lua: which segment to load next."""
    id: str
    state_path: str
    description: str
    end_type: str
    expected_time_ms: int | None = None
    auto_advance_delay_ms: int = 1000

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)
```

- [ ] **Step 3: Run full test suite, fix any breakage**

Run: `python -m pytest tests/ -x -q`

StrEnum values serialize as strings in JSON/SQL, so most things should work. Fix any tests that construct Segment with bare strings — they need `EndpointType("entrance")` or just `"entrance"` (StrEnum accepts both).

Note: Places that construct `Segment(start_type="entrance", ...)` will still work because StrEnum accepts string assignment. The type annotation just enables IDE checking.

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/models.py
git commit -m "feat: wire EndpointType and AttemptSource enums into model fields"
```

---

### Task 8: Wire AppConfig into cli.py and dashboard.py

**Files:**
- Modify: `python/spinlab/cli.py:39-54`
- Modify: `python/spinlab/dashboard.py:66-77`

- [ ] **Step 1: Update cli.py to use AppConfig**

In `python/spinlab/cli.py`, replace the dashboard command handler:

```python
    elif parsed.command == "dashboard":
        import uvicorn
        from spinlab.config import AppConfig
        from spinlab.dashboard import create_app
        from spinlab.db import Database

        config = AppConfig.from_yaml(Path(parsed.config))
        dashboard_port = parsed.port or config.network.dashboard_port
        db = Database(config.data_dir / "spinlab.db")

        app = create_app(db=db, config=config)
        _write_ports_file(Path(parsed.config).parent, config.network.port, dashboard_port)
        print(f"SpinLab Dashboard: http://localhost:{dashboard_port}")
        uvicorn.run(app, host="0.0.0.0", port=dashboard_port, log_level="warning")
```

- [ ] **Step 2: Update create_app signature**

In `python/spinlab/dashboard.py`, change `create_app`:

```python
from .config import AppConfig

def create_app(
    db: Database,
    config: AppConfig,
) -> FastAPI:
    tcp = TcpManager(config.network.host, config.network.port)
    session = SessionManager(
        db, tcp, config.rom_dir, config.category, data_dir=config.data_dir,
    )
    tcp.on_disconnect = session.on_disconnect
    # ... rest unchanged, but replace config.get() chains with config attributes
```

Update all `cfg.get(...)` patterns inside endpoints to use `config` attributes:
- `cfg.get("rom", {}).get("dir", "")` → `str(config.rom_dir) if config.rom_dir else ""`
- `cfg.get("emulator", {}).get("path", "")` → `str(config.emulator.path) if config.emulator.path else ""`
- `cfg.get("emulator", {}).get("lua_script", "")` → config.emulator.lua_script
- Store `config` on `app.state.config`

- [ ] **Step 3: Update tests that call create_app**

Tests that call `create_app(db=..., host=..., port=..., config=...)` need updating. Search for `create_app` in test files and update to pass `AppConfig` instances. Create a helper in tests if needed:

```python
from spinlab.config import AppConfig, NetworkConfig, EmulatorConfig

def _test_config(**overrides) -> AppConfig:
    return AppConfig(
        network=NetworkConfig(port=overrides.get("port", 59999)),
        emulator=EmulatorConfig(),
        data_dir=overrides.get("data_dir", Path("data")),
        rom_dir=overrides.get("rom_dir"),
        category="any%",
    )
```

- [ ] **Step 4: Run full test suite**

Run: `python -m pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/cli.py python/spinlab/dashboard.py python/spinlab/config.py tests/
git commit -m "feat: wire AppConfig into cli and dashboard, replace .get() chains"
```

---

### Task 9: Wire Status enum and ActionResult into controllers

**Files:**
- Modify: `python/spinlab/capture_controller.py`
- Modify: `python/spinlab/draft_manager.py`
- Modify: `python/spinlab/session_manager.py`
- Modify: `python/spinlab/dashboard.py`

- [ ] **Step 1: Update CaptureController to return ActionResult**

In `python/spinlab/capture_controller.py`, replace all `return {"status": "..."}` with `return ActionResult(status=Status....)`:

Example for `start_reference`:
```python
from .models import ActionResult, Mode, SegmentVariant, Status

async def start_reference(self, mode, tcp, db, game_id, data_dir, run_name=None) -> ActionResult:
    if self.draft.has_draft:
        return ActionResult(status=Status.DRAFT_PENDING)
    if mode in (Mode.PRACTICE, Mode.REPLAY):
        return ActionResult(status=Status.PRACTICE_ACTIVE if mode == Mode.PRACTICE else Status.ALREADY_REPLAYING)
    if not tcp.is_connected:
        return ActionResult(status=Status.NOT_CONNECTED)
    # ... setup ...
    return ActionResult(status=Status.STARTED, new_mode=Mode.REFERENCE)
```

Apply the same pattern to: `stop_reference`, `start_replay`, `stop_replay`, `start_fill_gap`, `_load_next_cold_fill`.

- [ ] **Step 2: Update DraftManager to return ActionResult**

In `python/spinlab/draft_manager.py`:

```python
from .models import ActionResult, Status

def save(self, db, name) -> ActionResult:
    if not self.run_id:
        return ActionResult(status=Status.NO_DRAFT)
    db.promote_draft(self.run_id, name)
    db.set_active_capture_run(self.run_id)
    self.run_id = None
    self.segments_count = 0
    return ActionResult(status=Status.OK)

def discard(self, db) -> ActionResult:
    if not self.run_id:
        return ActionResult(status=Status.NO_DRAFT)
    db.hard_delete_capture_run(self.run_id)
    self.run_id = None
    self.segments_count = 0
    return ActionResult(status=Status.OK)
```

- [ ] **Step 3: Update SessionManager to use ActionResult**

In `python/spinlab/session_manager.py`, replace all the `result.pop("new_mode")` patterns:

```python
async def start_reference(self, run_name=None) -> ActionResult:
    result = await self.capture.start_reference(
        self.mode, self.tcp, self.db,
        self._require_game(), self.data_dir, run_name,
    )
    if result.new_mode is not None:
        self.mode = result.new_mode
    await self._notify_sse()
    return result
```

Apply to: `stop_reference`, `start_replay`, `stop_replay`, `start_fill_gap`, `save_draft`.

For `start_practice` and `stop_practice`, return `ActionResult` directly:

```python
async def start_practice(self) -> ActionResult:
    if self.capture.has_draft:
        return ActionResult(status=Status.DRAFT_PENDING)
    if self.practice_session and self.practice_session.is_running:
        return ActionResult(status=Status.ALREADY_RUNNING)
    if not self.tcp.is_connected:
        return ActionResult(status=Status.NOT_CONNECTED)
    # ... setup ...
    return ActionResult(status=Status.STARTED, session_id=ps.session_id)
```

- [ ] **Step 4: Update dashboard _check_result**

In `python/spinlab/dashboard.py`:

```python
from .models import ActionResult, Status

_ERROR_STATUS_CODES: dict[Status, int] = {
    Status.NOT_CONNECTED: 503,
    Status.DRAFT_PENDING: 409,
    Status.PRACTICE_ACTIVE: 409,
    Status.REFERENCE_ACTIVE: 409,
    Status.ALREADY_RUNNING: 409,
    Status.ALREADY_REPLAYING: 409,
    Status.NOT_IN_REFERENCE: 409,
    Status.NOT_REPLAYING: 409,
    Status.NOT_RUNNING: 409,
    Status.NO_DRAFT: 404,
    Status.NO_HOT_VARIANT: 404,
}

def _check_result(result: ActionResult) -> dict:
    code = _ERROR_STATUS_CODES.get(result.status)
    if code:
        raise HTTPException(status_code=code, detail=result.status.value)
    return result.to_response()
```

- [ ] **Step 5: Run full test suite, fix breakage**

Run: `python -m pytest tests/ -x -q`

Tests that mock return values like `AsyncMock(return_value={"status": "ok"})` need updating to return `ActionResult(status=Status.OK)`.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/capture_controller.py python/spinlab/draft_manager.py python/spinlab/session_manager.py python/spinlab/dashboard.py tests/
git commit -m "feat: wire ActionResult and Status enum into all controller returns"
```

---

### Task 10: Wire EventType into dispatch + TCP logging

**Files:**
- Modify: `python/spinlab/session_manager.py:59-73`
- Modify: `python/spinlab/tcp_manager.py:88-105`

- [ ] **Step 1: Update event dispatch table to use EventType**

In `python/spinlab/session_manager.py`:

```python
from .models import EventType, Mode

# In __init__:
self._event_handlers: dict[EventType, callable] = {
    EventType.ROM_INFO: self._handle_rom_info,
    EventType.GAME_CONTEXT: self._handle_game_context,
    EventType.LEVEL_ENTRANCE: self._handle_level_entrance,
    EventType.CHECKPOINT: self._handle_checkpoint,
    EventType.DEATH: self._handle_death,
    EventType.SPAWN: self._handle_spawn,
    EventType.LEVEL_EXIT: self._handle_level_exit,
    EventType.ATTEMPT_RESULT: self._handle_attempt_result,
    EventType.REC_SAVED: self._handle_rec_saved,
    EventType.REPLAY_STARTED: self._handle_replay_started,
    EventType.REPLAY_PROGRESS: self._handle_replay_progress,
    EventType.REPLAY_FINISHED: self._handle_replay_finished,
    EventType.REPLAY_ERROR: self._handle_replay_error,
}
```

Update `route_event`:

```python
async def route_event(self, event: dict) -> None:
    evt_type_str = event.get("event")
    try:
        evt_type = EventType(evt_type_str)
    except ValueError:
        logger.warning("Unknown event type from Lua: %r", evt_type_str)
        return
    handler = self._event_handlers.get(evt_type)
    if handler:
        await handler(event)
```

- [ ] **Step 2: Add non-JSON line logging to tcp_manager.py**

In `python/spinlab/tcp_manager.py`, replace the silent `pass`:

```python
_KNOWN_NON_JSON = {"pong", "ok:queued"}

async def _read_loop(self) -> None:
    if not self._reader:
        return
    try:
        while True:
            line = await self._reader.readline()
            if not line:
                logger.info("TCP: connection closed by remote")
                break
            text = line.decode("utf-8").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
                await self.events.put(event)
            except json.JSONDecodeError:
                if text in _KNOWN_NON_JSON:
                    logger.debug("TCP non-JSON (expected): %s", text)
                else:
                    logger.warning("Unexpected non-JSON from Lua: %r", text)
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        self._writer = None
        self._reader = None
        if self.on_disconnect:
            self.on_disconnect()
```

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/session_manager.py python/spinlab/tcp_manager.py
git commit -m "feat: wire EventType enum into dispatch, log unexpected non-JSON lines"
```

---

## Phase 3: Structural Refactors

### Task 11: Estimator and allocator registration cleanup

**Files:**
- Modify: `python/spinlab/estimators/__init__.py`
- Modify: `python/spinlab/allocators/__init__.py`
- Modify: `python/spinlab/scheduler.py:17-26`

- [ ] **Step 1: Add _register_all() to estimators/__init__.py**

At the bottom of `python/spinlab/estimators/__init__.py`, add:

```python
def _register_all():
    """Import all estimator modules to trigger @register_estimator decorators."""
    from . import kalman, rolling_mean
    try:
        from . import exp_decay
    except ImportError:
        pass

_register_all()
```

- [ ] **Step 2: Add _register_all() to allocators/__init__.py**

At the bottom of `python/spinlab/allocators/__init__.py`, add:

```python
def _register_all():
    """Import all allocator modules to trigger @register_allocator decorators."""
    from . import greedy, random, round_robin

_register_all()
```

- [ ] **Step 3: Remove side-effect imports from scheduler.py**

In `python/spinlab/scheduler.py`, delete lines 17-26:

```python
# DELETE these lines:
from spinlab.allocators.greedy import GreedyAllocator  # ensure registered
from spinlab.allocators.random import RandomAllocator
from spinlab.allocators.round_robin import RoundRobinAllocator
from spinlab.estimators.kalman import KalmanEstimator  # noqa: F401 — ensure registered
from spinlab.estimators.rolling_mean import RollingMeanEstimator  # noqa: F401 — ensure registered
try:
    from spinlab.estimators.exp_decay import ExpDecayEstimator  # noqa: F401 — ensure registered
except ImportError:
    logger.warning("exp_decay unavailable (numpy/scipy not installed)")
```

The imports of `get_estimator`, `list_estimators`, `get_allocator`, `list_allocators` from the `__init__.py` modules are sufficient — `_register_all()` fires on import.

- [ ] **Step 4: Run full test suite**

Run: `python -m pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/estimators/__init__.py python/spinlab/allocators/__init__.py python/spinlab/scheduler.py
git commit -m "refactor: explicit estimator/allocator registration, remove side-effect imports"
```

---

### Task 12: Inject db and tcp into CaptureController at construction

**Files:**
- Modify: `python/spinlab/capture_controller.py`
- Modify: `python/spinlab/session_manager.py`

- [ ] **Step 1: Update CaptureController.__init__ to accept db and tcp**

```python
class CaptureController:
    def __init__(self, db: "Database", tcp: "TcpManager") -> None:
        self.db = db
        self.tcp = tcp
        self.ref_capture = ReferenceCapture()
        self.draft = DraftManager()
        self.fill_gap_segment_id: str | None = None
        self.cold_fill_queue: list[dict] = []
        self.cold_fill_current: str | None = None
        self.cold_fill_total: int = 0
```

- [ ] **Step 2: Update all CaptureController methods to use self.db and self.tcp**

Remove `tcp` and `db` from method parameters where they were passed per-call. Keep `mode` and `game_id` as args (they change). Examples:

```python
async def start_reference(self, mode: Mode, game_id: str, data_dir: Path,
                          run_name: str | None = None) -> ActionResult:
    # use self.tcp, self.db instead of args

async def stop_reference(self, mode: Mode) -> ActionResult:
    # use self.tcp

def handle_checkpoint(self, event: dict, game_id: str) -> None:
    self.ref_capture.handle_checkpoint(event, game_id, self.db)

async def handle_cold_fill_spawn(self, event: dict) -> bool:
    # use self.tcp, self.db
```

Go through every method and replace `tcp` → `self.tcp`, `db` → `self.db`.

- [ ] **Step 3: Update SessionManager to pass db and tcp at construction**

In `python/spinlab/session_manager.py`:

```python
self.capture = CaptureController(db, tcp)
```

Update all call sites to remove `self.tcp` and `self.db` args:

```python
# Before:
result = await self.capture.start_reference(self.mode, self.tcp, self.db, ...)
# After:
result = await self.capture.start_reference(self.mode, ...)
```

Apply to all methods: `start_reference`, `stop_reference`, `start_replay`, `stop_replay`, `start_fill_gap`, `save_draft`, `discard_draft`, `handle_checkpoint`, `handle_spawn`, `handle_exit`, `handle_cold_fill_spawn`, `handle_replay_error`, `handle_disconnect`, `handle_fill_gap_spawn`, `start_cold_fill`, `recover_draft`.

- [ ] **Step 4: Run full test suite, fix breakage**

Run: `python -m pytest tests/ -x -q`

Tests that construct `CaptureController()` with no args need updating to `CaptureController(mock_db, mock_tcp)`. Fix session manager tests.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/capture_controller.py python/spinlab/session_manager.py tests/
git commit -m "refactor: inject db and tcp into CaptureController at construction"
```

---

### Task 13: Extract StateBuilder from SessionManager

**Files:**
- Create: `python/spinlab/state_builder.py`
- Modify: `python/spinlab/session_manager.py`
- Test: `tests/test_state_builder.py` (new)

- [ ] **Step 1: Write test for StateBuilder**

Create `tests/test_state_builder.py`:

```python
"""Tests for StateBuilder view-model construction."""
from unittest.mock import MagicMock

from spinlab.models import Mode
from spinlab.state_builder import StateBuilder


class TestStateBuilder:
    def test_idle_state(self):
        db = MagicMock()
        db.get_recent_attempts.return_value = []
        session = MagicMock()
        session.mode = Mode.IDLE
        session.tcp.is_connected = True
        session.game_id = None
        session.game_name = None
        session.capture.sections_captured = 0
        session.capture.get_draft_state.return_value = None
        session.practice_session = None

        builder = StateBuilder(db)
        state = builder.build(session)
        assert state["mode"] == "idle"
        assert state["tcp_connected"] is True
        assert state["game_id"] is None
        assert state["current_segment"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_state_builder.py -v`
Expected: ImportError

- [ ] **Step 3: Create state_builder.py**

Create `python/spinlab/state_builder.py`:

```python
"""StateBuilder — assembles API/SSE state snapshots.

Pure view-model construction. Extracted from SessionManager.get_state()
and _build_practice_state() to separate coordination from presentation.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import Database
    from .session_manager import SessionManager

from .models import Mode, ModelOutput

logger = logging.getLogger(__name__)

RECENT_ATTEMPTS_LIMIT = 8


class StateBuilder:
    """Assembles state snapshots for API and SSE consumers."""

    def __init__(self, db: "Database"):
        self.db = db

    def build(self, session: "SessionManager") -> dict:
        """Full state snapshot — replaces SessionManager.get_state()."""
        base = {
            "mode": session.mode.value,
            "tcp_connected": session.tcp.is_connected,
            "game_id": session.game_id,
            "game_name": session.game_name,
            "current_segment": None,
            "recent": [],
            "session": None,
            "sections_captured": session.capture.sections_captured,
            "allocator_weights": None,
            "estimator": None,
        }

        if session.game_id is None:
            return base

        sched = session._get_scheduler()
        base["allocator_weights"] = {
            alloc.name: int(w) for alloc, w in sched.allocator.entries
        }
        base["estimator"] = sched.estimator.name

        if session.mode == Mode.PRACTICE and session.practice_session:
            self._build_practice_state(base, session, sched)

        if session.mode in (Mode.REFERENCE, Mode.REPLAY):
            base["capture_run_id"] = session.capture.ref_capture.capture_run_id
        if session.mode == Mode.REPLAY:
            base["replay"] = {"rec_path": session.capture.rec_path}

        draft_state = session.capture.get_draft_state()
        if draft_state:
            base["draft"] = draft_state

        if session.mode == Mode.COLD_FILL:
            cf_state = session.capture.get_cold_fill_state()
            if cf_state:
                base["cold_fill"] = cf_state

        base["recent"] = self.db.get_recent_attempts(
            session.game_id, limit=RECENT_ATTEMPTS_LIMIT,
        )
        return base

    def _build_practice_state(self, base: dict, session: "SessionManager", sched) -> None:
        """Populate practice-specific fields into state dict."""
        ps = session.practice_session
        base["session"] = {
            "id": ps.session_id,
            "started_at": ps.started_at,
            "segments_attempted": ps.segments_attempted,
            "segments_completed": ps.segments_completed,
        }
        if ps.current_segment_id:
            segments = self.db.get_all_segments_with_model(session.game_id)
            seg_map = {s["id"]: s for s in segments}
            if ps.current_segment_id in seg_map:
                current_seg = seg_map[ps.current_segment_id]
                current_seg["attempt_count"] = self.db.get_segment_attempt_count(
                    ps.current_segment_id, ps.session_id,
                )
                state_rows = self.db.load_all_model_states_for_segment(
                    ps.current_segment_id,
                )
                model_outputs = {}
                for sr in state_rows:
                    if sr.get("output_json"):
                        try:
                            model_outputs[sr["estimator"]] = ModelOutput.from_dict(
                                json.loads(sr["output_json"])
                            ).to_dict()
                        except (json.JSONDecodeError, KeyError):
                            pass
                current_seg["model_outputs"] = model_outputs
                current_seg["selected_model"] = sched.estimator.name
                base["current_segment"] = current_seg
```

- [ ] **Step 4: Wire StateBuilder into SessionManager**

In `python/spinlab/session_manager.py`, replace `get_state()` and `_build_practice_state()`:

```python
from .state_builder import StateBuilder

# In __init__:
self._state_builder = StateBuilder(db)

# Replace get_state and _build_practice_state:
def get_state(self) -> dict:
    """Full state snapshot for API and SSE."""
    return self._state_builder.build(self)
```

Delete `_build_practice_state` from SessionManager — it now lives in StateBuilder.

Also delete the `RECENT_ATTEMPTS_LIMIT` constant from session_manager.py (it's now in state_builder.py).

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/state_builder.py python/spinlab/session_manager.py tests/test_state_builder.py
git commit -m "refactor: extract StateBuilder from SessionManager for view-model construction"
```

---

### Task 14: Split dashboard.py into routers

**Files:**
- Create: `python/spinlab/routes/__init__.py`, `_deps.py`, `practice.py`, `reference.py`, `model.py`, `segments.py`, `system.py`
- Modify: `python/spinlab/dashboard.py` (slim down)

This is the largest task. The key insight is that all endpoints are simple — they call session/db methods and return JSON. The `Depends` pattern replaces closure captures.

- [ ] **Step 1: Create routes directory and _deps.py**

```bash
mkdir -p python/spinlab/routes
```

Create `python/spinlab/routes/__init__.py` (empty file).

Create `python/spinlab/routes/_deps.py`:

```python
"""FastAPI dependency injection helpers."""
from __future__ import annotations

from fastapi import Request

from spinlab.config import AppConfig
from spinlab.db import Database
from spinlab.session_manager import SessionManager


def get_session(request: Request) -> SessionManager:
    return request.app.state.session


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_config(request: Request) -> AppConfig:
    return request.app.state.config
```

- [ ] **Step 2: Create routes/practice.py**

```python
"""Practice endpoints."""
from fastapi import APIRouter, Depends

from .._deps import get_session  # Note: relative import within routes package
# Actually, _deps is in same package:
from ._deps import get_session
from ..dashboard import _check_result
from ..session_manager import SessionManager

router = APIRouter(prefix="/api", tags=["practice"])


@router.post("/practice/start")
async def practice_start(session: SessionManager = Depends(get_session)):
    return _check_result(await session.start_practice())


@router.post("/practice/stop")
async def practice_stop(session: SessionManager = Depends(get_session)):
    return _check_result(await session.stop_practice())
```

- [ ] **Step 3: Create routes/reference.py**

```python
"""Reference capture and replay endpoints."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request

from ._deps import get_session, get_db
from ..dashboard import _check_result
from ..db import Database
from ..session_manager import SessionManager

router = APIRouter(prefix="/api", tags=["reference"])


@router.post("/reference/start")
async def reference_start(session: SessionManager = Depends(get_session)):
    return _check_result(await session.start_reference())


@router.post("/reference/stop")
async def reference_stop(session: SessionManager = Depends(get_session)):
    return _check_result(await session.stop_reference())


@router.post("/replay/start")
async def replay_start(req: Request, session: SessionManager = Depends(get_session)):
    body = await req.json()
    ref_id = body.get("ref_id")
    speed = body.get("speed", 0)
    if not ref_id:
        raise HTTPException(status_code=400, detail="ref_id required")
    gid = session.game_id or "unknown"
    spinrec_path = str(session.data_dir / gid / "rec" / f"{ref_id}.spinrec")
    return _check_result(await session.start_replay(spinrec_path, speed=speed))


@router.post("/replay/stop")
async def replay_stop(session: SessionManager = Depends(get_session)):
    return _check_result(await session.stop_replay())


@router.get("/references")
def list_references(session: SessionManager = Depends(get_session)):
    gid = session._require_game()
    refs = session.db.list_capture_runs(gid)
    for ref in refs:
        rec_path = session.data_dir / gid / "rec" / f"{ref['id']}.spinrec"
        ref["has_spinrec"] = rec_path.is_file()
    return {"references": refs}


@router.post("/references")
def create_reference(body: dict, session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    import uuid
    run_id = f"ref_{uuid.uuid4().hex[:8]}"
    name = body.get("name", "Untitled")
    db.create_capture_run(run_id, session._require_game(), name)
    return {"id": run_id, "name": name}


@router.post("/references/draft/save")
async def draft_save(req: Request, session: SessionManager = Depends(get_session)):
    body = await req.json()
    name = body.get("name", "Untitled")
    return _check_result(await session.save_draft(name))


@router.post("/references/draft/discard")
async def draft_discard(session: SessionManager = Depends(get_session)):
    return _check_result(await session.discard_draft())


@router.get("/references/{ref_id}/spinrec")
def check_spinrec(ref_id: str, session: SessionManager = Depends(get_session)):
    gid = session.game_id or "unknown"
    rec_path = session.data_dir / gid / "rec" / f"{ref_id}.spinrec"
    if rec_path.is_file():
        return {"exists": True, "path": str(rec_path)}
    return {"exists": False}


@router.patch("/references/{ref_id}")
def rename_reference(ref_id: str, body: dict, db: Database = Depends(get_db)):
    name = body.get("name")
    if name:
        db.rename_capture_run(ref_id, name)
    return {"status": "ok"}


@router.delete("/references/{ref_id}")
def delete_reference(ref_id: str, db: Database = Depends(get_db)):
    db.delete_capture_run(ref_id)
    return {"status": "ok"}


@router.post("/references/{ref_id}/activate")
def activate_reference(ref_id: str, db: Database = Depends(get_db)):
    db.set_active_capture_run(ref_id)
    return {"status": "ok"}


@router.get("/references/{ref_id}/segments")
def get_reference_segments(ref_id: str, db: Database = Depends(get_db)):
    return {"segments": db.get_segments_by_reference(ref_id)}
```

- [ ] **Step 4: Create routes/model.py**

```python
"""Model, allocator, and estimator endpoints."""
import json

from fastapi import APIRouter, Depends, HTTPException

from ._deps import get_session, get_db
from ..db import Database
from ..estimators import get_estimator, list_estimators
from ..session_manager import SessionManager

router = APIRouter(prefix="/api", tags=["model"])


@router.get("/model")
def api_model(session: SessionManager = Depends(get_session)):
    if session.game_id is None:
        return {"estimator": None, "estimators": [], "allocator_weights": None, "segments": []}
    sched = session._get_scheduler()
    segments = sched.get_all_model_states()
    return {
        "estimator": sched.estimator.name,
        "estimators": [
            {"name": n, "display_name": get_estimator(n).display_name or n}
            for n in list_estimators()
        ],
        "allocator_weights": {alloc.name: int(w) for alloc, w in sched.allocator.entries},
        "segments": [
            {
                "segment_id": s.segment_id,
                "description": s.description,
                "level_number": s.level_number,
                "start_type": s.start_type,
                "start_ordinal": s.start_ordinal,
                "end_type": s.end_type,
                "end_ordinal": s.end_ordinal,
                "selected_model": s.selected_model,
                "model_outputs": {
                    name: out.to_dict()
                    for name, out in s.model_outputs.items()
                },
                "n_completed": s.n_completed,
                "n_attempts": s.n_attempts,
                "gold_ms": s.gold_ms,
                "clean_gold_ms": s.clean_gold_ms,
            }
            for s in segments
        ],
    }


@router.post("/allocator-weights")
def set_allocator_weights(body: dict, session: SessionManager = Depends(get_session)):
    sched = session._get_scheduler()
    try:
        sched.set_allocator_weights(body)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"weights": body}


@router.post("/estimator")
def switch_estimator(body: dict, session: SessionManager = Depends(get_session)):
    name = body.get("name")
    valid = list_estimators()
    if name not in valid:
        raise HTTPException(status_code=400, detail=f"Unknown estimator: {name}. Valid: {valid}")
    sched = session._get_scheduler()
    sched.switch_estimator(name)
    return {"estimator": name}


@router.get("/estimator-params")
def get_estimator_params(session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    sched = session._get_scheduler()
    est = sched.estimator
    declared = est.declared_params()
    raw = db.load_allocator_config(f"estimator_params:{est.name}")
    saved = json.loads(raw) if raw else {}
    return {
        "estimator": est.name,
        "params": [
            {**p.to_dict(), "value": saved.get(p.name, p.default)}
            for p in declared
        ],
    }


@router.post("/estimator-params")
def set_estimator_params(body: dict, session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    sched = session._get_scheduler()
    est = sched.estimator
    params = body.get("params", {})
    valid_names = {p.name for p in est.declared_params()}
    for name in params:
        if name not in valid_names:
            raise HTTPException(status_code=400, detail=f"Unknown param: {name}")
    db.save_allocator_config(f"estimator_params:{est.name}", json.dumps(params))
    sched.rebuild_all_states()
    return {"status": "ok"}
```

- [ ] **Step 5: Create routes/segments.py**

```python
"""Segment management endpoints."""
from fastapi import APIRouter, Depends, HTTPException

from ._deps import get_session, get_db
from ..dashboard import _check_result
from ..db import Database
from ..session_manager import SessionManager

router = APIRouter(prefix="/api", tags=["segments"])


@router.get("/segments")
def api_segments(session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    segments = db.get_all_segments_with_model(session._require_game())
    return {"segments": segments}


@router.patch("/segments/{segment_id}")
def update_segment_endpoint(segment_id: str, body: dict, db: Database = Depends(get_db)):
    if not db.segment_exists(segment_id):
        raise HTTPException(status_code=404, detail="Segment not found")
    db.update_segment(segment_id, **body)
    return {"status": "ok"}


@router.delete("/segments/{segment_id}")
def delete_segment(segment_id: str, db: Database = Depends(get_db)):
    if not db.segment_exists(segment_id):
        raise HTTPException(status_code=404, detail="Segment not found")
    db.soft_delete_segment(segment_id)
    return {"status": "ok"}


@router.post("/segments/{segment_id}/fill-gap")
async def fill_gap(segment_id: str, session: SessionManager = Depends(get_session)):
    return _check_result(await session.start_fill_gap(segment_id))
```

- [ ] **Step 6: Create routes/system.py**

```python
"""System endpoints: state, SSE, ROMs, emulator, reset, shutdown."""
import asyncio
import json
import signal
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import StreamingResponse

from ._deps import get_session, get_db, get_config
from ..config import AppConfig
from ..db import Database
from ..models import Mode
from ..session_manager import SessionManager

SSE_KEEPALIVE_S = 30

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/state")
def api_state(session: SessionManager = Depends(get_session)):
    return session.get_state()


@router.get("/events")
async def sse_events(session: SessionManager = Depends(get_session)):
    queue = session.subscribe_sse()

    async def event_stream():
        try:
            while True:
                try:
                    state = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_S)
                    yield f"data: {json.dumps(state)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            session.unsubscribe_sse(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/sessions")
def api_sessions(session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    sessions = db.get_session_history(session._require_game())
    return {"sessions": sessions}


@router.post("/reset")
async def reset_data(session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    await session.stop_practice()
    if session.mode == Mode.REFERENCE:
        session._clear_ref_and_idle()
    gid = session.game_id
    if gid:
        db.reset_game_data(gid)
    session.scheduler = None
    session.mode = Mode.IDLE
    return {"status": "ok"}


@router.get("/roms")
def list_roms(config: AppConfig = Depends(get_config)):
    rom_dir = config.rom_dir
    if not rom_dir or not rom_dir.is_dir():
        return {"roms": [], "error": f"ROM directory not found: {rom_dir}"}
    exts = {".sfc", ".smc", ".fig", ".swc"}
    roms = sorted(
        [p.name for p in rom_dir.iterdir() if p.suffix.lower() in exts],
        key=str.lower,
    )
    return {"roms": roms}


@router.post("/emulator/launch")
def launch_emulator(body: dict | None = None, config: AppConfig = Depends(get_config)):
    emu_path = config.emulator.path
    if not emu_path or not emu_path.exists():
        raise HTTPException(status_code=400, detail=f"Emulator not found: {emu_path}")

    rom_name = (body or {}).get("rom", "")
    if rom_name and config.rom_dir:
        rom_path = config.rom_dir / rom_name
    else:
        rom_path = Path("")  # will fail the is_file check

    if config.rom_dir:
        resolved_rom = rom_path.resolve()
        resolved_dir = config.rom_dir.resolve()
        if not str(resolved_rom).startswith(str(resolved_dir)):
            raise HTTPException(status_code=400, detail="ROM path outside rom_dir")

    if not rom_path.is_file():
        raise HTTPException(status_code=400, detail=f"ROM not found: {rom_path}")

    cmd = [str(emu_path), str(rom_path)]
    lua_script = config.emulator.lua_script
    if lua_script:
        script_path = lua_script if lua_script.is_absolute() else Path.cwd() / lua_script
        if script_path.exists():
            cmd.append(str(script_path))
    subprocess.Popen(cmd)
    return {"status": "ok"}


@router.post("/import-manifest")
def import_manifest(body: dict, session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    import yaml
    from spinlab.manifest import seed_db_from_manifest
    manifest_path = Path(body["path"])
    with manifest_path.open(encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    game_name = manifest.get("game_id", session.game_id or "unknown")
    seed_db_from_manifest(db, manifest, game_name)
    return {"status": "ok", "segments_imported": len(manifest.get("segments", manifest.get("splits", [])))}


@router.post("/shutdown")
async def api_shutdown(session: SessionManager = Depends(get_session)):
    await session.shutdown()
    try:
        signal.raise_signal(signal.SIGINT)
    except (OSError, AttributeError):
        pass
    return {"status": "shutting_down"}
```

- [ ] **Step 7: Slim down dashboard.py**

Replace `python/spinlab/dashboard.py` with:

```python
"""SpinLab dashboard — FastAPI web app assembly."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from .config import AppConfig
from .db import Database
from .models import ActionResult, Status
from .session_manager import SessionManager
from .tcp_manager import TcpManager

logger = logging.getLogger(__name__)

TCP_CONNECT_TIMEOUT_S = 2
TCP_RETRY_DELAY_S = 2
TCP_EVENT_TIMEOUT_S = 1.0

_ERROR_STATUS_CODES: dict[Status, int] = {
    Status.NOT_CONNECTED: 503,
    Status.DRAFT_PENDING: 409,
    Status.PRACTICE_ACTIVE: 409,
    Status.REFERENCE_ACTIVE: 409,
    Status.ALREADY_RUNNING: 409,
    Status.ALREADY_REPLAYING: 409,
    Status.NOT_IN_REFERENCE: 409,
    Status.NOT_REPLAYING: 409,
    Status.NOT_RUNNING: 409,
    Status.NO_DRAFT: 404,
    Status.NO_HOT_VARIANT: 404,
}


def _check_result(result: ActionResult) -> dict:
    """Convert ActionResult to API response, raising HTTPException for errors."""
    code = _ERROR_STATUS_CODES.get(result.status)
    if code:
        raise HTTPException(status_code=code, detail=result.status.value)
    return result.to_response()


async def event_loop(session: SessionManager, tcp: TcpManager) -> None:
    """Bridge TCP events to SessionManager."""
    while True:
        if not tcp.is_connected:
            await tcp.connect(timeout=TCP_CONNECT_TIMEOUT_S)
            if not tcp.is_connected:
                await asyncio.sleep(TCP_RETRY_DELAY_S)
                continue
        try:
            event = await tcp.recv_event(timeout=TCP_EVENT_TIMEOUT_S)
            if event:
                await session.route_event(event)
        except Exception:
            logger.exception("Error in event loop")
            await asyncio.sleep(1)


def create_app(db: Database, config: AppConfig) -> FastAPI:
    tcp = TcpManager(config.network.host, config.network.port)
    session = SessionManager(
        db, tcp, config.rom_dir, config.category, data_dir=config.data_dir,
    )
    tcp.on_disconnect = session.on_disconnect

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(event_loop(session, tcp))
        yield
        task.cancel()
        await session.shutdown()

    app = FastAPI(title="SpinLab Dashboard", lifespan=lifespan)
    app.state.config = config
    app.state.tcp = tcp
    app.state.session = session
    app.state.db = db

    # Static files
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    class NoCacheStaticMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            if request.url.path.startswith("/static/"):
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
            return response

    app.add_middleware(NoCacheStaticMiddleware)

    @app.get("/")
    def root():
        return FileResponse(str(static_dir / "index.html"))

    # Mount routers
    from .routes.practice import router as practice_router
    from .routes.reference import router as reference_router
    from .routes.model import router as model_router
    from .routes.segments import router as segments_router
    from .routes.system import router as system_router

    app.include_router(practice_router)
    app.include_router(reference_router)
    app.include_router(model_router)
    app.include_router(segments_router)
    app.include_router(system_router)

    return app
```

- [ ] **Step 8: Run full test suite**

Run: `python -m pytest tests/ -x -q`

Fix any import issues. The main risk is tests that imported internals from `dashboard.py`. Update them to import from the router modules or use the test client as before.

- [ ] **Step 9: Commit**

```bash
git add python/spinlab/dashboard.py python/spinlab/routes/ tests/
git commit -m "refactor: split dashboard.py into FastAPI routers with dependency injection"
```

---

## Phase 4: Cross-Component & Tests

### Task 15: Lua address map dedup

**Files:**
- Create: `lua/addresses.lua`
- Modify: `lua/spinlab.lua:44-55`
- Modify: `lua/poke_engine.lua:22-34`
- Modify: `tests/integration/addresses.py`

- [ ] **Step 1: Create lua/addresses.lua**

```lua
-- addresses.lua — Single source of truth for SNES memory addresses.
-- Both spinlab.lua and poke_engine.lua load this via dofile().
-- Tests parse this file at import time (tests/integration/addresses.py).
--
-- Ported from kaizosplits/Memory.cs

ADDR_GAME_MODE     = 0x0100   -- game mode: 18=prepare level, 20=in level
ADDR_LEVEL_NUM     = 0x13BF   -- current level number
ADDR_ROOM_NUM      = 0x010B   -- current room/sublevel
ADDR_LEVEL_START   = 0x1935   -- 0->1 when player appears in level
ADDR_PLAYER_ANIM   = 0x0071   -- player animation: 9=death
ADDR_EXIT_MODE     = 0x0DD5   -- 0=not exiting, non-zero=exiting level
ADDR_IO            = 0x1DFB   -- SPC I/O: 3=orb, 4=goal, 7=key, 8=fadeout
ADDR_FANFARE       = 0x0906   -- steps to 1 when goal reached
ADDR_BOSS_DEFEAT   = 0x13C6   -- 0=alive, non-zero=defeated
ADDR_MIDWAY        = 0x13CE   -- midway checkpoint tape: 0->1 when touched
ADDR_CP_ENTRANCE   = 0x1B403  -- ASM-style checkpoint entrance
```

- [ ] **Step 2: Update spinlab.lua to use dofile**

In `lua/spinlab.lua`, replace lines 44-55 (the address constants) with:

```lua
-- Memory addresses — loaded from shared source of truth
local script_dir_for_addr = debug.getinfo(1, "S").source:match("@?(.*[\\/])") or ""
dofile(script_dir_for_addr .. "addresses.lua")
```

Note: `script_dir` may not be defined yet at this point in spinlab.lua. Use `debug.getinfo` inline or define it earlier. Check existing code — line 168 of poke_engine.lua does `local script_dir = debug.getinfo(1, "S").source:match("@?(.*[\\/])")`.

- [ ] **Step 3: Update poke_engine.lua to use dofile**

In `lua/poke_engine.lua`, replace lines 19-34 (the address map section) with:

```lua
-----------------------------------------------------------------------
-- ADDRESS MAP (loaded from shared source of truth)
-----------------------------------------------------------------------
local pe_script_dir = debug.getinfo(1, "S").source:match("@?(.*[\\/])") or ""
dofile(pe_script_dir .. "addresses.lua")

local ADDR_MAP = {
  game_mode    = ADDR_GAME_MODE,
  level_num    = ADDR_LEVEL_NUM,
  room_num     = ADDR_ROOM_NUM,
  level_start  = ADDR_LEVEL_START,
  player_anim  = ADDR_PLAYER_ANIM,
  exit_mode    = ADDR_EXIT_MODE,
  io_port      = ADDR_IO,
  fanfare      = ADDR_FANFARE,
  boss_defeat  = ADDR_BOSS_DEFEAT,
  midway       = ADDR_MIDWAY,
  cp_entrance  = ADDR_CP_ENTRANCE,
}
```

This preserves `ADDR_MAP` as a table (poke_engine uses it for iteration in `reset_poke_state`), but the values come from the shared file.

- [ ] **Step 4: Update tests/integration/addresses.py to parse Lua**

Replace `tests/integration/addresses.py`:

```python
"""SNES memory address constants — parsed from lua/addresses.lua (single source of truth)."""
import re
from pathlib import Path

_LUA_FILE = Path(__file__).resolve().parents[2] / "lua" / "addresses.lua"

ADDR_MAP: dict[str, int] = {}

for line in _LUA_FILE.read_text().splitlines():
    m = re.match(r"(ADDR_\w+)\s*=\s*(0x[0-9a-fA-F]+)", line)
    if m:
        # Convert ADDR_GAME_MODE -> game_mode
        key = m.group(1).replace("ADDR_", "").lower()
        ADDR_MAP[key] = int(m.group(2), 16)
```

- [ ] **Step 5: Run unit tests (integration tests need emulator)**

Run: `python -m pytest tests/ -x -q -m "not integration"`
Expected: All pass

Manually verify `addresses.py` parses correctly:
Run: `python -c "from tests.integration.addresses import ADDR_MAP; print(ADDR_MAP)"`
Expected: `{'game_mode': 256, 'level_num': 5055, ...}`

- [ ] **Step 6: Commit**

```bash
git add lua/addresses.lua lua/spinlab.lua lua/poke_engine.lua tests/integration/addresses.py
git commit -m "refactor: deduplicate Lua address maps into single addresses.lua source of truth"
```

---

### Task 16: Wire test factories into existing tests

**Files:**
- Modify: `tests/test_kalman.py`, `tests/test_exp_decay.py`, `tests/test_rolling_mean.py`, `tests/test_estimator_sanity.py`, `tests/test_estimator_params.py`, `tests/test_allocators.py`, `tests/test_mix_allocator.py`

- [ ] **Step 1: Update estimator test files to use factories**

In each of `test_kalman.py`, `test_exp_decay.py`, `test_rolling_mean.py`, `test_estimator_sanity.py`, `test_estimator_params.py`:

- Remove local `_attempt()` and `_incomplete()` functions
- Add: `from tests.factories import make_attempt_record, make_incomplete`
- Replace all calls: `_attempt(12000, True)` → `make_attempt_record(12000, True)`
- Replace: `_incomplete()` → `make_incomplete()`

- [ ] **Step 2: Update allocator test files to use factories**

In `test_allocators.py` and `test_mix_allocator.py`:

- Remove local `_make_segment()` functions
- Add: `from tests.factories import make_segment_with_model`
- Replace calls, matching parameters

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "refactor: consolidate test helpers into tests/factories.py"
```

---

### Task 17: Conftest improvements and parametrized estimator fixture

**Files:**
- Modify: `tests/conftest.py`

- [ ] **Step 1: Add shared fixtures to conftest.py**

Add to `tests/conftest.py`:

```python
import pytest
from spinlab.estimators import list_estimators, get_estimator


@pytest.fixture(params=list_estimators())
def estimator_name(request):
    """Parametrized fixture that yields each registered estimator name."""
    return request.param


@pytest.fixture
def estimator(estimator_name):
    """Instantiated estimator from parametrized name."""
    return get_estimator(estimator_name)
```

- [ ] **Step 2: Run full test suite**

Run: `python -m pytest tests/ -x -q`
Expected: All pass (new fixtures don't break anything, they're opt-in)

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "feat: add parametrized estimator fixtures to conftest.py"
```

---

### Task 18: Improve test behavior focus (Kalman state → model_output)

**Files:**
- Modify: `tests/test_kalman.py` (specific tests that assert on state.mu)

- [ ] **Step 1: Identify and rewrite internal-state tests**

In `tests/test_kalman.py`, find tests that assert `state.mu == ...` or `state.d == ...` and rewrite them to test through `model_output()`:

For example, if a test does:
```python
state = est.init_state(attempt, priors={})
assert state.mu == pytest.approx(12.0)
```

Rewrite to:
```python
state = est.init_state(attempt, priors={})
output = est.model_output(state, [attempt])
assert output.total.expected_ms == pytest.approx(12000.0, rel=0.1)
```

Not every internal assertion needs changing — tests for `rebuild_state` round-trip or specific filter math are fine. Focus on tests where the public contract (`model_output`) is a better assertion target.

- [ ] **Step 2: Run Kalman tests**

Run: `python -m pytest tests/test_kalman.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add tests/test_kalman.py
git commit -m "test: rewrite Kalman tests to assert on model_output instead of internal state"
```

---

### Task 19: Final integration test run and cleanup

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -x -v`
Expected: All pass

- [ ] **Step 2: Run with coverage**

Run: `python -m pytest tests/ --cov=spinlab --cov-report=term-missing -q`
Check for any significant coverage regressions.

- [ ] **Step 3: Clean up any unused imports across modified files**

Run: `python -m py_compile python/spinlab/models.py python/spinlab/dashboard.py python/spinlab/session_manager.py python/spinlab/capture_controller.py`
Expected: No errors

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "chore: final cleanup from architectural refactor"
```

---

## Follow-up (not in this plan)

- **Dashboard mock test rewrite (spec 4c):** Replace `session.save_draft = AsyncMock(return_value=...)` patterns in `test_dashboard_integration.py` with real SessionManager + real Database + mocked TCP only. This is a substantial rewrite best done as a separate effort after the structural refactors land.
- **Move `_sync_switch()` to conftest:** Currently defined in `test_dashboard_integration.py` and `test_multi_game.py`. Move to `tests/conftest.py` as a shared fixture after dashboard router split stabilizes.

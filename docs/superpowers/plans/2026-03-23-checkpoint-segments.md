# Checkpoint Segments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the split model with a segment model that supports checkpoint-based sub-sections within levels, with cold/hot start variants and a fill-gaps capture flow.

**Architecture:** Two-phase approach: (1) mechanical rename of split→segment throughout the codebase with schema changes, (2) add checkpoint-specific features (new Lua detection, segment variants, fill-gaps flow). All existing data is deleted — no migration needed.

**Tech Stack:** Python 3.11+ (FastAPI, SQLite, dataclasses), Lua (Mesen2 LuaSocket), vanilla JS ES modules.

**Spec:** `docs/superpowers/specs/2026-03-23-checkpoint-segments-design.md`

**Notes:**
- `reference_time_ms` is removed from the segment model. The Kalman filter initializes from first completed attempt — no reference time seed. For new segments with no attempts, no timer comparison is shown on the overlay.
- `capture_runs` table and reference management endpoints are unchanged.

---

## File Structure

### New files
- `tests/test_segment_variants.py` — tests for segment_variants DB operations and fill-gap logic

### Modified files (by layer)

**Data model:**
- `python/spinlab/models.py` — `Split`→`Segment`, `SplitCommand`→`SegmentCommand`, `Attempt.split_id`→`segment_id`, new `SegmentVariant` dataclass, add `SPAWN` to `TransitionEvent`
- `python/spinlab/allocators/__init__.py` — `SplitWithModel`→`SegmentWithModel`, method params renamed
- `python/spinlab/allocators/greedy.py` — param/var rename
- `python/spinlab/allocators/random.py` — param/var rename
- `python/spinlab/allocators/round_robin.py` — param/var rename

**Database:**
- `python/spinlab/db.py` — drop+recreate `splits`→`segments` table with new columns, add `segment_variants` table, rename all methods and FKs, add variant CRUD

**Core logic:**
- `python/spinlab/scheduler.py` — `_load_splits_with_model`→`_load_segments_with_model`, all var renames, state_path resolved from variants
- `python/spinlab/practice.py` — `SplitCommand`→`SegmentCommand`, `current_split_id`→`current_segment_id`, counter renames
- `python/spinlab/session_manager.py` — rename refs, add `ref_pending_start` structure, add checkpoint/death/spawn event handling, add fill-gap mode
- `python/spinlab/manifest.py` — `Split`→`Segment`, method renames

**API:**
- `python/spinlab/dashboard.py` — `/api/splits`→`/api/segments`, response key renames, new `/api/segments/{id}/fill-gap` endpoint

**Frontend:**
- `python/spinlab/static/format.js` — `splitName`→`segmentName`
- `python/spinlab/static/live.js` — `current_split`→`current_segment`, function renames
- `python/spinlab/static/manage.js` — new flat segment table with State column, fill-gap trigger
- `python/spinlab/static/model.js` — rename refs
- `python/spinlab/static/index.html` — rename DOM element IDs (`split-body`→`segment-body`, table headers)
- `python/spinlab/static/app.js` — verify no split-specific keys in SSE handling (update if needed)

**Lua:**
- `lua/spinlab.lua` — new memory addresses ($13CE, $1B403), checkpoint/death/spawn detection, state tracking (died, firstRoom, cp_ordinal), practice end-condition for checkpoint segments

**Tests (rename + new):**
- `tests/test_allocators.py` — `SplitWithModel`→`SegmentWithModel`
- `tests/test_dashboard_integration.py` — endpoint renames, response key renames
- `tests/test_dashboard_references.py` — endpoint renames
- `tests/test_db_dashboard.py` — method renames
- `tests/test_db_references.py` — method renames
- `tests/test_dashboard.py` — key renames
- `tests/test_practice.py` — class/var renames
- `tests/test_scheduler_kalman.py` — var renames
- `tests/test_session_manager.py` — var/key renames, new checkpoint event tests
- `tests/test_multi_game.py` — var renames
- `tests/test_segment_variants.py` — NEW: variant CRUD, fill-gap logic

---

## Task 1: Data Models — Segment, SegmentCommand, SegmentVariant

**Files:**
- Modify: `python/spinlab/models.py`

- [ ] **Step 1: Update models.py**

Replace `Split` with `Segment`, `SplitCommand` with `SegmentCommand`, add `SegmentVariant`, update `Attempt`, add `SPAWN`:

```python
"""SpinLab data models."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Optional


class TransitionEvent(str):
    LEVEL_START = "level_start"
    ROOM_CHANGE = "room_change"
    DEATH = "death"
    GOAL = "goal"
    CHECKPOINT = "checkpoint"
    SPAWN = "spawn"


@dataclass
class Segment:
    id: str
    game_id: str
    level_number: int
    start_type: str          # 'entrance', 'checkpoint'
    start_ordinal: int
    end_type: str            # 'checkpoint', 'goal'
    end_ordinal: int
    description: str = ""
    strat_version: int = 1
    active: bool = True
    ordinal: Optional[int] = None
    reference_id: Optional[str] = None

    @staticmethod
    def make_id(game_id: str, level: int, start_type: str, start_ord: int,
                end_type: str, end_ord: int) -> str:
        return f"{game_id}:{level}:{start_type}.{start_ord}:{end_type}.{end_ord}"


@dataclass
class SegmentVariant:
    segment_id: str
    variant_type: str        # 'cold', 'hot'
    state_path: str
    is_default: bool = False


@dataclass
class Attempt:
    segment_id: str
    session_id: str
    completed: bool
    time_ms: int | None = None
    goal_matched: bool | None = None
    rating: str | None = None
    strat_version: int = 1
    source: str = "practice"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class SegmentCommand:
    """Sent from orchestrator to Lua: which segment to load next."""
    id: str
    state_path: str
    description: str
    end_type: str              # 'checkpoint' or 'goal'
    expected_time_ms: int | None = None
    auto_advance_delay_ms: int = 2000

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "state_path": self.state_path,
            "description": self.description,
            "end_type": self.end_type,
            "expected_time_ms": self.expected_time_ms,
            "auto_advance_delay_ms": self.auto_advance_delay_ms,
        }
```

- [ ] **Step 2: Quick smoke test**

Run: `python -c "from spinlab.models import Segment, SegmentVariant, SegmentCommand, Attempt; print('OK')"`

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/models.py
git commit -m "feat: replace Split/SplitCommand with Segment/SegmentCommand/SegmentVariant"
```

---

## Task 2: Database Schema — segments + segment_variants

**Files:**
- Modify: `python/spinlab/db.py`
- Test: `tests/test_db_dashboard.py`, `tests/test_db_references.py`, `tests/test_segment_variants.py` (new)

- [ ] **Step 1: Write failing tests for new segment DB operations**

Create `tests/test_segment_variants.py`:

```python
"""Tests for segment_variants DB operations."""
import pytest
from spinlab.db import Database
from spinlab.models import Segment, SegmentVariant


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_game("g1", "Test Game", "any%")
    return d


@pytest.fixture
def segment(db):
    s = Segment(
        id="g1:105:entrance.0:checkpoint.1",
        game_id="g1",
        level_number=105,
        start_type="entrance",
        start_ordinal=0,
        end_type="checkpoint",
        end_ordinal=1,
        description="entrance → cp.1",
    )
    db.upsert_segment(s)
    return s


def test_upsert_and_get_segment(db, segment):
    segments = db.get_active_segments("g1")
    assert len(segments) == 1
    assert segments[0].id == segment.id
    assert segments[0].start_type == "entrance"
    assert segments[0].end_type == "checkpoint"


def test_add_variant(db, segment):
    v = SegmentVariant(
        segment_id=segment.id,
        variant_type="cold",
        state_path="/states/105_entrance.mss",
        is_default=True,
    )
    db.add_variant(v)
    variants = db.get_variants(segment.id)
    assert len(variants) == 1
    assert variants[0].variant_type == "cold"
    assert variants[0].is_default is True


def test_add_variant_replace(db, segment):
    """INSERT OR REPLACE: re-adding same variant type overwrites."""
    v1 = SegmentVariant(segment.id, "cold", "/old.mss", True)
    db.add_variant(v1)
    v2 = SegmentVariant(segment.id, "cold", "/new.mss", True)
    db.add_variant(v2)
    variants = db.get_variants(segment.id)
    assert len(variants) == 1
    assert variants[0].state_path == "/new.mss"


def test_get_default_variant(db, segment):
    db.add_variant(SegmentVariant(segment.id, "hot", "/hot.mss", False))
    db.add_variant(SegmentVariant(segment.id, "cold", "/cold.mss", True))
    default = db.get_default_variant(segment.id)
    assert default is not None
    assert default.variant_type == "cold"


def test_get_default_variant_fallback(db, segment):
    """If no variant marked default, return any available variant."""
    db.add_variant(SegmentVariant(segment.id, "hot", "/hot.mss", False))
    default = db.get_default_variant(segment.id)
    assert default is not None
    assert default.variant_type == "hot"


def test_get_variant_by_type(db, segment):
    db.add_variant(SegmentVariant(segment.id, "hot", "/hot.mss", False))
    db.add_variant(SegmentVariant(segment.id, "cold", "/cold.mss", True))
    hot = db.get_variant(segment.id, "hot")
    assert hot is not None
    assert hot.state_path == "/hot.mss"
    missing = db.get_variant(segment.id, "nonexistent")
    assert missing is None


def test_segments_with_model_includes_state_path(db, segment):
    db.add_variant(SegmentVariant(segment.id, "cold", "/cold.mss", True))
    rows = db.get_all_segments_with_model("g1")
    assert len(rows) == 1
    assert rows[0]["state_path"] == "/cold.mss"
    assert rows[0]["start_type"] == "entrance"
    assert rows[0]["end_type"] == "checkpoint"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_segment_variants.py -v`
Expected: FAIL — `upsert_segment` not found

- [ ] **Step 3: Rewrite db.py schema and methods**

Full rewrite of `db.py`. Key changes:
- Drop `splits` table, create `segments` table with `start_type`, `start_ordinal`, `end_type`, `end_ordinal` columns
- Create `segment_variants` table
- Rename all methods: `upsert_split`→`upsert_segment`, `get_active_splits`→`get_active_segments`, etc.
- `model_state` PK column: `split_id`→`segment_id`
- `attempts` FK: `split_id`→`segment_id`
- `sessions`: `splits_attempted`→`segments_attempted`, `splits_completed`→`segments_completed`
- Add variant CRUD: `add_variant()`, `get_variants()`, `get_variant(segment_id, variant_type)`, `get_default_variant()`
- `get_all_segments_with_model()`: use subquery to get default variant's state_path (with fallback to any variant)
- `get_segments_by_reference()`: replaces `get_splits_by_reference()`
- `_row_to_segment()`: replaces `_row_to_split()`
- Drop all old migration code (midway columns, schedule table, etc.) — clean slate

For `get_all_segments_with_model()`, use a subquery for state_path to avoid row duplication:
```sql
SELECT s.id, s.game_id, s.level_number, s.start_type, s.start_ordinal,
       s.end_type, s.end_ordinal, s.description, s.strat_version,
       s.active, s.ordinal,
       (SELECT sv.state_path FROM segment_variants sv
        WHERE sv.segment_id = s.id
        ORDER BY sv.is_default DESC LIMIT 1) AS state_path,
       m.estimator, m.state_json, m.marginal_return
FROM segments s
LEFT JOIN model_state m ON s.id = m.segment_id
WHERE s.game_id = ? AND s.active = 1
ORDER BY s.ordinal, s.level_number
```

- [ ] **Step 4: Run new variant tests**

Run: `pytest tests/test_segment_variants.py -v`
Expected: PASS

- [ ] **Step 5: Update test_db_dashboard.py**

Rename all `split` references to `segment`. Update test fixtures to create `Segment` objects. Update method calls (`upsert_split`→`upsert_segment`, etc.). Update assertions for new column names in query results.

The key fixture change — segments need `start_type`, `start_ordinal`, `end_type`, `end_ordinal` instead of `room_id` and `goal`:

```python
def _make_segment(db, game_id, level, start_type="entrance", start_ord=0,
                  end_type="goal", end_ord=0, desc="", ordinal=1, ref_id=None):
    seg = Segment(
        id=Segment.make_id(game_id, level, start_type, start_ord, end_type, end_ord),
        game_id=game_id, level_number=level,
        start_type=start_type, start_ordinal=start_ord,
        end_type=end_type, end_ordinal=end_ord,
        description=desc, ordinal=ordinal, reference_id=ref_id,
    )
    db.upsert_segment(seg)
    return seg
```

- [ ] **Step 6: Update test_db_references.py**

Same pattern: rename split→segment in fixtures, method calls, assertions.

- [ ] **Step 7: Run all DB tests**

Run: `pytest tests/test_db_dashboard.py tests/test_db_references.py tests/test_segment_variants.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/db.py tests/test_segment_variants.py tests/test_db_dashboard.py tests/test_db_references.py
git commit -m "feat: segments + segment_variants DB schema, drop splits table"
```

---

## Task 3: Allocators — SplitWithModel → SegmentWithModel

**Files:**
- Modify: `python/spinlab/allocators/__init__.py`, `python/spinlab/allocators/greedy.py`, `python/spinlab/allocators/random.py`, `python/spinlab/allocators/round_robin.py`
- Test: `tests/test_allocators.py`

- [ ] **Step 1: Update allocators/__init__.py**

Rename `SplitWithModel` to `SegmentWithModel`. Update fields:
- `split_id` → `segment_id`
- Remove `room_id`, `goal`, `end_on_goal`, `reference_time_ms`
- Add `start_type`, `start_ordinal`, `end_type`, `end_ordinal`

```python
@dataclass
class SegmentWithModel:
    """Segment metadata combined with estimator output."""
    segment_id: str
    game_id: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int
    description: str
    strat_version: int
    state_path: str | None
    active: bool
    # Estimator output
    estimator_state: EstimatorState | None = None
    marginal_return: float = 0.0
    drift_info: dict = field(default_factory=dict)
    n_completed: int = 0
    n_attempts: int = 0
    gold_ms: int | None = None
```

Rename method params in `Allocator` base class: `split_states` → `segment_states`, return type comments updated.

- [ ] **Step 2: Update greedy.py, random.py, round_robin.py**

Mechanical rename: `split_states`→`segment_states`, `split`→`segment` in local vars, `s.split_id`→`s.segment_id`.

- [ ] **Step 3: Update test_allocators.py**

Rename all `SplitWithModel`→`SegmentWithModel`, `split_id`→`segment_id`, remove `room_id`/`goal`/`end_on_goal`/`reference_time_ms` from test fixtures, add `start_type`/`start_ordinal`/`end_type`/`end_ordinal`.

- [ ] **Step 4: Run allocator tests**

Run: `pytest tests/test_allocators.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/allocators/ tests/test_allocators.py
git commit -m "refactor: SplitWithModel → SegmentWithModel in allocators"
```

---

## Task 4: Scheduler — rename + resolve state_path from variants

**Files:**
- Modify: `python/spinlab/scheduler.py`
- Test: `tests/test_scheduler_kalman.py`

- [ ] **Step 1: Update scheduler.py**

Key changes:
- `_load_splits_with_model` → `_load_segments_with_model`
- Build `SegmentWithModel` with new fields from DB rows
- `state_path` now comes from `get_all_segments_with_model()` query (already resolves from variants)
- All local var names: `splits`→`segments`, `split_id`→`segment_id`, `practicable` stays
- `pick_next()` returns `SegmentWithModel | None`
- `get_all_model_states()` → `get_all_model_states()` (name stays, type changes)
- `rebuild_all_states()`: `get_all_splits_with_model`→`get_all_segments_with_model`, `get_split_attempts`→`get_segment_attempts`

- [ ] **Step 2: Update test_scheduler_kalman.py**

Rename fixtures and assertions. The test data needs to create segments via `upsert_segment` + `add_variant` instead of `upsert_split` with `state_path`. Update `SplitWithModel`→`SegmentWithModel` references.

- [ ] **Step 3: Run scheduler tests**

Run: `pytest tests/test_scheduler_kalman.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/scheduler.py tests/test_scheduler_kalman.py
git commit -m "refactor: scheduler uses SegmentWithModel, resolves state_path from variants"
```

---

## Task 5: Practice Session — SegmentCommand + rename

**Files:**
- Modify: `python/spinlab/practice.py`
- Test: `tests/test_practice.py`

- [ ] **Step 1: Update practice.py**

Key changes:
- Import `SegmentCommand` instead of `SplitCommand`
- `current_split_id` → `current_segment_id`
- `splits_attempted` → `segments_attempted`
- `splits_completed` → `segments_completed`
- Build `SegmentCommand` in `run_one()`:
  ```python
  cmd = SegmentCommand(
      id=picked.segment_id,
      state_path=picked.state_path,
      description=picked.description,
      end_type=picked.end_type,
      expected_time_ms=expected_time_ms,
      auto_advance_delay_ms=self.auto_advance_delay_ms,
  )
  ```
- `_process_result()`: `split_id`→`segment_id` in Attempt and scheduler call
- `stop()`: use renamed counter fields

- [ ] **Step 2: Update test_practice.py**

Rename all refs. Update mock/fixture data to use `SegmentWithModel` fields.

- [ ] **Step 3: Run practice tests**

Run: `pytest tests/test_practice.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/practice.py tests/test_practice.py
git commit -m "refactor: practice session uses SegmentCommand, rename split→segment"
```

---

## Task 6: Session Manager — rename + checkpoint event routing

**Files:**
- Modify: `python/spinlab/session_manager.py`
- Test: `tests/test_session_manager.py`

- [ ] **Step 1: Write failing test for checkpoint event handling**

Add to `tests/test_session_manager.py`:

```python
async def test_reference_checkpoint_creates_segment(session_mgr):
    """Checkpoint event during reference creates entrance→cp segment."""
    # Start reference
    await session_mgr.start_reference()
    # Simulate entrance
    await session_mgr.route_event({
        "event": "level_entrance",
        "level": 105,
        "room": 0,
        "state_path": "/states/105_entrance.mss",
    })
    # Simulate checkpoint
    await session_mgr.route_event({
        "event": "checkpoint",
        "level_num": 105,
        "cp_type": "midway",
        "cp_ordinal": 1,
        "timestamp_ms": 5000,
        "state_path": "/states/105_cp1_hot.mss",
    })
    # Should have created entrance.0→checkpoint.1 segment
    segments = session_mgr.db.get_active_segments(session_mgr.game_id)
    assert len(segments) == 1
    assert segments[0].start_type == "entrance"
    assert segments[0].end_type == "checkpoint"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_session_manager.py::test_reference_checkpoint_creates_segment -v`
Expected: FAIL

- [ ] **Step 3: Update session_manager.py**

Rename all split refs:
- `ref_splits_count` → `ref_segments_count`
- `current_split` → `current_segment` in `get_state()` response
- `splits_attempted`/`splits_completed` → `segments_attempted`/`segments_completed`

Add `ref_pending_start` tracking:
```python
self.ref_pending_start: dict | None = None
# Structure: {"type": "entrance"|"checkpoint", "ordinal": int,
#             "state_path": str, "timestamp_ms": int, "level_num": int}
```

Add checkpoint event handling in `route_event()`:
```python
if evt_type == "checkpoint" and self.mode == "reference":
    await self._handle_ref_checkpoint(event)
    return

if evt_type == "death" and self.mode == "reference":
    self.ref_died = True
    return

if evt_type == "spawn" and self.mode == "reference":
    await self._handle_ref_spawn(event)
    return
```

Implement `_handle_ref_checkpoint()`:
- Create segment from `ref_pending_start` → this checkpoint
- Set new `ref_pending_start` to this checkpoint
- Store hot variant via `db.add_variant()`

Update `_handle_ref_exit()` to use `ref_pending_start` instead of `ref_pending`:
- Create segment from `ref_pending_start` → goal
- Clear `ref_pending_start`

Implement `_handle_ref_spawn()`:
- If `is_cold_cp` and `state_captured`, store cold variant

Note: fill-gap mode (`fill_gap_segment_id`, `start_fill_gap()`, spawn handling during fill-gap) is implemented separately in Task 11.

- [ ] **Step 4: Update existing session manager tests**

Rename all split→segment references in existing tests. Update `get_state()` assertions to use `current_segment` key.

- [ ] **Step 5: Run all session manager tests**

Run: `pytest tests/test_session_manager.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/session_manager.py tests/test_session_manager.py
git commit -m "feat: session manager handles checkpoint/death/spawn events, rename split→segment"
```

---

## Task 7: Dashboard API — endpoint renames + fill-gap endpoint

**Files:**
- Modify: `python/spinlab/dashboard.py`
- Test: `tests/test_dashboard.py`, `tests/test_dashboard_integration.py`, `tests/test_dashboard_references.py`

- [ ] **Step 1: Update dashboard.py endpoints**

Rename:
- `/api/splits` → `/api/segments`
- `/api/references/{ref_id}/splits` → `/api/references/{ref_id}/segments`
- `/api/splits/{split_id}` (PATCH, DELETE) → `/api/segments/{segment_id}`
- Response keys: `"splits"` → `"segments"`, `"current_split"` → `"current_segment"`
- `api_model()`: `"split_id"` → `"segment_id"`, remove `"goal"` and `"reference_time_ms"` from response, add `"start_type"`, `"end_type"`
- `import_manifest`: `"splits_imported"` → `"segments_imported"`

Add new fill-gap endpoint:
```python
@app.post("/api/segments/{segment_id}/fill-gap")
async def fill_gap(segment_id: str):
    return await session.start_fill_gap(segment_id)
```

- [ ] **Step 2: Update test_dashboard_integration.py**

This is the highest-impact test file. Rename all:
- Endpoint URLs
- Response key assertions
- Fixture data (create segments instead of splits, add variants)
- `split_id` → `segment_id` in assertions

- [ ] **Step 3: Update test_dashboard_references.py**

Rename endpoint URLs and response keys.

- [ ] **Step 4: Update test_dashboard.py**

Rename response key assertions.

- [ ] **Step 5: Run dashboard tests**

Run: `pytest tests/test_dashboard.py tests/test_dashboard_integration.py tests/test_dashboard_references.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/dashboard.py tests/test_dashboard.py tests/test_dashboard_integration.py tests/test_dashboard_references.py
git commit -m "feat: /api/segments endpoints, fill-gap endpoint, rename split→segment in API"
```

---

## Task 8: Manifest + Multi-game + remaining tests

**Files:**
- Modify: `python/spinlab/manifest.py`
- Test: `tests/test_multi_game.py`

- [ ] **Step 1: Update manifest.py**

```python
from .models import Segment, SegmentVariant

# In seed_db_from_manifest:
for idx, entry in enumerate(manifest["segments"], start=1):
    seg = Segment(
        id=entry["id"],
        game_id=game_id,
        level_number=entry["level_number"],
        start_type=entry.get("start_type", "entrance"),
        start_ordinal=entry.get("start_ordinal", 0),
        end_type=entry.get("end_type", "goal"),
        end_ordinal=entry.get("end_ordinal", 0),
        description=entry.get("name", ""),
        ordinal=idx,
        reference_id=run_id,
    )
    db.upsert_segment(seg)
    if entry.get("state_path"):
        variant = SegmentVariant(
            segment_id=seg.id,
            variant_type="cold",
            state_path=entry["state_path"],
            is_default=True,
        )
        db.add_variant(variant)
```

Note: existing manifests use `"splits"` key. Support both:
```python
entries = manifest.get("segments", manifest.get("splits", []))
```

- [ ] **Step 2: Update test_multi_game.py**

Rename split→segment in all fixtures, method calls, and assertions.

- [ ] **Step 3: Run all Python tests**

Run: `pytest tests/ -v --ignore=tests/test_tcp_manager.py`
Expected: PASS (tcp tests may need running emulator)

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/manifest.py tests/test_multi_game.py
git commit -m "refactor: manifest loads segments + variants, rename split→segment in remaining tests"
```

---

## Task 9: Lua — Checkpoint Detection + New Events

**Files:**
- Modify: `lua/spinlab.lua`

- [ ] **Step 1: Add new memory addresses to CONFIG section**

After existing address definitions (around line 50):
```lua
local ADDR_MIDWAY      = 0x13CE  -- midway checkpoint tape: 0→1 when touched
local ADDR_CP_ENTRANCE = 0x1B403 -- ASM-style checkpoint entrance
```

- [ ] **Step 2: Add state tracking variables**

In the STATE section (around line 69):
```lua
-- Checkpoint tracking
local cp_ordinal     = 0      -- per-level counter, incremented on each new CP
local cp_acquired    = false  -- true when a new CP was hit without cold capture yet
local first_cp_entrance = 0   -- initial cpEntrance value at level start
```

- [ ] **Step 3: Add checkpoint detection in passive recorder**

In the `startFrame` callback where memory is read, add after existing death detection:

```lua
-- Checkpoint detection
local midway = emu.read(ADDR_MIDWAY, emu.memType.snesMemory, false)
local cp_entrance = emu.read(ADDR_CP_ENTRANCE, emu.memType.snesMemory, false)

-- Midway: 0→1 transition, excluding goal/orb/key/fadeout
local midway_hit = (prev.midway == 0 and midway == 1)
    and not got_orb and not got_goal and not got_key and not got_fadeout

-- CPEntrance: value shifted, not to firstRoom, excluding goal/orb/key/fadeout
local cp_entrance_hit = (prev.cp_entrance ~= nil and cp_entrance ~= prev.cp_entrance
    and cp_entrance ~= first_cp_entrance)
    and not got_orb and not got_goal and not got_key and not got_fadeout

local cp_hit = midway_hit or cp_entrance_hit

if cp_hit then
    cp_ordinal = cp_ordinal + 1
    cp_acquired = true
    -- After first CP, clear firstRoom so future cpEntrance shifts are real CPs
    -- Setting to 0 is safe: cpEntrance values are room IDs (non-zero in levels)
    first_cp_entrance = 0
    -- Capture hot save state
    local state_path = STATE_DIR .. "/" .. game_id .. "/" .. level_num .. "_cp" .. cp_ordinal .. "_hot.mss"
    pending_save = state_path
    send_event({
        event = "checkpoint",
        level_num = level_num,
        cp_type = midway_hit and "midway" or "cp_entrance",
        cp_ordinal = cp_ordinal,
        timestamp_ms = elapsed_ms(),
        state_path = state_path,
    })
end

prev.midway = midway
prev.cp_entrance = cp_entrance
```

- [ ] **Step 4: Add death event emission**

Where death is already detected (playerAnimation → 9):
```lua
if not died_flag then
    send_event({
        event = "death",
        level_num = level_num,
        timestamp_ms = elapsed_ms(),
    })
end
died_flag = true
```

- [ ] **Step 5: Add spawn detection and cold CP capture**

Where level entrance (GmPrepareLevel equivalent) is detected:
```lua
-- Distinguish Put (fresh entry) vs Spawn (respawn after death)
if is_level_prepare then
    if died_flag then
        -- Spawn: respawn after death
        local state_captured = false
        local state_path = nil
        local was_cp_acquired = cp_acquired  -- capture before clearing
        if cp_acquired then
            state_path = STATE_DIR .. "/" .. game_id .. "/" .. level_num .. "_cp" .. cp_ordinal .. "_cold.mss"
            pending_save = state_path
            state_captured = true
            cp_acquired = false  -- only capture first cold spawn per CP
        end
        send_event({
            event = "spawn",
            level_num = level_num,
            is_cold_cp = was_cp_acquired,
            cp_ordinal = cp_ordinal,
            timestamp_ms = elapsed_ms(),
            state_captured = state_captured,
            state_path = state_path,
        })
        died_flag = false
    else
        -- Put: fresh level entry
        cp_ordinal = 0
        cp_acquired = false
        first_cp_entrance = cp_entrance  -- record initial entrance
        -- existing entrance handling...
    end
end
```

- [ ] **Step 6: Update practice mode end-condition**

In `parse_practice_split()` (rename to `parse_practice_segment()`), store `end_type` from the command JSON.

In the practice playing state check, add checkpoint end-condition:
```lua
if practice.segment.end_type == "checkpoint" then
    -- Check for CP event using same composite condition
    if cp_hit then
        practice.completed = true
        practice.elapsed_ms = elapsed_ms() - practice.start_ms
        practice.state = PSTATE_RESULT
    end
elseif practice.segment.end_type == "goal" then
    -- Existing goal detection
    ...
end
```

- [ ] **Step 7: Rename split→segment in Lua variable names**

- `practice.split` → `practice.segment`
- `parse_practice_split()` → `parse_practice_segment()`
- Log messages and overlay text

- [ ] **Step 8: Test manually in Mesen2**

Verify: load a ROM, trigger a checkpoint, verify events are emitted over TCP. This requires manual testing.

- [ ] **Step 9: Commit**

```bash
git add lua/spinlab.lua
git commit -m "feat: Lua checkpoint/death/spawn detection, practice end-condition for checkpoint segments"
```

---

## Task 10: Frontend JS — rename + Manage tab segment table

**Files:**
- Modify: `python/spinlab/static/format.js`, `python/spinlab/static/live.js`, `python/spinlab/static/manage.js`, `python/spinlab/static/model.js`

- [ ] **Step 1: Update format.js**

Rename `splitName` → `segmentName`. Update the display logic to handle segment format:

```javascript
export function segmentName(s) {
  if (s.description) return s.description;
  const start = s.start_type === 'entrance' ? 'entrance' : s.start_type + '.' + s.start_ordinal;
  const end = s.end_type === 'goal' ? 'goal' : s.end_type + '.' + s.end_ordinal;
  return 'L' + s.level_number + ' ' + start + ' → ' + end;
}
```

- [ ] **Step 2: Update live.js**

- Import `segmentName` instead of `splitName`
- `data.current_split` → `data.current_segment`
- `data.session.splits_completed` → `data.session.segments_completed`
- `data.session.splits_attempted` → `data.session.segments_attempted`
- All `splitName(` calls → `segmentName(`

- [ ] **Step 3: Update model.js**

- Import `segmentName` instead of `splitName`
- `"split_id"` → `"segment_id"` in data access
- `splitName(` → `segmentName(`

- [ ] **Step 4: Rewrite manage.js for flat segment table with State column**

The Manage tab changes significantly. New table structure:

```javascript
function updateManage(refs, segments) {
  // ... ref select dropdown (unchanged) ...

  const body = document.getElementById('segment-body');
  body.innerHTML = '';
  segments.forEach(s => {
    const tr = document.createElement('tr');
    const hasState = s.state_path != null;
    const stateCell = hasState
      ? '<span class="state-ok">✅</span>'
      : '<button class="btn-fill-gap" data-id="' + s.id + '">❌</button>';
    tr.innerHTML =
      '<td><input class="segment-name-input" value="' + (s.description || '') + '" ' +
        'placeholder="' + segmentName(s) + '" ' +
        'data-id="' + s.id + '" data-field="description"></td>' +
      '<td>' + s.level_number + '</td>' +
      '<td>' + (s.start_type === 'entrance' ? 'entrance' : 'cp.' + s.start_ordinal) +
        ' → ' + (s.end_type === 'goal' ? 'goal' : 'cp.' + s.end_ordinal) + '</td>' +
      '<td>' + stateCell + '</td>' +
      '<td><button class="btn-x" data-id="' + s.id + '">✕</button></td>';
    body.appendChild(tr);
  });
}
```

Update `initManageTab()`:
- CSS class renames: `split-name-input`→`segment-name-input`, `split-toggle`→removed (end_on_goal gone), `split-body`→`segment-body`
- API endpoints: `/api/splits/`→`/api/segments/`
- `/api/references/{id}/splits`→`/api/references/{id}/segments`
- Add fill-gap button handler:
  ```javascript
  document.getElementById('segment-body').addEventListener('click', async (e) => {
    if (e.target.classList.contains('btn-fill-gap')) {
      const id = e.target.dataset.id;
      const data = await postJSON('/api/segments/' + id + '/fill-gap');
      if (data?.status === 'started') {
        e.target.textContent = '⏳';
        e.target.disabled = true;
      }
    }
  });
  ```

- [ ] **Step 5: Update index.html**

Rename DOM element IDs:
- `split-body` → `segment-body`
- Table headers: add "Segment" and "State" columns, remove "Goal" and "Ref Time" columns

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/static/
git commit -m "feat: frontend uses segments, manage tab shows flat segment table with fill-gap"
```

---

## Task 11: Fill-Gap Flow — SessionManager integration

**Files:**
- Modify: `python/spinlab/session_manager.py`
- Test: `tests/test_session_manager.py`

- [ ] **Step 1: Write failing test for fill-gap flow**

```python
async def test_fill_gap_loads_hot_and_captures_cold(session_mgr):
    """Fill-gap mode loads hot CP state, captures cold on spawn."""
    gid = session_mgr.game_id
    # Create a segment with hot variant but no cold
    seg = Segment(
        id=Segment.make_id(gid, 105, "checkpoint", 1, "goal", 0),
        game_id=gid, level_number=105,
        start_type="checkpoint", start_ordinal=1,
        end_type="goal", end_ordinal=0,
        reference_id=session_mgr.ref_capture_run_id,
    )
    session_mgr.db.upsert_segment(seg)
    session_mgr.db.add_variant(SegmentVariant(seg.id, "hot", "/hot.mss", False))

    result = await session_mgr.start_fill_gap(seg.id)
    assert result["status"] == "started"
    assert session_mgr.fill_gap_segment_id == seg.id

    # Simulate spawn with cold capture
    await session_mgr.route_event({
        "event": "spawn",
        "level_num": 105,
        "is_cold_cp": True,
        "cp_ordinal": 1,
        "timestamp_ms": 1000,
        "state_captured": True,
        "state_path": "/cold.mss",
    })

    # Cold variant should now exist
    variants = session_mgr.db.get_variants(seg.id)
    cold = [v for v in variants if v.variant_type == "cold"]
    assert len(cold) == 1
    assert session_mgr.fill_gap_segment_id is None  # fill-gap ended
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_session_manager.py::test_fill_gap_loads_hot_and_captures_cold -v`
Expected: FAIL

- [ ] **Step 3: Implement fill-gap in session_manager.py**

```python
async def start_fill_gap(self, segment_id: str) -> dict:
    """Enter fill-gap mode: load hot variant so user can die for cold capture."""
    if not self.tcp.is_connected:
        return {"status": "not_connected"}

    hot = self.db.get_variant(segment_id, "hot")
    if not hot:
        return {"status": "no_hot_variant"}

    self.fill_gap_segment_id = segment_id
    self.mode = "fill_gap"
    # Load the hot save state
    await self.tcp.send(json.dumps({
        "event": "fill_gap_load",
        "state_path": hot.state_path,
        "message": "Die to capture cold start",
    }))
    await self._notify_sse()
    return {"status": "started", "segment_id": segment_id}
```

Update `route_event()` to handle spawn during fill-gap:
```python
if evt_type == "spawn" and self.mode == "fill_gap":
    await self._handle_fill_gap_spawn(event)
    return
```

```python
async def _handle_fill_gap_spawn(self, event: dict) -> None:
    if not event.get("state_captured") or not self.fill_gap_segment_id:
        return
    from .models import SegmentVariant
    variant = SegmentVariant(
        segment_id=self.fill_gap_segment_id,
        variant_type="cold",
        state_path=event["state_path"],
        is_default=True,
    )
    self.db.add_variant(variant)
    self.fill_gap_segment_id = None
    self.mode = "idle"
    await self._notify_sse()
```

- [ ] **Step 4: Run fill-gap test**

Run: `pytest tests/test_session_manager.py::test_fill_gap_loads_hot_and_captures_cold -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/session_manager.py tests/test_session_manager.py
git commit -m "feat: fill-gap flow — load hot CP, capture cold spawn"
```

---

## Task 12: Final integration pass

**Files:**
- All modified files

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v`
Expected: PASS

- [ ] **Step 2: Fix any remaining failures**

Address any broken imports, missed renames, or test fixtures.

- [ ] **Step 3: Run the dashboard manually**

Run: `spinlab dashboard` (or `python -m spinlab.cli dashboard`)
Verify:
- Dashboard loads without errors
- Manage tab shows segment table
- Model tab renders with segment data
- Live tab shows segment names

- [ ] **Step 4: Commit any fixes**

```bash
git add -u
git commit -m "fix: integration fixes for segment remodel"
```

- [ ] **Step 5: Final commit — update CLAUDE.md references**

Update any split→segment terminology in CLAUDE.md and DESIGN.md that no longer matches. Keep it brief — just rename where the code has changed.

```bash
git add CLAUDE.md docs/DESIGN.md
git commit -m "docs: update split→segment terminology in project docs"
```

# Session Manager Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge dashboard + orchestrator into a single always-on process that owns the Lua TCP connection, with reference management and live capture.

**Architecture:** Three-phase migration. Phase 1 adds quick wins within the current two-process architecture (expected_time overlay, model tab polish, ordinal ordering, server-side queue). Phase 2 introduces reference/capture_run management and split editing. Phase 3 extracts the practice loop into the dashboard process and adds live reference capture via TCP.

**Tech Stack:** Python 3.11+ / FastAPI / SQLite / Lua (Mesen2) / vanilla JS

**Spec:** `docs/superpowers/specs/2026-03-12-session-manager-design.md`

---

## File Structure

### Phase 1 — Modifications only

| File | Change |
|------|--------|
| `python/spinlab/models.py` | Add `expected_time_ms` to `SplitCommand` |
| `python/spinlab/orchestrator.py` | Compute `expected_time_ms` from Kalman μ |
| `lua/spinlab.lua` | Read `expected_time_ms`, use for overlay comparison |
| `python/spinlab/static/index.html` | Rename Model tab column headers, add tooltips |
| `python/spinlab/dashboard.py` | Compute Up Next queue server-side via scheduler |
| `python/spinlab/db.py` | Add `ordinal` column migration, update ORDER BY |
| `python/spinlab/scheduler.py` | Pass ordinal through `_load_splits_with_model()` |
| `python/spinlab/allocators/round_robin.py` | (No change — already iterates in list order) |
| `tests/test_allocators.py` | Add ordinal-ordering test for Round Robin |
| `tests/test_dashboard.py` | Add server-side queue test |

### Phase 2 — Modifications + new DB tables

| File | Change |
|------|--------|
| `python/spinlab/db.py` | `capture_runs` table, `reference_id` FK, split/reference CRUD |
| `python/spinlab/dashboard.py` | Split PATCH/DELETE, reference CRUD, import-manifest endpoints |
| `python/spinlab/static/index.html` | Manage tab: split list, reference selector |
| `python/spinlab/static/app.js` | Manage tab JS: inline editing, delete, reference switching |
| `python/spinlab/static/style.css` | Manage tab styling |
| `tests/test_db_references.py` | **Create**: capture_run CRUD + split edit DB tests |
| `tests/test_dashboard_references.py` | **Create**: reference + split API endpoint tests |

### Phase 3 — New files + major refactor

| File | Change |
|------|--------|
| `python/spinlab/tcp_manager.py` | **Create**: async TCP client for Lua |
| `python/spinlab/practice.py` | **Create**: practice loop as async function |
| `python/spinlab/dashboard.py` | Mode management, TCP integration, practice start/stop |
| `python/spinlab/orchestrator.py` | Deprecated — kept as CLI fallback |
| `lua/spinlab.lua` | Forward transition events over TCP in passive mode |
| `tests/test_tcp_manager.py` | **Create**: TCP manager with mock socket |
| `tests/test_practice.py` | **Create**: practice loop with mock TCP |

---

## Chunk 1: Phase 1 — Quick Wins

All changes work within the current two-process architecture. No new files except tests.

---

### Task 1: Commit existing bug fixes

The working tree contains uncommitted fixes for bugs #6–13 from the 2026-03-12 testing session (allocator sync, greedy tiebreaking, abort filtering, queue rendering, UI polish). These need to be committed before new work begins.

**Files:**
- Verify: `python/spinlab/allocators/greedy.py`, `python/spinlab/capture.py`, `python/spinlab/scheduler.py`, `python/spinlab/static/app.js`, `python/spinlab/static/index.html`, `python/spinlab/static/style.css`

- [ ] **Step 1: Run existing tests to verify fixes don't break anything**

Run: `cd python && python -m pytest ../tests/ -v`
Expected: All tests PASS

- [ ] **Step 2: Commit the bug fixes**

```bash
cd /c/Users/thedo/git/spinlab
git add python/spinlab/allocators/greedy.py python/spinlab/capture.py \
        python/spinlab/scheduler.py python/spinlab/static/app.js \
        python/spinlab/static/index.html python/spinlab/static/style.css
git commit -m "fix: bugs #6-13 — allocator sync, greedy tiebreak, abort filter, queue render, UI polish"
```

> **Note:** If these files were already committed in a prior session (check `git status` first), skip this task.

---

### Task 2: Add `expected_time_ms` to SplitCommand

The Lua overlay currently compares elapsed time against `reference_time_ms`. Change it to use the Kalman model's expected time (μ), falling back to reference_time_ms when no model state exists.

**Files:**
- Modify: `python/spinlab/models.py:48-65`
- Modify: `python/spinlab/orchestrator.py:200-207`
- Modify: `lua/spinlab.lua:141-149` (parse_practice_split)
- Modify: `lua/spinlab.lua:176-220` (draw_practice_overlay)
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: Write failing test for SplitCommand.to_dict() including expected_time_ms**

Add to `tests/test_orchestrator.py`:

```python
def test_split_command_includes_expected_time_ms():
    from spinlab.models import SplitCommand
    cmd = SplitCommand(
        id="s1", state_path="/tmp/s.mss", goal="normal",
        description="Test", reference_time_ms=5000,
        expected_time_ms=4200,
    )
    d = cmd.to_dict()
    assert d["expected_time_ms"] == 4200
    assert d["reference_time_ms"] == 5000


def test_split_command_expected_time_defaults_none():
    from spinlab.models import SplitCommand
    cmd = SplitCommand(
        id="s1", state_path="/tmp/s.mss", goal="normal",
        description="Test", reference_time_ms=5000,
    )
    d = cmd.to_dict()
    assert d["expected_time_ms"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest ../tests/test_orchestrator.py::test_split_command_includes_expected_time_ms -v`
Expected: FAIL — `TypeError: SplitCommand.__init__() got an unexpected keyword argument 'expected_time_ms'`

- [ ] **Step 3: Add expected_time_ms field to SplitCommand**

In `python/spinlab/models.py`, add the field and update `to_dict()`:

```python
@dataclass
class SplitCommand:
    """Sent from orchestrator to Lua: which split to load next."""
    id: str
    state_path: str
    goal: str
    description: str
    reference_time_ms: int | None
    auto_advance_delay_ms: int = 2000
    expected_time_ms: int | None = None  # Kalman μ*1000, falls back to reference_time_ms

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "state_path": self.state_path,
            "goal": self.goal,
            "description": self.description,
            "reference_time_ms": self.reference_time_ms,
            "auto_advance_delay_ms": self.auto_advance_delay_ms,
            "expected_time_ms": self.expected_time_ms,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && python -m pytest ../tests/test_orchestrator.py::test_split_command_includes_expected_time_ms ../tests/test_orchestrator.py::test_split_command_expected_time_defaults_none -v`
Expected: PASS

- [ ] **Step 5: Compute expected_time_ms in orchestrator**

In `python/spinlab/orchestrator.py`, in the `run()` function, after `picked = scheduler.pick_next()` (line ~200), compute the expected time:

```python
            # Compute expected time from estimator μ, fall back to reference
            expected_time_ms = None
            if (picked.estimator_state is not None
                    and hasattr(picked.estimator_state, "mu")
                    and picked.estimator_state.mu > 0):
                expected_time_ms = int(picked.estimator_state.mu * 1000)

            cmd = SplitCommand(
                id=picked.split_id,
                state_path=picked.state_path,
                goal=picked.goal,
                description=picked.description,
                reference_time_ms=picked.reference_time_ms,
                auto_advance_delay_ms=auto_advance_delay_ms,
                expected_time_ms=expected_time_ms,
            )
```

- [ ] **Step 6: Update Lua parse_practice_split to read expected_time_ms**

In `lua/spinlab.lua`, update `parse_practice_split()` (~line 141):

```lua
local function parse_practice_split(json_str)
  return {
    id                     = json_get_str(json_str, "id") or "",
    state_path             = json_get_str(json_str, "state_path") or "",
    goal                   = json_get_str(json_str, "goal") or "",
    description            = json_get_str(json_str, "description") or "",
    reference_time_ms      = json_get_num(json_str, "reference_time_ms"),
    auto_advance_delay_ms  = json_get_num(json_str, "auto_advance_delay_ms") or 2000,
    expected_time_ms       = json_get_num(json_str, "expected_time_ms"),
  }
end
```

- [ ] **Step 7: Update Lua overlay to use expected_time_ms for comparison**

In `lua/spinlab.lua`, update `draw_practice_overlay()` (~line 176). Replace the `ref` variable with a `compare_time` that prefers `expected_time_ms`:

```lua
local function draw_practice_overlay()
  if not practice_mode then return end

  local label = practice_split and format_goal(practice_split.goal) or "?"
  -- Use expected time (Kalman μ) for comparison, fall back to reference time
  local compare_time = nil
  if practice_split then
    compare_time = practice_split.expected_time_ms or practice_split.reference_time_ms
  end

  if practice_state == PSTATE_PLAYING or practice_state == PSTATE_LOADING then
    local elapsed = ts_ms() - practice_start_ms

    -- Row 1: goal label
    draw_text(4, 2, label, 0x00000000, 0xFFFFFFFF)

    -- Row 2: timer / compare_time, color-coded
    local timer_color
    if compare_time then
      timer_color = (elapsed < compare_time) and 0xFF44FF44 or 0xFFFF4444
    else
      timer_color = 0xFFFFFFFF
    end
    local cmp_str = compare_time and ms_to_display(compare_time) or "?"
    draw_text(4, 12, ms_to_display(elapsed) .. " / " .. cmp_str, 0x00000000, timer_color)

  elseif practice_state == PSTATE_RESULT then
    local prefix = practice_completed and "Clear!" or "Abort"

    -- Row 1: goal label
    draw_text(4, 2, label, 0x00000000, 0xFFFFFFFF)

    -- Row 2: result time / compare_time
    local timer_color
    if compare_time then
      timer_color = (practice_elapsed_ms < compare_time) and 0xFF44FF44 or 0xFFFF4444
    else
      timer_color = 0xFFFFFFFF
    end
    local cmp_str2 = compare_time and ms_to_display(compare_time) or "?"
    draw_text(4, 12, prefix .. "  " .. ms_to_display(practice_elapsed_ms) .. " / " .. cmp_str2, 0x00000000, timer_color)

    -- Row 3: countdown to auto-advance
    local remaining = practice_auto_advance_ms - (ts_ms() - practice_result_start_ms)
    local secs = string.format("%.1f", math.max(0, remaining / 1000))
    draw_text(4, 22, "Next in " .. secs .. "s", 0x00000000, 0xFF888888)
  end
end
```

- [ ] **Step 8: Run all tests**

Run: `cd python && python -m pytest ../tests/ -v`
Expected: All PASS

- [ ] **Step 9: Commit**

```bash
git add python/spinlab/models.py python/spinlab/orchestrator.py lua/spinlab.lua tests/test_orchestrator.py
git commit -m "feat: send expected_time_ms (Kalman μ) in practice_load for overlay comparison"
```

---

### Task 3: Rename Model tab column headers and add tooltips

**Files:**
- Modify: `python/spinlab/static/index.html:64-77`

- [ ] **Step 1: Update the Model tab `<thead>` in index.html**

Replace the `<thead>` block in the Model tab:

```html
        <thead>
          <tr>
            <th title="Level section being practiced">Split</th>
            <th title="Expected completion time in seconds">Avg</th>
            <th title="How your time changes per run (negative = improving)">Trend</th>
            <th title="95% confidence interval for the trend">Range</th>
            <th title="Practice value: how much time you save per run here">Value</th>
            <th title="Completed practice attempts">Runs</th>
            <th title="Your fastest completion">Best</th>
          </tr>
        </thead>
```

- [ ] **Step 2: Verify manually** — load dashboard, check Model tab headers show new names and tooltips on hover.

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/static/index.html
git commit -m "feat(dashboard): rename Model tab headers with tooltips"
```

---

### Task 4: Compute Up Next queue server-side

The dashboard currently reads the queue from the orchestrator state file, which is stale. Compute it server-side using `scheduler.peek_next_n()`.

**Files:**
- Modify: `python/spinlab/dashboard.py:60-124`
- Test: `tests/test_dashboard.py`

- [ ] **Step 1: Write failing test for server-side queue**

Add to `tests/test_dashboard.py`:

```python
def test_api_state_queue_from_scheduler(client, db, state_file):
    """Queue should come from scheduler.peek_next_n(), not state file."""
    import json
    # Create 3 splits
    for i in range(1, 4):
        s = Split(id=f"s{i}", game_id="test_game", level_number=i,
                  room_id=0, goal="normal", description=f"Level {i}",
                  reference_time_ms=5000)
        db.upsert_split(s)

    db.create_session("sess1", "test_game")
    # State file has current_split but empty queue
    state_file.write_text(json.dumps({
        "session_id": "sess1",
        "current_split_id": "s1",
        "queue": [],
    }))

    resp = client.get("/api/state")
    data = resp.json()
    # Queue should have 2 entries from scheduler (3 splits minus current s1)
    assert len(data["queue"]) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest ../tests/test_dashboard.py::test_api_state_queue_from_scheduler -v`
Expected: FAIL — queue is empty because it's read from state file

- [ ] **Step 3: Modify `/api/state` to compute queue from scheduler**

In `python/spinlab/dashboard.py`, in `api_state()`:

1. **Delete** the old state-file queue logic (~lines 103-107) that reads `queue_ids` from `orch_state.get("queue", [])`.
2. **Add** the following block **outside** the `if orch_state:` block, just before the `return` statement (~line 115), after `current_split` has been resolved. This ensures the queue is computed even without a state file:

```python
        # Compute queue server-side from scheduler (bypasses stale state file)
        sched = _get_scheduler()
        queue_ids = sched.peek_next_n(3)
        # Exclude current split from queue
        current_id = current_split["id"] if current_split else None
        queue_ids = [q for q in queue_ids if q != current_id][:2]
        if queue_ids:
            splits_all = db.get_all_splits_with_model(game_id)
            split_map = {s["id"]: s for s in splits_all}
            queue = [split_map[sid] for sid in queue_ids if sid in split_map]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && python -m pytest ../tests/test_dashboard.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): compute Up Next queue server-side from scheduler"
```

---

### Task 5: Add `ordinal` column and Round Robin ordering

Add an `ordinal` column to splits that records the order splits were captured in the reference run. Round Robin iterates in ordinal order. The DB query in `get_all_splits_with_model()` sorts by `ordinal` first.

**Files:**
- Modify: `python/spinlab/db.py:83-113` (migration), `python/spinlab/db.py:329-348` (ORDER BY)
- Modify: `python/spinlab/orchestrator.py:86-103` (seed_db_from_manifest passes ordinal)
- Modify: `python/spinlab/models.py:17-31` (Split dataclass gets ordinal)
- Test: `tests/test_allocators.py`

- [ ] **Step 1: Write failing test for ordinal-ordered Round Robin**

Add to `tests/test_allocators.py`:

```python
def _make_split_with_ordinal(split_id: str, ordinal: int) -> SplitWithModel:
    return SplitWithModel(
        split_id=split_id,
        game_id="test",
        level_number=ordinal * 10,  # level_number doesn't match ordinal
        room_id=None,
        goal="normal",
        description=f"Split {split_id}",
        strat_version=1,
        reference_time_ms=None,
        state_path=None,
        active=True,
        marginal_return=0.0,
    )


class TestRoundRobinOrdinalOrder:
    def test_cycles_in_list_order(self):
        """Round Robin should iterate in the order splits are provided.
        The caller (scheduler) is responsible for ordering by ordinal."""
        alloc = RoundRobinAllocator()
        # Splits provided in ordinal order (3rd level first, 1st level last)
        splits = [
            _make_split_with_ordinal("c", 1),
            _make_split_with_ordinal("a", 2),
            _make_split_with_ordinal("b", 3),
        ]
        results = [alloc.pick_next(splits) for _ in range(3)]
        assert results == ["c", "a", "b"]
```

- [ ] **Step 2: Run test to verify it passes** (Round Robin already iterates in list order)

Run: `cd python && python -m pytest ../tests/test_allocators.py::TestRoundRobinOrdinalOrder -v`
Expected: PASS — this confirms RR uses list order; now we need the DB to provide ordinal-sorted data.

- [ ] **Step 3: Write failing test for ordinal in DB**

Add to `tests/test_db_dashboard.py` (or create if it doesn't exist):

```python
def test_splits_ordered_by_ordinal(tmp_path):
    """get_all_splits_with_model should return splits ordered by ordinal."""
    from spinlab.db import Database
    from spinlab.models import Split

    db = Database(tmp_path / "test.db")
    db.upsert_game("g", "Game", "any%")

    # Insert splits with ordinals out of level_number order
    for level, ordinal in [(30, 1), (10, 2), (20, 3)]:
        s = Split(id=f"g:{level}:0:normal", game_id="g",
                  level_number=level, room_id=0, goal="normal",
                  ordinal=ordinal)
        db.upsert_split(s)

    rows = db.get_all_splits_with_model("g")
    levels = [r["level_number"] for r in rows]
    assert levels == [30, 10, 20], f"Expected ordinal order [30,10,20], got {levels}"
```

- [ ] **Step 4: Run test to verify it fails**

Run: `cd python && python -m pytest ../tests/test_db_dashboard.py::test_splits_ordered_by_ordinal -v`
Expected: FAIL — Split doesn't have ordinal field yet

- [ ] **Step 5: Add ordinal to Split dataclass**

In `python/spinlab/models.py`, add `ordinal` field:

```python
@dataclass
class Split:
    id: str
    game_id: str
    level_number: int
    room_id: Optional[int]
    goal: str
    description: str = ""
    state_path: Optional[str] = None
    reference_time_ms: Optional[int] = None
    strat_version: int = 1
    active: bool = True
    ordinal: Optional[int] = None
```

- [ ] **Step 6: Add ordinal column migration in db.py**

In `python/spinlab/db.py`, in `_init_schema()`, after the `allocator_config` table creation (~line 112), add:

```python
        # --- Migration: add ordinal column to splits ---
        cur = self.conn.execute("PRAGMA table_info(splits)")
        col_names = [row[1] for row in cur.fetchall()]
        if "ordinal" not in col_names:
            self.conn.execute("ALTER TABLE splits ADD COLUMN ordinal INTEGER")
            self.conn.commit()
```

- [ ] **Step 7: Update upsert_split to handle ordinal**

In `python/spinlab/db.py`, update `upsert_split()` to include ordinal in the INSERT and ON CONFLICT:

```python
    def upsert_split(self, split: Split) -> None:
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            """INSERT INTO splits (id, game_id, level_number, room_id, goal, description,
               state_path, reference_time_ms, strat_version, active, ordinal, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 state_path=excluded.state_path,
                 reference_time_ms=excluded.reference_time_ms,
                 description=excluded.description,
                 ordinal=excluded.ordinal,
                 updated_at=excluded.updated_at""",
            (split.id, split.game_id, split.level_number, split.room_id,
             split.goal, split.description, split.state_path,
             split.reference_time_ms, split.strat_version, int(split.active),
             split.ordinal, now, now),
        )
        self.conn.commit()
```

- [ ] **Step 8: Update get_all_splits_with_model ORDER BY**

In `python/spinlab/db.py`, update `get_all_splits_with_model()` to sort by ordinal first and include ordinal in the SELECT:

```python
    def get_all_splits_with_model(self, game_id: str) -> list[dict]:
        """Get all active splits LEFT JOIN model_state, ordered by ordinal."""
        cur = self.conn.execute(
            """SELECT s.id, s.game_id, s.level_number, s.room_id, s.goal,
                      s.description, s.strat_version, s.reference_time_ms,
                      s.state_path, s.active, s.ordinal,
                      m.estimator, m.state_json, m.marginal_return
               FROM splits s
               LEFT JOIN model_state m ON s.id = m.split_id
               WHERE s.game_id = ? AND s.active = 1
               ORDER BY s.ordinal, s.level_number, s.room_id""",
            (game_id,),
        )
        cols = [
            "id", "game_id", "level_number", "room_id", "goal",
            "description", "strat_version", "reference_time_ms",
            "state_path", "active", "ordinal",
            "estimator", "state_json", "marginal_return",
        ]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
```

- [ ] **Step 8b: Update `_row_to_split` to include ordinal**

In `python/spinlab/db.py`, update `_row_to_split()` (~line 377) to populate `ordinal`:

```python
    @staticmethod
    def _row_to_split(row: sqlite3.Row) -> Split:
        return Split(
            id=row["id"],
            game_id=row["game_id"],
            level_number=row["level_number"],
            room_id=row["room_id"],
            goal=row["goal"],
            description=row["description"] or "",
            state_path=row["state_path"],
            reference_time_ms=row["reference_time_ms"],
            strat_version=row["strat_version"],
            active=bool(row["active"]),
            ordinal=row["ordinal"] if "ordinal" in row.keys() else None,
        )
```

- [ ] **Step 9: Update seed_db_from_manifest to pass ordinal**

In `python/spinlab/orchestrator.py`, update `seed_db_from_manifest()`:

```python
def seed_db_from_manifest(db: Database, manifest: dict, game_name: str) -> None:
    """Upsert game + all splits from manifest into the DB."""
    game_id: str = manifest["game_id"]
    category: str = manifest.get("category", "any%")
    db.upsert_game(game_id, game_name, category)

    for idx, entry in enumerate(manifest["splits"], start=1):
        split = Split(
            id=entry["id"],
            game_id=game_id,
            level_number=entry["level_number"],
            room_id=entry.get("room_id"),
            goal=entry["goal"],
            description=entry.get("name", ""),
            state_path=entry.get("state_path"),
            reference_time_ms=entry.get("reference_time_ms"),
            ordinal=idx,
        )
        db.upsert_split(split)
```

- [ ] **Step 10: Run all tests**

Run: `cd python && python -m pytest ../tests/ -v`
Expected: All PASS

- [ ] **Step 11: Commit**

```bash
git add python/spinlab/models.py python/spinlab/db.py python/spinlab/orchestrator.py \
        tests/test_allocators.py tests/test_db_dashboard.py
git commit -m "feat: add ordinal column to splits, sort by capture order"
```

---

## Chunk 2: Phase 2 — Reference Editor + Split Management

Builds the Manage tab with reference (capture_run) management and split editing.

---

### Task 6: Add `capture_runs` table and `reference_id` FK

> **Prerequisite:** Phase 1 (Task 5) must be complete — `ordinal` field on Split and ordinal column in DB must exist.

**Files:**
- Modify: `python/spinlab/db.py`
- Modify: `python/spinlab/models.py` (add `reference_id`)
- Test: `tests/test_db_references.py` (**create**)

- [ ] **Step 0: Add reference_id to Split dataclass first (needed by tests)**

In `python/spinlab/models.py`, add to Split after `ordinal`:

```python
    reference_id: Optional[str] = None
```

- [ ] **Step 1: Write failing tests for capture_run CRUD**

Create `tests/test_db_references.py`:

```python
"""Tests for capture_run and split reference management."""
import pytest
from spinlab.db import Database
from spinlab.models import Split


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("g", "Game", "any%")
    return d


class TestCaptureRunCRUD:
    def test_create_and_list(self, db):
        db.create_capture_run("ref1", "g", "First Run")
        db.create_capture_run("ref2", "g", "Second Run")
        refs = db.list_capture_runs("g")
        assert len(refs) == 2
        assert refs[0]["name"] == "First Run"

    def test_set_active(self, db):
        db.create_capture_run("ref1", "g", "Run 1")
        db.create_capture_run("ref2", "g", "Run 2")
        db.set_active_capture_run("ref2")
        refs = db.list_capture_runs("g")
        active = [r for r in refs if r["active"]]
        assert len(active) == 1
        assert active[0]["id"] == "ref2"

    def test_rename(self, db):
        db.create_capture_run("ref1", "g", "Old Name")
        db.rename_capture_run("ref1", "New Name")
        refs = db.list_capture_runs("g")
        assert refs[0]["name"] == "New Name"

    def test_delete_deactivates_splits(self, db):
        db.create_capture_run("ref1", "g", "Run 1")
        s = Split(id="s1", game_id="g", level_number=1, room_id=0,
                  goal="normal", reference_id="ref1")
        db.upsert_split(s)
        db.delete_capture_run("ref1")
        splits = db.get_all_splits_with_model("g")
        assert len(splits) == 0  # s1 deactivated, not returned


class TestSplitEdit:
    def test_update_split_description(self, db):
        s = Split(id="s1", game_id="g", level_number=1, room_id=0, goal="normal")
        db.upsert_split(s)
        db.update_split("s1", description="Yoshi's Island 1")
        rows = db.get_all_splits_with_model("g")
        assert rows[0]["description"] == "Yoshi's Island 1"

    def test_update_split_goal(self, db):
        s = Split(id="s1", game_id="g", level_number=1, room_id=0, goal="normal")
        db.upsert_split(s)
        db.update_split("s1", goal="key")
        rows = db.get_all_splits_with_model("g")
        assert rows[0]["goal"] == "key"

    def test_soft_delete_split(self, db):
        s = Split(id="s1", game_id="g", level_number=1, room_id=0, goal="normal")
        db.upsert_split(s)
        db.soft_delete_split("s1")
        rows = db.get_all_splits_with_model("g")
        assert len(rows) == 0  # deactivated

    def test_get_splits_by_reference(self, db):
        db.create_capture_run("ref1", "g", "Run 1")
        for i in range(3):
            s = Split(id=f"s{i}", game_id="g", level_number=i, room_id=0,
                      goal="normal", reference_id="ref1", ordinal=i+1)
            db.upsert_split(s)
        rows = db.get_splits_by_reference("ref1")
        assert len(rows) == 3
        assert rows[0]["ordinal"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && python -m pytest ../tests/test_db_references.py -v`
Expected: FAIL — methods don't exist yet

- [ ] **Step 3: Add capture_runs table and reference_id FK migration**

In `python/spinlab/db.py`, add to `_init_schema()` after the ordinal migration:

```python
        # --- capture_runs table ---
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS capture_runs (
                id TEXT PRIMARY KEY,
                game_id TEXT NOT NULL REFERENCES games(id),
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                active INTEGER DEFAULT 0
            )
        """)

        # --- Migration: add reference_id to splits ---
        cur = self.conn.execute("PRAGMA table_info(splits)")
        col_names = [row[1] for row in cur.fetchall()]
        if "reference_id" not in col_names:
            self.conn.execute(
                "ALTER TABLE splits ADD COLUMN reference_id TEXT REFERENCES capture_runs(id)"
            )
            self.conn.commit()
```

- [ ] **Step 4: Update `_row_to_split` to include new fields**

In `python/spinlab/db.py`, update `_row_to_split()` (~line 377) to populate `ordinal` and `reference_id`:

```python
    @staticmethod
    def _row_to_split(row: sqlite3.Row) -> Split:
        return Split(
            id=row["id"],
            game_id=row["game_id"],
            level_number=row["level_number"],
            room_id=row["room_id"],
            goal=row["goal"],
            description=row["description"] or "",
            state_path=row["state_path"],
            reference_time_ms=row["reference_time_ms"],
            strat_version=row["strat_version"],
            active=bool(row["active"]),
            ordinal=row["ordinal"] if "ordinal" in row.keys() else None,
            reference_id=row["reference_id"] if "reference_id" in row.keys() else None,
        )
```

- [ ] **Step 5: Update upsert_split to include reference_id**

In `python/spinlab/db.py`, update `upsert_split()` INSERT to include `reference_id`:

```python
    def upsert_split(self, split: Split) -> None:
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            """INSERT INTO splits (id, game_id, level_number, room_id, goal, description,
               state_path, reference_time_ms, strat_version, active, ordinal, reference_id,
               created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 state_path=excluded.state_path,
                 reference_time_ms=excluded.reference_time_ms,
                 description=excluded.description,
                 ordinal=excluded.ordinal,
                 reference_id=excluded.reference_id,
                 updated_at=excluded.updated_at""",
            (split.id, split.game_id, split.level_number, split.room_id,
             split.goal, split.description, split.state_path,
             split.reference_time_ms, split.strat_version, int(split.active),
             split.ordinal, split.reference_id, now, now),
        )
        self.conn.commit()
```

- [ ] **Step 6: Implement capture_run CRUD methods**

Add to `python/spinlab/db.py`:

```python
    # -- Capture Runs --

    def create_capture_run(self, run_id: str, game_id: str, name: str) -> None:
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            "INSERT INTO capture_runs (id, game_id, name, created_at, active) "
            "VALUES (?, ?, ?, ?, 0)",
            (run_id, game_id, name, now),
        )
        self.conn.commit()

    def list_capture_runs(self, game_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, game_id, name, created_at, active FROM capture_runs "
            "WHERE game_id = ? ORDER BY created_at",
            (game_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_active_capture_run(self, run_id: str) -> None:
        # Get game_id from the run
        row = self.conn.execute(
            "SELECT game_id FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not row:
            return
        game_id = row[0]
        self.conn.execute(
            "UPDATE capture_runs SET active = 0 WHERE game_id = ?", (game_id,)
        )
        self.conn.execute(
            "UPDATE capture_runs SET active = 1 WHERE id = ?", (run_id,)
        )
        self.conn.commit()

    def rename_capture_run(self, run_id: str, name: str) -> None:
        self.conn.execute(
            "UPDATE capture_runs SET name = ? WHERE id = ?", (name, run_id)
        )
        self.conn.commit()

    def delete_capture_run(self, run_id: str) -> None:
        """Soft-delete: deactivate all splits in the run, remove the record."""
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            "UPDATE splits SET active = 0, updated_at = ? WHERE reference_id = ?",
            (now, run_id),
        )
        self.conn.execute("DELETE FROM capture_runs WHERE id = ?", (run_id,))
        self.conn.commit()

    def get_splits_by_reference(self, reference_id: str) -> list[dict]:
        cur = self.conn.execute(
            """SELECT id, game_id, level_number, room_id, goal, description,
                      reference_time_ms, state_path, active, ordinal, reference_id
               FROM splits WHERE reference_id = ? AND active = 1
               ORDER BY ordinal""",
            (reference_id,),
        )
        cols = ["id", "game_id", "level_number", "room_id", "goal", "description",
                "reference_time_ms", "state_path", "active", "ordinal", "reference_id"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
```

- [ ] **Step 7: Implement split edit methods**

Add to `python/spinlab/db.py`:

```python
    def update_split(self, split_id: str, **kwargs) -> None:
        """Partial update: pass description=, goal=, active= as kwargs."""
        allowed = {"description", "goal", "active"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        if "active" in updates:
            updates["active"] = int(updates["active"])  # bool → int for SQLite
        now = datetime.utcnow().isoformat()
        sets = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [now, split_id]
        self.conn.execute(
            f"UPDATE splits SET {sets}, updated_at = ? WHERE id = ?", vals
        )
        self.conn.commit()

    def soft_delete_split(self, split_id: str) -> None:
        self.update_split(split_id, active=0)
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd python && python -m pytest ../tests/test_db_references.py -v`
Expected: All PASS

- [ ] **Step 9: Run full test suite**

Run: `cd python && python -m pytest ../tests/ -v`
Expected: All PASS

- [ ] **Step 10: Commit**

```bash
git add python/spinlab/db.py python/spinlab/models.py tests/test_db_references.py
git commit -m "feat(db): capture_runs table, reference_id FK, split edit + reference CRUD"
```

---

### Task 7: Migration — create capture_runs from existing manifests

Existing splits (no `reference_id`) get migrated: create a capture_run from the manifest file name, assign splits to it, set ordinals from manifest order.

**Files:**
- Modify: `python/spinlab/orchestrator.py:86-103`
- Test: `tests/test_db_references.py`

- [ ] **Step 1: Write failing test for manifest-based capture_run creation**

Add to `tests/test_db_references.py`:

```python
class TestManifestMigration:
    def test_seed_creates_capture_run(self, db):
        from spinlab.orchestrator import seed_db_from_manifest
        manifest = {
            "game_id": "g",
            "category": "any%",
            "captured_at": "2026-03-12T00:00:00Z",
            "splits": [
                {"id": "g:1:0:normal", "level_number": 1, "room_id": 0,
                 "goal": "normal", "name": "Level 1", "reference_time_ms": 5000},
                {"id": "g:2:0:key", "level_number": 2, "room_id": 0,
                 "goal": "key", "name": "Level 2", "reference_time_ms": 8000},
            ],
        }
        seed_db_from_manifest(db, manifest, "Game")
        refs = db.list_capture_runs("g")
        assert len(refs) == 1
        splits = db.get_splits_by_reference(refs[0]["id"])
        assert len(splits) == 2
        assert splits[0]["ordinal"] == 1
        assert splits[1]["ordinal"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest ../tests/test_db_references.py::TestManifestMigration -v`
Expected: FAIL — seed_db_from_manifest doesn't create capture_runs

- [ ] **Step 3: Update seed_db_from_manifest to create capture_run**

In `python/spinlab/orchestrator.py`:

```python
import uuid

def seed_db_from_manifest(db: Database, manifest: dict, game_name: str) -> None:
    """Upsert game + all splits from manifest into the DB.

    Creates a capture_run for the manifest if one doesn't exist for these splits.
    """
    game_id: str = manifest["game_id"]
    category: str = manifest.get("category", "any%")
    db.upsert_game(game_id, game_name, category)

    # Create a capture_run for this manifest
    captured_at = manifest.get("captured_at", datetime.utcnow().isoformat())
    run_id = f"manifest_{uuid.uuid4().hex[:8]}"
    run_name = f"Capture {captured_at[:10]}"
    db.create_capture_run(run_id, game_id, run_name)
    db.set_active_capture_run(run_id)

    for idx, entry in enumerate(manifest["splits"], start=1):
        split = Split(
            id=entry["id"],
            game_id=game_id,
            level_number=entry["level_number"],
            room_id=entry.get("room_id"),
            goal=entry["goal"],
            description=entry.get("name", ""),
            state_path=entry.get("state_path"),
            reference_time_ms=entry.get("reference_time_ms"),
            ordinal=idx,
            reference_id=run_id,
        )
        db.upsert_split(split)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && python -m pytest ../tests/test_db_references.py -v`
Expected: All PASS

- [ ] **Step 5: Run full test suite**

Run: `cd python && python -m pytest ../tests/ -v`
Expected: All PASS (check that existing orchestrator tests still work with the new seed behavior)

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/orchestrator.py tests/test_db_references.py
git commit -m "feat: seed_db_from_manifest creates capture_run with ordinals"
```

---

### Task 8: Reference and split API endpoints

Add REST endpoints for split editing and reference CRUD to the dashboard.

**Files:**
- Modify: `python/spinlab/dashboard.py`
- Test: `tests/test_dashboard_references.py` (**create**)

- [ ] **Step 1: Write failing tests for API endpoints**

Create `tests/test_dashboard_references.py`:

```python
"""Tests for reference and split management API endpoints."""
import json
import pytest
from pathlib import Path

from fastapi.testclient import TestClient
from spinlab.db import Database
from spinlab.models import Split


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("test_game", "Test Game", "any%")
    return d


@pytest.fixture
def client(db, tmp_path):
    from spinlab.dashboard import create_app
    state_file = tmp_path / "state.json"
    app = create_app(db=db, game_id="test_game", state_file=state_file)
    return TestClient(app)


class TestReferenceEndpoints:
    def test_list_references(self, client, db):
        db.create_capture_run("ref1", "test_game", "Run 1")
        resp = client.get("/api/references")
        assert resp.status_code == 200
        assert len(resp.json()["references"]) == 1

    def test_create_reference(self, client):
        resp = client.post("/api/references", json={"name": "New Run"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "New Run"

    def test_rename_reference(self, client, db):
        db.create_capture_run("ref1", "test_game", "Old")
        resp = client.patch("/api/references/ref1", json={"name": "New"})
        assert resp.status_code == 200
        refs = db.list_capture_runs("test_game")
        assert refs[0]["name"] == "New"

    def test_delete_reference(self, client, db):
        db.create_capture_run("ref1", "test_game", "Run 1")
        s = Split(id="s1", game_id="test_game", level_number=1, room_id=0,
                  goal="normal", reference_id="ref1")
        db.upsert_split(s)
        resp = client.delete("/api/references/ref1")
        assert resp.status_code == 200
        assert db.list_capture_runs("test_game") == []

    def test_activate_reference(self, client, db):
        db.create_capture_run("ref1", "test_game", "Run 1")
        db.create_capture_run("ref2", "test_game", "Run 2")
        resp = client.post("/api/references/ref2/activate")
        assert resp.status_code == 200
        refs = db.list_capture_runs("test_game")
        active = [r for r in refs if r["active"]]
        assert active[0]["id"] == "ref2"


class TestSplitEditEndpoints:
    def test_patch_split(self, client, db):
        s = Split(id="s1", game_id="test_game", level_number=1, room_id=0, goal="normal")
        db.upsert_split(s)
        resp = client.patch("/api/splits/s1", json={"description": "Yoshi 1"})
        assert resp.status_code == 200

    def test_delete_split(self, client, db):
        s = Split(id="s1", game_id="test_game", level_number=1, room_id=0, goal="normal")
        db.upsert_split(s)
        resp = client.delete("/api/splits/s1")
        assert resp.status_code == 200
        assert db.get_all_splits_with_model("test_game") == []


class TestImportManifest:
    def test_import_manifest(self, client, tmp_path):
        import yaml
        manifest = {
            "game_id": "test_game",
            "category": "any%",
            "splits": [
                {"id": "test_game:1:0:normal", "level_number": 1, "room_id": 0,
                 "goal": "normal", "name": "L1", "reference_time_ms": 5000},
            ],
        }
        manifest_path = tmp_path / "test_manifest.yaml"
        with manifest_path.open("w") as f:
            yaml.dump(manifest, f)
        resp = client.post(
            "/api/import-manifest",
            json={"path": str(manifest_path)},
        )
        assert resp.status_code == 200
        assert resp.json()["splits_imported"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && python -m pytest ../tests/test_dashboard_references.py -v`
Expected: FAIL — endpoints don't exist

- [ ] **Step 3: Add reference and split endpoints to dashboard.py**

Add these endpoints inside `create_app()` in `python/spinlab/dashboard.py`:

```python
    # -- Reference management --

    @app.get("/api/references")
    def list_references():
        return {"references": db.list_capture_runs(game_id)}

    @app.post("/api/references")
    def create_reference(body: dict):
        import uuid
        run_id = f"ref_{uuid.uuid4().hex[:8]}"
        name = body.get("name", "Untitled")
        db.create_capture_run(run_id, game_id, name)
        return {"id": run_id, "name": name}

    @app.patch("/api/references/{ref_id}")
    def rename_reference(ref_id: str, body: dict):
        name = body.get("name")
        if name:
            db.rename_capture_run(ref_id, name)
        return {"status": "ok"}

    @app.delete("/api/references/{ref_id}")
    def delete_reference(ref_id: str):
        db.delete_capture_run(ref_id)
        return {"status": "ok"}

    @app.post("/api/references/{ref_id}/activate")
    def activate_reference(ref_id: str):
        db.set_active_capture_run(ref_id)
        return {"status": "ok"}

    @app.get("/api/references/{ref_id}/splits")
    def get_reference_splits(ref_id: str):
        return {"splits": db.get_splits_by_reference(ref_id)}

    # -- Split editing --

    @app.patch("/api/splits/{split_id}")
    def update_split(split_id: str, body: dict):
        db.update_split(split_id, **body)
        return {"status": "ok"}

    @app.delete("/api/splits/{split_id}")
    def delete_split(split_id: str):
        db.soft_delete_split(split_id)
        return {"status": "ok"}

    # -- Manifest import --

    @app.post("/api/import-manifest")
    def import_manifest(body: dict):
        import yaml
        from spinlab.orchestrator import seed_db_from_manifest
        manifest_path = Path(body["path"])
        with manifest_path.open(encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
        game_name = manifest.get("game_id", game_id)
        seed_db_from_manifest(db, manifest, game_name)
        return {"status": "ok", "splits_imported": len(manifest.get("splits", []))}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && python -m pytest ../tests/test_dashboard_references.py -v`
Expected: All PASS

- [ ] **Step 5: Run full suite**

Run: `cd python && python -m pytest ../tests/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/dashboard.py tests/test_dashboard_references.py
git commit -m "feat(api): reference CRUD, split PATCH/DELETE, manifest import endpoints"
```

---

### Task 9: Manage tab UI — split list and reference selector

Build the Manage tab frontend: split table with inline editing, reference dropdown.

**Files:**
- Modify: `python/spinlab/static/index.html`
- Modify: `python/spinlab/static/app.js`
- Modify: `python/spinlab/static/style.css`

- [ ] **Step 1: Update Manage tab HTML**

Replace the Manage tab `<section>` in `index.html`:

```html
    <!-- Manage Tab -->
    <section id="tab-manage" class="tab-content">
      <div class="manage-section">
        <h3>Reference Run</h3>
        <div class="ref-row">
          <select id="ref-select"></select>
          <button id="btn-ref-rename" class="btn-sm" title="Rename">✎</button>
          <button id="btn-ref-delete" class="btn-sm btn-danger-sm" title="Delete">✕</button>
        </div>
      </div>

      <div class="manage-section">
        <h3>Splits</h3>
        <table id="split-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Level</th>
              <th>Goal</th>
              <th>Ref</th>
              <th></th>
            </tr>
          </thead>
          <tbody id="split-body"></tbody>
        </table>
      </div>

      <div class="manage-section">
        <h3>Data</h3>
        <p class="dim">Clear all session history, attempts, and model state. Keeps splits and game config.</p>
        <button id="btn-reset" class="btn-danger">Clear All Data</button>
        <p id="reset-status" class="dim"></p>
      </div>
    </section>
```

- [ ] **Step 2: Add Manage tab JS**

Append to `app.js`:

```javascript
// === Manage tab ===
async function fetchManage() {
  try {
    const refsRes = await fetch('/api/references');
    const refsData = await refsRes.json();
    const refs = refsData.references || [];
    // Find active reference, fetch its splits
    const active = refs.find(r => r.active);
    let splits = [];
    if (active) {
      const splitsRes = await fetch('/api/references/' + active.id + '/splits');
      const splitsData = await splitsRes.json();
      splits = splitsData.splits || [];
    }
    updateManage(refs, splits);
  } catch (_) {}
}

function updateManage(refs, splits) {
  // Reference dropdown
  const sel = document.getElementById('ref-select');
  sel.innerHTML = '';
  refs.forEach(r => {
    const opt = document.createElement('option');
    opt.value = r.id;
    opt.textContent = r.name + (r.active ? ' ●' : '');
    if (r.active) opt.selected = true;
    sel.appendChild(opt);
  });

  // Split table
  const body = document.getElementById('split-body');
  body.innerHTML = '';
  splits.forEach(s => {
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td><input class="split-name-input" value="' + (s.description || '') + '" ' +
        'data-id="' + s.id + '" data-field="description"></td>' +
      '<td>' + s.level_number + '</td>' +
      '<td>' + s.goal + '</td>' +
      '<td>' + (s.reference_time_ms ? formatTime(s.reference_time_ms) : '—') + '</td>' +
      '<td><button class="btn-x" data-id="' + s.id + '">✕</button></td>';
    body.appendChild(tr);
  });
}

// Inline edit: blur saves
document.getElementById('split-body').addEventListener('focusout', async (e) => {
  if (!e.target.classList.contains('split-name-input')) return;
  const id = e.target.dataset.id;
  const field = e.target.dataset.field;
  const value = e.target.value;
  await fetch('/api/splits/' + id, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ [field]: value }),
  });
});

// Delete split
document.getElementById('split-body').addEventListener('click', async (e) => {
  if (!e.target.classList.contains('btn-x')) return;
  if (!confirm('Remove this split?')) return;
  const id = e.target.dataset.id;
  await fetch('/api/splits/' + id, { method: 'DELETE' });
  fetchManage();
});

// Activate reference on change
document.getElementById('ref-select').addEventListener('change', async (e) => {
  await fetch('/api/references/' + e.target.value + '/activate', { method: 'POST' });
  fetchManage();
});

// Rename reference
document.getElementById('btn-ref-rename').addEventListener('click', async () => {
  const sel = document.getElementById('ref-select');
  const name = prompt('New name:', sel.options[sel.selectedIndex]?.text.replace(' ●', ''));
  if (!name) return;
  await fetch('/api/references/' + sel.value, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
  fetchManage();
});

// Delete reference
document.getElementById('btn-ref-delete').addEventListener('click', async () => {
  if (!confirm('Delete this reference and all its splits?')) return;
  const sel = document.getElementById('ref-select');
  await fetch('/api/references/' + sel.value, { method: 'DELETE' });
  fetchManage();
});

// Integrate into existing tab handler — modify the existing listener at top of file.
// Change `if (btn.dataset.tab === 'model') fetchModel();` to:
//   if (btn.dataset.tab === 'model') fetchModel();
//   if (btn.dataset.tab === 'manage') fetchManage();
```

- [ ] **Step 3: Add Manage tab CSS**

Append to `style.css`:

```css
/* Manage: reference row */
.ref-row {
  display: flex;
  gap: 6px;
  align-items: center;
}
.ref-row select {
  flex: 1;
  background: var(--surface);
  color: var(--text);
  border: 1px solid var(--card);
  padding: 4px 8px;
  border-radius: 4px;
  font-family: inherit;
  font-size: 12px;
}
.btn-sm {
  background: var(--card);
  color: var(--text);
  border: 1px solid var(--text-dim);
  padding: 3px 8px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
}
.btn-danger-sm {
  background: var(--red);
  color: #fff;
  border: none;
  padding: 3px 8px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
}

/* Manage: split table */
#split-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11px;
}
#split-table th {
  text-align: left;
  color: var(--text-dim);
  padding: 4px 5px;
  border-bottom: 1px solid var(--card);
}
#split-table td {
  padding: 4px 5px;
  border-bottom: 1px solid var(--surface);
}
.split-name-input {
  background: transparent;
  color: var(--text);
  border: 1px solid transparent;
  padding: 2px 4px;
  width: 100%;
  font-family: inherit;
  font-size: 11px;
}
.split-name-input:focus {
  border-color: var(--accent);
  outline: none;
}
.btn-x {
  background: none;
  border: none;
  color: var(--red);
  cursor: pointer;
  font-size: 12px;
  padding: 2px 4px;
}
```

- [ ] **Step 4: Verify manually** — load dashboard, switch to Manage tab, check reference dropdown and split list render correctly. Test inline name editing (focus out saves) and delete.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/static/index.html python/spinlab/static/app.js \
        python/spinlab/static/style.css
git commit -m "feat(dashboard): Manage tab with split editor and reference selector"
```

---

## Chunk 3: Phase 3 — Unified Session Manager

Dashboard becomes the single always-on process. TCP connection moves into the FastAPI app. Practice loop runs as an async background task.

---

### Task 10: Create tcp_manager.py — async TCP client

**Files:**
- Create: `python/spinlab/tcp_manager.py`
- Create: `tests/test_tcp_manager.py`

- [ ] **Step 1: Write failing tests for TcpManager**

Create `tests/test_tcp_manager.py`:

```python
"""Tests for TcpManager async TCP client."""
import asyncio
import json
import pytest

from spinlab.tcp_manager import TcpManager


@pytest.fixture
def tcp_server():
    """Create a real TCP server on a random port for testing."""
    import socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    yield srv, port
    srv.close()


@pytest.mark.asyncio
async def test_connect_and_send(tcp_server):
    srv, port = tcp_server
    mgr = TcpManager("127.0.0.1", port)
    await mgr.connect()
    assert mgr.is_connected

    # Accept on server side
    conn, _ = srv.accept()
    conn.settimeout(2)

    await mgr.send("ping")
    data = conn.recv(1024).decode()
    assert data.strip() == "ping"

    conn.close()
    await mgr.disconnect()
    assert not mgr.is_connected


@pytest.mark.asyncio
async def test_recv_event(tcp_server):
    srv, port = tcp_server
    mgr = TcpManager("127.0.0.1", port)
    await mgr.connect()

    conn, _ = srv.accept()
    event = {"event": "attempt_result", "split_id": "s1", "completed": True, "time_ms": 5000}
    conn.sendall((json.dumps(event) + "\n").encode())

    evt = await mgr.recv_event(timeout=2.0)
    assert evt is not None
    assert evt["event"] == "attempt_result"

    conn.close()
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_connect_refused():
    mgr = TcpManager("127.0.0.1", 59999)  # nothing listening
    connected = await mgr.connect(timeout=0.5)
    assert not connected
    assert not mgr.is_connected
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && python -m pytest ../tests/test_tcp_manager.py -v`
Expected: FAIL — module doesn't exist

- [ ] **Step 3: Implement TcpManager**

Create `python/spinlab/tcp_manager.py`:

```python
"""Async TCP client for communicating with the Lua TCP server.

Uses a single reader coroutine that dispatches events to an asyncio.Queue.
This avoids the problem of multiple consumers competing for the same StreamReader.
Both reference capture and practice loop read from the same queue.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

logger = logging.getLogger(__name__)


class TcpManager:
    """Async wrapper around the Lua TCP socket with event dispatch."""

    def __init__(self, host: str = "127.0.0.1", port: int = 15482) -> None:
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self.on_disconnect: Callable | None = None  # callback when connection drops

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self, timeout: float = 5.0) -> bool:
        """Connect to Lua TCP server. Returns True on success."""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=timeout,
            )
            logger.info("TCP connected to %s:%d", self.host, self.port)
            # Start the single reader coroutine
            self._read_task = asyncio.create_task(self._read_loop())
            return True
        except (OSError, asyncio.TimeoutError) as e:
            logger.debug("TCP connect failed: %s", e)
            self._reader = None
            self._writer = None
            return False

    async def disconnect(self) -> None:
        """Clean shutdown."""
        if self._read_task:
            self._read_task.cancel()
            self._read_task = None
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except OSError:
                pass
            self._writer = None
            self._reader = None
            logger.info("TCP disconnected")
        # Drain any remaining events
        while not self.events.empty():
            try:
                self.events.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def send(self, msg: str) -> None:
        """Send a newline-delimited message."""
        if not self._writer:
            raise ConnectionError("Not connected")
        self._writer.write((msg + "\n").encode("utf-8"))
        await self._writer.drain()

    async def recv_event(self, timeout: float | None = None) -> dict | None:
        """Wait for the next JSON event from the queue. Returns None on timeout."""
        try:
            if timeout is not None:
                return await asyncio.wait_for(self.events.get(), timeout=timeout)
            return await self.events.get()
        except asyncio.TimeoutError:
            return None

    async def _read_loop(self) -> None:
        """Single reader coroutine: reads lines, parses JSON, puts on queue."""
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
                    pass  # skip non-JSON lines (ok:queued, pong, heartbeat)
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            # Connection dropped — clean up
            self._writer = None
            self._reader = None
            if self.on_disconnect:
                self.on_disconnect()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && python -m pytest ../tests/test_tcp_manager.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/tcp_manager.py tests/test_tcp_manager.py
git commit -m "feat: add TcpManager async TCP client for Lua communication"
```

---

### Task 11: Extract practice loop into practice.py

Extract the orchestrator's practice loop into an async function that can run as a background task in the FastAPI app.

**Files:**
- Create: `python/spinlab/practice.py`
- Create: `tests/test_practice.py`

- [ ] **Step 1: Write failing test for practice loop**

Create `tests/test_practice.py`:

```python
"""Tests for the async practice loop."""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

from spinlab.db import Database
from spinlab.models import Split
from spinlab.practice import PracticeSession


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("g", "Game", "any%")
    s = Split(id="g:1:0:normal", game_id="g", level_number=1, room_id=0,
              goal="normal", description="L1", state_path="/tmp/test.mss",
              reference_time_ms=5000, ordinal=1)
    d.upsert_split(s)
    return d


@pytest.mark.asyncio
async def test_practice_session_picks_and_sends(db):
    """Practice session should pick a split and send practice_load."""
    mock_tcp = AsyncMock()
    mock_tcp.is_connected = True
    mock_tcp.send = AsyncMock()

    # Simulate receiving an attempt_result after send
    result_event = {
        "event": "attempt_result",
        "split_id": "g:1:0:normal",
        "completed": True,
        "time_ms": 4500,
        "goal": "normal",
    }

    async def fake_recv_event(timeout=None):
        return result_event

    mock_tcp.recv_event = fake_recv_event

    session = PracticeSession(tcp=mock_tcp, db=db, game_id="g")
    # Run one iteration
    await session.run_one()

    # Verify practice_load was sent
    mock_tcp.send.assert_called_once()
    sent = mock_tcp.send.call_args[0][0]
    assert sent.startswith("practice_load:")

    # Verify attempt was logged
    attempts = db.get_split_attempts("g:1:0:normal")
    assert len(attempts) == 1
    assert attempts[0]["completed"] == 1


@pytest.mark.asyncio
async def test_practice_session_state(db):
    session = PracticeSession(tcp=AsyncMock(), db=db, game_id="g")
    assert session.is_running is False
    assert session.current_split_id is None
    assert session.splits_attempted == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && python -m pytest ../tests/test_practice.py -v`
Expected: FAIL — module doesn't exist

- [ ] **Step 3: Implement PracticeSession**

Create `python/spinlab/practice.py`:

```python
"""Practice session loop — runs as async background task in dashboard."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from .models import Attempt, SplitCommand
from .scheduler import Scheduler

if TYPE_CHECKING:
    from .db import Database
    from .tcp_manager import TcpManager

logger = logging.getLogger(__name__)


class PracticeSession:
    """Manages a practice session: picks splits, sends to Lua, processes results."""

    def __init__(
        self,
        tcp: TcpManager,
        db: Database,
        game_id: str,
        auto_advance_delay_ms: int = 2000,
        on_attempt: Callable | None = None,
    ) -> None:
        self.tcp = tcp
        self.db = db
        self.game_id = game_id
        self.auto_advance_delay_ms = auto_advance_delay_ms
        self.on_attempt = on_attempt

        self.scheduler = Scheduler(db, game_id)
        self.session_id = uuid.uuid4().hex
        self.started_at = datetime.utcnow().isoformat() + "Z"

        self.is_running = False
        self.current_split_id: str | None = None
        self.queue: list[str] = []
        self.splits_attempted = 0
        self.splits_completed = 0
        self._skipped: set[str] = set()

    def start(self) -> None:
        self.db.create_session(self.session_id, self.game_id)
        self.is_running = True

    def stop(self) -> None:
        self.is_running = False
        self.db.end_session(
            self.session_id, self.splits_attempted, self.splits_completed
        )

    async def run_one(self) -> bool:
        """Run one pick-send-receive cycle. Returns False if no splits available."""
        import os

        picked = self.scheduler.pick_next()
        if picked is None:
            return False

        # Skip missing state files
        if picked.state_path and not os.path.exists(picked.state_path):
            self._skipped.add(picked.split_id)
            active = self.db.get_active_splits(self.game_id)
            if all(s.id in self._skipped for s in active):
                return False
            return True  # skip but continue

        # Compute expected time
        expected_time_ms = None
        if picked.estimator_state and picked.estimator_state.mu > 0:
            expected_time_ms = int(picked.estimator_state.mu * 1000)

        cmd = SplitCommand(
            id=picked.split_id,
            state_path=picked.state_path,
            goal=picked.goal,
            description=picked.description,
            reference_time_ms=picked.reference_time_ms,
            auto_advance_delay_ms=self.auto_advance_delay_ms,
            expected_time_ms=expected_time_ms,
        )

        self.current_split_id = cmd.id
        self.queue = [q for q in self.scheduler.peek_next_n(3) if q != cmd.id][:2]

        await self.tcp.send("practice_load:" + json.dumps(cmd.to_dict()))

        # Wait for attempt_result from the shared event queue
        while self.is_running and self.tcp.is_connected:
            event = await self.tcp.recv_event(timeout=1.0)
            if event is None:
                continue  # timeout, check if still running
            if event.get("event") == "attempt_result":
                self._process_result(event, cmd)
                break

        self.current_split_id = None
        return True

    def _process_result(self, result: dict, cmd: SplitCommand) -> None:
        attempt = Attempt(
            split_id=result["split_id"],
            session_id=self.session_id,
            completed=result["completed"],
            time_ms=result.get("time_ms"),
            goal_matched=(result.get("goal") == cmd.goal) if result.get("completed") else None,
            source="practice",
        )
        self.db.log_attempt(attempt)
        self.scheduler.process_attempt(
            result["split_id"],
            time_ms=result.get("time_ms", 0),
            completed=result["completed"],
        )
        self.splits_attempted += 1
        if result["completed"]:
            self.splits_completed += 1
        if self.on_attempt:
            self.on_attempt(attempt)

    async def run_loop(self) -> None:
        """Run the full practice loop until stopped or no splits."""
        self.start()
        try:
            while self.is_running and self.tcp.is_connected:
                if not await self.run_one():
                    break
        finally:
            try:
                await self.tcp.send("practice_stop")
            except (ConnectionError, OSError):
                pass
            self.stop()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && python -m pytest ../tests/test_practice.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/practice.py tests/test_practice.py
git commit -m "feat: extract practice loop into async PracticeSession class"
```

---

### Task 12: Integrate TCP + practice into dashboard

Wire up the TCP manager and practice session into the FastAPI app. Add mode detection, practice start/stop endpoints, and remove state file dependency.

**Files:**
- Modify: `python/spinlab/dashboard.py` (major rewrite)
- Modify: `tests/test_dashboard.py` (update for new state shape)

- [ ] **Step 1: Rewrite dashboard.py with TCP integration**

Rewrite `python/spinlab/dashboard.py` to integrate the TCP manager, practice session, and auto-reconnect. Key changes:

1. `create_app()` accepts `host`/`port` instead of `state_file`
2. Background task for TCP auto-reconnect (every 3s)
3. Background task for reference capture (pairing events in reference mode)
4. `/api/state` reads from in-memory state (no state file)
5. New endpoints: `POST /api/practice/start`, `POST /api/practice/stop`

```python
"""SpinLab dashboard — FastAPI web app, session manager, TCP client."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .db import Database
from .tcp_manager import TcpManager
from .practice import PracticeSession

logger = logging.getLogger(__name__)


def create_app(
    db: Database,
    game_id: str,
    state_file: Path | None = None,  # deprecated, ignored
    host: str = "127.0.0.1",
    port: int = 15482,
) -> FastAPI:
    from spinlab.scheduler import Scheduler

    app = FastAPI(title="SpinLab Dashboard")

    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    from fastapi.responses import FileResponse
    from starlette.middleware.base import BaseHTTPMiddleware

    class NoCacheStaticMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            if request.url.path.startswith("/static/"):
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
            return response

    app.add_middleware(NoCacheStaticMiddleware)

    # -- Shared state --
    tcp = TcpManager(host, port)
    _scheduler: list = [None]  # mutable container for nonlocal
    _practice: list = [None]   # PracticeSession | None
    _practice_task: list = [None]  # asyncio.Task | None
    _reconnect_task: list = [None]

    # Reference capture state
    _ref_pending: dict[tuple[int, int], dict] = {}  # (level, room) -> entrance event

    def _get_scheduler() -> Scheduler:
        if _scheduler[0] is None:
            _scheduler[0] = Scheduler(db, game_id)
        return _scheduler[0]

    def _current_mode() -> str:
        if _practice[0] and _practice[0].is_running:
            return "practice"
        if tcp.is_connected:
            return "reference"
        return "idle"

    # -- TCP auto-reconnect --
    async def _reconnect_loop():
        while True:
            await asyncio.sleep(3)
            if not tcp.is_connected:
                await tcp.connect(timeout=2)

    @app.on_event("startup")
    async def startup():
        _reconnect_task[0] = asyncio.create_task(_reconnect_loop())

    @app.on_event("shutdown")
    async def shutdown():
        if _reconnect_task[0]:
            _reconnect_task[0].cancel()
        if _practice_task[0]:
            _practice_task[0].cancel()
        await tcp.disconnect()

    # -- Endpoints --

    @app.get("/")
    def root():
        return FileResponse(str(static_dir / "index.html"))

    @app.get("/api/state")
    def api_state():
        mode = _current_mode()
        sched = _get_scheduler()

        current_split = None
        queue: list[dict] = []
        session_dict = None

        if mode == "practice" and _practice[0]:
            ps = _practice[0]
            session_dict = {
                "id": ps.session_id,
                "started_at": ps.started_at,
                "splits_attempted": ps.splits_attempted,
                "splits_completed": ps.splits_completed,
            }
            if ps.current_split_id:
                splits = db.get_all_splits_with_model(game_id)
                split_map = {s["id"]: s for s in splits}
                if ps.current_split_id in split_map:
                    current_split = split_map[ps.current_split_id]
                    current_split["attempt_count"] = db.get_split_attempt_count(
                        ps.current_split_id, ps.session_id
                    )
                    model_row = db.load_model_state(ps.current_split_id)
                    if model_row and model_row["state_json"]:
                        from spinlab.estimators.kalman import KalmanState
                        from spinlab.estimators import get_estimator
                        state = KalmanState.from_dict(json.loads(model_row["state_json"]))
                        est = get_estimator(model_row["estimator"])
                        current_split["drift_info"] = est.drift_info(state)

            # Queue from scheduler
            queue_ids = sched.peek_next_n(3)
            if ps.current_split_id:
                queue_ids = [q for q in queue_ids if q != ps.current_split_id][:2]
            splits_all = db.get_all_splits_with_model(game_id)
            smap = {s["id"]: s for s in splits_all}
            queue = [smap[sid] for sid in queue_ids if sid in smap]

        recent = db.get_recent_attempts(game_id, limit=8)

        return {
            "mode": mode,
            "tcp_connected": tcp.is_connected,
            "current_split": current_split,
            "queue": queue,
            "recent": recent,
            "session": session_dict,
            "allocator": sched.allocator.name,
            "estimator": sched.estimator.name,
        }

    @app.post("/api/practice/start")
    async def practice_start():
        if _practice[0] and _practice[0].is_running:
            return {"status": "already_running"}
        if not tcp.is_connected:
            return {"status": "not_connected"}

        ps = PracticeSession(tcp=tcp, db=db, game_id=game_id)
        _practice[0] = ps
        _practice_task[0] = asyncio.create_task(ps.run_loop())
        return {"status": "started", "session_id": ps.session_id}

    @app.post("/api/practice/stop")
    async def practice_stop():
        if _practice[0] and _practice[0].is_running:
            _practice[0].is_running = False
            # Wait briefly for clean shutdown
            if _practice_task[0]:
                try:
                    await asyncio.wait_for(_practice_task[0], timeout=5)
                except asyncio.TimeoutError:
                    _practice_task[0].cancel()
            return {"status": "stopped"}
        return {"status": "not_running"}

    # -- Model / allocator / estimator (unchanged) --

    @app.get("/api/model")
    def api_model():
        sched = _get_scheduler()
        splits = sched.get_all_model_states()
        return {
            "estimator": sched.estimator.name,
            "allocator": sched.allocator.name,
            "splits": [
                {
                    "split_id": s.split_id,
                    "goal": s.goal,
                    "description": s.description,
                    "level_number": s.level_number,
                    "mu": round(s.estimator_state.mu, 2) if s.estimator_state else None,
                    "drift": round(s.estimator_state.d, 3) if s.estimator_state else None,
                    "marginal_return": round(s.marginal_return, 4),
                    "drift_info": s.drift_info,
                    "n_completed": s.n_completed,
                    "n_attempts": s.n_attempts,
                    "gold_ms": s.gold_ms,
                    "reference_time_ms": s.reference_time_ms,
                }
                for s in splits
            ],
        }

    @app.post("/api/allocator")
    def switch_allocator(body: dict):
        name = body.get("name")
        sched = _get_scheduler()
        sched.switch_allocator(name)
        return {"allocator": name}

    @app.post("/api/estimator")
    def switch_estimator(body: dict):
        name = body.get("name")
        sched = _get_scheduler()
        sched.switch_estimator(name)
        return {"estimator": name}

    @app.post("/api/reset")
    def reset_data():
        db.reset_all_data()
        _scheduler[0] = None  # force re-init with fresh defaults
        return {"status": "ok"}

    @app.get("/api/splits")
    def api_splits():
        splits = db.get_all_splits_with_model(game_id)
        return {"splits": splits}

    @app.get("/api/sessions")
    def api_sessions():
        sessions = db.get_session_history(game_id)
        return {"sessions": sessions}

    # -- Reference management --

    @app.get("/api/references")
    def list_references():
        return {"references": db.list_capture_runs(game_id)}

    @app.post("/api/references")
    def create_reference(body: dict):
        run_id = f"ref_{uuid.uuid4().hex[:8]}"
        name = body.get("name", "Untitled")
        db.create_capture_run(run_id, game_id, name)
        return {"id": run_id, "name": name}

    @app.patch("/api/references/{ref_id}")
    def rename_reference(ref_id: str, body: dict):
        name = body.get("name")
        if name:
            db.rename_capture_run(ref_id, name)
        return {"status": "ok"}

    @app.delete("/api/references/{ref_id}")
    def delete_reference(ref_id: str):
        db.delete_capture_run(ref_id)
        return {"status": "ok"}

    @app.post("/api/references/{ref_id}/activate")
    def activate_reference(ref_id: str):
        db.set_active_capture_run(ref_id)
        return {"status": "ok"}

    @app.get("/api/references/{ref_id}/splits")
    def get_reference_splits(ref_id: str):
        return {"splits": db.get_splits_by_reference(ref_id)}

    # -- Split editing --

    @app.patch("/api/splits/{split_id}")
    def update_split_endpoint(split_id: str, body: dict):
        db.update_split(split_id, **body)
        return {"status": "ok"}

    @app.delete("/api/splits/{split_id}")
    def delete_split(split_id: str):
        db.soft_delete_split(split_id)
        return {"status": "ok"}

    # -- Manifest import --

    @app.post("/api/import-manifest")
    def import_manifest(body: dict):
        import yaml
        from spinlab.orchestrator import seed_db_from_manifest
        manifest_path = Path(body["path"])
        with manifest_path.open(encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
        game_name = manifest.get("game_id", game_id)
        seed_db_from_manifest(db, manifest, game_name)
        return {"status": "ok", "splits_imported": len(manifest.get("splits", []))}

    return app
```

- [ ] **Step 2: Update existing dashboard tests**

Rewrite `tests/test_dashboard.py` for the new `create_app` signature:

```python
"""Tests for dashboard API endpoints."""
import json
import pytest
from pathlib import Path

from fastapi.testclient import TestClient
from spinlab.db import Database
from spinlab.models import Split, Attempt


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("test_game", "Test Game", "any%")
    return d


@pytest.fixture
def client(db, tmp_path):
    from spinlab.dashboard import create_app
    # TCP will fail to connect (nothing listening) — dashboard stays in idle mode
    app = create_app(db=db, game_id="test_game", host="127.0.0.1", port=59999)
    return TestClient(app)


def test_api_state_no_session(client):
    resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "idle"
    assert data["tcp_connected"] is False
    assert data["current_split"] is None


def test_api_state_idle_has_allocator(client):
    resp = client.get("/api/state")
    data = resp.json()
    assert "allocator" in data
    assert "estimator" in data


def test_api_splits_returns_all_with_model(client, db):
    s1 = Split(id="s1", game_id="test_game", level_number=1, room_id=0, goal="normal")
    s2 = Split(id="s2", game_id="test_game", level_number=2, room_id=0, goal="key")
    db.upsert_split(s1)
    db.upsert_split(s2)
    resp = client.get("/api/splits")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["splits"]) == 2


def test_api_sessions_returns_history(client, db):
    db.create_session("sess1", "test_game")
    db.end_session("sess1", 10, 8)
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    assert len(resp.json()["sessions"]) >= 1


def test_root_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "SpinLab" in resp.text


def test_practice_start_not_connected(client):
    """Practice start should fail gracefully when TCP is not connected."""
    resp = client.post("/api/practice/start")
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_connected"


def test_practice_stop_not_running(client):
    resp = client.post("/api/practice/stop")
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_running"
```

- [ ] **Step 3: Run all tests**

Run: `cd python && python -m pytest ../tests/ -v`
Expected: All PASS (update failing tests)

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): integrate TCP manager and practice loop, remove state file"
```

---

### Task 13: Lua TCP event forwarding for live reference capture

Modify the Lua script to forward transition events (level_entrance, level_exit) over TCP when a client is connected and practice mode is not active.

**Files:**
- Modify: `lua/spinlab.lua:281-332` (detect_transitions)

- [ ] **Step 1: Update detect_transitions to forward events over TCP**

In `lua/spinlab.lua`, in `detect_transitions()`, after each `log_jsonl()` call, also send the event over TCP:

```lua
local function detect_transitions(curr)
  -- Death: player animation transitions to 9
  if curr.player_anim == 9 and prev.player_anim ~= 9 then
    died_flag = true
    log("Death at level " .. curr.level_num .. " (not logged to JSONL)")
  end

  -- Level entrance: gameMode transitions to 18 (GmPrepareLevel)
  if curr.game_mode == 18 and prev.game_mode ~= 18 then
    if not died_flag then
      level_start_frame = frame_counter
      local state_fname = GAME_ID .. "_" .. curr.level_num .. "_" .. curr.room_num .. ".mss"
      local state_path  = STATE_DIR .. "/" .. state_fname
      if pending_save then
        log("WARNING: pending_save overwritten (was: " .. pending_save .. ")")
      end
      pending_save = state_path
      local event_data = {
        event      = "level_entrance",
        level      = curr.level_num,
        room       = curr.room_num,
        frame      = frame_counter,
        ts_ms      = ts_ms(),
        session    = "passive",
        state_path = state_path,
      }
      log_jsonl(event_data)
      -- Forward over TCP for live reference capture
      if client and not practice_mode then
        client:send(to_json(event_data) .. "\n")
      end
      log("Level entrance: " .. curr.level_num .. " -> queued state save: " .. state_fname)
    else
      died_flag = false
      log("Quick retry at level " .. curr.level_num .. " (not logged as entrance)")
    end
  end

  -- Level exit: exitMode leaves 0
  if curr.exit_mode ~= 0 and prev.exit_mode == 0 then
    local elapsed = math.floor((frame_counter - level_start_frame) / 60.0 * 1000)
    local goal = goal_type(curr)
    local event_data = {
      event      = "level_exit",
      level      = curr.level_num,
      room       = curr.room_num,
      goal       = goal,
      elapsed_ms = elapsed,
      frame      = frame_counter,
      ts_ms      = ts_ms(),
      session    = "passive",
    }
    log_jsonl(event_data)
    -- Forward over TCP for live reference capture
    if client and not practice_mode then
      client:send(to_json(event_data) .. "\n")
    end
    log("Level exit: " .. curr.level_num .. " goal=" .. goal .. " elapsed=" .. elapsed .. "ms")
  end
end
```

- [ ] **Step 2: Test manually in Mesen2** — connect dashboard, do a reference run, verify events appear in dashboard logs.

- [ ] **Step 3: Commit**

```bash
git add lua/spinlab.lua
git commit -m "feat(lua): forward transition events over TCP for live reference capture"
```

---

### Task 14: Live reference capture in dashboard

When in reference mode (TCP connected, no practice), the dashboard receives transition events and pairs them into splits in real-time.

**Files:**
- Modify: `python/spinlab/dashboard.py`

- [ ] **Step 1: Add reference capture background task**

Add a background task to `create_app()` that runs when in reference mode, consuming TCP events and pairing them into splits:

```python
    # Reference capture state
    _ref_pending: dict[tuple, dict] = {}  # (level, room) -> entrance event
    _ref_splits_count: list[int] = [0]
    _ref_capture_run_id: list[str | None] = [None]  # active capture_run for live capture

    def _clear_ref_state():
        """Clear reference capture state on disconnect or mode change."""
        _ref_pending.clear()
        _ref_splits_count[0] = 0
        _ref_capture_run_id[0] = None

    # Register disconnect callback to clear stale reference state
    tcp.on_disconnect = _clear_ref_state

    async def _event_dispatch_loop():
        """Single event consumer: reads from tcp.events queue, dispatches to
        reference capture (when not practicing) or ignores (practice reads its own)."""
        while True:
            if not tcp.is_connected:
                await asyncio.sleep(1)
                continue
            try:
                event = await tcp.recv_event(timeout=1.0)
                if event is None:
                    continue

                # During practice, the PracticeSession reads from the same queue
                # via tcp.recv_event() — skip non-attempt events here
                if _practice[0] and _practice[0].is_running:
                    continue

                # Reference mode: pair transition events into splits
                evt_type = event.get("event")
                if evt_type == "level_entrance":
                    key = (event["level"], event["room"])
                    _ref_pending[key] = event

                    # Create capture_run on first entrance event
                    if _ref_capture_run_id[0] is None:
                        import uuid
                        from datetime import datetime
                        run_id = f"live_{uuid.uuid4().hex[:8]}"
                        run_name = f"Live {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
                        db.create_capture_run(run_id, game_id, run_name)
                        db.set_active_capture_run(run_id)
                        _ref_capture_run_id[0] = run_id

                elif evt_type == "level_exit":
                    key = (event["level"], event["room"])
                    goal = event.get("goal", "abort")
                    if goal == "abort":
                        _ref_pending.pop(key, None)
                        continue
                    entrance = _ref_pending.pop(key, None)
                    if entrance:
                        _ref_splits_count[0] += 1
                        from .models import Split
                        split_id = Split.make_id(
                            game_id, entrance["level"], entrance["room"], goal
                        )
                        split = Split(
                            id=split_id,
                            game_id=game_id,
                            level_number=entrance["level"],
                            room_id=entrance["room"],
                            goal=goal,
                            state_path=entrance.get("state_path"),
                            reference_time_ms=event.get("elapsed_ms"),
                            ordinal=_ref_splits_count[0],
                            reference_id=_ref_capture_run_id[0],
                        )
                        db.upsert_split(split)
            except Exception:
                await asyncio.sleep(1)
```

Update the startup to use the single dispatch loop:

```python
    @app.on_event("startup")
    async def startup():
        _reconnect_task[0] = asyncio.create_task(_reconnect_loop())
        asyncio.create_task(_event_dispatch_loop())
```

**Important note on event routing:** During practice mode, `PracticeSession.run_one()` calls `tcp.recv_event()` directly on the shared queue, so it gets `attempt_result` events. The `_event_dispatch_loop` skips events during practice (`continue`). This means transition events during practice are dropped — which is correct because Lua suppresses passive logging during practice mode anyway.

- [ ] **Step 2: Update `/api/state` to include reference capture info**

In the `api_state()` response, when mode is "reference":

```python
        if mode == "reference":
            # Include live capture count
            pass  # sections_captured in response

        return {
            "mode": mode,
            "tcp_connected": tcp.is_connected,
            "current_split": current_split,
            "queue": queue,
            "recent": recent,
            "session": session_dict,
            "sections_captured": _ref_splits_count[0],
            "allocator": sched.allocator.name,
            "estimator": sched.estimator.name,
        }
```

- [ ] **Step 3: Run all tests**

Run: `cd python && python -m pytest ../tests/ -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/dashboard.py
git commit -m "feat(dashboard): live reference capture from TCP events"
```

---

### Task 15: Add practice start/stop to frontend

Add buttons to the Live tab for starting/stopping practice from the dashboard.

**Files:**
- Modify: `python/spinlab/static/index.html`
- Modify: `python/spinlab/static/app.js`
- Modify: `python/spinlab/static/style.css`

- [ ] **Step 1: Add practice controls to Live tab HTML**

In `index.html`, update the idle and practice mode sections:

Add to idle mode section:
```html
      <div id="mode-idle">
        <p class="dim">No active session</p>
        <p class="dim" id="tcp-status"></p>
      </div>
```

Add to reference mode section:
```html
      <div id="mode-reference" style="display:none">
        <h2>Reference Run</h2>
        <p id="ref-sections">Sections: 0</p>
        <button id="btn-practice-start" class="btn-primary">Start Practice</button>
      </div>
```

Add stop button to practice mode:
```html
        <button id="btn-practice-stop" class="btn-danger" style="margin:8px">Stop Practice</button>
```

- [ ] **Step 2: Add practice start/stop JS handlers**

Append to `app.js`:

```javascript
// === Practice start/stop ===
document.getElementById('btn-practice-start')?.addEventListener('click', async () => {
  await fetch('/api/practice/start', { method: 'POST' });
});

document.getElementById('btn-practice-stop')?.addEventListener('click', async () => {
  await fetch('/api/practice/stop', { method: 'POST' });
});
```

Update `updateLive()` to show TCP status:

```javascript
  // TCP status in idle mode
  const tcpEl = document.getElementById('tcp-status');
  if (tcpEl) {
    tcpEl.textContent = data.tcp_connected ? 'Emulator connected' : 'Waiting for emulator...';
  }
```

- [ ] **Step 3: Add button styles**

Append to `style.css`:

```css
.btn-primary {
  background: var(--accent);
  color: #000;
  border: none;
  padding: 8px 16px;
  border-radius: 4px;
  cursor: pointer;
  font-family: inherit;
  font-size: 12px;
  font-weight: 600;
}
.btn-primary:hover { opacity: 0.85; }
```

- [ ] **Step 4: Verify manually** — load dashboard, verify Start Practice button appears in reference mode, Stop Practice button in practice mode.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/static/index.html python/spinlab/static/app.js \
        python/spinlab/static/style.css
git commit -m "feat(dashboard): practice start/stop buttons in Live tab"
```

---

### Task 16: Update CLI and deprecate standalone orchestrator

Update `cli.py` so `spinlab dashboard` is the primary entry point. Keep `spinlab practice` working but print a deprecation warning.

**Files:**
- Modify: `python/spinlab/cli.py`
- Modify: `python/spinlab/orchestrator.py` (add deprecation warning)

- [ ] **Step 1: Add deprecation warning to orchestrator.run()**

At the top of `run()` in `python/spinlab/orchestrator.py`:

```python
def run(config_path: Path = Path("config.yaml")) -> None:
    import warnings
    warnings.warn(
        "Standalone orchestrator is deprecated. Use 'spinlab dashboard' instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    print("[DEPRECATED] Use 'spinlab dashboard' — the dashboard now manages practice sessions.")
    # ... rest of function unchanged
```

- [ ] **Step 2: Update dashboard CLI to pass host/port from config**

In `python/spinlab/cli.py`, update the dashboard subcommand to read TCP config:

```python
def dashboard_cmd(args):
    import yaml
    import uvicorn
    from spinlab.db import Database
    from spinlab.dashboard import create_app

    config_path = Path(args.config if hasattr(args, 'config') else "config.yaml")
    with config_path.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)

    game_id = config["game"]["id"]
    data_dir = Path(config["data"]["dir"])
    host = config["network"]["host"]
    port = config["network"]["port"]

    db = Database(data_dir / "spinlab.db")

    # Seed DB from manifest if splits are empty
    from spinlab.orchestrator import find_latest_manifest, load_manifest, seed_db_from_manifest
    if not db.get_active_splits(game_id):
        manifest_path = find_latest_manifest(data_dir)
        if manifest_path:
            manifest = load_manifest(manifest_path)
            seed_db_from_manifest(db, manifest, config["game"]["name"])

    app = create_app(db=db, game_id=game_id, host=host, port=port)
    uvicorn.run(app, host="0.0.0.0", port=args.port)
```

- [ ] **Step 3: Run all tests**

Run: `cd python && python -m pytest ../tests/ -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/cli.py python/spinlab/orchestrator.py
git commit -m "feat(cli): dashboard reads TCP config, deprecate standalone orchestrator"
```

---

## Post-implementation checklist

- [ ] All tests pass: `cd python && python -m pytest ../tests/ -v`
- [ ] Dashboard loads and all three tabs work
- [ ] Practice start/stop works from dashboard (requires Mesen2 running)
- [ ] Reference capture shows live splits during a run (requires Mesen2)
- [ ] Manage tab shows split list, inline editing works, reference switching works
- [ ] Model tab shows renamed headers with tooltips
- [ ] Lua overlay shows expected time (Kalman μ) instead of reference time

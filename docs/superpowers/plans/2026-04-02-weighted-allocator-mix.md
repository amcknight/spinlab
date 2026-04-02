# Weighted Allocator Mix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable multiple allocators with configurable integer weights (summing to 100%), dispatched via weighted random selection on each pick.

**Architecture:** A `MixAllocator` wrapper holds `(allocator, weight)` pairs and rolls a weighted die per pick. The scheduler always uses `MixAllocator`. Weights persist as JSON in the existing `allocator_config` table. The dashboard replaces the allocator dropdown with a multi-handle colored range slider and legend row.

**Tech Stack:** Python 3.11+ / FastAPI / vanilla JS + CSS / SQLite

---

### Task 1: Remove `peek_next_n` from Allocator ABC and implementations

**Files:**
- Modify: `python/spinlab/allocators/__init__.py:118-121`
- Modify: `python/spinlab/allocators/greedy.py:27-31`
- Modify: `python/spinlab/allocators/random.py:18-20`
- Modify: `python/spinlab/allocators/round_robin.py:21-28`
- Modify: `tests/test_allocators.py:28-45,63-68,82-86`

- [ ] **Step 1: Remove `peek_next_n` tests from `test_allocators.py`**

Delete these test methods:
- `TestGreedyAllocator.test_peek_returns_sorted_order` (lines 28-32)
- `TestGreedyAllocator.test_peek_empty_returns_empty` (lines 38-40)
- `TestGreedyAllocator.test_peek_more_than_available` (lines 42-45)
- `TestRandomAllocator.test_peek_no_replacement` (lines 63-68)
- `TestRoundRobinAllocator.test_peek_returns_upcoming` (lines 82-86)

- [ ] **Step 2: Run tests to verify they still pass (remaining allocator tests)**

Run: `python -m pytest tests/test_allocators.py -v`
Expected: 6 tests pass (3 Greedy empty+pick, 2 Random, 2 RoundRobin cycle tests), 0 peek tests remain.

- [ ] **Step 3: Remove `peek_next_n` abstract method from `Allocator` ABC**

In `python/spinlab/allocators/__init__.py`, remove lines 118-121:

```python
    @abstractmethod
    def peek_next_n(self, segment_states: list[SegmentWithModel], n: int) -> list[str]:
        """Preview next N segment_ids without side effects."""
        ...
```

- [ ] **Step 4: Remove `peek_next_n` from `GreedyAllocator`**

In `python/spinlab/allocators/greedy.py`, remove lines 27-31:

```python
    def peek_next_n(self, segment_states: list[SegmentWithModel], n: int) -> list[str]:
        shuffled = list(segment_states)
        random.shuffle(shuffled)
        shuffled.sort(key=_score, reverse=True)
        return [s.segment_id for s in shuffled[:n]]
```

- [ ] **Step 5: Remove `peek_next_n` from `RandomAllocator`**

In `python/spinlab/allocators/random.py`, remove lines 18-20:

```python
    def peek_next_n(self, segment_states: list[SegmentWithModel], n: int) -> list[str]:
        sample_size = min(n, len(segment_states))
        return [s.segment_id for s in _random.sample(segment_states, sample_size)]
```

- [ ] **Step 6: Remove `peek_next_n` from `RoundRobinAllocator`**

In `python/spinlab/allocators/round_robin.py`, remove lines 21-28:

```python
    def peek_next_n(self, segment_states: list[SegmentWithModel], n: int) -> list[str]:
        if not segment_states:
            return []
        result = []
        for i in range(min(n, len(segment_states))):
            idx = (self._index + i) % len(segment_states)
            result.append(segment_states[idx].segment_id)
        return result
```

- [ ] **Step 7: Run allocator tests**

Run: `python -m pytest tests/test_allocators.py -v`
Expected: All remaining tests pass.

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/allocators/__init__.py python/spinlab/allocators/greedy.py python/spinlab/allocators/random.py python/spinlab/allocators/round_robin.py tests/test_allocators.py
git commit -m "refactor: remove peek_next_n from Allocator ABC and implementations"
```

---

### Task 2: Remove `peek_next_n` from Scheduler, PracticeSession, and SessionManager

**Files:**
- Modify: `python/spinlab/scheduler.py:125-128`
- Modify: `python/spinlab/practice.py:44,95`
- Modify: `python/spinlab/session_manager.py:100,167-172`
- Modify: `tests/test_scheduler_kalman.py:93-98`
- Modify: `tests/test_practice.py:122`
- Modify: `tests/test_dashboard_integration.py:189-193`

- [ ] **Step 1: Remove `TestSchedulerPeek` from test_scheduler_kalman.py**

Delete the entire class (lines 93-98):

```python
class TestSchedulerPeek:
    def test_peek_next_n(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        results = sched.peek_next_n(3)
        assert len(results) == 3
        assert all(isinstance(r, str) for r in results)
```

- [ ] **Step 2: Remove queue test from test_dashboard_integration.py**

Delete `test_queue_contains_next_segments` (lines 189-193):

```python
    def test_queue_contains_next_segments(self, active_client):
        data = active_client.get("/api/state").json()
        queue_ids = [s["id"] for s in data["queue"]]
        assert len(queue_ids) == 2
        assert "s1" not in queue_ids
```

- [ ] **Step 3: Remove `peek_next_n` mock from test_practice.py**

In `tests/test_practice.py`, remove line 122:

```python
        ps.scheduler.peek_next_n = MagicMock(return_value=[])
```

- [ ] **Step 4: Remove `Scheduler.peek_next_n` method**

In `python/spinlab/scheduler.py`, remove lines 125-128:

```python
    def peek_next_n(self, n: int) -> list[str]:
        segments = SegmentWithModel.load_all(self.db, self.game_id, self.estimator.name)
        practicable = [s for s in segments if s.state_path and os.path.exists(s.state_path)]
        return self.allocator.peek_next_n(practicable, n)
```

- [ ] **Step 5: Remove `self.queue` from PracticeSession**

In `python/spinlab/practice.py`:

Remove line 44:
```python
        self.queue: list[str] = []
```

Remove line 95:
```python
        self.queue = [q for q in self.scheduler.peek_next_n(3) if q != cmd.id][:2]
```

- [ ] **Step 6: Remove queue building from SessionManager**

In `python/spinlab/session_manager.py`:

Remove `"queue": [],` from `get_state()` base dict (line 100).

Remove queue building from `_build_practice_state` (lines 167-172):
```python
        queue_ids = sched.peek_next_n(3)
        if ps.current_segment_id:
            queue_ids = [q for q in queue_ids if q != ps.current_segment_id][:2]
        segments_all = self.db.get_all_segments_with_model(self.game_id)
        smap = {s["id"]: s for s in segments_all}
        base["queue"] = [smap[sid] for sid in queue_ids if sid in smap]
```

- [ ] **Step 7: Run full test suite**

Run: `python -m pytest tests/ -v --timeout=30`
Expected: All tests pass. No references to `peek_next_n` or practice `queue` remain.

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/scheduler.py python/spinlab/practice.py python/spinlab/session_manager.py tests/test_scheduler_kalman.py tests/test_practice.py tests/test_dashboard_integration.py
git commit -m "refactor: remove peek_next_n from scheduler, practice loop, and session manager"
```

---

### Task 3: Create MixAllocator

**Files:**
- Create: `python/spinlab/allocators/mix.py`
- Create: `tests/test_mix_allocator.py`

- [ ] **Step 1: Write failing tests for MixAllocator**

Create `tests/test_mix_allocator.py`:

```python
"""Tests for MixAllocator weighted dispatch."""
import pytest
from unittest.mock import MagicMock
from spinlab.allocators import SegmentWithModel
from spinlab.allocators.greedy import GreedyAllocator
from spinlab.allocators.random import RandomAllocator
from spinlab.allocators.round_robin import RoundRobinAllocator
from spinlab.models import Estimate, ModelOutput


def _make_segment(segment_id: str, ms_per_attempt: float = 0.0) -> SegmentWithModel:
    out = ModelOutput(
        total=Estimate(expected_ms=10000.0, ms_per_attempt=ms_per_attempt, floor_ms=8000.0),
        clean=Estimate(expected_ms=10000.0, ms_per_attempt=ms_per_attempt, floor_ms=8000.0),
    )
    return SegmentWithModel(
        segment_id=segment_id, game_id="test", level_number=1,
        start_type="level_enter", start_ordinal=0,
        end_type="level_exit", end_ordinal=0,
        description="test", strat_version=1, state_path=None, active=True,
        model_outputs={"kalman": out}, selected_model="kalman",
    )


class TestMixAllocator:
    def test_single_allocator_100_percent(self):
        from spinlab.allocators.mix import MixAllocator
        greedy = GreedyAllocator()
        mix = MixAllocator(entries=[(greedy, 100)])
        segments = [_make_segment("a", 50.0), _make_segment("b", 100.0)]
        assert mix.pick_next(segments) == "b"

    def test_empty_segments_returns_none(self):
        from spinlab.allocators.mix import MixAllocator
        greedy = GreedyAllocator()
        mix = MixAllocator(entries=[(greedy, 100)])
        assert mix.pick_next([]) is None

    def test_empty_entries_returns_none(self):
        from spinlab.allocators.mix import MixAllocator
        mix = MixAllocator(entries=[])
        segments = [_make_segment("a", 50.0)]
        assert mix.pick_next(segments) is None

    def test_zero_weight_allocator_never_picked(self):
        from spinlab.allocators.mix import MixAllocator
        greedy = GreedyAllocator()
        random = RandomAllocator()
        # greedy always picks "b" (highest ms_per_attempt), random could pick either
        mix = MixAllocator(entries=[(greedy, 100), (random, 0)])
        segments = [_make_segment("a", 50.0), _make_segment("b", 100.0)]
        # With random at weight 0, greedy should always win
        results = {mix.pick_next(segments) for _ in range(20)}
        assert results == {"b"}

    def test_weighted_distribution_over_many_picks(self):
        from spinlab.allocators.mix import MixAllocator
        # Use mock allocators that return distinguishable values
        alloc_a = MagicMock()
        alloc_a.pick_next = MagicMock(return_value="from_a")
        alloc_b = MagicMock()
        alloc_b.pick_next = MagicMock(return_value="from_b")
        mix = MixAllocator(entries=[(alloc_a, 80), (alloc_b, 20)])
        segments = [_make_segment("x")]
        results = [mix.pick_next(segments) for _ in range(1000)]
        a_count = results.count("from_a")
        # With 80/20 split over 1000 picks, a should get ~800. Allow wide margin.
        assert 650 < a_count < 950

    def test_round_robin_preserves_state_across_picks(self):
        from spinlab.allocators.mix import MixAllocator
        rr = RoundRobinAllocator()
        # Give RR 100% weight so we can verify index advances
        mix = MixAllocator(entries=[(rr, 100)])
        segments = [_make_segment("a"), _make_segment("b"), _make_segment("c")]
        results = [mix.pick_next(segments) for _ in range(6)]
        assert results == ["a", "b", "c", "a", "b", "c"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mix_allocator.py -v`
Expected: ImportError — `spinlab.allocators.mix` does not exist yet.

- [ ] **Step 3: Implement MixAllocator**

Create `python/spinlab/allocators/mix.py`:

```python
"""MixAllocator: weighted random dispatch across multiple allocators."""
from __future__ import annotations

import random
from dataclasses import dataclass

from spinlab.allocators import Allocator, SegmentWithModel


@dataclass
class MixAllocator:
    """Holds (allocator, weight) pairs; dispatches each pick via weighted random."""

    entries: list[tuple[Allocator, float]]

    def pick_next(self, segment_states: list[SegmentWithModel]) -> str | None:
        if not segment_states or not self.entries:
            return None
        allocators, weights = zip(*self.entries)
        chosen = random.choices(allocators, weights=weights, k=1)[0]
        return chosen.pick_next(segment_states)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mix_allocator.py -v`
Expected: All 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/allocators/mix.py tests/test_mix_allocator.py
git commit -m "feat: add MixAllocator for weighted multi-allocator dispatch"
```

---

### Task 4: Update Scheduler to use MixAllocator with weights

**Files:**
- Modify: `python/spinlab/scheduler.py`
- Modify: `tests/test_scheduler_kalman.py:101-111`

- [ ] **Step 1: Write failing tests for weight-based scheduler**

In `tests/test_scheduler_kalman.py`, replace `TestSchedulerSwitch` (lines 101-111) with:

```python
class TestSchedulerWeights:
    def test_set_weights_persists_and_rebuilds(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        sched.set_allocator_weights({"greedy": 50, "random": 50})
        raw = db_with_segments.load_allocator_config("allocator_weights")
        import json
        saved = json.loads(raw)
        assert saved == {"greedy": 50, "random": 50}

    def test_set_weights_invalid_sum_raises(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        with pytest.raises(ValueError, match="must sum to 100"):
            sched.set_allocator_weights({"greedy": 50, "random": 30})

    def test_set_weights_unknown_allocator_raises(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        with pytest.raises(ValueError, match="Unknown allocator"):
            sched.set_allocator_weights({"greedy": 50, "nonexistent": 50})

    def test_default_weights_uniform(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        # Default is uniform across all registered allocators
        from spinlab.allocators import list_allocators
        n = len(list_allocators())
        assert len(sched.allocator.entries) == n

    def test_sync_picks_up_weight_change(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        import json
        db_with_segments.save_allocator_config(
            "allocator_weights", json.dumps({"random": 100})
        )
        sched._sync_config_from_db()
        assert len(sched.allocator.entries) == 1
        alloc, weight = sched.allocator.entries[0]
        assert alloc.name == "random"
        assert weight == 100
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_scheduler_kalman.py::TestSchedulerWeights -v`
Expected: AttributeError — `Scheduler` has no `set_allocator_weights`.

- [ ] **Step 3: Update Scheduler implementation**

Replace the scheduler's allocator logic in `python/spinlab/scheduler.py`:

Replace imports — add `list_allocators` to the import from `spinlab.allocators`, add import of `MixAllocator`:

```python
from spinlab.allocators import SegmentWithModel, get_allocator, list_allocators
from spinlab.allocators.mix import MixAllocator
```

Remove the `TYPE_CHECKING` import of `Allocator` (line 29) since we no longer type-hint with it.

Replace `__init__` (lines 48-57):

```python
class Scheduler:
    def __init__(
        self, db: "Database", game_id: str,
        estimator_name: str = "kalman",
    ) -> None:
        self.db = db
        self.game_id = game_id
        saved_est = db.load_allocator_config("estimator")
        self.estimator: Estimator = get_estimator(saved_est or estimator_name)
        self.allocator: MixAllocator = self._build_mix_from_db()
        self._weights_json: str = self.db.load_allocator_config("allocator_weights") or ""
```

Add `_build_mix_from_db` and `_build_mix` helper methods after `__init__`:

```python
    def _build_mix_from_db(self) -> MixAllocator:
        raw = self.db.load_allocator_config("allocator_weights")
        if raw:
            weights = json.loads(raw)
        else:
            names = list_allocators()
            base = 100 // len(names)
            remainder = 100 - base * len(names)
            weights = {n: base + (1 if i < remainder else 0) for i, n in enumerate(names)}
        return self._build_mix(weights)

    @staticmethod
    def _build_mix(weights: dict[str, int]) -> MixAllocator:
        entries = [(get_allocator(name), w) for name, w in weights.items() if w > 0]
        return MixAllocator(entries=entries)
```

Replace `_sync_config_from_db` (lines 59-65):

```python
    def _sync_config_from_db(self) -> None:
        raw = self.db.load_allocator_config("allocator_weights") or ""
        if raw != self._weights_json:
            self._weights_json = raw
            self.allocator = self._build_mix_from_db()
        saved_est = self.db.load_allocator_config("estimator")
        if saved_est and saved_est != self.estimator.name:
            self.estimator = get_estimator(saved_est)
```

Replace `switch_allocator` (lines 133-135) with `set_allocator_weights`:

```python
    def set_allocator_weights(self, weights: dict[str, int]) -> None:
        total = sum(weights.values())
        if total != 100:
            raise ValueError(f"Weights must sum to 100, got {total}")
        valid = set(list_allocators())
        for name in weights:
            if name not in valid:
                raise ValueError(f"Unknown allocator: {name!r}. Available: {valid}")
        raw = json.dumps(weights)
        self.db.save_allocator_config("allocator_weights", raw)
        self._weights_json = raw
        self.allocator = self._build_mix(weights)
```

Remove the old `"allocator"` key handling. Remove `allocator_name` parameter from `__init__`.

- [ ] **Step 4: Run scheduler tests**

Run: `python -m pytest tests/test_scheduler_kalman.py -v`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/scheduler.py tests/test_scheduler_kalman.py
git commit -m "feat: scheduler uses MixAllocator with weighted dispatch"
```

---

### Task 5: Update API endpoint and state broadcast

**Files:**
- Modify: `python/spinlab/dashboard.py:148-158,182-191`
- Modify: `python/spinlab/session_manager.py:104,112`
- Modify: `tests/test_dashboard_integration.py:179,206-253`

- [ ] **Step 1: Update failing tests in test_dashboard_integration.py**

Replace `test_allocator_and_estimator_reported` and `TestAllocatorSwitch`:

```python
    def test_allocator_weights_and_estimator_reported(self, active_client):
        data = active_client.get("/api/state").json()
        assert "allocator_weights" in data
        assert isinstance(data["allocator_weights"], dict)
        assert data["estimator"] == "kalman"
```

Also update `test_no_game_loaded_returns_defaults` — change the assertion from `assert data["allocator"] is None` to `assert data["allocator_weights"] is None`.

Replace `TestAllocatorSwitch`:

```python
class TestAllocatorWeights:
    def test_set_weights(self, active_client):
        resp = active_client.post("/api/allocator-weights", json={"greedy": 70, "random": 30})
        assert resp.status_code == 200
        assert resp.json()["weights"] == {"greedy": 70, "random": 30}

    def test_set_weights_invalid_sum(self, active_client):
        resp = active_client.post("/api/allocator-weights", json={"greedy": 50, "random": 30})
        assert resp.status_code == 400

    def test_set_weights_unknown_allocator(self, active_client):
        resp = active_client.post("/api/allocator-weights", json={"greedy": 50, "fake": 50})
        assert resp.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_dashboard_integration.py::TestAllocatorWeights -v`
Expected: 404 — `/api/allocator-weights` does not exist yet.

- [ ] **Step 3: Update SessionManager state broadcast**

In `python/spinlab/session_manager.py`, in `get_state()`:

Replace `"allocator": None,` (line 104) with `"allocator_weights": None,`.

Replace `base["allocator"] = sched.allocator.name` (line 112) with:

```python
        raw = self.db.load_allocator_config("allocator_weights")
        if raw:
            import json as _json
            base["allocator_weights"] = _json.loads(raw)
        else:
            from spinlab.allocators import list_allocators
            names = list_allocators()
            base_w = 100 // len(names)
            remainder = 100 - base_w * len(names)
            base["allocator_weights"] = {
                n: base_w + (1 if i < remainder else 0)
                for i, n in enumerate(names)
            }
```

- [ ] **Step 4: Replace `/api/allocator` endpoint with `/api/allocator-weights` in dashboard.py**

In `python/spinlab/dashboard.py`, replace lines 182-191:

```python
    @app.post("/api/allocator-weights")
    def set_allocator_weights(body: dict):
        sched = session._get_scheduler()
        try:
            sched.set_allocator_weights(body)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"weights": body}
```

Also update `/api/model` (lines 148-158) — replace `"allocator": sched.allocator.name,` with:

```python
            "allocator_weights": {
                alloc.name: int(w) for alloc, w in sched.allocator.entries
            },
```

- [ ] **Step 5: Run integration tests**

Run: `python -m pytest tests/test_dashboard_integration.py -v`
Expected: All tests pass.

- [ ] **Step 6: Run full test suite**

Run: `python -m pytest tests/ -v --timeout=30`
Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/dashboard.py python/spinlab/session_manager.py tests/test_dashboard_integration.py
git commit -m "feat: replace /api/allocator with /api/allocator-weights endpoint"
```

---

### Task 6: Remove queue UI and allocator dropdown from dashboard

**Files:**
- Modify: `python/spinlab/static/index.html:48-49,55-62`
- Modify: `python/spinlab/static/model.js:78-84,104-106,120-122`
- Modify: `python/spinlab/static/style.css:275-294`

- [ ] **Step 1: Remove queue HTML and allocator dropdown from index.html**

In `python/spinlab/static/index.html`:

Remove the "Up Next" heading and queue list (lines 48-49):
```html
        <h3>Up Next</h3>
        <ul id="queue"></ul>
```

Remove the allocator dropdown (lines 55-62):
```html
        <div class="allocator-row">
          <label>Allocator:</label>
          <select id="allocator-select">
            <option value="greedy">Greedy</option>
            <option value="random">Random</option>
            <option value="round_robin">Round Robin</option>
          </select>
        </div>
```

Add the allocator weight slider container in its place:

```html
        <div class="allocator-weights" id="allocator-weights">
          <div class="weight-slider" id="weight-slider"></div>
          <div class="weight-legend" id="weight-legend"></div>
        </div>
```

- [ ] **Step 2: Remove queue rendering and allocator dropdown JS from model.js**

In `python/spinlab/static/model.js`:

Remove queue rendering (lines 78-84):
```javascript
  const queue = document.getElementById('queue');
  queue.innerHTML = '';
  (data.queue || []).forEach(q => {
    const li = document.createElement('li');
    li.textContent = segmentName(q);
    queue.appendChild(li);
  });
```

Remove allocator select sync (lines 104-106):
```javascript
  if (data.allocator) {
    document.getElementById('allocator-select').value = data.allocator;
  }
```

Remove allocator select event listener from `initModelTab()` (lines 120-122):
```javascript
  document.getElementById('allocator-select').addEventListener('change', async (e) => {
    await postJSON('/api/allocator', { name: e.target.value });
  });
```

- [ ] **Step 3: Remove queue CSS from style.css**

In `python/spinlab/static/style.css`:

Remove the "Queue and recent" comment and `#queue` rules (lines 275-294). Keep `#recent` styles:

Replace lines 275-294 with:

```css
/* Recent */
h3 {
  font-size: 11px;
  text-transform: uppercase;
  color: var(--text-dim);
  letter-spacing: 0.5px;
  margin: 10px 8px 4px;
}
#recent {
  list-style: none;
  margin: 0 8px;
}
#recent li {
  font-size: 11px;
  padding: 5px 8px;
  margin-bottom: 3px;
  background: var(--surface);
  border: 1px solid var(--card);
  border-radius: 4px;
}
```

Remove the `.allocator-row` styles (lines 216-229) since the dropdown is gone.

- [ ] **Step 4: Verify dashboard loads without errors**

Run: `python -m pytest tests/test_dashboard_integration.py -v`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/static/index.html python/spinlab/static/model.js python/spinlab/static/style.css
git commit -m "refactor: remove queue UI and allocator dropdown from dashboard"
```

---

### Task 7: Build multi-handle weight slider UI

**Files:**
- Modify: `python/spinlab/static/model.js`
- Modify: `python/spinlab/static/style.css`

- [ ] **Step 1: Add slider CSS to style.css**

Append to `python/spinlab/static/style.css`:

```css
/* Allocator weight slider */
.allocator-weights {
  margin: 8px;
}
.weight-slider {
  position: relative;
  height: 24px;
  border-radius: 4px;
  overflow: visible;
  display: flex;
  cursor: pointer;
}
.weight-segment {
  height: 100%;
  transition: flex 0.1s;
  min-width: 0;
}
.weight-segment:first-child { border-radius: 4px 0 0 4px; }
.weight-segment:last-child { border-radius: 0 4px 4px 0; }
.weight-segment:only-child { border-radius: 4px; }
.weight-handle {
  position: absolute;
  top: -2px;
  width: 6px;
  height: 28px;
  background: var(--text);
  border-radius: 2px;
  cursor: col-resize;
  z-index: 10;
  transform: translateX(-3px);
  opacity: 0.7;
}
.weight-handle:hover, .weight-handle.dragging { opacity: 1; }
.weight-legend {
  display: flex;
  gap: 12px;
  margin-top: 6px;
  font-size: 11px;
}
.weight-legend-item {
  display: flex;
  align-items: center;
  gap: 4px;
}
.weight-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
```

- [ ] **Step 2: Add slider JS to model.js**

Add the following to `python/spinlab/static/model.js`. This goes after the existing imports and before `fetchModel`:

```javascript
const ALLOCATOR_COLORS = {
  greedy: '#4caf50',
  random: '#2196f3',
  round_robin: '#ff9800',
};
const ALLOCATOR_LABELS = {
  greedy: 'Greedy',
  random: 'Random',
  round_robin: 'Round Robin',
};
const ALLOCATOR_ORDER = ['greedy', 'random', 'round_robin'];

let _currentWeights = null;

function renderWeightSlider(weights) {
  _currentWeights = { ...weights };
  const slider = document.getElementById('weight-slider');
  const legend = document.getElementById('weight-legend');
  if (!slider || !legend) return;

  slider.innerHTML = '';
  legend.innerHTML = '';

  // Build ordered entries
  const entries = ALLOCATOR_ORDER.filter(k => k in weights);

  // Segments
  entries.forEach(name => {
    const seg = document.createElement('div');
    seg.className = 'weight-segment';
    seg.style.flex = weights[name];
    seg.style.background = ALLOCATOR_COLORS[name] || '#666';
    seg.dataset.allocator = name;
    slider.appendChild(seg);
  });

  // Handles (between segments)
  const totalWidth = () => slider.getBoundingClientRect().width;
  for (let i = 0; i < entries.length - 1; i++) {
    const handle = document.createElement('div');
    handle.className = 'weight-handle';
    handle.dataset.index = i;
    _positionHandle(handle, entries, weights, slider);
    slider.appendChild(handle);

    handle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      handle.classList.add('dragging');
      const left = entries[i];
      const right = entries[i + 1];
      const startX = e.clientX;
      const startLeftW = weights[left];
      const startRightW = weights[right];
      const pxPerPercent = totalWidth() / 100;

      const onMove = (ev) => {
        const dx = ev.clientX - startX;
        const dp = Math.round(dx / pxPerPercent);
        const newLeft = Math.max(0, Math.min(startLeftW + startRightW, startLeftW + dp));
        const newRight = startLeftW + startRightW - newLeft;
        weights[left] = newLeft;
        weights[right] = newRight;
        _updateSliderVisuals(entries, weights, slider, legend);
      };
      const onUp = () => {
        handle.classList.remove('dragging');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        _currentWeights = { ...weights };
        _postWeights(weights);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }

  // Legend
  _renderLegend(entries, weights, legend);
}

function _positionHandle(handle, entries, weights, slider) {
  let cumulative = 0;
  const idx = parseInt(handle.dataset.index);
  for (let i = 0; i <= idx; i++) cumulative += weights[entries[i]];
  handle.style.left = cumulative + '%';
}

function _updateSliderVisuals(entries, weights, slider, legend) {
  const segments = slider.querySelectorAll('.weight-segment');
  entries.forEach((name, i) => {
    if (segments[i]) segments[i].style.flex = weights[name];
  });
  const handles = slider.querySelectorAll('.weight-handle');
  handles.forEach(h => _positionHandle(h, entries, weights, slider));
  _renderLegend(entries, weights, legend);
}

function _renderLegend(entries, weights, legend) {
  legend.innerHTML = '';
  entries.forEach(name => {
    const item = document.createElement('span');
    item.className = 'weight-legend-item';
    const dot = document.createElement('span');
    dot.className = 'weight-dot';
    dot.style.background = ALLOCATOR_COLORS[name] || '#666';
    item.appendChild(dot);
    item.appendChild(document.createTextNode(
      (ALLOCATOR_LABELS[name] || name) + ' ' + weights[name] + '%'
    ));
    legend.appendChild(item);
  });
}

async function _postWeights(weights) {
  await postJSON('/api/allocator-weights', weights);
}
```

- [ ] **Step 3: Wire slider into state sync**

In `updatePracticeCard()` in `model.js`, add after the insight rendering (replacing the removed allocator select sync):

```javascript
  if (data.allocator_weights) {
    renderWeightSlider(data.allocator_weights);
  }
```

In `initModelTab()`, remove the old allocator-select listener (already done in Task 6). No new init needed — the slider is rendered on state update.

- [ ] **Step 4: Verify dashboard loads and tests pass**

Run: `python -m pytest tests/test_dashboard_integration.py -v`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/static/model.js python/spinlab/static/style.css
git commit -m "feat: add multi-handle weight slider UI for allocator weights"
```

---

### Task 8: Clean up old allocator config key

**Files:**
- Modify: `python/spinlab/scheduler.py`
- Modify: `python/spinlab/db/model_state.py`

- [ ] **Step 1: Write test for old key cleanup**

Add to `tests/test_scheduler_kalman.py`:

```python
class TestOldConfigCleanup:
    def test_old_allocator_key_deleted_on_init(self, db_with_segments):
        db_with_segments.save_allocator_config("allocator", "greedy")
        Scheduler(db_with_segments, "g1")
        assert db_with_segments.load_allocator_config("allocator") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_scheduler_kalman.py::TestOldConfigCleanup -v`
Expected: FAIL — old key not deleted.

- [ ] **Step 3: Add cleanup to Scheduler.__init__**

In `python/spinlab/scheduler.py`, add to the end of `__init__`:

```python
        # Clean up legacy single-allocator config key
        if db.load_allocator_config("allocator") is not None:
            db.delete_allocator_config("allocator")
```

- [ ] **Step 4: Add `delete_allocator_config` to Database**

In `python/spinlab/db/model_state.py`, add after `save_allocator_config`:

```python
    def delete_allocator_config(self, key: str) -> None:
        self.conn.execute("DELETE FROM allocator_config WHERE key = ?", (key,))
        self.conn.commit()
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_scheduler_kalman.py -v`
Expected: All tests pass.

- [ ] **Step 6: Run full test suite**

Run: `python -m pytest tests/ -v --timeout=30`
Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/scheduler.py python/spinlab/db/model_state.py tests/test_scheduler_kalman.py
git commit -m "chore: clean up legacy single-allocator config key on startup"
```

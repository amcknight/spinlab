# Scheduler Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract scattered private functions from `scheduler.py` into the types that own the data, improving legibility and testability.

**Architecture:** Three targeted moves: (1) segment assembly becomes a `SegmentWithModel.load_all` classmethod, (2) priors become an `Estimator.get_priors` polymorphic method, (3) state deserialization becomes `EstimatorState.deserialize` classmethod. Estimator helper one-liners get inlined.

**Tech Stack:** Python 3.11+, pytest, in-memory SQLite

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `python/spinlab/allocators/__init__.py` | Modify | Add `SegmentWithModel.load_all` classmethod |
| `python/spinlab/estimators/__init__.py` | Modify | Add `Estimator.get_priors` default, add `EstimatorState.deserialize` classmethod |
| `python/spinlab/estimators/kalman.py` | Modify | Add `KalmanEstimator.get_priors` override |
| `python/spinlab/scheduler.py` | Modify | Remove 7 private functions, update callers |
| `tests/test_segment_with_model.py` | Create | Tests for `SegmentWithModel.load_all` |
| `tests/test_kalman.py` | Modify | Add `get_priors` tests |
| `tests/test_estimator_sanity.py` | Modify | Add `EstimatorState.deserialize` tests |

---

### Task 1: `SegmentWithModel.load_all` — test and implement

**Files:**
- Create: `tests/test_segment_with_model.py`
- Modify: `python/spinlab/allocators/__init__.py`

- [ ] **Step 1: Write failing tests for `load_all`**

Create `tests/test_segment_with_model.py`:

```python
"""Tests for SegmentWithModel.load_all factory classmethod."""
import json
import pytest
from spinlab.allocators import SegmentWithModel
from spinlab.db import Database
from spinlab.models import Segment, SegmentVariant


@pytest.fixture
def db_with_segments(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Game", "any%")
    states_dir = tmp_path / "states"
    states_dir.mkdir()
    for i, (start_type, end_type) in enumerate(
        [("entrance", "checkpoint"), ("checkpoint", "checkpoint"), ("checkpoint", "goal")],
        start=1,
    ):
        state_file = states_dir / f"{i}.mss"
        state_file.write_bytes(b"\x00" * 100)
        seg = Segment(
            id=f"g1:{i}:{start_type}.0:{end_type}.0",
            game_id="g1", level_number=i,
            start_type=start_type, start_ordinal=0,
            end_type=end_type, end_ordinal=0,
            description=f"Segment {i}", strat_version=1,
        )
        db.upsert_segment(seg)
        db.add_variant(SegmentVariant(
            segment_id=seg.id, variant_type="cold",
            state_path=str(state_file), is_default=True,
        ))
    return db


class TestLoadAll:
    def test_basic_assembly(self, db_with_segments):
        """3 segments with no model state yet."""
        results = SegmentWithModel.load_all(db_with_segments, "g1")
        assert len(results) == 3
        for s in results:
            assert isinstance(s, SegmentWithModel)
            assert s.game_id == "g1"
            assert s.model_outputs == {}
            assert s.n_completed == 0
            assert s.n_attempts == 0

    def test_empty_game(self, tmp_path):
        db = Database(str(tmp_path / "empty.db"))
        db.upsert_game("g2", "Empty", "any%")
        results = SegmentWithModel.load_all(db, "g2")
        assert results == []

    def test_with_model_state(self, db_with_segments):
        """Segments with saved model outputs get them populated."""
        from spinlab.models import ModelOutput, Estimate
        seg_id = "g1:1:entrance.0:checkpoint.0"
        out = ModelOutput(
            total=Estimate(expected_ms=12000.0, ms_per_attempt=-500.0, floor_ms=None),
            clean=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
        )
        state_json = json.dumps({"mu": 12.0, "d": -0.5, "n_completed": 5, "n_attempts": 7})
        db_with_segments.save_model_state(seg_id, "kalman", state_json, json.dumps(out.to_dict()))

        results = SegmentWithModel.load_all(db_with_segments, "g1")
        seg1 = next(s for s in results if s.segment_id == seg_id)
        assert "kalman" in seg1.model_outputs
        assert seg1.model_outputs["kalman"].total.expected_ms == 12000.0
        assert seg1.n_completed == 5
        assert seg1.n_attempts == 7

    def test_with_golds(self, db_with_segments):
        """Gold times from attempts are populated."""
        from spinlab.models import Attempt
        seg_id = "g1:1:entrance.0:checkpoint.0"
        db_with_segments.log_attempt(Attempt(
            segment_id=seg_id, session_id="s1", completed=True,
            time_ms=10000, deaths=0, clean_tail_ms=10000,
        ))
        results = SegmentWithModel.load_all(db_with_segments, "g1")
        seg1 = next(s for s in results if s.segment_id == seg_id)
        assert seg1.gold_ms == 10000

    def test_malformed_output_json_skipped(self, db_with_segments):
        """Bad JSON in output_json is skipped, not a crash."""
        seg_id = "g1:1:entrance.0:checkpoint.0"
        state_json = json.dumps({"n_completed": 1, "n_attempts": 1})
        db_with_segments.save_model_state(seg_id, "kalman", state_json, "{bad json")
        results = SegmentWithModel.load_all(db_with_segments, "g1")
        seg1 = next(s for s in results if s.segment_id == seg_id)
        assert "kalman" not in seg1.model_outputs

    def test_selected_model_passthrough(self, db_with_segments):
        results = SegmentWithModel.load_all(db_with_segments, "g1", selected_model="rolling_mean")
        for s in results:
            assert s.selected_model == "rolling_mean"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_segment_with_model.py -v`
Expected: FAIL — `SegmentWithModel` has no `load_all` attribute.

- [ ] **Step 3: Implement `SegmentWithModel.load_all`**

In `python/spinlab/allocators/__init__.py`, add these imports at the top (after existing imports):

```python
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from spinlab.models import ModelOutput

if TYPE_CHECKING:
    from spinlab.db import Database

logger = logging.getLogger(__name__)
```

Then add the classmethod to `SegmentWithModel`, after the field definitions:

```python
    @classmethod
    def load_all(
        cls,
        db: "Database",
        game_id: str,
        selected_model: str = "kalman",
    ) -> list["SegmentWithModel"]:
        """Load all segments for a game with model outputs, golds, and stats."""
        rows = db.get_all_segments_with_model(game_id)
        all_model_states = db.load_all_model_states_for_game(game_id)
        golds = db.compute_golds(game_id)

        segments = []
        for row in rows:
            segment_id = row["id"]
            model_outputs: dict[str, ModelOutput] = {}
            n_completed = 0
            n_attempts = 0

            for sr in all_model_states.get(segment_id, []):
                if sr["output_json"]:
                    try:
                        out = ModelOutput.from_dict(json.loads(sr["output_json"]))
                        model_outputs[sr["estimator"]] = out
                    except (json.JSONDecodeError, KeyError):
                        logger.warning(
                            "Failed to deserialize model output for segment=%s estimator=%s",
                            segment_id, sr["estimator"],
                        )
                if sr["state_json"]:
                    try:
                        sd = json.loads(sr["state_json"])
                        nc = sd.get("n_completed", 0)
                        na = sd.get("n_attempts", 0)
                        if nc > n_completed:
                            n_completed = nc
                            n_attempts = na
                    except (json.JSONDecodeError, KeyError):
                        logger.warning(
                            "Failed to deserialize model state for segment=%s estimator=%s",
                            segment_id, sr["estimator"],
                        )

            gold_data = golds.get(segment_id, {})

            segments.append(cls(
                segment_id=segment_id,
                game_id=row["game_id"],
                level_number=row["level_number"],
                start_type=row["start_type"],
                start_ordinal=row["start_ordinal"],
                end_type=row["end_type"],
                end_ordinal=row["end_ordinal"],
                description=row["description"],
                strat_version=row["strat_version"],
                state_path=row.get("state_path"),
                active=bool(row["active"]),
                model_outputs=model_outputs,
                selected_model=selected_model,
                n_completed=n_completed,
                n_attempts=n_attempts,
                gold_ms=gold_data.get("gold_ms"),
                clean_gold_ms=gold_data.get("clean_gold_ms"),
            ))
        return segments
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_segment_with_model.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_segment_with_model.py python/spinlab/allocators/__init__.py
git commit -m "feat: add SegmentWithModel.load_all factory classmethod"
```

---

### Task 2: `Estimator.get_priors` — test and implement

**Files:**
- Modify: `python/spinlab/estimators/__init__.py`
- Modify: `python/spinlab/estimators/kalman.py`
- Modify: `tests/test_kalman.py`

- [ ] **Step 1: Write failing test for `KalmanEstimator.get_priors`**

Append to `tests/test_kalman.py`:

```python
class TestKalmanGetPriors:
    def test_no_mature_states_returns_defaults(self, tmp_path):
        from spinlab.db import Database
        db = Database(str(tmp_path / "test.db"))
        db.upsert_game("g1", "Game", "any%")
        est = KalmanEstimator()
        priors = est.get_priors(db, "g1")
        assert priors["d"] == -0.5
        assert priors["R"] == 25.0

    def test_population_priors_from_mature_states(self, tmp_path):
        import json
        from spinlab.db import Database
        from spinlab.models import Segment
        db = Database(str(tmp_path / "test.db"))
        db.upsert_game("g1", "Game", "any%")
        # Create two segments with mature kalman states (n_completed >= 10)
        for i in range(2):
            seg = Segment(
                id=f"s{i}", game_id="g1", level_number=i,
                start_type="entrance", start_ordinal=0,
                end_type="checkpoint", end_ordinal=0,
            )
            db.upsert_segment(seg)
            state = KalmanState(
                mu=10.0 + i, d=-0.3 - (0.1 * i), R=20.0 + i,
                Q_mm=0.2, Q_dd=0.02,
                n_completed=15, n_attempts=20,
            )
            db.save_model_state(f"s{i}", "kalman", json.dumps(state.to_dict()), "{}")
        est = KalmanEstimator()
        priors = est.get_priors(db, "g1")
        # Should be averages of the two states
        assert priors["d"] == pytest.approx((-0.3 + -0.4) / 2)
        assert priors["R"] == pytest.approx((20.0 + 21.0) / 2)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_kalman.py::TestKalmanGetPriors -v`
Expected: FAIL — `KalmanEstimator` has no `get_priors` method (with `db` signature).

- [ ] **Step 3: Add `get_priors` default to `Estimator` base class**

In `python/spinlab/estimators/__init__.py`, add this method to the `Estimator` class after `rebuild_state`:

```python
    def get_priors(self, db: "Database", game_id: str) -> dict:
        """Return population priors for init_state. Default: no priors."""
        return {}
```

Also update the `TYPE_CHECKING` block to include `Database`:

```python
if TYPE_CHECKING:
    from spinlab.db import Database
    from spinlab.models import AttemptRecord, ModelOutput
```

- [ ] **Step 4: Add `get_priors` override to `KalmanEstimator`**

In `python/spinlab/estimators/kalman.py`, add this method to `KalmanEstimator` (after `get_population_priors`):

```python
    def get_priors(self, db: "Database", game_id: str) -> dict:
        """Load population priors from all mature kalman states for this game."""
        from spinlab.db import Database  # runtime import to avoid circular
        all_rows = db.load_all_model_states(game_id)
        kalman_rows = [r for r in all_rows if r["estimator"] == "kalman"]
        all_states = []
        for r in kalman_rows:
            if r["state_json"]:
                try:
                    import json
                    all_states.append(KalmanState.from_dict(json.loads(r["state_json"])))
                except (json.JSONDecodeError, KeyError):
                    pass
        return self.get_population_priors(all_states)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_kalman.py -v`
Expected: All tests PASS (old + new).

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/estimators/__init__.py python/spinlab/estimators/kalman.py tests/test_kalman.py
git commit -m "feat: add Estimator.get_priors with Kalman override"
```

---

### Task 3: `EstimatorState.deserialize` — test and implement

**Files:**
- Modify: `python/spinlab/estimators/__init__.py`
- Modify: `tests/test_estimator_sanity.py`

- [ ] **Step 1: Write failing tests for `EstimatorState.deserialize`**

Append to `tests/test_estimator_sanity.py`:

```python
class TestEstimatorStateDeserialize:
    def test_kalman_round_trip(self):
        from spinlab.estimators import EstimatorState
        from spinlab.estimators.kalman import KalmanState
        import json
        original = KalmanState(mu=12.0, d=-0.5, n_completed=5, n_attempts=8)
        json_str = json.dumps(original.to_dict())
        restored = EstimatorState.deserialize("kalman", json_str)
        assert isinstance(restored, KalmanState)
        assert restored.mu == 12.0
        assert restored.n_completed == 5

    def test_rolling_mean_round_trip(self):
        from spinlab.estimators import EstimatorState
        from spinlab.estimators.rolling_mean import RollingMeanState
        import json
        original = RollingMeanState(n_completed=10, n_attempts=15)
        json_str = json.dumps(original.to_dict())
        restored = EstimatorState.deserialize("rolling_mean", json_str)
        assert isinstance(restored, RollingMeanState)
        assert restored.n_completed == 10

    def test_unknown_estimator_raises(self):
        from spinlab.estimators import EstimatorState
        with pytest.raises(ValueError, match="No state class"):
            EstimatorState.deserialize("nonexistent", "{}")

    def test_malformed_json_raises(self):
        from spinlab.estimators import EstimatorState
        with pytest.raises(json.JSONDecodeError):
            EstimatorState.deserialize("kalman", "{bad json")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_estimator_sanity.py::TestEstimatorStateDeserialize -v`
Expected: FAIL — `EstimatorState` has no `deserialize` attribute.

- [ ] **Step 3: Implement `EstimatorState.deserialize`**

In `python/spinlab/estimators/__init__.py`, add this import at top level:

```python
import json
```

Add the `deserialize` classmethod and `register_state` to `EstimatorState`. The `_state_classes` registry must be a plain class variable set AFTER the class body (not a dataclass field, which would break subclass constructors):

```python
@dataclass
class EstimatorState(ABC):
    """Base class for estimator-specific state."""

    @classmethod
    def register_state(cls, name: str, state_cls: type["EstimatorState"]) -> None:
        cls._state_classes[name] = state_cls

    @classmethod
    def deserialize(cls, estimator_name: str, state_json: str) -> "EstimatorState":
        """Deserialize state JSON for a named estimator."""
        state_cls = cls._state_classes.get(estimator_name)
        if state_cls is None:
            raise ValueError(f"No state class for estimator: {estimator_name}")
        return state_cls.from_dict(json.loads(state_json))

    @abstractmethod
    def to_dict(self) -> dict:
        ...

    @classmethod
    @abstractmethod
    def from_dict(cls, d: dict) -> "EstimatorState":
        ...


# Class-level registry — set after class body to avoid dataclass field issues
EstimatorState._state_classes: dict[str, type[EstimatorState]] = {}
```

Then in each estimator module, register after the state class is defined:

In `python/spinlab/estimators/kalman.py`, after the `KalmanState` class:
```python
EstimatorState.register_state("kalman", KalmanState)
```

In `python/spinlab/estimators/rolling_mean.py`, after the `RollingMeanState` class:
```python
EstimatorState.register_state("rolling_mean", RollingMeanState)
```

In `python/spinlab/estimators/exp_decay.py`, after the `ExpDecayState` class:
```python
EstimatorState.register_state("exp_decay", ExpDecayState)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_estimator_sanity.py -v`
Expected: All tests PASS (old + new).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/estimators/__init__.py python/spinlab/estimators/kalman.py python/spinlab/estimators/rolling_mean.py python/spinlab/estimators/exp_decay.py tests/test_estimator_sanity.py
git commit -m "feat: add EstimatorState.deserialize classmethod with registry"
```

---

### Task 4: Rewrite `scheduler.py` to use extracted pieces

**Files:**
- Modify: `python/spinlab/scheduler.py`

- [ ] **Step 1: Run existing scheduler tests to confirm green baseline**

Run: `pytest tests/test_scheduler_kalman.py -v`
Expected: All PASS.

- [ ] **Step 2: Replace `_load_segments_with_model` with `SegmentWithModel.load_all`**

In `python/spinlab/scheduler.py`, delete the entire `_load_segments_with_model` method (lines 94-147 in the original file). Then update each caller:

In `pick_next`:
```python
    def pick_next(self) -> SegmentWithModel | None:
        self._sync_config_from_db()
        segments = SegmentWithModel.load_all(self.db, self.game_id, self.estimator.name)
        if not segments:
            return None
        practicable = [s for s in segments if s.state_path and os.path.exists(s.state_path)]
        if not practicable:
            return None
        segment_id = self.allocator.pick_next(practicable)
        if segment_id is None:
            return None
        return next((s for s in practicable if s.segment_id == segment_id), None)
```

In `peek_next_n`:
```python
    def peek_next_n(self, n: int) -> list[str]:
        segments = SegmentWithModel.load_all(self.db, self.game_id, self.estimator.name)
        practicable = [s for s in segments if s.state_path and os.path.exists(s.state_path)]
        return self.allocator.peek_next_n(practicable, n)
```

In `get_all_model_states`:
```python
    def get_all_model_states(self) -> list[SegmentWithModel]:
        return SegmentWithModel.load_all(self.db, self.game_id, self.estimator.name)
```

In `get_model_api_state`, replace `self._load_segments_with_model()` with:
```python
        segments = SegmentWithModel.load_all(self.db, self.game_id, self.estimator.name)
```

- [ ] **Step 3: Replace `_get_priors` with `est.get_priors`**

In `process_attempt`, find the line:
```python
                    priors = self._get_priors(est)
```
Replace with:
```python
                    priors = est.get_priors(self.db, self.game_id)
```

Delete the entire `_get_priors` method.

- [ ] **Step 4: Replace `_deserialize_state` with `EstimatorState.deserialize`**

In `process_attempt`, find:
```python
                state = self._deserialize_state(est.name, row["state_json"])
```
Replace with:
```python
                state = EstimatorState.deserialize(est.name, row["state_json"])
```

Delete the entire `_deserialize_state` method.

- [ ] **Step 5: Inline `_all_estimators*` helpers**

Delete these three methods:
- `_all_estimators`
- `_all_estimators_names`
- `_all_estimators_info`

In `process_attempt`, replace `for est in self._all_estimators():` with:
```python
        for est in [get_estimator(n) for n in list_estimators()]:
```

In `get_model_api_state`, replace `self._all_estimators_info()` with:
```python
            "estimators": [
                {"name": n, "display_name": get_estimator(n).display_name or n}
                for n in list_estimators()
            ],
```

In `rebuild_all_states`, replace `for est in self._all_estimators():` with:
```python
            for est in [get_estimator(n) for n in list_estimators()]:
```

- [ ] **Step 6: Clean up imports**

Remove from `scheduler.py` imports that are no longer needed:
- `KalmanEstimator`, `KalmanState` — no longer needed for `_get_priors` / `_deserialize_state` (still needed for `# ensure registered` side effect — keep the import but simplify)
- `RollingMeanState` — no longer needed for `_STATE_CLASSES`
- `ExpDecayState` — no longer needed for `_STATE_CLASSES`

The `_has_exp_decay` flag and `_STATE_CLASSES` dict at module level can be deleted entirely.

Keep the bare imports that trigger registration:
```python
from spinlab.estimators.kalman import KalmanEstimator  # ensure registered
from spinlab.estimators.rolling_mean import RollingMeanEstimator  # ensure registered
_has_exp_decay = False
try:
    from spinlab.estimators.exp_decay import ExpDecayEstimator  # ensure registered
    _has_exp_decay = True
except ImportError:
    logger.warning("exp_decay unavailable (numpy/scipy not installed)")
```

Also delete the module-level `_STATE_CLASSES` dict and associated `if _has_exp_decay: _STATE_CLASSES[...]` block.

Also remove the `_attempts_from_rows` function — wait, check if it's still used. It IS still used in `process_attempt` and `rebuild_all_states`. Keep it.

- [ ] **Step 7: Run all scheduler tests**

Run: `pytest tests/test_scheduler_kalman.py -v`
Expected: All PASS.

- [ ] **Step 8: Run full test suite**

Run: `pytest tests/ -v`
Expected: All PASS. No regressions.

- [ ] **Step 9: Commit**

```bash
git add python/spinlab/scheduler.py
git commit -m "refactor: scheduler uses extracted SegmentWithModel.load_all, Estimator.get_priors, EstimatorState.deserialize"
```

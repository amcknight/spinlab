# ModelOutput V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure ModelOutput into two-sided Estimate (total + clean), rename Model A/B, fix prediction off-by-one, eliminate silent fallbacks.

**Architecture:** Replace flat 5-field ModelOutput with nested `ModelOutput(total: Estimate, clean: Estimate)` where each Estimate has 3 nullable fields. Rename model_a → rolling_mean and model_b → exp_decay across files, classes, registry, and tests. Fix Kalman and Exp Decay to predict forward (next attempt, not last observed).

**Tech Stack:** Python 3.11+, dataclasses, pytest, FastAPI, vanilla JS

---

### Task 1: Create Estimate dataclass and restructure ModelOutput

**Files:**
- Modify: `python/spinlab/models.py:117-143`
- Test: `tests/test_model_output.py`

- [ ] **Step 1: Write failing tests for new ModelOutput structure**

Replace the existing `TestModelOutput` class in `tests/test_model_output.py`:

```python
from spinlab.models import Estimate, ModelOutput


class TestEstimate:
    def test_round_trip_serialization(self):
        e = Estimate(expected_ms=12000.0, ms_per_attempt=150.0, floor_ms=7000.0)
        d = e.to_dict()
        e2 = Estimate.from_dict(d)
        assert e2.expected_ms == 12000.0
        assert e2.ms_per_attempt == 150.0
        assert e2.floor_ms == 7000.0

    def test_all_none(self):
        e = Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None)
        d = e.to_dict()
        e2 = Estimate.from_dict(d)
        assert e2.expected_ms is None
        assert e2.ms_per_attempt is None
        assert e2.floor_ms is None


class TestModelOutput:
    def test_round_trip_serialization(self):
        mo = ModelOutput(
            total=Estimate(expected_ms=12000.0, ms_per_attempt=150.0, floor_ms=9500.0),
            clean=Estimate(expected_ms=8000.0, ms_per_attempt=80.0, floor_ms=6200.0),
        )
        d = mo.to_dict()
        mo2 = ModelOutput.from_dict(d)
        assert mo2.total.expected_ms == 12000.0
        assert mo2.total.ms_per_attempt == 150.0
        assert mo2.total.floor_ms == 9500.0
        assert mo2.clean.expected_ms == 8000.0
        assert mo2.clean.ms_per_attempt == 80.0
        assert mo2.clean.floor_ms == 6200.0

    def test_nested_dict_structure(self):
        mo = ModelOutput(
            total=Estimate(expected_ms=1.0, ms_per_attempt=2.0, floor_ms=3.0),
            clean=Estimate(expected_ms=4.0, ms_per_attempt=5.0, floor_ms=6.0),
        )
        d = mo.to_dict()
        assert set(d.keys()) == {"total", "clean"}
        assert set(d["total"].keys()) == {"expected_ms", "ms_per_attempt", "floor_ms"}

    def test_v1_backward_compat(self):
        """V1 flat dict should load into total side, clean gets all None."""
        v1 = {
            "expected_time_ms": 12000.0, "clean_expected_ms": 8000.0,
            "ms_per_attempt": 150.0, "floor_estimate_ms": 7000.0,
            "clean_floor_estimate_ms": 6000.0,
        }
        mo = ModelOutput.from_dict(v1)
        assert mo.total.expected_ms == 12000.0
        assert mo.total.ms_per_attempt == 150.0
        assert mo.total.floor_ms == 7000.0
        assert mo.clean.expected_ms == 8000.0
        assert mo.clean.ms_per_attempt is None
        assert mo.clean.floor_ms == 6000.0

    def test_all_none_sides(self):
        mo = ModelOutput(
            total=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
            clean=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
        )
        d = mo.to_dict()
        mo2 = ModelOutput.from_dict(d)
        assert mo2.total.expected_ms is None
        assert mo2.clean.expected_ms is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_model_output.py::TestEstimate tests/test_model_output.py::TestModelOutput -v`
Expected: FAIL — `Estimate` doesn't exist, `ModelOutput` has wrong signature

- [ ] **Step 3: Implement Estimate and new ModelOutput**

Replace the `ModelOutput` class in `python/spinlab/models.py` (lines 117-143) with:

```python
@dataclass
class Estimate:
    """One coherent set of predictions for a single time series."""
    expected_ms: float | None = None
    ms_per_attempt: float | None = None
    floor_ms: float | None = None

    def to_dict(self) -> dict:
        return {
            "expected_ms": self.expected_ms,
            "ms_per_attempt": self.ms_per_attempt,
            "floor_ms": self.floor_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Estimate":
        return cls(
            expected_ms=d.get("expected_ms"),
            ms_per_attempt=d.get("ms_per_attempt"),
            floor_ms=d.get("floor_ms"),
        )


@dataclass
class ModelOutput:
    """What every estimator produces — predictions for total time and clean tail."""
    total: Estimate
    clean: Estimate

    def to_dict(self) -> dict:
        return {
            "total": self.total.to_dict(),
            "clean": self.clean.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelOutput":
        # V2 nested format
        if "total" in d:
            return cls(
                total=Estimate.from_dict(d["total"]),
                clean=Estimate.from_dict(d["clean"]),
            )
        # V1 backward compatibility: flat keys -> map to sides
        return cls(
            total=Estimate(
                expected_ms=d.get("expected_time_ms"),
                ms_per_attempt=d.get("ms_per_attempt"),
                floor_ms=d.get("floor_estimate_ms"),
            ),
            clean=Estimate(
                expected_ms=d.get("clean_expected_ms"),
                ms_per_attempt=None,
                floor_ms=d.get("clean_floor_estimate_ms"),
            ),
        )
```

Also add `Estimate` to any imports at the top of the file if needed (it's in the same file, so no import needed).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_model_output.py::TestEstimate tests/test_model_output.py::TestModelOutput -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/models.py tests/test_model_output.py
git commit -m "feat: restructure ModelOutput into two-sided Estimate (total + clean)"
```

---

### Task 2: Update Kalman estimator

**Files:**
- Modify: `python/spinlab/estimators/kalman.py:146-154`
- Test: `tests/test_kalman.py`

- [ ] **Step 1: Write failing tests for new Kalman output**

Replace the `TestKalmanModelOutput` class in `tests/test_kalman.py`:

```python
from spinlab.models import AttemptRecord, Estimate, ModelOutput


class TestKalmanModelOutput:
    def test_produces_model_output(self):
        est = KalmanEstimator()
        a1 = _attempt(12000, True)
        state = est.init_state(a1, priors={})
        out = est.model_output(state, [a1])
        assert isinstance(out, ModelOutput)
        # expected = (mu + d) * 1000 = (12.0 + -0.5) * 1000 = 11500
        assert out.total.expected_ms == pytest.approx(11500.0)
        assert out.total.ms_per_attempt == pytest.approx(500.0)  # -d * 1000
        assert out.total.floor_ms is None

    def test_clean_side_is_all_none(self):
        """Kalman has no clean filter — clean side should be None, not a copy."""
        est = KalmanEstimator()
        a1 = _attempt(12000, True)
        state = est.init_state(a1, priors={})
        out = est.model_output(state, [a1])
        assert out.clean.expected_ms is None
        assert out.clean.ms_per_attempt is None
        assert out.clean.floor_ms is None

    def test_improving_attempts_positive_ms_per_attempt(self):
        est = KalmanEstimator()
        times = [12000, 11500, 11000, 10500, 10000, 9500, 9000, 8500, 8000, 7500]
        attempts = [_attempt(t, True) for t in times]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.total.ms_per_attempt > 0

    def test_expected_predicts_forward(self):
        """expected_ms should be mu + d (predicted next), not just mu (current)."""
        est = KalmanEstimator()
        a1 = _attempt(12000, True)
        state = est.init_state(a1, priors={})
        # mu=12.0, d=-0.5 after init, so predicted next = 11.5s = 11500ms
        out = est.model_output(state, [a1])
        assert out.total.expected_ms == pytest.approx((state.mu + state.d) * 1000)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_kalman.py::TestKalmanModelOutput -v`
Expected: FAIL — ModelOutput no longer has `.expected_time_ms`, wrong structure

- [ ] **Step 3: Update Kalman model_output method**

Replace the `model_output` method in `python/spinlab/estimators/kalman.py` (lines 146-154):

```python
    def model_output(self, state: KalmanState, all_attempts: list[AttemptRecord]) -> ModelOutput:
        return ModelOutput(
            total=Estimate(
                expected_ms=(state.mu + state.d) * 1000,
                ms_per_attempt=-state.d * 1000,
                floor_ms=None,
            ),
            clean=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
        )
```

Add `Estimate` to the import line at the top of the file:

```python
from spinlab.models import AttemptRecord, Estimate, ModelOutput
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_kalman.py::TestKalmanModelOutput -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/estimators/kalman.py tests/test_kalman.py
git commit -m "fix: Kalman predicts forward (mu+d), clean=None, floor=None"
```

---

### Task 3: Rename Model A → Rolling Mean

**Files:**
- Rename: `python/spinlab/estimators/model_a.py` → `python/spinlab/estimators/rolling_mean.py`
- Rename: `tests/test_model_a.py` → `tests/test_rolling_mean.py`
- Modify: `python/spinlab/scheduler.py` (imports and _STATE_CLASSES)
- Modify: `tests/test_scheduler_kalman.py` (registry name checks)
- Modify: `tests/test_model_output.py` (DB test references "model_a")

- [ ] **Step 1: Rename files and update class/registry names**

Create `python/spinlab/estimators/rolling_mean.py` with the full file content. The class structure (init_state, process_attempt, rebuild_state) is identical to model_a.py but with renamed classes and registry name. The `model_output` method is rewritten for V2:

```python
# python/spinlab/estimators/rolling_mean.py
"""Rolling mean estimator (model-free)."""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from spinlab.estimators import Estimator, EstimatorState, register_estimator
from spinlab.models import AttemptRecord, Estimate, ModelOutput


@dataclass
class RollingMeanState(EstimatorState):
    """Minimal bookkeeping. Rolling mean recomputes from all_attempts each time."""

    n_completed: int = 0
    n_attempts: int = 0

    def to_dict(self) -> dict:
        return {"n_completed": self.n_completed, "n_attempts": self.n_attempts}

    @classmethod
    def from_dict(cls, d: dict) -> "RollingMeanState":
        return cls(n_completed=d.get("n_completed", 0), n_attempts=d.get("n_attempts", 0))


@register_estimator
class RollingMeanEstimator(Estimator):
    name = "rolling_mean"
    display_name = "Rolling Mean"

    def init_state(self, first_attempt: AttemptRecord, priors: dict) -> RollingMeanState:
        return RollingMeanState(n_completed=1, n_attempts=1)

    def process_attempt(
        self, state: RollingMeanState, new_attempt: AttemptRecord,
        all_attempts: list[AttemptRecord],
    ) -> RollingMeanState:
        n_completed = state.n_completed + (1 if new_attempt.completed else 0)
        return RollingMeanState(n_completed=n_completed, n_attempts=state.n_attempts + 1)

    def model_output(self, state: RollingMeanState, all_attempts: list[AttemptRecord]) -> ModelOutput:
        completed = [a for a in all_attempts if a.completed and a.time_ms is not None]
        if not completed:
            return ModelOutput(
                total=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
                clean=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
            )

        total_times = [a.time_ms for a in completed]
        clean_tails = [a.clean_tail_ms for a in completed if a.clean_tail_ms is not None]

        avg_total = statistics.mean(total_times)

        n = len(completed)
        if n >= 2:
            half = max(n // 2, 1)
            first_half = statistics.mean(total_times[:half])
            second_half = statistics.mean(total_times[half:])
            total_trend = (first_half - second_half) / half
        else:
            total_trend = None

        clean_estimate: Estimate
        if clean_tails:
            avg_clean = statistics.mean(clean_tails)
            if len(clean_tails) >= 2:
                half_c = max(len(clean_tails) // 2, 1)
                first_c = statistics.mean(clean_tails[:half_c])
                second_c = statistics.mean(clean_tails[half_c:])
                clean_trend = (first_c - second_c) / half_c
            else:
                clean_trend = None
            clean_estimate = Estimate(
                expected_ms=avg_clean,
                ms_per_attempt=clean_trend,
                floor_ms=float(min(clean_tails)),
            )
        else:
            clean_estimate = Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None)

        return ModelOutput(
            total=Estimate(
                expected_ms=avg_total,
                ms_per_attempt=total_trend,
                floor_ms=float(min(total_times)),
            ),
            clean=clean_estimate,
        )

    def rebuild_state(self, attempts: list[AttemptRecord]) -> RollingMeanState:
        n_completed = sum(1 for a in attempts if a.completed)
        return RollingMeanState(n_completed=n_completed, n_attempts=len(attempts))
```

- [ ] **Step 2: Delete the old model_a.py**

```bash
rm python/spinlab/estimators/model_a.py
```

- [ ] **Step 3: Update scheduler.py imports**

In `python/spinlab/scheduler.py`, change:

```python
from spinlab.estimators.model_a import ModelAEstimator, ModelAState  # ensure registered
```
to:
```python
from spinlab.estimators.rolling_mean import RollingMeanEstimator, RollingMeanState  # ensure registered
```

And in `_STATE_CLASSES`:
```python
_STATE_CLASSES: dict[str, type[EstimatorState]] = {
    "kalman": KalmanState,
    "rolling_mean": RollingMeanState,
}
```

- [ ] **Step 4: Rename test file and update imports**

Copy `tests/test_model_a.py` to `tests/test_rolling_mean.py`. Update all references:

```python
# tests/test_rolling_mean.py
"""Tests for Rolling Mean estimator."""
import pytest
from spinlab.estimators.rolling_mean import RollingMeanEstimator, RollingMeanState
from spinlab.models import AttemptRecord, Estimate, ModelOutput
```

Rename all `TestModelA*` classes to `TestRollingMean*`. Update assertions to use the new `out.total.*` and `out.clean.*` access pattern. For example:

```python
class TestRollingMeanModelOutput:
    def test_constant_times_zero_trend(self):
        est = RollingMeanEstimator()
        attempts = [_attempt(10000) for _ in range(10)]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.total.ms_per_attempt == pytest.approx(0.0)

    def test_strictly_decreasing_positive_trend(self):
        est = RollingMeanEstimator()
        times = [15000, 14000, 13000, 12000, 11000, 10000, 9000, 8000, 7000, 6000]
        attempts = [_attempt(t) for t in times]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.total.ms_per_attempt > 0

    def test_strictly_increasing_negative_trend(self):
        est = RollingMeanEstimator()
        times = [6000, 7000, 8000, 9000, 10000, 11000, 12000, 13000, 14000, 15000]
        attempts = [_attempt(t) for t in times]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.total.ms_per_attempt < 0

    def test_single_attempt_none_trend(self):
        est = RollingMeanEstimator()
        a1 = _attempt(12000)
        state = est.init_state(a1, priors={})
        out = est.model_output(state, [a1])
        assert out.total.expected_ms == pytest.approx(12000.0)
        assert out.total.ms_per_attempt is None  # <2 attempts

    def test_two_attempts_computes_trend(self):
        est = RollingMeanEstimator()
        attempts = [_attempt(12000), _attempt(10000)]
        state = est.init_state(attempts[0], priors={})
        state = est.process_attempt(state, attempts[1], attempts)
        out = est.model_output(state, attempts)
        assert out.total.ms_per_attempt > 0  # improving

    def test_floor_is_min_observed(self):
        est = RollingMeanEstimator()
        attempts = [_attempt(t) for t in [15000, 12000, 10000, 11000, 13000]]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.total.floor_ms == pytest.approx(10000.0)
        assert out.clean.floor_ms == pytest.approx(10000.0)

    def test_dirty_attempts_separate_clean_and_total(self):
        est = RollingMeanEstimator()
        attempts = [
            _attempt(20000, deaths=2, clean_tail_ms=8000),
            _attempt(18000, deaths=1, clean_tail_ms=9000),
            _attempt(15000, deaths=0, clean_tail_ms=15000),
            _attempt(19000, deaths=2, clean_tail_ms=7000),
            _attempt(14000, deaths=0, clean_tail_ms=14000),
        ]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.clean.expected_ms != out.total.expected_ms
        assert out.clean.floor_ms == pytest.approx(7000.0)
        assert out.total.floor_ms == pytest.approx(14000.0)

    def test_no_clean_data_returns_none_clean(self):
        """If no clean_tail_ms values exist, clean side is all None."""
        est = RollingMeanEstimator()
        attempts = [
            AttemptRecord(time_ms=12000, completed=True, deaths=0, clean_tail_ms=None, created_at="2026-01-01T00:00:00"),
            AttemptRecord(time_ms=11000, completed=True, deaths=0, clean_tail_ms=None, created_at="2026-01-01T00:00:00"),
        ]
        state = est.init_state(attempts[0], priors={})
        state = est.process_attempt(state, attempts[1], attempts)
        out = est.model_output(state, attempts)
        assert out.clean.expected_ms is None
        assert out.clean.ms_per_attempt is None
        assert out.clean.floor_ms is None
```

Delete old test file:
```bash
rm tests/test_model_a.py
```

- [ ] **Step 5: Update test_model_output.py DB test**

In `tests/test_model_output.py`, the `TestDBMultiModel` class references `"model_a"`. Update:

```python
    def test_save_and_load_multi_model_state(self):
        db = self._setup_db()
        out_k = ModelOutput(
            total=Estimate(expected_ms=12000.0, ms_per_attempt=500.0, floor_ms=None),
            clean=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
        )
        out_r = ModelOutput(
            total=Estimate(expected_ms=12500.0, ms_per_attempt=300.0, floor_ms=11000.0),
            clean=Estimate(expected_ms=12500.0, ms_per_attempt=300.0, floor_ms=11000.0),
        )
        db.save_model_state("s1", "kalman", '{"mu": 12.0}', json.dumps(out_k.to_dict()))
        db.save_model_state("s1", "rolling_mean", '{"n_completed": 5}', json.dumps(out_r.to_dict()))
        rows = db.load_all_model_states_for_segment("s1")
        assert len(rows) == 2
        names = {r["estimator"] for r in rows}
        assert names == {"kalman", "rolling_mean"}

    def test_load_model_state_by_estimator(self):
        db = self._setup_db()
        out = ModelOutput(
            total=Estimate(expected_ms=12000.0, ms_per_attempt=500.0, floor_ms=None),
            clean=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
        )
        db.save_model_state("s1", "kalman", '{"mu": 12.0}', json.dumps(out.to_dict()))
        row = db.load_model_state("s1", "kalman")
        assert row is not None
        assert row["estimator"] == "kalman"
        loaded_out = ModelOutput.from_dict(json.loads(row["output_json"]))
        assert loaded_out.total.expected_ms == 12000.0
```

- [ ] **Step 6: Update test_scheduler_kalman.py registry references**

In `tests/test_scheduler_kalman.py`, change `"model_a"` to `"rolling_mean"` and `"model_b"` to `"exp_decay"`:

```python
    def test_process_attempt_creates_all_model_states(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        segment_id = "g1:1:entrance.0:checkpoint.0"
        sched.process_attempt(segment_id, time_ms=12000, completed=True)
        rows = db_with_segments.load_all_model_states_for_segment(segment_id)
        estimator_names = {r["estimator"] for r in rows}
        assert "kalman" in estimator_names
        assert "rolling_mean" in estimator_names
        try:
            import numpy  # noqa: F401
            assert "exp_decay" in estimator_names
        except ImportError:
            pass  # exp_decay unavailable without numpy
        for r in rows:
            out = ModelOutput.from_dict(json.loads(r["output_json"]))
            assert out.total.expected_ms is not None or out.clean.expected_ms is not None
```

- [ ] **Step 7: Run all tests**

Run: `pytest tests/test_rolling_mean.py tests/test_kalman.py tests/test_model_output.py tests/test_scheduler_kalman.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/estimators/rolling_mean.py tests/test_rolling_mean.py python/spinlab/scheduler.py tests/test_model_output.py tests/test_scheduler_kalman.py
git rm python/spinlab/estimators/model_a.py tests/test_model_a.py
git commit -m "refactor: rename Model A to Rolling Mean, update to V2 ModelOutput"
```

---

### Task 4: Rename Model B → Exp Decay

**Files:**
- Rename: `python/spinlab/estimators/model_b.py` → `python/spinlab/estimators/exp_decay.py`
- Rename: `tests/test_model_b.py` → `tests/test_exp_decay.py`
- Modify: `python/spinlab/scheduler.py` (imports and _STATE_CLASSES)

- [ ] **Step 1: Create exp_decay.py with renamed classes**

Copy `python/spinlab/estimators/model_b.py` to `python/spinlab/estimators/exp_decay.py`. Update class names and registry:

```python
# python/spinlab/estimators/exp_decay.py
"""Exponential decay estimator.

Fits time(n) = amplitude * exp(-decay_rate * n) + asymptote
via scipy.optimize.curve_fit. Two fits: one on total times, one on clean tails.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import curve_fit

from spinlab.estimators import Estimator, EstimatorState, register_estimator
from spinlab.models import AttemptRecord, Estimate, ModelOutput

MIN_POINTS_FOR_FIT = 3


def _exp_decay(n: np.ndarray, amplitude: float, decay_rate: float, asymptote: float) -> np.ndarray:
    return amplitude * np.exp(-decay_rate * n) + asymptote


def _fit_exp_decay(ns: np.ndarray, ts: np.ndarray) -> tuple[float, float, float, float]:
    """Fit amplitude*exp(-decay_rate*n)+asymptote. Returns (amplitude, decay_rate, asymptote, sigma)."""
    best = float(np.min(ts))
    initial_amplitude = max(float(np.median(ts)) - best, 1.0)
    try:
        popt, _ = curve_fit(
            _exp_decay, ns, ts,
            p0=[initial_amplitude, 0.05, best],
            bounds=([0, 0, 0], [np.inf, np.inf, best]),
        )
        amplitude, decay_rate, asymptote = popt
        residuals = ts - _exp_decay(ns, amplitude, decay_rate, asymptote)
        sigma = float(np.std(residuals))
        return float(amplitude), float(decay_rate), float(asymptote), sigma
    except RuntimeError:
        return initial_amplitude, 0.0, best, float(np.std(ts))


@dataclass
class ExpDecayState(EstimatorState):
    """Bookkeeping + cached fit params."""

    n_completed: int = 0
    n_attempts: int = 0
    amplitude: float = 0.0
    decay_rate: float = 0.0
    asymptote: float = 0.0
    sigma: float = 0.0
    total_amplitude: float = 0.0
    total_decay_rate: float = 0.0
    total_asymptote: float = 0.0
    total_sigma: float = 0.0

    def to_dict(self) -> dict:
        return {
            "n_completed": self.n_completed, "n_attempts": self.n_attempts,
            "amplitude": self.amplitude, "decay_rate": self.decay_rate,
            "asymptote": self.asymptote, "sigma": self.sigma,
            "total_amplitude": self.total_amplitude,
            "total_decay_rate": self.total_decay_rate,
            "total_asymptote": self.total_asymptote,
            "total_sigma": self.total_sigma,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExpDecayState":
        return cls(
            n_completed=d.get("n_completed", 0), n_attempts=d.get("n_attempts", 0),
            amplitude=d.get("amplitude", 0.0), decay_rate=d.get("decay_rate", 0.0),
            asymptote=d.get("asymptote", 0.0), sigma=d.get("sigma", 0.0),
            total_amplitude=d.get("total_amplitude", 0.0),
            total_decay_rate=d.get("total_decay_rate", 0.0),
            total_asymptote=d.get("total_asymptote", 0.0),
            total_sigma=d.get("total_sigma", 0.0),
        )


@register_estimator
class ExpDecayEstimator(Estimator):
    name = "exp_decay"
    display_name = "Exp. Decay"

    def _run_fits(self, completed: list[AttemptRecord]) -> ExpDecayState:
        state = ExpDecayState(n_completed=len(completed), n_attempts=len(completed))
        if len(completed) < MIN_POINTS_FOR_FIT:
            return state

        ns = np.arange(len(completed), dtype=float)

        clean_ts = np.array([a.clean_tail_ms if a.clean_tail_ms is not None else a.time_ms
                             for a in completed], dtype=float)
        a, b, c, sigma = _fit_exp_decay(ns, clean_ts)
        state.amplitude = a
        state.decay_rate = b
        state.asymptote = c
        state.sigma = sigma

        total_ts = np.array([att.time_ms for att in completed], dtype=float)
        ta, tb, tc, tsigma = _fit_exp_decay(ns, total_ts)
        state.total_amplitude = ta
        state.total_decay_rate = tb
        state.total_asymptote = tc
        state.total_sigma = tsigma

        return state

    def init_state(self, first_attempt: AttemptRecord, priors: dict) -> ExpDecayState:
        return ExpDecayState(n_completed=1, n_attempts=1)

    def process_attempt(
        self, state: ExpDecayState, new_attempt: AttemptRecord,
        all_attempts: list[AttemptRecord],
    ) -> ExpDecayState:
        n_completed = state.n_completed + (1 if new_attempt.completed else 0)
        completed = [a for a in all_attempts if a.completed and a.time_ms is not None]
        new_state = self._run_fits(completed)
        new_state.n_completed = n_completed
        new_state.n_attempts = state.n_attempts + 1
        return new_state

    def model_output(self, state: ExpDecayState, all_attempts: list[AttemptRecord]) -> ModelOutput:
        completed = [a for a in all_attempts if a.completed and a.time_ms is not None]
        n = len(completed)

        none_estimate = Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None)

        if n < MIN_POINTS_FOR_FIT:
            return ModelOutput(total=none_estimate, clean=none_estimate)

        next_n = float(n)  # predict at index n (next unobserved)

        # Total time fit
        total_expected = float(state.total_amplitude * np.exp(-state.total_decay_rate * next_n) + state.total_asymptote)
        total_next_next = float(state.total_amplitude * np.exp(-state.total_decay_rate * (next_n + 1)) + state.total_asymptote)
        total_mpa = total_expected - total_next_next  # discrete difference, positive = improving

        # Clean tail fit
        clean_expected = float(state.amplitude * np.exp(-state.decay_rate * next_n) + state.asymptote)
        clean_next_next = float(state.amplitude * np.exp(-state.decay_rate * (next_n + 1)) + state.asymptote)
        clean_mpa = clean_expected - clean_next_next

        return ModelOutput(
            total=Estimate(
                expected_ms=total_expected,
                ms_per_attempt=total_mpa,
                floor_ms=max(0.0, state.total_asymptote),
            ),
            clean=Estimate(
                expected_ms=clean_expected,
                ms_per_attempt=clean_mpa,
                floor_ms=max(0.0, state.asymptote),
            ),
        )

    def rebuild_state(self, attempts: list[AttemptRecord]) -> ExpDecayState:
        completed = [a for a in attempts if a.completed and a.time_ms is not None]
        state = self._run_fits(completed)
        state.n_completed = len(completed)
        state.n_attempts = len(attempts)
        return state
```

- [ ] **Step 2: Delete old model_b.py**

```bash
rm python/spinlab/estimators/model_b.py
```

- [ ] **Step 3: Update scheduler.py imports**

In `python/spinlab/scheduler.py`, change:

```python
_has_model_b = False
try:
    from spinlab.estimators.model_b import ModelBEstimator, ModelBState  # ensure registered
    _has_model_b = True
except ImportError:
    logger.warning("model_b unavailable (numpy/scipy not installed)")
```
to:
```python
_has_exp_decay = False
try:
    from spinlab.estimators.exp_decay import ExpDecayEstimator, ExpDecayState  # ensure registered
    _has_exp_decay = True
except ImportError:
    logger.warning("exp_decay unavailable (numpy/scipy not installed)")
```

And update `_STATE_CLASSES`:
```python
if _has_exp_decay:
    _STATE_CLASSES["exp_decay"] = ExpDecayState
```

- [ ] **Step 4: Rename test file and update**

Copy `tests/test_model_b.py` to `tests/test_exp_decay.py`. Update imports and class names:

```python
# tests/test_exp_decay.py
"""Tests for Exp Decay estimator."""
import math
import pytest

np = pytest.importorskip("numpy")
from spinlab.estimators.exp_decay import ExpDecayEstimator, ExpDecayState
from spinlab.models import AttemptRecord, Estimate, ModelOutput
```

Rename all `TestModelB*` to `TestExpDecay*`. Update assertions to use `out.total.*` / `out.clean.*`:

```python
class TestExpDecayModelOutput:
    def test_output_with_enough_data(self):
        est = ExpDecayEstimator()
        attempts = _synthetic_exp_attempts(25)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert isinstance(out, ModelOutput)
        assert out.total.ms_per_attempt > 0
        assert out.total.floor_ms > 0
        assert out.clean.floor_ms > 0
        assert out.clean.floor_ms < out.total.expected_ms

    def test_ms_per_attempt_is_discrete_difference(self):
        """ms_per_attempt should be f(n) - f(n+1) from total fit."""
        est = ExpDecayEstimator()
        a, b, c = 12000.0, 0.1, 3000.0
        attempts = _synthetic_exp_attempts(25, amplitude=a, decay_rate=b, asymptote=c)
        state = est.init_state(attempts[0], priors={})
        for att in attempts[1:]:
            state = est.process_attempt(state, att, attempts)
        out = est.model_output(state, attempts)
        # Discrete difference at n=25: f(25) - f(26)
        f_n = a * math.exp(-b * 25) + c
        f_n1 = a * math.exp(-b * 26) + c
        expected_mpa = f_n - f_n1
        assert out.total.ms_per_attempt == pytest.approx(expected_mpa, rel=0.15)

    def test_floor_never_negative(self):
        est = ExpDecayEstimator()
        attempts = _synthetic_exp_attempts(25, asymptote=100)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.total.floor_ms >= 0
        assert out.clean.floor_ms >= 0

    def test_few_points_returns_none(self):
        """With <3 completed, returns all None — no silent fallback."""
        est = ExpDecayEstimator()
        attempts = [_attempt(12000), _attempt(11500)]
        state = est.init_state(attempts[0], priors={})
        state = est.process_attempt(state, attempts[1], attempts)
        out = est.model_output(state, attempts)
        assert out.total.expected_ms is None
        assert out.total.ms_per_attempt is None
        assert out.total.floor_ms is None
        assert out.clean.expected_ms is None

    def test_two_fits_total_and_clean(self):
        est = ExpDecayEstimator()
        n = 25
        attempts = []
        for i in range(n):
            total = int(12000 * math.exp(-0.1 * i) + 5000)
            clean = int(8000 * math.exp(-0.1 * i) + 3000)
            deaths = 2 if i % 3 == 0 else 0
            attempts.append(_attempt(total, deaths=deaths, clean_tail_ms=clean))
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.total.floor_ms > out.clean.floor_ms
```

Delete old test file:
```bash
rm tests/test_model_b.py
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_exp_decay.py tests/test_scheduler_kalman.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/estimators/exp_decay.py tests/test_exp_decay.py python/spinlab/scheduler.py
git rm python/spinlab/estimators/model_b.py tests/test_model_b.py
git commit -m "refactor: rename Model B to Exp Decay, update to V2 ModelOutput"
```

---

### Task 5: Update allocator and greedy scorer

**Files:**
- Modify: `python/spinlab/allocators/greedy.py`
- Test: `tests/test_allocators.py`

- [ ] **Step 1: Update test helper and assertions**

In `tests/test_allocators.py`, update `_make_segment` and assertions:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_allocators.py -v`
Expected: FAIL — greedy scorer reads `out.ms_per_attempt` which no longer exists

- [ ] **Step 3: Update greedy allocator scorer**

In `python/spinlab/allocators/greedy.py`, change the `_score` function:

```python
def _score(s: SegmentWithModel) -> float:
    out = s.model_outputs.get(s.selected_model)
    if out is None:
        return 0.0
    return out.total.ms_per_attempt if out.total.ms_per_attempt is not None else 0.0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_allocators.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/allocators/greedy.py tests/test_allocators.py
git commit -m "fix: update greedy allocator to read output.total.ms_per_attempt"
```

---

### Task 6: Update practice.py and dashboard consumers

**Files:**
- Modify: `python/spinlab/practice.py:73-76`
- Modify: `python/spinlab/dashboard.py:165-167`
- Test: `tests/test_dashboard_integration.py`

- [ ] **Step 1: Update practice.py**

In `python/spinlab/practice.py`, change lines 73-76:

```python
        expected_time_ms = None
        sel_out = picked.model_outputs.get(picked.selected_model)
        if sel_out and sel_out.expected_time_ms > 0:
            expected_time_ms = int(sel_out.expected_time_ms)
```
to:
```python
        expected_time_ms = None
        sel_out = picked.model_outputs.get(picked.selected_model)
        if sel_out and sel_out.total.expected_ms is not None and sel_out.total.expected_ms > 0:
            expected_time_ms = int(sel_out.total.expected_ms)
```

- [ ] **Step 2: Update dashboard test fixture**

In `tests/test_dashboard_integration.py`, update the model state output dicts (around line 93):

```python
    for segment_id, mu, d, mr in MODEL_STATES:
        state = {"mu": mu, "P": 1.0, "d": d, "Q_mu": 0.5, "Q_d": 0.01, "R": 1.0, "n": 5,
                 "gold": gold_times[segment_id], "n_completed": 3, "n_attempts": 3}
        output = {
            "total": {"expected_ms": mu * 1000, "ms_per_attempt": mr * 1000, "floor_ms": mu * 800},
            "clean": {"expected_ms": None, "ms_per_attempt": None, "floor_ms": None},
        }
        db.save_model_state(segment_id, "kalman", json.dumps(state), json.dumps(output))
```

Update the assertion (around line 221):

```python
        kalman = s1["model_outputs"]["kalman"]
        assert kalman["total"]["expected_ms"] == pytest.approx(3800, abs=100)
        assert kalman["total"]["ms_per_attempt"] is not None
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_dashboard_integration.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/practice.py tests/test_dashboard_integration.py
git commit -m "fix: update practice and dashboard to read V2 ModelOutput"
```

---

### Task 7: Update frontend JavaScript

**Files:**
- Modify: `python/spinlab/static/model.js`

- [ ] **Step 1: Update model table rendering**

In `python/spinlab/static/model.js`, the `updateModel` function reads `sel.expected_time_ms`, `sel.ms_per_attempt`, and `sel.floor_estimate_ms`. Update to read from nested `total`:

```javascript
function updateModel(data) {
  const body = document.getElementById('model-body');
  if (!data.segments || !data.segments.length) {
    body.innerHTML = '<tr><td colspan="6" class="dim">No game loaded</td></tr>';
    return;
  }
  body.innerHTML = '';
  data.segments.forEach(s => {
    const tr = document.createElement('tr');
    const sel = s.model_outputs[s.selected_model];
    const total = sel ? sel.total : null;

    let improvClass = 'flat';
    let arrow = '\u2192';
    if (total && total.ms_per_attempt != null) {
      if (total.ms_per_attempt > 10) { improvClass = 'improving'; arrow = '\u2193'; }
      else if (total.ms_per_attempt < -10) { improvClass = 'regressing'; arrow = '\u2191'; }
    }
    tr.className = 'drift-row-' + improvClass;

    const mpaStr = total && total.ms_per_attempt != null
      ? total.ms_per_attempt.toFixed(1) + ' ms/att'
      : '\u2014';

    tr.innerHTML =
      '<td>' + segmentName(s) + '</td>' +
      '<td>' + formatTime(total ? total.expected_ms : null) + '</td>' +
      '<td class="drift-' + improvClass + '">' + arrow + ' ' + mpaStr + '</td>' +
      '<td>' + formatTime(total ? total.floor_ms : null) + '</td>' +
      '<td>' + s.n_completed + '</td>' +
      '<td>' + formatTime(s.gold_ms) + '</td>';
    body.appendChild(tr);
  });
  const estSelect = document.getElementById('estimator-select');
  if (estSelect && data.estimators) {
    const current = data.estimator || estSelect.value;
    estSelect.innerHTML = '';
    data.estimators.forEach(e => {
      const opt = document.createElement('option');
      opt.value = e.name;
      opt.textContent = e.display_name;
      estSelect.appendChild(opt);
    });
    estSelect.value = current;
  }
}
```

Also update the `updatePracticeCard` function's insight section:

```javascript
  const selOut = cs.model_outputs && cs.model_outputs[cs.selected_model];
  if (selOut && selOut.total) {
    const mpa = selOut.total.ms_per_attempt;
    if (mpa != null) {
      const arrow = mpa > 10 ? '\u2193' : mpa < -10 ? '\u2191' : '\u2192';
      const label = mpa > 10 ? 'improving' : mpa < -10 ? 'regressing' : 'flat';
      insight.innerHTML =
        '<span class="drift-' + label + '">' +
        arrow + ' ' + Math.abs(mpa).toFixed(1) + ' ms/att</span>';
    } else {
      insight.textContent = '\u2014';
    }
  } else {
    insight.textContent = 'No data yet';
  }
```

- [ ] **Step 2: Commit**

```bash
git add python/spinlab/static/model.js
git commit -m "fix: update dashboard JS to read V2 nested ModelOutput"
```

---

### Task 8: Run full test suite and fix stragglers

**Files:**
- Possibly modify: any file with remaining V1 references

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass. If any fail, they are V1 field references that were missed.

- [ ] **Step 2: Fix any remaining failures**

Common stragglers:
- `tests/test_db_dashboard.py:116` — uses raw JSON with `expected_time_ms`. This is a raw string test of DB storage, not ModelOutput structure. Leave as-is (it tests that the DB stores/retrieves arbitrary JSON).
- Any remaining `out.expected_time_ms` or `out.ms_per_attempt` direct accesses.

- [ ] **Step 3: Run full suite again to confirm clean**

Run: `pytest tests/ -v`
Expected: All PASS

- [ ] **Step 4: Commit any remaining fixes**

```bash
git add -u
git commit -m "fix: remaining V1 ModelOutput references"
```

---

### Task 9: Verify dashboard end-to-end (manual smoke check)

- [ ] **Step 1: Check for import errors**

Run: `python -c "from spinlab.scheduler import Scheduler; print('OK')"`
Expected: `OK`

- [ ] **Step 2: Check estimator registry**

Run: `python -c "from spinlab.estimators import list_estimators; print(list_estimators())"`
Expected: `['kalman', 'rolling_mean', 'exp_decay']` (or `['kalman', 'rolling_mean']` if numpy not installed)

- [ ] **Step 3: Commit if any final fixes needed**

If any issues found, fix and commit.

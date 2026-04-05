# Multi-Model Estimator Upgrade — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Models A (rolling stats) and B (exponential decay) alongside Kalman, with death/clean-tail tracking and a unified ModelOutput contract.

**Architecture:** Widen the Estimator ABC to produce a shared `ModelOutput` dataclass. Change `model_state` table PK from `segment_id` to `(segment_id, estimator)`. Scheduler runs ALL registered estimators on each attempt, allocator reads from the user-selected one. Death count and clean-tail time flow from Lua through TCP to Python models.

**Tech Stack:** Python 3.11+, SQLite, scipy (curve_fit for Model B), Lua (Mesen2), vanilla JS frontend

**Spec:** `docs/multi-model-spec.md`

**Reference implementation:** `reference/speedrun_models.py` (algorithm reference only — do NOT copy floor clamping or magic ratios)

### Design decisions (from brainstorming refinement)

- **Four distinct "best time" concepts:**
  - `gold_ms` — best observed total time (fact, on SegmentWithModel)
  - `clean_gold_ms` — best observed clean tail (fact, on SegmentWithModel)
  - `floor_estimate_ms` — E[total_time | infinite practice] (model estimate, on ModelOutput)
  - `clean_floor_estimate_ms` — E[clean_tail | infinite practice] (model estimate, on ModelOutput)
- **No floor clamping magic.** No `*0.60`, no `*1.1`. Floors are `max(0, asymptote)` or `min(observed)`. If we ever need priors, they go in config.
- **Model B internal params** named `amplitude`, `decay_rate`, `asymptote` (not a/b/c) to avoid confusion with the generic "floor" concept.
- **Model B does two fits:** one on total times (→ `floor_estimate_ms`), one on clean tails (→ `clean_floor_estimate_ms`).
- **Kalman floor = gold_ms** for now (placeholder — future: use uncertainty via `mu - k*sqrt(P_mm)`).
- **No old data migration.** Schema changes are destructive. `clean_tail_ms` always present for completed attempts.
- **`ms_per_attempt`** positive = improving, consistently across all models.
- **apm computed by allocator**, not models. `attempts_per_minute ≈ 60_000 / expected_time_ms`.

---

## File Structure

### New files
- `python/spinlab/estimators/model_a.py` — Rolling statistics estimator
- `python/spinlab/estimators/model_b.py` — Exponential decay estimator
- `tests/test_model_output.py` — Tests for ModelOutput contract and AttemptRecord
- `tests/test_model_a.py` — Tests for Model A
- `tests/test_model_b.py` — Tests for Model B

### Modified files
- `python/spinlab/models.py` — Add `AttemptRecord`, `ModelOutput` dataclasses
- `python/spinlab/estimators/__init__.py` — Widen `Estimator` ABC with new signatures
- `python/spinlab/estimators/kalman.py` — Adapt to new ABC, add `model_output()`
- `python/spinlab/db.py` — Schema changes (model_state PK, attempts columns), query changes
- `python/spinlab/scheduler.py` — Run all estimators, use ModelOutput for allocator
- `python/spinlab/allocators/__init__.py` — Change `SegmentWithModel` to use `model_outputs` dict
- `python/spinlab/allocators/greedy.py` — Use `ms_per_attempt` instead of `marginal_return`
- `python/spinlab/practice.py` — Pass deaths/clean_tail through to scheduler
- `python/spinlab/dashboard.py` — Return all models' outputs in `/api/model`
- `python/spinlab/session_manager.py` — Adapt state snapshot for multi-model
- `python/spinlab/static/model.js` — Render all models side-by-side
- `tests/test_scheduler_kalman.py` — Adapt for new signatures
- `tests/test_allocators.py` — Adapt for new SegmentWithModel shape

---

## Task 1: Add AttemptRecord and ModelOutput dataclasses

**Files:**
- Modify: `python/spinlab/models.py`
- Create: `tests/test_model_output.py`

- [ ] **Step 1: Write the failing test for AttemptRecord**

```python
# tests/test_model_output.py
"""Tests for AttemptRecord and ModelOutput dataclasses."""
from spinlab.models import AttemptRecord, ModelOutput


class TestAttemptRecord:
    def test_completed_attempt(self):
        ar = AttemptRecord(
            time_ms=12000, completed=True, deaths=2,
            clean_tail_ms=4500, created_at="2026-03-27T12:00:00",
        )
        assert ar.time_ms == 12000
        assert ar.completed is True
        assert ar.deaths == 2
        assert ar.clean_tail_ms == 4500

    def test_incomplete_attempt(self):
        ar = AttemptRecord(
            time_ms=None, completed=False, deaths=0,
            clean_tail_ms=None, created_at="2026-03-27T12:00:00",
        )
        assert ar.time_ms is None
        assert ar.completed is False
        assert ar.clean_tail_ms is None

    def test_zero_death_clean_tail_equals_time(self):
        ar = AttemptRecord(
            time_ms=8000, completed=True, deaths=0,
            clean_tail_ms=8000, created_at="2026-03-27T12:00:00",
        )
        assert ar.clean_tail_ms == ar.time_ms
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_model_output.py -v`
Expected: FAIL with `ImportError: cannot import name 'AttemptRecord'`

- [ ] **Step 3: Implement AttemptRecord and ModelOutput in models.py**

Add to end of `python/spinlab/models.py`:

```python
@dataclass
class AttemptRecord:
    """Attempt data flowing through the estimator pipeline."""
    time_ms: int | None          # total time including deaths; None if incomplete
    completed: bool
    deaths: int                  # 0 if clean
    clean_tail_ms: int | None    # time from last death to finish; None if incomplete
    created_at: str              # ISO timestamp


@dataclass
class ModelOutput:
    """What every estimator produces."""
    expected_time_ms: float             # E[total_time] for next attempt
    clean_expected_ms: float            # E[clean_tail] for next attempt
    ms_per_attempt: float               # improvement rate (positive = improving)
    floor_estimate_ms: float            # E[total_time | infinite practice]
    clean_floor_estimate_ms: float      # E[clean_tail | infinite practice]

    def to_dict(self) -> dict:
        return {
            "expected_time_ms": self.expected_time_ms,
            "clean_expected_ms": self.clean_expected_ms,
            "ms_per_attempt": self.ms_per_attempt,
            "floor_estimate_ms": self.floor_estimate_ms,
            "clean_floor_estimate_ms": self.clean_floor_estimate_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelOutput":
        return cls(
            expected_time_ms=d["expected_time_ms"],
            clean_expected_ms=d["clean_expected_ms"],
            ms_per_attempt=d["ms_per_attempt"],
            floor_estimate_ms=d["floor_estimate_ms"],
            clean_floor_estimate_ms=d["clean_floor_estimate_ms"],
        )
```

- [ ] **Step 4: Add ModelOutput tests**

Append to `tests/test_model_output.py`:

```python
class TestModelOutput:
    def test_round_trip_serialization(self):
        mo = ModelOutput(
            expected_time_ms=12000.0,
            clean_expected_ms=8000.0,
            ms_per_attempt=150.0,
            floor_estimate_ms=7000.0,
            clean_floor_estimate_ms=6000.0,
        )
        d = mo.to_dict()
        mo2 = ModelOutput.from_dict(d)
        assert mo2.expected_time_ms == 12000.0
        assert mo2.clean_expected_ms == 8000.0
        assert mo2.ms_per_attempt == 150.0
        assert mo2.floor_estimate_ms == 7000.0
        assert mo2.clean_floor_estimate_ms == 6000.0

    def test_all_five_fields_present(self):
        mo = ModelOutput(0.0, 0.0, 0.0, 0.0, 0.0)
        d = mo.to_dict()
        assert set(d.keys()) == {
            "expected_time_ms", "clean_expected_ms", "ms_per_attempt",
            "floor_estimate_ms", "clean_floor_estimate_ms",
        }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_model_output.py -v`
Expected: All 5 tests PASS

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/models.py tests/test_model_output.py
git commit -m "feat: add AttemptRecord and ModelOutput dataclasses"
```

---

## Task 2: Widen the Estimator ABC

**Files:**
- Modify: `python/spinlab/estimators/__init__.py`

- [ ] **Step 1: Update the Estimator ABC with new signatures**

Replace `python/spinlab/estimators/__init__.py` entirely:

```python
"""Estimator abstract base class and registry."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spinlab.models import AttemptRecord, ModelOutput


@dataclass
class EstimatorState(ABC):
    """Base class for estimator-specific state."""

    @abstractmethod
    def to_dict(self) -> dict:
        ...

    @classmethod
    @abstractmethod
    def from_dict(cls, d: dict) -> "EstimatorState":
        ...


class Estimator(ABC):
    """Abstract estimator that tracks per-split performance."""

    name: str

    @abstractmethod
    def init_state(
        self, first_attempt: "AttemptRecord", priors: dict
    ) -> EstimatorState:
        """Initialize state from the first completed attempt."""
        ...

    @abstractmethod
    def process_attempt(
        self,
        state: EstimatorState,
        new_attempt: "AttemptRecord",
        all_attempts: list["AttemptRecord"],
    ) -> EstimatorState:
        """Process one attempt. Uses new_attempt and/or all_attempts as needed."""
        ...

    @abstractmethod
    def model_output(
        self, state: EstimatorState, all_attempts: list["AttemptRecord"]
    ) -> "ModelOutput":
        """Produce standardized ModelOutput from current state."""
        ...

    @abstractmethod
    def rebuild_state(
        self, attempts: list["AttemptRecord"]
    ) -> EstimatorState:
        """Rebuild state by replaying all attempts."""
        ...


# Registry: name -> Estimator class
_ESTIMATOR_REGISTRY: dict[str, type[Estimator]] = {}


def register_estimator(cls: type[Estimator]) -> type[Estimator]:
    """Decorator to register an estimator class."""
    _ESTIMATOR_REGISTRY[cls.name] = cls
    return cls


def get_estimator(name: str) -> Estimator:
    """Instantiate an estimator by name."""
    if name not in _ESTIMATOR_REGISTRY:
        raise ValueError(
            f"Unknown estimator: {name!r}. "
            f"Available: {list(_ESTIMATOR_REGISTRY.keys())}"
        )
    return _ESTIMATOR_REGISTRY[name]()


def list_estimators() -> list[str]:
    """Return list of registered estimator names."""
    return list(_ESTIMATOR_REGISTRY.keys())
```

Removed from ABC: `marginal_return()`, `drift_info()`, `get_population_priors()`. `marginal_return` replaced by `ModelOutput.ms_per_attempt`. `drift_info` and `get_population_priors` stay as non-abstract methods on `KalmanEstimator`.

- [ ] **Step 2: Verify the module loads**

Run: `python -c "from spinlab.estimators import Estimator, EstimatorState; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/estimators/__init__.py
git commit -m "feat: widen Estimator ABC with process_attempt, model_output, rebuild_state"
```

---

## Task 3: Adapt Kalman to the new ABC

**Files:**
- Modify: `python/spinlab/estimators/kalman.py`
- Modify: `tests/test_kalman.py`

- [ ] **Step 1: Write the failing test for Kalman's new interface**

Replace `tests/test_kalman.py`:

```python
"""Tests for the Kalman estimator (new multi-model interface)."""
import pytest
from spinlab.estimators.kalman import KalmanEstimator, KalmanState
from spinlab.models import AttemptRecord, ModelOutput


def _attempt(time_ms: int | None, completed: bool, deaths: int = 0,
             clean_tail_ms: int | None = None) -> AttemptRecord:
    if clean_tail_ms is None and completed and time_ms is not None:
        clean_tail_ms = time_ms
    return AttemptRecord(
        time_ms=time_ms, completed=completed, deaths=deaths,
        clean_tail_ms=clean_tail_ms, created_at="2026-01-01T00:00:00",
    )


class TestKalmanProcessAttempt:
    def test_first_completed_attempt_initializes(self):
        est = KalmanEstimator()
        attempt = _attempt(12000, True)
        state = est.init_state(attempt, priors={})
        assert state.mu == pytest.approx(12.0)
        assert state.n_completed == 1
        assert state.n_attempts == 1

    def test_process_completed_updates_mu(self):
        est = KalmanEstimator()
        a1 = _attempt(12000, True)
        state = est.init_state(a1, priors={})
        a2 = _attempt(11000, True)
        state = est.process_attempt(state, a2, [a1, a2])
        assert state.n_completed == 2
        assert state.mu < 12.0

    def test_process_incomplete_increments_attempts_only(self):
        est = KalmanEstimator()
        a1 = _attempt(12000, True)
        state = est.init_state(a1, priors={})
        a2 = _attempt(None, False)
        state = est.process_attempt(state, a2, [a1, a2])
        assert state.n_completed == 1
        assert state.n_attempts == 2


class TestKalmanModelOutput:
    def test_produces_model_output(self):
        est = KalmanEstimator()
        a1 = _attempt(12000, True)
        state = est.init_state(a1, priors={})
        out = est.model_output(state, [a1])
        assert isinstance(out, ModelOutput)
        assert out.expected_time_ms == pytest.approx(12000.0)
        assert out.ms_per_attempt == pytest.approx(500.0)  # -d * 1000, default d=-0.5
        # Kalman floor = gold for now (placeholder)
        assert out.floor_estimate_ms == pytest.approx(12000.0)
        assert out.clean_floor_estimate_ms == pytest.approx(12000.0)

    def test_clean_expected_equals_expected(self):
        """Kalman doesn't distinguish clean/dirty yet."""
        est = KalmanEstimator()
        a1 = _attempt(12000, True)
        state = est.init_state(a1, priors={})
        out = est.model_output(state, [a1])
        assert out.clean_expected_ms == out.expected_time_ms

    def test_improving_attempts_positive_ms_per_attempt(self):
        est = KalmanEstimator()
        times = [12000, 11500, 11000, 10500, 10000, 9500, 9000, 8500, 8000, 7500]
        attempts = [_attempt(t, True) for t in times]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.ms_per_attempt > 0

    def test_floor_equals_gold(self):
        """Kalman floor is gold_ms (placeholder for future uncertainty-based floor)."""
        est = KalmanEstimator()
        attempts = [_attempt(t, True) for t in [12000, 11000, 10000]]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        gold_ms = min(a.time_ms for a in attempts) * 1.0
        out = est.model_output(state, attempts)
        assert out.floor_estimate_ms == pytest.approx(gold_ms)
        assert out.clean_floor_estimate_ms == pytest.approx(gold_ms)


class TestKalmanRebuildState:
    def test_rebuild_from_attempts(self):
        est = KalmanEstimator()
        attempts = [_attempt(12000, True), _attempt(None, False), _attempt(11000, True)]
        state = est.rebuild_state(attempts)
        assert state.n_completed == 2
        assert state.n_attempts == 3

    def test_rebuild_empty(self):
        est = KalmanEstimator()
        state = est.rebuild_state([])
        assert state.n_completed == 0
        assert state.n_attempts == 0


class TestKalmanDriftInfo:
    def test_drift_info_returns_dict(self):
        est = KalmanEstimator()
        a1 = _attempt(12000, True)
        state = est.init_state(a1, priors={})
        info = est.drift_info(state)
        assert "drift" in info
        assert "label" in info
        assert "ci_lower" in info
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_kalman.py -v`
Expected: FAIL — `init_state()` still expects `(first_time: float, priors: dict)`

- [ ] **Step 3: Implement the Kalman adapter**

Replace `python/spinlab/estimators/kalman.py`:

```python
"""Kalman filter estimator for speedrun split times."""
from __future__ import annotations

from dataclasses import dataclass, replace

from spinlab.estimators import Estimator, EstimatorState, register_estimator
from spinlab.models import AttemptRecord, ModelOutput

# === Defaults ===
DEFAULT_D = -0.5
DEFAULT_R = 25.0
DEFAULT_P_D0 = 1.0
DEFAULT_Q_MM = 0.1
DEFAULT_Q_MD = 0.0
DEFAULT_Q_DD = 0.01
R_FLOOR = 1.0
R_REESTIMATE_INTERVAL = 10


@dataclass
class KalmanState(EstimatorState):
    """Per-split Kalman filter state."""

    mu: float = 0.0
    d: float = DEFAULT_D
    P_mm: float = DEFAULT_R
    P_md: float = 0.0
    P_dm: float = 0.0
    P_dd: float = DEFAULT_P_D0
    R: float = DEFAULT_R
    Q_mm: float = DEFAULT_Q_MM
    Q_md: float = DEFAULT_Q_MD
    Q_dm: float = DEFAULT_Q_MD
    Q_dd: float = DEFAULT_Q_DD
    gold: float = float("inf")
    n_completed: int = 0
    n_attempts: int = 0

    def to_dict(self) -> dict:
        return {
            "mu": self.mu, "d": self.d,
            "P_mm": self.P_mm, "P_md": self.P_md,
            "P_dm": self.P_dm, "P_dd": self.P_dd,
            "R": self.R,
            "Q_mm": self.Q_mm, "Q_md": self.Q_md,
            "Q_dm": self.Q_dm, "Q_dd": self.Q_dd,
            "gold": self.gold,
            "n_completed": self.n_completed,
            "n_attempts": self.n_attempts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "KalmanState":
        return cls(
            mu=d.get("mu", 0.0), d=d.get("d", DEFAULT_D),
            P_mm=d.get("P_mm", DEFAULT_R), P_md=d.get("P_md", 0.0),
            P_dm=d.get("P_dm", 0.0), P_dd=d.get("P_dd", DEFAULT_P_D0),
            R=d.get("R", DEFAULT_R),
            Q_mm=d.get("Q_mm", DEFAULT_Q_MM), Q_md=d.get("Q_md", DEFAULT_Q_MD),
            Q_dm=d.get("Q_dm", DEFAULT_Q_MD), Q_dd=d.get("Q_dd", DEFAULT_Q_DD),
            gold=d.get("gold", float("inf")),
            n_completed=d.get("n_completed", 0),
            n_attempts=d.get("n_attempts", 0),
        )


@register_estimator
class KalmanEstimator(Estimator):
    name = "kalman"

    def _predict(self, state: KalmanState) -> KalmanState:
        mu_pred = state.mu + state.d
        d_pred = state.d
        P_mm_pred = state.P_mm + state.P_md + state.P_dm + state.P_dd + state.Q_mm
        P_md_pred = state.P_md + state.P_dd + state.Q_md
        P_dm_pred = state.P_dm + state.P_dd + state.Q_dm
        P_dd_pred = state.P_dd + state.Q_dd
        return replace(state,
            mu=mu_pred, d=d_pred,
            P_mm=P_mm_pred, P_md=P_md_pred, P_dm=P_dm_pred, P_dd=P_dd_pred,
        )

    def _update(self, predicted: KalmanState, observed_time: float) -> KalmanState:
        z = observed_time - predicted.mu
        S = predicted.P_mm + predicted.R
        K_mu = predicted.P_mm / S
        K_d = predicted.P_dm / S
        mu_new = predicted.mu + K_mu * z
        d_new = predicted.d + K_d * z
        P_mm_new = (1 - K_mu) * predicted.P_mm
        P_md_new = (1 - K_mu) * predicted.P_md
        P_dm_new = -K_d * predicted.P_mm + predicted.P_dm
        P_dd_new = -K_d * predicted.P_md + predicted.P_dd
        return replace(predicted,
            mu=mu_new, d=d_new,
            P_mm=P_mm_new, P_md=P_md_new, P_dm=P_dm_new, P_dd=P_dd_new,
        )

    def _reestimate_R(self, state: KalmanState, predicted: KalmanState, observed_time: float) -> KalmanState:
        innovation_sq = (observed_time - predicted.mu) ** 2
        R_est = innovation_sq - predicted.P_mm
        R_new = max(R_est, R_FLOOR)
        R_blended = 0.7 * state.R + 0.3 * R_new
        return replace(state, R=max(R_blended, R_FLOOR))

    def init_state(self, first_attempt: AttemptRecord, priors: dict) -> KalmanState:
        first_time = first_attempt.time_ms / 1000.0
        d = priors.get("d", DEFAULT_D)
        R = priors.get("R", DEFAULT_R)
        Q_mm = priors.get("Q_mm", DEFAULT_Q_MM)
        Q_md = priors.get("Q_md", DEFAULT_Q_MD)
        Q_dd = priors.get("Q_dd", DEFAULT_Q_DD)
        return KalmanState(
            mu=first_time, d=d,
            P_mm=R, P_md=0.0, P_dm=0.0, P_dd=DEFAULT_P_D0,
            R=R, Q_mm=Q_mm, Q_md=Q_md, Q_dm=Q_md, Q_dd=Q_dd,
            gold=first_time, n_completed=1, n_attempts=1,
        )

    def process_attempt(
        self, state: KalmanState, new_attempt: AttemptRecord,
        all_attempts: list[AttemptRecord],
    ) -> KalmanState:
        observed_time = (
            new_attempt.time_ms / 1000.0
            if new_attempt.completed and new_attempt.time_ms is not None
            else None
        )
        if observed_time is None:
            return replace(state, n_attempts=state.n_attempts + 1)

        predicted = self._predict(state)
        updated = self._update(predicted, observed_time)
        n_completed = state.n_completed + 1
        gold = min(state.gold, observed_time)

        result = replace(updated,
            Q_mm=state.Q_mm, Q_md=state.Q_md, Q_dm=state.Q_dm, Q_dd=state.Q_dd,
            gold=gold, n_completed=n_completed, n_attempts=state.n_attempts + 1,
        )
        if n_completed >= R_REESTIMATE_INTERVAL and n_completed % R_REESTIMATE_INTERVAL == 0:
            result = self._reestimate_R(result, predicted, observed_time)
        return result

    def model_output(self, state: KalmanState, all_attempts: list[AttemptRecord]) -> ModelOutput:
        gold_ms = state.gold * 1000 if state.gold != float("inf") else state.mu * 1000
        return ModelOutput(
            expected_time_ms=state.mu * 1000,
            clean_expected_ms=state.mu * 1000,
            ms_per_attempt=-state.d * 1000,
            floor_estimate_ms=gold_ms,
            clean_floor_estimate_ms=gold_ms,
        )

    def drift_info(self, state: KalmanState) -> dict:
        import math
        p_dd_sqrt = math.sqrt(max(state.P_dd, 0.0))
        ci_lower = state.d - 1.96 * p_dd_sqrt
        ci_upper = state.d + 1.96 * p_dd_sqrt
        if state.d < 0:
            label = "improving"
        elif state.d > 0:
            label = "regressing"
        else:
            label = "flat"
        if ci_lower > 0 or ci_upper < 0:
            confidence = "confident"
        elif p_dd_sqrt < 0.5:
            confidence = "moderate"
        else:
            confidence = "uncertain"
        return {
            "drift": state.d, "ci_lower": ci_lower, "ci_upper": ci_upper,
            "label": label, "confidence": confidence,
        }

    def get_population_priors(self, all_states: list[KalmanState]) -> dict:
        mature = [s for s in all_states if s.n_completed >= R_REESTIMATE_INTERVAL]
        if not mature:
            return {"d": DEFAULT_D, "R": DEFAULT_R, "Q_mm": DEFAULT_Q_MM, "Q_dd": DEFAULT_Q_DD}
        n = len(mature)
        return {
            "d": sum(s.d for s in mature) / n,
            "R": sum(s.R for s in mature) / n,
            "Q_mm": sum(s.Q_mm for s in mature) / n,
            "Q_dd": sum(s.Q_dd for s in mature) / n,
        }

    def rebuild_state(self, attempts: list[AttemptRecord]) -> KalmanState:
        completed = [a for a in attempts if a.completed and a.time_ms is not None]
        if not completed:
            return KalmanState(n_attempts=len(attempts))
        first = completed[0]
        state = self.init_state(first, priors={})
        first_idx = attempts.index(first)
        for a in attempts[:first_idx]:
            state = self.process_attempt(state, a, attempts)
        for a in attempts[first_idx + 1:]:
            state = self.process_attempt(state, a, attempts)
        return state
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_kalman.py -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/estimators/kalman.py tests/test_kalman.py
git commit -m "feat: adapt Kalman estimator to new ABC with ModelOutput"
```

---

## Task 4: Implement Model A (Rolling Statistics)

**Files:**
- Create: `python/spinlab/estimators/model_a.py`
- Create: `tests/test_model_a.py`

- [ ] **Step 1: Write the failing tests for Model A**

```python
# tests/test_model_a.py
"""Tests for Model A (rolling statistics estimator)."""
import pytest
from spinlab.estimators.model_a import ModelAEstimator, ModelAState
from spinlab.models import AttemptRecord, ModelOutput


def _attempt(time_ms: int, deaths: int = 0, clean_tail_ms: int | None = None) -> AttemptRecord:
    if clean_tail_ms is None:
        clean_tail_ms = time_ms
    return AttemptRecord(
        time_ms=time_ms, completed=True, deaths=deaths,
        clean_tail_ms=clean_tail_ms, created_at="2026-01-01T00:00:00",
    )


def _incomplete() -> AttemptRecord:
    return AttemptRecord(
        time_ms=None, completed=False, deaths=0,
        clean_tail_ms=None, created_at="2026-01-01T00:00:00",
    )


class TestModelAProcessAttempt:
    def test_init_from_first_attempt(self):
        est = ModelAEstimator()
        state = est.init_state(_attempt(12000), priors={})
        assert state.n_completed == 1
        assert state.n_attempts == 1

    def test_process_multiple_attempts(self):
        est = ModelAEstimator()
        attempts = [_attempt(t) for t in [12000, 11500, 11000, 10500, 10000]]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        assert state.n_completed == 5
        assert state.n_attempts == 5

    def test_incomplete_increments_attempts_only(self):
        est = ModelAEstimator()
        state = est.init_state(_attempt(12000), priors={})
        state = est.process_attempt(state, _incomplete(), [_attempt(12000), _incomplete()])
        assert state.n_completed == 1
        assert state.n_attempts == 2


class TestModelAModelOutput:
    def test_constant_times_zero_trend(self):
        est = ModelAEstimator()
        attempts = [_attempt(10000) for _ in range(10)]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.ms_per_attempt == pytest.approx(0.0)

    def test_strictly_decreasing_positive_trend(self):
        est = ModelAEstimator()
        times = [15000, 14000, 13000, 12000, 11000, 10000, 9000, 8000, 7000, 6000]
        attempts = [_attempt(t) for t in times]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.ms_per_attempt > 0

    def test_strictly_increasing_negative_trend(self):
        est = ModelAEstimator()
        times = [6000, 7000, 8000, 9000, 10000, 11000, 12000, 13000, 14000, 15000]
        attempts = [_attempt(t) for t in times]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.ms_per_attempt < 0

    def test_single_attempt_zero_trend(self):
        est = ModelAEstimator()
        a1 = _attempt(12000)
        state = est.init_state(a1, priors={})
        out = est.model_output(state, [a1])
        assert out.expected_time_ms == pytest.approx(12000.0)
        assert out.ms_per_attempt == pytest.approx(0.0)

    def test_floor_is_min_observed(self):
        est = ModelAEstimator()
        attempts = [_attempt(t) for t in [15000, 12000, 10000, 11000, 13000]]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.floor_estimate_ms == pytest.approx(10000.0)
        assert out.clean_floor_estimate_ms == pytest.approx(10000.0)

    def test_dirty_attempts_separate_clean_and_total(self):
        """clean_expected and expected_time differ when deaths are present."""
        est = ModelAEstimator()
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
        assert out.clean_expected_ms != out.expected_time_ms
        assert out.clean_floor_estimate_ms == pytest.approx(7000.0)
        assert out.floor_estimate_ms == pytest.approx(14000.0)


class TestModelARebuild:
    def test_rebuild_from_attempts(self):
        est = ModelAEstimator()
        attempts = [_attempt(12000), _incomplete(), _attempt(11000)]
        state = est.rebuild_state(attempts)
        assert state.n_completed == 2
        assert state.n_attempts == 3

    def test_rebuild_empty(self):
        est = ModelAEstimator()
        state = est.rebuild_state([])
        assert state.n_completed == 0
        assert state.n_attempts == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_model_a.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'spinlab.estimators.model_a'`

- [ ] **Step 3: Implement Model A**

```python
# python/spinlab/estimators/model_a.py
"""Model A: Rolling statistics estimator (model-free)."""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from spinlab.estimators import Estimator, EstimatorState, register_estimator
from spinlab.models import AttemptRecord, ModelOutput


@dataclass
class ModelAState(EstimatorState):
    """Minimal bookkeeping. Model A recomputes from all_attempts each time."""

    n_completed: int = 0
    n_attempts: int = 0

    def to_dict(self) -> dict:
        return {"n_completed": self.n_completed, "n_attempts": self.n_attempts}

    @classmethod
    def from_dict(cls, d: dict) -> "ModelAState":
        return cls(n_completed=d.get("n_completed", 0), n_attempts=d.get("n_attempts", 0))


@register_estimator
class ModelAEstimator(Estimator):
    name = "model_a"

    def init_state(self, first_attempt: AttemptRecord, priors: dict) -> ModelAState:
        return ModelAState(n_completed=1, n_attempts=1)

    def process_attempt(
        self, state: ModelAState, new_attempt: AttemptRecord,
        all_attempts: list[AttemptRecord],
    ) -> ModelAState:
        n_completed = state.n_completed + (1 if new_attempt.completed else 0)
        return ModelAState(n_completed=n_completed, n_attempts=state.n_attempts + 1)

    def model_output(self, state: ModelAState, all_attempts: list[AttemptRecord]) -> ModelOutput:
        completed = [a for a in all_attempts if a.completed and a.time_ms is not None]
        if not completed:
            return ModelOutput(0.0, 0.0, 0.0, 0.0, 0.0)

        total_times = [a.time_ms for a in completed]
        clean_tails = [a.clean_tail_ms for a in completed]
        n = len(completed)

        # Window sizes
        n_recent = min(max(3, int(n * 0.2)), n)
        n_broad = min(max(5, int(n * 0.5)), n)

        recent_total = total_times[-n_recent:]
        recent_clean = clean_tails[-n_recent:]
        recent_median_total = statistics.median(recent_total)
        recent_median_clean = statistics.median(recent_clean)

        # Trend from clean tails
        if n_broad > n_recent:
            broad_clean = clean_tails[-n_broad:]
            broad_median_clean = statistics.median(broad_clean)
            attempt_gap = (n_broad - n_recent) / 2.0
            trend = (recent_median_clean - broad_median_clean) / attempt_gap
            ms_per_attempt = -trend
        else:
            ms_per_attempt = 0.0

        return ModelOutput(
            expected_time_ms=recent_median_total,
            clean_expected_ms=recent_median_clean,
            ms_per_attempt=ms_per_attempt,
            floor_estimate_ms=float(min(total_times)),
            clean_floor_estimate_ms=float(min(clean_tails)),
        )

    def rebuild_state(self, attempts: list[AttemptRecord]) -> ModelAState:
        n_completed = sum(1 for a in attempts if a.completed)
        return ModelAState(n_completed=n_completed, n_attempts=len(attempts))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_model_a.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/estimators/model_a.py tests/test_model_a.py
git commit -m "feat: add Model A (rolling statistics) estimator"
```

---

## Task 5: Implement Model B (Exponential Decay)

**Files:**
- Create: `python/spinlab/estimators/model_b.py`
- Create: `tests/test_model_b.py`

- [ ] **Step 1: Write the failing tests for Model B**

```python
# tests/test_model_b.py
"""Tests for Model B (exponential decay estimator)."""
import math
import pytest
from spinlab.estimators.model_b import ModelBEstimator, ModelBState
from spinlab.models import AttemptRecord, ModelOutput


def _attempt(time_ms: int, deaths: int = 0, clean_tail_ms: int | None = None) -> AttemptRecord:
    if clean_tail_ms is None:
        clean_tail_ms = time_ms
    return AttemptRecord(
        time_ms=time_ms, completed=True, deaths=deaths,
        clean_tail_ms=clean_tail_ms, created_at="2026-01-01T00:00:00",
    )


def _incomplete() -> AttemptRecord:
    return AttemptRecord(
        time_ms=None, completed=False, deaths=0,
        clean_tail_ms=None, created_at="2026-01-01T00:00:00",
    )


def _synthetic_exp_attempts(
    n: int = 25, amplitude: float = 12000.0, decay_rate: float = 0.1,
    asymptote: float = 3000.0,
) -> list[AttemptRecord]:
    """Generate n attempts following exact a*exp(-b*n)+c (no noise)."""
    return [
        _attempt(int(amplitude * math.exp(-decay_rate * i) + asymptote))
        for i in range(n)
    ]


class TestModelBProcessAttempt:
    def test_init_from_first_attempt(self):
        est = ModelBEstimator()
        state = est.init_state(_attempt(12000), priors={})
        assert state.n_completed == 1
        assert state.n_attempts == 1

    def test_process_tracks_counts(self):
        est = ModelBEstimator()
        attempts = _synthetic_exp_attempts(5)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        assert state.n_completed == 5
        assert state.n_attempts == 5

    def test_incomplete_increments_attempts_only(self):
        est = ModelBEstimator()
        state = est.init_state(_attempt(12000), priors={})
        state = est.process_attempt(state, _incomplete(), [_attempt(12000), _incomplete()])
        assert state.n_completed == 1
        assert state.n_attempts == 2


class TestModelBFit:
    def test_recovers_known_asymptote(self):
        """Fit on exact exponential data should recover the asymptote."""
        est = ModelBEstimator()
        attempts = _synthetic_exp_attempts(25, amplitude=12000, decay_rate=0.1, asymptote=3000)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        # asymptote should be close to 3000
        assert state.asymptote == pytest.approx(3000, rel=0.05)

    def test_recovers_known_decay_rate(self):
        est = ModelBEstimator()
        attempts = _synthetic_exp_attempts(25, amplitude=12000, decay_rate=0.1, asymptote=3000)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        assert state.decay_rate == pytest.approx(0.1, rel=0.1)


class TestModelBModelOutput:
    def test_output_with_enough_data(self):
        est = ModelBEstimator()
        attempts = _synthetic_exp_attempts(25)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert isinstance(out, ModelOutput)
        assert out.ms_per_attempt > 0
        assert out.floor_estimate_ms > 0
        assert out.clean_floor_estimate_ms > 0
        assert out.clean_floor_estimate_ms < out.expected_time_ms

    def test_ms_per_attempt_matches_derivative(self):
        """ms_per_attempt should approximate a*b*exp(-b*(n-1)) for synthetic data."""
        est = ModelBEstimator()
        a, b, c = 12000.0, 0.1, 3000.0
        attempts = _synthetic_exp_attempts(25, amplitude=a, decay_rate=b, asymptote=c)
        state = est.init_state(attempts[0], priors={})
        for att in attempts[1:]:
            state = est.process_attempt(state, att, attempts)
        out = est.model_output(state, attempts)
        expected_deriv = a * b * math.exp(-b * 24)  # derivative at n=24
        assert out.ms_per_attempt == pytest.approx(expected_deriv, rel=0.15)

    def test_floor_never_negative(self):
        est = ModelBEstimator()
        attempts = _synthetic_exp_attempts(25, asymptote=100)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.floor_estimate_ms >= 0
        assert out.clean_floor_estimate_ms >= 0

    def test_fallback_with_few_points(self):
        """With <3 completed, falls back to simple stats."""
        est = ModelBEstimator()
        attempts = [_attempt(12000), _attempt(11500)]
        state = est.init_state(attempts[0], priors={})
        state = est.process_attempt(state, attempts[1], attempts)
        out = est.model_output(state, attempts)
        assert isinstance(out, ModelOutput)
        assert out.floor_estimate_ms > 0

    def test_two_fits_total_and_clean(self):
        """With dirty attempts, total and clean floors should differ."""
        est = ModelBEstimator()
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
        assert out.floor_estimate_ms > out.clean_floor_estimate_ms


class TestModelBRebuild:
    def test_rebuild_from_attempts(self):
        est = ModelBEstimator()
        attempts = [_attempt(12000), _incomplete(), _attempt(11000)]
        state = est.rebuild_state(attempts)
        assert state.n_completed == 2
        assert state.n_attempts == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_model_b.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'spinlab.estimators.model_b'`

- [ ] **Step 3: Implement Model B**

```python
# python/spinlab/estimators/model_b.py
"""Model B: Exponential decay estimator.

Fits time(n) = amplitude * exp(-decay_rate * n) + asymptote
via scipy.optimize.curve_fit. Two fits: one on total times, one on clean tails.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

import numpy as np
from scipy.optimize import curve_fit

from spinlab.estimators import Estimator, EstimatorState, register_estimator
from spinlab.models import AttemptRecord, ModelOutput

MIN_POINTS_FOR_FIT = 3


def _exp_decay(n: np.ndarray, amplitude: float, decay_rate: float, asymptote: float) -> np.ndarray:
    return amplitude * np.exp(-decay_rate * n) + asymptote


def _fit_exp_decay(ns: np.ndarray, ts: np.ndarray) -> tuple[float, float, float, float]:
    """Fit amplitude*exp(-decay_rate*n)+asymptote. Returns (amplitude, decay_rate, asymptote, sigma)."""
    c0 = float(np.min(ts)) * 0.95
    a0 = max(float(np.median(ts[:5])) - c0, 100.0)
    b0 = 0.03
    try:
        popt, _ = curve_fit(
            _exp_decay, ns, ts,
            p0=[a0, b0, c0],
            bounds=([0, 1e-6, 0], [np.inf, 2.0, float(np.min(ts))]),
            maxfev=5000,
        )
        amplitude, decay_rate, asymptote = popt
        residuals = ts - _exp_decay(ns, amplitude, decay_rate, asymptote)
        sigma = float(np.std(residuals))
        return float(amplitude), float(decay_rate), float(asymptote), sigma
    except RuntimeError:
        asymptote = float(np.min(ts)) * 0.95
        amplitude = max(float(np.median(ts[:5])) - asymptote, 100.0)
        decay_rate = 0.01
        sigma = float(np.std(ts))
        return amplitude, decay_rate, asymptote, sigma


@dataclass
class ModelBState(EstimatorState):
    """Bookkeeping + cached fit params. Model B recomputes curve_fit each time."""

    n_completed: int = 0
    n_attempts: int = 0
    # Cached fit params for clean tail fit
    amplitude: float = 0.0
    decay_rate: float = 0.0
    asymptote: float = 0.0
    sigma: float = 0.0
    # Cached fit params for total time fit
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
    def from_dict(cls, d: dict) -> "ModelBState":
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
class ModelBEstimator(Estimator):
    name = "model_b"

    def _run_fits(self, completed: list[AttemptRecord]) -> ModelBState:
        """Run both fits (clean tails and total times) and return updated state."""
        state = ModelBState(n_completed=len(completed), n_attempts=len(completed))
        if len(completed) < MIN_POINTS_FOR_FIT:
            return state

        ns = np.arange(len(completed), dtype=float)

        # Fit on clean tails
        clean_ts = np.array([a.clean_tail_ms for a in completed], dtype=float)
        a, b, c, sigma = _fit_exp_decay(ns, clean_ts)
        state.amplitude = a
        state.decay_rate = b
        state.asymptote = c
        state.sigma = sigma

        # Fit on total times
        total_ts = np.array([a.time_ms for a in completed], dtype=float)
        ta, tb, tc, tsigma = _fit_exp_decay(ns, total_ts)
        state.total_amplitude = ta
        state.total_decay_rate = tb
        state.total_asymptote = tc
        state.total_sigma = tsigma

        return state

    def init_state(self, first_attempt: AttemptRecord, priors: dict) -> ModelBState:
        return ModelBState(n_completed=1, n_attempts=1)

    def process_attempt(
        self, state: ModelBState, new_attempt: AttemptRecord,
        all_attempts: list[AttemptRecord],
    ) -> ModelBState:
        n_completed = state.n_completed + (1 if new_attempt.completed else 0)
        completed = [a for a in all_attempts if a.completed and a.time_ms is not None]
        new_state = self._run_fits(completed)
        new_state.n_completed = n_completed
        new_state.n_attempts = state.n_attempts + 1
        return new_state

    def model_output(self, state: ModelBState, all_attempts: list[AttemptRecord]) -> ModelOutput:
        completed = [a for a in all_attempts if a.completed and a.time_ms is not None]
        if not completed:
            return ModelOutput(0.0, 0.0, 0.0, 0.0, 0.0)

        total_times = [a.time_ms for a in completed]
        clean_tails = [a.clean_tail_ms for a in completed]
        n = len(completed)

        # Not enough data for a fit — fall back to simple stats
        if n < MIN_POINTS_FOR_FIT:
            med_total = float(statistics.median(total_times))
            med_clean = float(statistics.median(clean_tails))
            return ModelOutput(
                expected_time_ms=med_total,
                clean_expected_ms=med_clean,
                ms_per_attempt=0.0,
                floor_estimate_ms=float(min(total_times)),
                clean_floor_estimate_ms=float(min(clean_tails)),
            )

        current_n = float(n - 1)

        # Clean tail fit outputs
        clean_expected = float(state.amplitude * np.exp(-state.decay_rate * current_n) + state.asymptote)
        clean_ms_per_attempt = float(state.amplitude * state.decay_rate * np.exp(-state.decay_rate * current_n))

        # Total time: use recent median for expected (not the curve, which may not capture deaths well)
        n_recent = min(max(3, int(n * 0.2)), n)
        expected_total = float(statistics.median(total_times[-n_recent:]))

        return ModelOutput(
            expected_time_ms=expected_total,
            clean_expected_ms=clean_expected,
            ms_per_attempt=clean_ms_per_attempt,
            floor_estimate_ms=max(0.0, state.total_asymptote),
            clean_floor_estimate_ms=max(0.0, state.asymptote),
        )

    def rebuild_state(self, attempts: list[AttemptRecord]) -> ModelBState:
        completed = [a for a in attempts if a.completed and a.time_ms is not None]
        state = self._run_fits(completed)
        state.n_completed = len(completed)
        state.n_attempts = len(attempts)
        return state
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_model_b.py -v`
Expected: All 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/estimators/model_b.py tests/test_model_b.py
git commit -m "feat: add Model B (exponential decay) estimator"
```

---

## Task 6: Update DB schema and queries for multi-model + death tracking

**Files:**
- Modify: `python/spinlab/db.py`
- Modify: `python/spinlab/models.py` (Attempt dataclass)

- [ ] **Step 1: Write the failing test for new DB operations**

Append to `tests/test_model_output.py`:

```python
import json
from spinlab.db import Database
from spinlab.models import Attempt, AttemptRecord, ModelOutput, Segment


class TestDBMultiModel:
    def _setup_db(self):
        db = Database(":memory:")
        db.upsert_game("g1", "Game", "any%")
        seg = Segment(
            id="s1", game_id="g1", level_number=1,
            start_type="entrance", start_ordinal=0,
            end_type="checkpoint", end_ordinal=0,
        )
        db.upsert_segment(seg)
        return db

    def test_save_and_load_multi_model_state(self):
        db = self._setup_db()
        out_k = ModelOutput(12000.0, 12000.0, 500.0, 10000.0, 10000.0)
        out_a = ModelOutput(12500.0, 12500.0, 300.0, 11000.0, 11000.0)
        db.save_model_state("s1", "kalman", '{"mu": 12.0}', json.dumps(out_k.to_dict()))
        db.save_model_state("s1", "model_a", '{"n_completed": 5}', json.dumps(out_a.to_dict()))
        rows = db.load_all_model_states_for_segment("s1")
        assert len(rows) == 2
        names = {r["estimator"] for r in rows}
        assert names == {"kalman", "model_a"}

    def test_load_model_state_by_estimator(self):
        db = self._setup_db()
        out = ModelOutput(12000.0, 12000.0, 500.0, 10000.0, 10000.0)
        db.save_model_state("s1", "kalman", '{"mu": 12.0}', json.dumps(out.to_dict()))
        row = db.load_model_state("s1", "kalman")
        assert row is not None
        assert row["estimator"] == "kalman"
        loaded_out = ModelOutput.from_dict(json.loads(row["output_json"]))
        assert loaded_out.expected_time_ms == 12000.0

    def test_attempt_with_deaths_and_clean_tail(self):
        db = self._setup_db()
        db.create_session("sess1", "g1")
        attempt = Attempt(
            segment_id="s1", session_id="sess1", completed=True,
            time_ms=12000, deaths=3, clean_tail_ms=4000,
        )
        db.log_attempt(attempt)
        rows = db.get_segment_attempts("s1")
        assert len(rows) == 1
        assert rows[0]["deaths"] == 3
        assert rows[0]["clean_tail_ms"] == 4000

    def test_attempt_defaults_zero_deaths(self):
        db = self._setup_db()
        db.create_session("sess1", "g1")
        attempt = Attempt(
            segment_id="s1", session_id="sess1", completed=True,
            time_ms=12000,
        )
        db.log_attempt(attempt)
        rows = db.get_segment_attempts("s1")
        assert rows[0]["deaths"] == 0
        assert rows[0]["clean_tail_ms"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_model_output.py::TestDBMultiModel -v`
Expected: FAIL — signature/schema mismatches

- [ ] **Step 3: Update Attempt dataclass in models.py**

In `python/spinlab/models.py`, add `deaths` and `clean_tail_ms` fields to `Attempt`:

Find:
```python
    strat_version: int = 1
    source: str = "practice"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
```

Replace with:
```python
    strat_version: int = 1
    source: str = "practice"
    deaths: int = 0
    clean_tail_ms: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
```

- [ ] **Step 4: Update DB schema and methods in db.py**

**4a.** In the attempts table CREATE statement, add after `source TEXT DEFAULT 'practice',`:
```sql
  deaths INTEGER DEFAULT 0,
  clean_tail_ms INTEGER,
```

**4b.** Replace the model_state CREATE statement:
```sql
CREATE TABLE IF NOT EXISTS model_state (
  segment_id TEXT NOT NULL REFERENCES segments(id),
  estimator TEXT NOT NULL,
  state_json TEXT NOT NULL,
  output_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (segment_id, estimator)
);
```

**4c.** Update `log_attempt()` — add deaths and clean_tail_ms to INSERT:
```python
    def log_attempt(self, attempt: Attempt) -> None:
        self.conn.execute(
            """INSERT INTO attempts
               (segment_id, session_id, completed, time_ms, goal_matched,
                rating, strat_version, source, deaths, clean_tail_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (attempt.segment_id, attempt.session_id, int(attempt.completed),
             attempt.time_ms, attempt.goal_matched,
             attempt.rating,
             attempt.strat_version, attempt.source,
             attempt.deaths, attempt.clean_tail_ms,
             attempt.created_at.isoformat()),
        )
        self.conn.commit()
```

**4d.** Replace `save_model_state()`:
```python
    def save_model_state(
        self, segment_id: str, estimator: str, state_json: str, output_json: str
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """INSERT INTO model_state (segment_id, estimator, state_json, output_json, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(segment_id, estimator) DO UPDATE SET
                 state_json=excluded.state_json,
                 output_json=excluded.output_json,
                 updated_at=excluded.updated_at""",
            (segment_id, estimator, state_json, output_json, now),
        )
        self.conn.commit()
```

**4e.** Replace `load_model_state()`:
```python
    def load_model_state(self, segment_id: str, estimator: str | None = None) -> dict | None:
        if estimator:
            cur = self.conn.execute(
                "SELECT segment_id, estimator, state_json, output_json, updated_at "
                "FROM model_state WHERE segment_id = ? AND estimator = ?",
                (segment_id, estimator),
            )
        else:
            cur = self.conn.execute(
                "SELECT segment_id, estimator, state_json, output_json, updated_at "
                "FROM model_state WHERE segment_id = ? LIMIT 1",
                (segment_id,),
            )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "segment_id": row[0], "estimator": row[1], "state_json": row[2],
            "output_json": row[3], "updated_at": row[4],
        }
```

**4f.** Add `load_all_model_states_for_segment()` after `load_model_state`:
```python
    def load_all_model_states_for_segment(self, segment_id: str) -> list[dict]:
        """Load all estimator states for a single segment."""
        cur = self.conn.execute(
            "SELECT segment_id, estimator, state_json, output_json, updated_at "
            "FROM model_state WHERE segment_id = ?",
            (segment_id,),
        )
        cols = ["segment_id", "estimator", "state_json", "output_json", "updated_at"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
```

**4g.** Replace `load_all_model_states()`:
```python
    def load_all_model_states(self, game_id: str) -> list[dict]:
        cur = self.conn.execute(
            """SELECT m.segment_id, m.estimator, m.state_json, m.output_json, m.updated_at
               FROM model_state m
               JOIN segments s ON m.segment_id = s.id
               WHERE s.game_id = ? AND s.active = 1""",
            (game_id,),
        )
        cols = ["segment_id", "estimator", "state_json", "output_json", "updated_at"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
```

**4h.** Replace `get_segment_attempts()`:
```python
    def get_segment_attempts(self, segment_id: str) -> list[dict]:
        """Get all attempts for a segment, ordered by created_at."""
        cur = self.conn.execute(
            "SELECT segment_id, completed, time_ms, deaths, clean_tail_ms, created_at "
            "FROM attempts WHERE segment_id = ? ORDER BY created_at",
            (segment_id,),
        )
        cols = ["segment_id", "completed", "time_ms", "deaths", "clean_tail_ms", "created_at"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
```

**4i.** Replace `get_all_segments_with_model()` — remove the LEFT JOIN on model_state:
```python
    def get_all_segments_with_model(self, game_id: str) -> list[dict]:
        """Get all active segments with default variant state_path."""
        cur = self.conn.execute(
            """SELECT s.id, s.game_id, s.level_number, s.start_type, s.start_ordinal,
                      s.end_type, s.end_ordinal, s.description, s.strat_version,
                      s.active, s.ordinal,
                      (SELECT sv.state_path FROM segment_variants sv
                       WHERE sv.segment_id = s.id
                       ORDER BY sv.is_default DESC LIMIT 1) AS state_path
               FROM segments s
               WHERE s.game_id = ? AND s.active = 1
               ORDER BY s.ordinal, s.level_number""",
            (game_id,),
        )
        actual_cols = [desc[0] for desc in cur.description]
        return [dict(zip(actual_cols, row)) for row in cur.fetchall()]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_model_output.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/db.py python/spinlab/models.py tests/test_model_output.py
git commit -m "feat: update DB schema for multi-model PK and death tracking"
```

---

## Task 7: Update SegmentWithModel and allocators

**Files:**
- Modify: `python/spinlab/allocators/__init__.py`
- Modify: `python/spinlab/allocators/greedy.py`
- Modify: `tests/test_allocators.py`

- [ ] **Step 1: Update SegmentWithModel dataclass**

In `python/spinlab/allocators/__init__.py`, replace the import and dataclass:

Replace:
```python
from spinlab.estimators import EstimatorState
```
With:
```python
from spinlab.models import ModelOutput
```

Replace the `SegmentWithModel` class:
```python
@dataclass
class SegmentWithModel:
    """Segment metadata combined with all estimator outputs."""

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
    # Multi-model output
    model_outputs: dict[str, ModelOutput] = field(default_factory=dict)
    selected_model: str = "kalman"
    n_completed: int = 0
    n_attempts: int = 0
    gold_ms: int | None = None
    clean_gold_ms: int | None = None
```

- [ ] **Step 2: Update GreedyAllocator**

Replace `python/spinlab/allocators/greedy.py`:

```python
"""Greedy allocator: picks segment with highest ms_per_attempt from selected model."""
from __future__ import annotations

import random

from spinlab.allocators import Allocator, SegmentWithModel, register_allocator


def _score(s: SegmentWithModel) -> float:
    out = s.model_outputs.get(s.selected_model)
    if out is None:
        return 0.0
    return out.ms_per_attempt


@register_allocator
class GreedyAllocator(Allocator):
    name = "greedy"

    def pick_next(self, segment_states: list[SegmentWithModel]) -> str | None:
        if not segment_states:
            return None
        best = max(_score(s) for s in segment_states)
        tied = [s for s in segment_states if _score(s) == best]
        return random.choice(tied).segment_id

    def peek_next_n(self, segment_states: list[SegmentWithModel], n: int) -> list[str]:
        shuffled = list(segment_states)
        random.shuffle(shuffled)
        shuffled.sort(key=_score, reverse=True)
        return [s.segment_id for s in shuffled[:n]]
```

- [ ] **Step 3: Update test_allocators.py**

Replace the `_make_segment` helper:
```python
from spinlab.models import ModelOutput

def _make_segment(segment_id: str, ms_per_attempt: float = 0.0) -> SegmentWithModel:
    out = ModelOutput(
        expected_time_ms=10000.0, clean_expected_ms=10000.0,
        ms_per_attempt=ms_per_attempt,
        floor_estimate_ms=8000.0, clean_floor_estimate_ms=8000.0,
    )
    return SegmentWithModel(
        segment_id=segment_id, game_id="test", level_number=1,
        start_type="level_enter", start_ordinal=0,
        end_type="level_exit", end_ordinal=0,
        description="test", strat_version=1, state_path=None, active=True,
        model_outputs={"kalman": out}, selected_model="kalman",
    )
```

Replace greedy tests:
```python
class TestGreedyAllocator:
    def test_picks_highest_ms_per_attempt(self):
        alloc = GreedyAllocator()
        segments = [_make_segment("a", 50.0), _make_segment("b", 100.0), _make_segment("c", 20.0)]
        assert alloc.pick_next(segments) == "b"

    def test_peek_returns_sorted_order(self):
        alloc = GreedyAllocator()
        segments = [_make_segment("a", 50.0), _make_segment("b", 100.0), _make_segment("c", 20.0)]
        result = alloc.peek_next_n(segments, 2)
        assert result == ["b", "a"]
```

Replace `_make_segment_with_ordinal`:
```python
def _make_segment_with_ordinal(segment_id: str, ordinal: int) -> SegmentWithModel:
    return SegmentWithModel(
        segment_id=segment_id, game_id="test", level_number=ordinal * 10,
        start_type="level_enter", start_ordinal=ordinal,
        end_type="level_exit", end_ordinal=ordinal,
        description=f"Segment {segment_id}", strat_version=1,
        state_path=None, active=True,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_allocators.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/allocators/__init__.py python/spinlab/allocators/greedy.py tests/test_allocators.py
git commit -m "feat: update SegmentWithModel and greedy allocator for multi-model"
```

---

## Task 8: Update Scheduler to run all estimators

**Files:**
- Modify: `python/spinlab/scheduler.py`
- Modify: `tests/test_scheduler_kalman.py`

- [ ] **Step 1: Rewrite scheduler.py**

Replace `python/spinlab/scheduler.py` entirely:

```python
"""Scheduler coordinator: wires estimators + allocator together.

Runs ALL registered estimators on each attempt. The "active" estimator
selection only affects which ModelOutput the allocator reads.
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from spinlab.allocators import SegmentWithModel, get_allocator, list_allocators
from spinlab.allocators.greedy import GreedyAllocator  # ensure registered
from spinlab.allocators.random import RandomAllocator
from spinlab.allocators.round_robin import RoundRobinAllocator
from spinlab.estimators import EstimatorState, get_estimator, list_estimators
from spinlab.estimators.kalman import KalmanEstimator, KalmanState  # ensure registered
from spinlab.estimators.model_a import ModelAEstimator, ModelAState  # ensure registered
from spinlab.estimators.model_b import ModelBEstimator, ModelBState  # ensure registered
from spinlab.models import AttemptRecord, ModelOutput

if TYPE_CHECKING:
    from spinlab.allocators import Allocator
    from spinlab.db import Database
    from spinlab.estimators import Estimator

# Maps estimator name -> state class for deserialization
_STATE_CLASSES: dict[str, type[EstimatorState]] = {
    "kalman": KalmanState,
    "model_a": ModelAState,
    "model_b": ModelBState,
}


def _attempts_from_rows(rows: list[dict]) -> list[AttemptRecord]:
    return [
        AttemptRecord(
            time_ms=r["time_ms"],
            completed=bool(r["completed"]),
            deaths=r.get("deaths", 0) or 0,
            clean_tail_ms=r.get("clean_tail_ms"),
            created_at=r["created_at"],
        )
        for r in rows
    ]


class Scheduler:
    def __init__(
        self, db: "Database", game_id: str,
        estimator_name: str = "kalman", allocator_name: str = "greedy",
    ) -> None:
        self.db = db
        self.game_id = game_id
        saved_alloc = db.load_allocator_config("allocator")
        saved_est = db.load_allocator_config("estimator")
        self.estimator: Estimator = get_estimator(saved_est or estimator_name)
        self.allocator: Allocator = get_allocator(saved_alloc or allocator_name)

    def _sync_config_from_db(self) -> None:
        saved_alloc = self.db.load_allocator_config("allocator")
        if saved_alloc and saved_alloc != self.allocator.name:
            self.allocator = get_allocator(saved_alloc)
        saved_est = self.db.load_allocator_config("estimator")
        if saved_est and saved_est != self.estimator.name:
            self.estimator = get_estimator(saved_est)

    def _all_estimators(self) -> list[Estimator]:
        return [get_estimator(name) for name in list_estimators()]

    def _deserialize_state(self, estimator_name: str, state_json: str) -> EstimatorState:
        d = json.loads(state_json)
        cls = _STATE_CLASSES.get(estimator_name)
        if cls is None:
            raise ValueError(f"No state class for estimator: {estimator_name}")
        return cls.from_dict(d)

    def _load_segments_with_model(self) -> list[SegmentWithModel]:
        rows = self.db.get_all_segments_with_model(self.game_id)
        segments = []

        for row in rows:
            segment_id = row["id"]
            model_outputs: dict[str, ModelOutput] = {}
            n_completed = 0
            n_attempts = 0
            gold_ms = None
            clean_gold_ms = None

            state_rows = self.db.load_all_model_states_for_segment(segment_id)
            for sr in state_rows:
                if sr["output_json"]:
                    try:
                        out = ModelOutput.from_dict(json.loads(sr["output_json"]))
                        model_outputs[sr["estimator"]] = out
                    except (json.JSONDecodeError, KeyError):
                        pass
                if sr["state_json"]:
                    try:
                        sd = json.loads(sr["state_json"])
                        nc = sd.get("n_completed", 0)
                        na = sd.get("n_attempts", 0)
                        if nc > n_completed:
                            n_completed = nc
                            n_attempts = na
                        g = sd.get("gold")
                        if g is not None and g != float("inf"):
                            g_ms = int(g * 1000) if g < 1000 else int(g)
                            if gold_ms is None or g_ms < gold_ms:
                                gold_ms = g_ms
                    except (json.JSONDecodeError, KeyError):
                        pass

            # Compute clean_gold from attempt history
            attempt_rows = self.db.get_segment_attempts(segment_id)
            for ar in attempt_rows:
                if ar["completed"] and ar.get("clean_tail_ms") is not None:
                    ct = ar["clean_tail_ms"]
                    if clean_gold_ms is None or ct < clean_gold_ms:
                        clean_gold_ms = ct

            segments.append(SegmentWithModel(
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
                selected_model=self.estimator.name,
                n_completed=n_completed,
                n_attempts=n_attempts,
                gold_ms=gold_ms,
                clean_gold_ms=clean_gold_ms,
            ))
        return segments

    def pick_next(self) -> SegmentWithModel | None:
        self._sync_config_from_db()
        segments = self._load_segments_with_model()
        if not segments:
            return None
        practicable = [s for s in segments if s.state_path and os.path.exists(s.state_path)]
        if not practicable:
            return None
        segment_id = self.allocator.pick_next(practicable)
        if segment_id is None:
            return None
        return next((s for s in practicable if s.segment_id == segment_id), None)

    def process_attempt(
        self, segment_id: str, time_ms: int, completed: bool,
        deaths: int = 0, clean_tail_ms: int | None = None,
    ) -> None:
        new_attempt = AttemptRecord(
            time_ms=time_ms if completed else None,
            completed=completed, deaths=deaths,
            clean_tail_ms=clean_tail_ms if completed else None,
            created_at="",
        )

        attempt_rows = self.db.get_segment_attempts(segment_id)
        all_attempts = _attempts_from_rows(attempt_rows)

        for est in self._all_estimators():
            row = self.db.load_model_state(segment_id, est.name)

            if row and row["state_json"]:
                state = self._deserialize_state(est.name, row["state_json"])
                state = est.process_attempt(state, new_attempt, all_attempts)
            else:
                if completed and time_ms is not None:
                    priors = self._get_priors(est)
                    state = est.init_state(new_attempt, priors)
                else:
                    state = est.rebuild_state([new_attempt])
                    output = est.model_output(state, [new_attempt])
                    self.db.save_model_state(
                        segment_id, est.name,
                        json.dumps(state.to_dict()), json.dumps(output.to_dict()),
                    )
                    continue

            output = est.model_output(state, all_attempts)
            self.db.save_model_state(
                segment_id, est.name,
                json.dumps(state.to_dict()), json.dumps(output.to_dict()),
            )

    def _get_priors(self, est: Estimator) -> dict:
        if isinstance(est, KalmanEstimator):
            all_rows = self.db.load_all_model_states(self.game_id)
            kalman_rows = [r for r in all_rows if r["estimator"] == "kalman"]
            all_states = []
            for r in kalman_rows:
                if r["state_json"]:
                    try:
                        all_states.append(KalmanState.from_dict(json.loads(r["state_json"])))
                    except (json.JSONDecodeError, KeyError):
                        pass
            return est.get_population_priors(all_states)
        return {}

    def peek_next_n(self, n: int) -> list[str]:
        segments = self._load_segments_with_model()
        practicable = [s for s in segments if s.state_path and os.path.exists(s.state_path)]
        return self.allocator.peek_next_n(practicable, n)

    def get_all_model_states(self) -> list[SegmentWithModel]:
        return self._load_segments_with_model()

    def switch_allocator(self, name: str) -> None:
        self.allocator = get_allocator(name)
        self.db.save_allocator_config("allocator", name)

    def switch_estimator(self, name: str) -> None:
        self.estimator = get_estimator(name)
        self.db.save_allocator_config("estimator", name)

    def rebuild_all_states(self) -> None:
        segments = self.db.get_all_segments_with_model(self.game_id)
        for row in segments:
            segment_id = row["id"]
            attempt_rows = self.db.get_segment_attempts(segment_id)
            if not attempt_rows:
                continue
            all_attempts = _attempts_from_rows(attempt_rows)
            for est in self._all_estimators():
                state = est.rebuild_state(all_attempts)
                output = est.model_output(state, all_attempts)
                self.db.save_model_state(
                    segment_id, est.name,
                    json.dumps(state.to_dict()), json.dumps(output.to_dict()),
                )
```

- [ ] **Step 2: Rewrite test_scheduler_kalman.py**

```python
"""Tests for the scheduler coordinator (multi-model)."""
import json
import pytest
from spinlab.db import Database
from spinlab.models import ModelOutput
from spinlab.scheduler import Scheduler


@pytest.fixture
def db_with_segments(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Game", "any%")
    from spinlab.models import Segment, SegmentVariant
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


class TestSchedulerPickNext:
    def test_pick_next_returns_segment_with_model(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        result = sched.pick_next()
        assert result is not None
        assert result.segment_id.startswith("g1:")

    def test_pick_next_no_segments_returns_none(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.upsert_game("g1", "Game", "any%")
        sched = Scheduler(db, "g1")
        assert sched.pick_next() is None


class TestSchedulerProcessAttempt:
    def test_process_attempt_creates_all_model_states(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        segment_id = "g1:1:entrance.0:checkpoint.0"
        sched.process_attempt(segment_id, time_ms=12000, completed=True)
        rows = db_with_segments.load_all_model_states_for_segment(segment_id)
        estimator_names = {r["estimator"] for r in rows}
        assert "kalman" in estimator_names
        assert "model_a" in estimator_names
        assert "model_b" in estimator_names
        for r in rows:
            out = ModelOutput.from_dict(json.loads(r["output_json"]))
            assert out.expected_time_ms > 0

    def test_process_attempt_incomplete(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        segment_id = "g1:1:entrance.0:checkpoint.0"
        sched.process_attempt(segment_id, time_ms=12000, completed=True)
        sched.process_attempt(segment_id, time_ms=5000, completed=False)
        row = db_with_segments.load_model_state(segment_id, "kalman")
        state = json.loads(row["state_json"])
        assert state["n_completed"] == 1
        assert state["n_attempts"] == 2

    def test_process_attempt_with_deaths(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        segment_id = "g1:1:entrance.0:checkpoint.0"
        sched.process_attempt(
            segment_id, time_ms=12000, completed=True,
            deaths=3, clean_tail_ms=4000,
        )
        rows = db_with_segments.load_all_model_states_for_segment(segment_id)
        assert len(rows) == 3


class TestSchedulerPeek:
    def test_peek_next_n(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        results = sched.peek_next_n(3)
        assert len(results) == 3
        assert all(isinstance(r, str) for r in results)


class TestSchedulerSwitch:
    def test_switch_allocator(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        sched.switch_allocator("random")
        assert sched.allocator.name == "random"

    def test_switch_unknown_allocator_raises(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        with pytest.raises(ValueError):
            sched.switch_allocator("nonexistent")


class TestSchedulerRebuild:
    def test_rebuild_all_states(self, db_with_segments):
        sched = Scheduler(db_with_segments, "g1")
        segment_id = "g1:1:entrance.0:checkpoint.0"
        sched.process_attempt(segment_id, time_ms=12000, completed=True)
        sched.process_attempt(segment_id, time_ms=11000, completed=True)
        sched.rebuild_all_states()
        rows = db_with_segments.load_all_model_states_for_segment(segment_id)
        assert len(rows) == 3


class TestStateFileFilter:
    def test_pick_next_skips_missing_state_files(self, tmp_path):
        from spinlab.models import Segment, SegmentVariant
        db = Database(":memory:")
        db.upsert_game("g1", "Test", "any%")
        valid_state = tmp_path / "valid.mss"
        valid_state.write_bytes(b"\x00" * 100)
        seg1 = Segment(id="s1", game_id="g1", level_number=1, start_type="entrance",
                        start_ordinal=0, end_type="checkpoint", end_ordinal=0)
        seg2 = Segment(id="s2", game_id="g1", level_number=2, start_type="entrance",
                        start_ordinal=0, end_type="checkpoint", end_ordinal=0)
        db.upsert_segment(seg1)
        db.upsert_segment(seg2)
        db.add_variant(SegmentVariant(segment_id="s1", variant_type="cold",
                                       state_path=str(valid_state), is_default=True))
        db.add_variant(SegmentVariant(segment_id="s2", variant_type="cold",
                                       state_path="/nonexistent/path.mss", is_default=True))
        sched = Scheduler(db, "g1")
        picked = sched.pick_next()
        assert picked is not None
        assert picked.segment_id == "s1"
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `python -m pytest tests/test_scheduler_kalman.py tests/test_allocators.py -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/scheduler.py tests/test_scheduler_kalman.py
git commit -m "feat: scheduler runs all estimators on each attempt"
```

---

## Task 9: Update practice.py to pass deaths/clean_tail

**Files:**
- Modify: `python/spinlab/practice.py`

- [ ] **Step 1: Update _process_result**

Find in `python/spinlab/practice.py`:
```python
        attempt = Attempt(
            segment_id=result["segment_id"],
            session_id=self.session_id,
            completed=result["completed"],
            time_ms=result.get("time_ms"),
            source="practice",
        )
        self.db.log_attempt(attempt)
        self.scheduler.process_attempt(
            result["segment_id"],
            time_ms=result.get("time_ms", 0),
            completed=result["completed"],
        )
```

Replace with:
```python
        attempt = Attempt(
            segment_id=result["segment_id"],
            session_id=self.session_id,
            completed=result["completed"],
            time_ms=result.get("time_ms"),
            deaths=result.get("deaths", 0),
            clean_tail_ms=result.get("clean_tail_ms"),
            source="practice",
        )
        self.db.log_attempt(attempt)
        self.scheduler.process_attempt(
            result["segment_id"],
            time_ms=result.get("time_ms", 0),
            completed=result["completed"],
            deaths=result.get("deaths", 0),
            clean_tail_ms=result.get("clean_tail_ms"),
        )
```

- [ ] **Step 2: Run practice tests**

Run: `python -m pytest tests/test_practice.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/practice.py
git commit -m "feat: pass deaths and clean_tail_ms through practice pipeline"
```

---

## Task 10: Update dashboard API for multi-model

**Files:**
- Modify: `python/spinlab/dashboard.py`
- Modify: `python/spinlab/session_manager.py`

- [ ] **Step 1: Update /api/model endpoint**

Find in `python/spinlab/dashboard.py` the `api_model()` function and replace the segments list comprehension:

```python
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
```

- [ ] **Step 2: Update session_manager.py**

Search `session_manager.py` for references to `estimator_state`, `marginal_return`, or `drift_info` and update them to use `model_outputs` and `selected_model`. Wherever Kalman-specific state was read (e.g. `s.estimator_state.mu`), read from `s.model_outputs.get(s.selected_model)` instead.

- [ ] **Step 3: Run dashboard tests**

Run: `python -m pytest tests/test_dashboard.py tests/test_dashboard_integration.py -v`
Expected: PASS (may need minor fixes if tests assert specific response shapes)

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/dashboard.py python/spinlab/session_manager.py
git commit -m "feat: dashboard API returns all model outputs"
```

---

## Task 11: Update frontend model.js

**Files:**
- Modify: `python/spinlab/static/model.js`

- [ ] **Step 1: Update the model table rendering**

Replace the `updateModel` function in `python/spinlab/static/model.js`:

```javascript
function updateModel(data) {
  const body = document.getElementById('model-body');
  if (!data.segments || !data.segments.length) {
    body.innerHTML = '<tr><td colspan="8" class="dim">No game loaded</td></tr>';
    return;
  }
  body.innerHTML = '';
  data.segments.forEach(s => {
    const tr = document.createElement('tr');
    const sel = s.model_outputs[s.selected_model];

    let improvClass = 'flat';
    let arrow = '\u2192';
    if (sel) {
      if (sel.ms_per_attempt > 10) { improvClass = 'improving'; arrow = '\u2193'; }
      else if (sel.ms_per_attempt < -10) { improvClass = 'regressing'; arrow = '\u2191'; }
    }
    tr.className = 'drift-row-' + improvClass;

    const models = Object.keys(s.model_outputs);
    let modelCells = '';
    models.forEach(name => {
      const out = s.model_outputs[name];
      const isSel = name === s.selected_model;
      const val = out.ms_per_attempt.toFixed(1);
      const cls = isSel ? ' style="font-weight:bold"' : ' class="dim"';
      modelCells += '<td' + cls + '>' + val + ' ms/att</td>';
    });
    for (let i = models.length; i < 3; i++) {
      modelCells += '<td class="dim">\u2014</td>';
    }

    tr.innerHTML =
      '<td>' + segmentName(s) + '</td>' +
      '<td class="drift-' + improvClass + '">' + arrow + ' ' +
        (sel ? sel.ms_per_attempt.toFixed(1) : '\u2014') + '</td>' +
      modelCells +
      '<td>' + s.n_completed + '</td>' +
      '<td>' + formatTime(s.gold_ms) + '</td>';
    body.appendChild(tr);
  });
  if (data.estimator) {
    document.getElementById('estimator-select').value = data.estimator;
  }
}
```

- [ ] **Step 2: Verify manually in browser**

Run: `spinlab dashboard` and check the Model tab renders correctly.

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/static/model.js
git commit -m "feat: model tab shows all estimators side-by-side"
```

---

## Task 12: Run full test suite and fix remaining failures

**Files:**
- Potentially any test or source file

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -v --tb=short`

- [ ] **Step 2: Fix any failures**

Common issues:
- Tests constructing `SegmentWithModel` with old `marginal_return` field
- Tests calling `scheduler.process_attempt` with old 3-arg signature
- Tests asserting `row["marginal_return"]` from DB queries
- Import errors

- [ ] **Step 3: Run full test suite again to confirm all green**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 4: Commit any fixes**

```bash
git add -u
git commit -m "fix: resolve test failures from multi-model migration"
```

---

## Deferred (not in this plan)

- **Lua death tracking + clean_tail** — Separate plan. Requires Mesen2 testing. Python side is ready to receive `deaths` and `clean_tail_ms` in TCP events.
- **Kalman uncertainty-based floor** — Future: `floor = (mu - k*sqrt(P_mm)) * 1000` where k is configurable or estimated from expected run count. For now, floor = gold.
- **apm-weighted allocator scoring** — Greedy currently uses raw `ms_per_attempt`. Future: `ms_per_attempt * (60_000 / expected_time_ms)` for proper ms/min ranking.
- **Model C (death-aware generative)** — v2 per spec.
- **Ensemble/auto-selection** — v3 per spec.
- **Dynamic time display formatting** — Show appropriate units based on magnitude.
- **Lua overlay updates** — Separate plan, tied to Lua death tracking.

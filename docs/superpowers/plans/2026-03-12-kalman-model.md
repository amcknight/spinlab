# Kalman Model & Allocator Redesign — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the SM-2 scheduler with a Kalman-filter estimator + pluggable allocator system, drop button-based ratings in favor of auto-advance on completion, and add a Model tab to the dashboard.

**Architecture:** Two pluggable layers — Estimator (Kalman filter tracking expected time + drift per split) and Allocator (Greedy/Random/Round Robin picking next split). A thin coordinator in `scheduler.py` wires them together with the same `pick_next()`/`process_attempt()` interface the orchestrator already uses. DB stores estimator state as JSON blobs. Lua drops the RATING state machine in favor of a timed RESULT display. Dashboard gets a Model tab showing all splits ranked by marginal return.

**Tech Stack:** Python 3.11, SQLite, pure scalar arithmetic (no numpy), FastAPI, vanilla JS, Mesen2 Lua

**Spec:** `docs/superpowers/specs/2026-03-12-kalman-model-design.md`
**Math reference:** `practice-optimizer-model-spec.md`

---

## File Structure

| File | Action | Role |
|------|--------|------|
| `python/spinlab/estimators/__init__.py` | Create | Estimator ABC, EstimatorState ABC, registry |
| `python/spinlab/estimators/kalman.py` | Create | KalmanEstimator + KalmanState dataclass |
| `python/spinlab/allocators/__init__.py` | Create | Allocator ABC, SplitWithModel dataclass, registry |
| `python/spinlab/allocators/greedy.py` | Create | Greedy allocator (highest marginal return) |
| `python/spinlab/allocators/random.py` | Create | Random allocator (uniform) |
| `python/spinlab/allocators/round_robin.py` | Create | Round robin allocator |
| `tests/test_kalman.py` | Create | KalmanEstimator unit tests |
| `tests/test_allocators.py` | Create | Allocator unit tests |
| `tests/test_scheduler_kalman.py` | Create | Coordinator integration tests |
| `python/spinlab/scheduler.py` | Rewrite | Thin coordinator (estimator + allocator) |
| `python/spinlab/db.py` | Modify | Drop `schedule`, add `model_state` + `allocator_config`, new queries |
| `python/spinlab/models.py` | Modify | Drop Schedule/Rating, add SplitCommand.auto_advance_delay_ms |
| `python/spinlab/orchestrator.py` | Modify | Drop ratings, auto-advance, use new coordinator |
| `python/spinlab/dashboard.py` | Modify | New endpoints: `/api/model`, `/api/allocator`, `/api/estimator` |
| `python/spinlab/static/index.html` | Modify | 3-tab layout (Live | Model | Manage) |
| `python/spinlab/static/app.js` | Modify | Model tab, reworked Live, allocator dropdown |
| `python/spinlab/static/style.css` | Modify | Model tab styles, remove rating colors |
| `lua/spinlab.lua` | Modify | Drop RATING state, add RESULT, remove R+D-pad input |
| `config.yaml` | Modify | Add `estimator`, `allocator`, `auto_advance_delay_s` fields |
| `tests/test_db_dashboard.py` | Modify | Update for schema changes |
| `tests/test_scheduler_peek.py` | Delete | Superseded by `test_scheduler_kalman.py` |

---

## Pre-flight: Create feature branch

- [ ] **Create the `kalman` branch off `main`**

```bash
git checkout -b kalman
```

---

## Chunk 1: Estimator Module (Pure Math, TDD)

### Task 1: Create estimator ABC and registry

**Files:**
- Create: `python/spinlab/estimators/__init__.py`

- [ ] **Step 1: Write the estimator ABC**

```python
"""Estimator abstract base class and registry."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


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
    def init_state(self, first_time: float, priors: dict) -> EstimatorState:
        """Initialize state from the first observed time."""
        ...

    @abstractmethod
    def process_attempt(
        self, state: EstimatorState, observed_time: float | None
    ) -> EstimatorState:
        """Process one attempt. observed_time=None for incomplete (death/abort)."""
        ...

    @abstractmethod
    def marginal_return(self, state: EstimatorState) -> float:
        """Compute marginal return m_i = -d_i / mu_i."""
        ...

    @abstractmethod
    def drift_info(self, state: EstimatorState) -> dict:
        """Return drift value, confidence interval, and label for dashboard."""
        ...

    @abstractmethod
    def get_population_priors(self, all_states: list[EstimatorState]) -> dict:
        """Compute population-level priors from all splits with enough data."""
        ...

    @abstractmethod
    def rebuild_state(self, attempts: list[float | None]) -> EstimatorState:
        """Rebuild state by replaying all attempts. None = incomplete."""
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

- [ ] **Step 2: Commit**

```bash
git add python/spinlab/estimators/__init__.py
git commit -m "feat(estimator): add Estimator ABC and registry"
```

---

### Task 2: KalmanState dataclass

**Files:**
- Create: `python/spinlab/estimators/kalman.py`
- Create: `tests/test_kalman.py`

- [ ] **Step 1: Write the failing test for KalmanState serialization**

```python
"""Tests for KalmanEstimator."""
import pytest
from spinlab.estimators.kalman import KalmanState


class TestKalmanState:
    def test_round_trip_serialization(self):
        state = KalmanState(
            mu=15.0,
            d=-0.5,
            P_mm=25.0,
            P_md=0.0,
            P_dm=0.0,
            P_dd=1.0,
            R=25.0,
            Q_mm=0.1,
            Q_md=0.0,
            Q_dm=0.0,
            Q_dd=0.01,
            gold=14.2,
            n_completed=5,
            n_attempts=7,
        )
        d = state.to_dict()
        restored = KalmanState.from_dict(d)
        assert restored.mu == state.mu
        assert restored.d == state.d
        assert restored.P_dd == state.P_dd
        assert restored.gold == state.gold
        assert restored.n_completed == state.n_completed
        assert restored.n_attempts == state.n_attempts

    def test_from_dict_missing_keys_uses_defaults(self):
        """Handles missing keys gracefully for forward-compat."""
        minimal = {"mu": 10.0, "d": -0.3, "gold": 9.5, "n_completed": 3, "n_attempts": 4}
        state = KalmanState.from_dict(minimal)
        assert state.mu == 10.0
        assert state.P_mm == 25.0  # default
        assert state.Q_dd == 0.01  # default
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_kalman.py::TestKalmanState -v`
Expected: FAIL — `ModuleNotFoundError` or `ImportError`

- [ ] **Step 3: Write KalmanState**

In `python/spinlab/estimators/kalman.py`:

```python
"""Kalman filter estimator for speedrun split times."""
from __future__ import annotations

from dataclasses import dataclass, field

from spinlab.estimators import Estimator, EstimatorState, register_estimator


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

    # State vector
    mu: float = 0.0  # expected time (seconds)
    d: float = DEFAULT_D  # drift (seconds/run, negative = improving)

    # Covariance matrix P (2x2, stored as 4 scalars)
    P_mm: float = DEFAULT_R  # variance of mu
    P_md: float = 0.0
    P_dm: float = 0.0
    P_dd: float = DEFAULT_P_D0  # variance of drift

    # Noise parameters
    R: float = DEFAULT_R  # observation noise variance
    Q_mm: float = DEFAULT_Q_MM  # process noise for mu
    Q_md: float = DEFAULT_Q_MD
    Q_dm: float = DEFAULT_Q_MD
    Q_dd: float = DEFAULT_Q_DD  # process noise for drift

    # Tracking
    gold: float = float("inf")  # best observed time
    n_completed: int = 0
    n_attempts: int = 0

    def to_dict(self) -> dict:
        return {
            "mu": self.mu,
            "d": self.d,
            "P_mm": self.P_mm,
            "P_md": self.P_md,
            "P_dm": self.P_dm,
            "P_dd": self.P_dd,
            "R": self.R,
            "Q_mm": self.Q_mm,
            "Q_md": self.Q_md,
            "Q_dm": self.Q_dm,
            "Q_dd": self.Q_dd,
            "gold": self.gold,
            "n_completed": self.n_completed,
            "n_attempts": self.n_attempts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "KalmanState":
        return cls(
            mu=d.get("mu", 0.0),
            d=d.get("d", DEFAULT_D),
            P_mm=d.get("P_mm", DEFAULT_R),
            P_md=d.get("P_md", 0.0),
            P_dm=d.get("P_dm", 0.0),
            P_dd=d.get("P_dd", DEFAULT_P_D0),
            R=d.get("R", DEFAULT_R),
            Q_mm=d.get("Q_mm", DEFAULT_Q_MM),
            Q_md=d.get("Q_md", DEFAULT_Q_MD),
            Q_dm=d.get("Q_dm", DEFAULT_Q_MD),
            Q_dd=d.get("Q_dd", DEFAULT_Q_DD),
            gold=d.get("gold", float("inf")),
            n_completed=d.get("n_completed", 0),
            n_attempts=d.get("n_attempts", 0),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_kalman.py::TestKalmanState -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/estimators/kalman.py tests/test_kalman.py
git commit -m "feat(kalman): add KalmanState dataclass with serialization"
```

---

### Task 3: Kalman predict + update (core math)

**Files:**
- Modify: `python/spinlab/estimators/kalman.py`
- Modify: `tests/test_kalman.py`

- [ ] **Step 1: Write failing tests for predict and update**

Append to `tests/test_kalman.py`:

```python
from spinlab.estimators.kalman import KalmanEstimator, KalmanState


class TestKalmanPredict:
    def test_predict_shifts_mu_by_drift(self):
        est = KalmanEstimator()
        state = KalmanState(mu=20.0, d=-1.0, P_mm=25.0, P_md=0.0, P_dm=0.0, P_dd=1.0,
                            R=25.0, Q_mm=0.1, Q_md=0.0, Q_dm=0.0, Q_dd=0.01)
        pred = est._predict(state)
        assert pred.mu == pytest.approx(19.0)  # 20 + (-1)
        assert pred.d == pytest.approx(-1.0)   # drift unchanged

    def test_predict_grows_covariance(self):
        est = KalmanEstimator()
        state = KalmanState(mu=20.0, d=-1.0, P_mm=25.0, P_md=0.0, P_dm=0.0, P_dd=1.0,
                            R=25.0, Q_mm=0.1, Q_md=0.0, Q_dm=0.0, Q_dd=0.01)
        pred = est._predict(state)
        # P_mm_pred = P_mm + 2*P_md + P_dd + Q_mm = 25 + 0 + 1 + 0.1 = 26.1
        assert pred.P_mm == pytest.approx(26.1)
        # P_dd_pred = P_dd + Q_dd = 1 + 0.01 = 1.01
        assert pred.P_dd == pytest.approx(1.01)


class TestKalmanUpdate:
    def test_update_pulls_mu_toward_observation(self):
        est = KalmanEstimator()
        # Predicted state where mu=19, observation is 17 (faster than expected)
        pred = KalmanState(mu=19.0, d=-1.0, P_mm=26.1, P_md=1.0, P_dm=1.0, P_dd=1.01,
                           R=25.0)
        updated = est._update(pred, observed_time=17.0)
        # Innovation z = 17 - 19 = -2 (faster), mu should decrease
        assert updated.mu < 19.0

    def test_update_adjusts_drift(self):
        est = KalmanEstimator()
        pred = KalmanState(mu=19.0, d=-1.0, P_mm=26.1, P_md=1.0, P_dm=1.0, P_dd=1.01,
                           R=25.0)
        updated = est._update(pred, observed_time=17.0)
        # Faster observation → drift should become more negative (more improvement)
        assert updated.d < -1.0

    def test_update_shrinks_covariance(self):
        est = KalmanEstimator()
        pred = KalmanState(mu=19.0, d=-1.0, P_mm=26.1, P_md=1.0, P_dm=1.0, P_dd=1.01,
                           R=25.0)
        updated = est._update(pred, observed_time=17.0)
        # After update, uncertainty should decrease
        assert updated.P_mm < pred.P_mm
        assert updated.P_dd < pred.P_dd
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_kalman.py::TestKalmanPredict tests/test_kalman.py::TestKalmanUpdate -v`
Expected: FAIL — `KalmanEstimator` has no `_predict`/`_update`

- [ ] **Step 3: Implement _predict and _update**

Add to `KalmanEstimator` class in `python/spinlab/estimators/kalman.py` (the class stub will be created here — register it but leave `process_attempt` and other ABC methods as stubs for now):

```python
@register_estimator
class KalmanEstimator(Estimator):
    name = "kalman"

    def _predict(self, state: KalmanState) -> KalmanState:
        """Predict step: propagate state one step forward.

        F = [[1, 1], [0, 1]]
        x_pred = F @ x
        P_pred = F @ P @ F^T + Q
        """
        mu_pred = state.mu + state.d
        d_pred = state.d

        # F @ P @ F^T expanded:
        # P_mm_pred = P_mm + P_md + P_dm + P_dd
        # P_md_pred = P_md + P_dd
        # P_dm_pred = P_dm + P_dd
        # P_dd_pred = P_dd
        # Then + Q:
        P_mm_pred = state.P_mm + state.P_md + state.P_dm + state.P_dd + state.Q_mm
        P_md_pred = state.P_md + state.P_dd + state.Q_md
        P_dm_pred = state.P_dm + state.P_dd + state.Q_dm
        P_dd_pred = state.P_dd + state.Q_dd

        return KalmanState(
            mu=mu_pred,
            d=d_pred,
            P_mm=P_mm_pred,
            P_md=P_md_pred,
            P_dm=P_dm_pred,
            P_dd=P_dd_pred,
            R=state.R,
            Q_mm=state.Q_mm,
            Q_md=state.Q_md,
            Q_dm=state.Q_dm,
            Q_dd=state.Q_dd,
            gold=state.gold,
            n_completed=state.n_completed,
            n_attempts=state.n_attempts,
        )

    def _update(self, predicted: KalmanState, observed_time: float) -> KalmanState:
        """Update step: incorporate observation.

        H = [1, 0]
        z = y - H @ x_pred = y - mu_pred
        S = H @ P_pred @ H^T + R = P_mm + R
        K = P_pred @ H^T / S = [P_mm/S, P_dm/S]
        x = x_pred + K * z
        P = (I - K @ H) @ P_pred
        """
        z = observed_time - predicted.mu  # innovation
        S = predicted.P_mm + predicted.R  # innovation variance

        # Kalman gain K (2x1)
        K_mu = predicted.P_mm / S
        K_d = predicted.P_dm / S

        # State update
        mu_new = predicted.mu + K_mu * z
        d_new = predicted.d + K_d * z

        # Covariance update: P = (I - K @ H) @ P_pred
        # (I - K @ H) = [[1 - K_mu, 0], [-K_d, 1]]
        P_mm_new = (1 - K_mu) * predicted.P_mm
        P_md_new = (1 - K_mu) * predicted.P_md
        P_dm_new = -K_d * predicted.P_mm + predicted.P_dm
        P_dd_new = -K_d * predicted.P_md + predicted.P_dd

        return KalmanState(
            mu=mu_new,
            d=d_new,
            P_mm=P_mm_new,
            P_md=P_md_new,
            P_dm=P_dm_new,
            P_dd=P_dd_new,
            R=predicted.R,
            Q_mm=predicted.Q_mm,
            Q_md=predicted.Q_md,
            Q_dm=predicted.Q_dm,
            Q_dd=predicted.Q_dd,
            gold=predicted.gold,
            n_completed=predicted.n_completed,
            n_attempts=predicted.n_attempts,
        )

    # --- ABC stubs (implemented in subsequent tasks) ---

    def init_state(self, first_time: float, priors: dict) -> KalmanState:
        raise NotImplementedError

    def process_attempt(self, state: KalmanState, observed_time: float | None) -> KalmanState:
        raise NotImplementedError

    def marginal_return(self, state: KalmanState) -> float:
        raise NotImplementedError

    def drift_info(self, state: KalmanState) -> dict:
        raise NotImplementedError

    def get_population_priors(self, all_states: list[KalmanState]) -> dict:
        raise NotImplementedError

    def rebuild_state(self, attempts: list[float | None]) -> KalmanState:
        raise NotImplementedError
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_kalman.py::TestKalmanPredict tests/test_kalman.py::TestKalmanUpdate -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/estimators/kalman.py tests/test_kalman.py
git commit -m "feat(kalman): implement predict and update steps"
```

---

### Task 4: init_state + process_attempt + marginal_return

**Files:**
- Modify: `python/spinlab/estimators/kalman.py`
- Modify: `tests/test_kalman.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_kalman.py`:

```python
class TestKalmanInitState:
    def test_init_from_first_observation(self):
        est = KalmanEstimator()
        state = est.init_state(first_time=15.0, priors={})
        assert state.mu == 15.0
        assert state.d == -0.5  # default prior
        assert state.gold == 15.0
        assert state.n_completed == 1
        assert state.n_attempts == 1
        assert state.R == 25.0  # default

    def test_init_with_population_priors(self):
        est = KalmanEstimator()
        priors = {"d": -0.8, "R": 16.0, "Q_mm": 0.2, "Q_dd": 0.02}
        state = est.init_state(first_time=10.0, priors=priors)
        assert state.d == -0.8
        assert state.R == 16.0
        assert state.Q_mm == 0.2
        assert state.Q_dd == 0.02


class TestKalmanProcessAttempt:
    def test_completed_attempt_updates_state(self):
        est = KalmanEstimator()
        state = est.init_state(first_time=20.0, priors={})
        state2 = est.process_attempt(state, observed_time=18.0)
        # mu should decrease (faster observation)
        assert state2.mu < 20.0
        assert state2.n_completed == 2
        assert state2.n_attempts == 2
        assert state2.gold == 18.0  # new best

    def test_incomplete_attempt_skips_kalman_update(self):
        est = KalmanEstimator()
        state = est.init_state(first_time=20.0, priors={})
        state2 = est.process_attempt(state, observed_time=None)
        # mu unchanged (no observation to incorporate)
        assert state2.mu == state.mu
        assert state2.d == state.d
        assert state2.n_completed == 1  # unchanged
        assert state2.n_attempts == 2   # incremented

    def test_gold_tracks_minimum(self):
        est = KalmanEstimator()
        state = est.init_state(first_time=20.0, priors={})
        state = est.process_attempt(state, 22.0)  # slower
        assert state.gold == 20.0  # still first
        state = est.process_attempt(state, 18.0)  # faster
        assert state.gold == 18.0


class TestKalmanMarginalReturn:
    def test_improving_split_has_positive_return(self):
        est = KalmanEstimator()
        state = KalmanState(mu=20.0, d=-1.0)
        assert est.marginal_return(state) == pytest.approx(0.05)  # -(-1)/20

    def test_regressing_split_has_negative_return(self):
        est = KalmanEstimator()
        state = KalmanState(mu=20.0, d=0.5)
        assert est.marginal_return(state) == pytest.approx(-0.025)  # -(0.5)/20

    def test_zero_mu_returns_zero(self):
        est = KalmanEstimator()
        state = KalmanState(mu=0.0, d=-1.0)
        assert est.marginal_return(state) == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_kalman.py::TestKalmanInitState tests/test_kalman.py::TestKalmanProcessAttempt tests/test_kalman.py::TestKalmanMarginalReturn -v`
Expected: FAIL — `NotImplementedError`

- [ ] **Step 3: Implement init_state, process_attempt, marginal_return**

Replace the stubs in `KalmanEstimator`:

```python
    def init_state(self, first_time: float, priors: dict) -> KalmanState:
        d = priors.get("d", DEFAULT_D)
        R = priors.get("R", DEFAULT_R)
        Q_mm = priors.get("Q_mm", DEFAULT_Q_MM)
        Q_md = priors.get("Q_md", DEFAULT_Q_MD)
        Q_dd = priors.get("Q_dd", DEFAULT_Q_DD)
        return KalmanState(
            mu=first_time,
            d=d,
            P_mm=R,
            P_md=0.0,
            P_dm=0.0,
            P_dd=DEFAULT_P_D0,
            R=R,
            Q_mm=Q_mm,
            Q_md=Q_md,
            Q_dm=Q_md,
            Q_dd=Q_dd,
            gold=first_time,
            n_completed=1,
            n_attempts=1,
        )

    def process_attempt(
        self, state: KalmanState, observed_time: float | None
    ) -> KalmanState:
        if observed_time is None:
            # Incomplete attempt: count it but skip Kalman update
            return KalmanState(
                mu=state.mu, d=state.d,
                P_mm=state.P_mm, P_md=state.P_md, P_dm=state.P_dm, P_dd=state.P_dd,
                R=state.R,
                Q_mm=state.Q_mm, Q_md=state.Q_md, Q_dm=state.Q_dm, Q_dd=state.Q_dd,
                gold=state.gold,
                n_completed=state.n_completed,
                n_attempts=state.n_attempts + 1,
            )

        predicted = self._predict(state)
        updated = self._update(predicted, observed_time)

        n_completed = state.n_completed + 1
        gold = min(state.gold, observed_time)

        result = KalmanState(
            mu=updated.mu, d=updated.d,
            P_mm=updated.P_mm, P_md=updated.P_md, P_dm=updated.P_dm, P_dd=updated.P_dd,
            R=updated.R,
            Q_mm=state.Q_mm, Q_md=state.Q_md, Q_dm=state.Q_dm, Q_dd=state.Q_dd,
            gold=gold,
            n_completed=n_completed,
            n_attempts=state.n_attempts + 1,
        )

        # R re-estimation every 10 completed runs
        if n_completed >= R_REESTIMATE_INTERVAL and n_completed % R_REESTIMATE_INTERVAL == 0:
            result = self._reestimate_R(result, predicted, observed_time)

        return result

    def marginal_return(self, state: KalmanState) -> float:
        if state.mu == 0.0:
            return 0.0
        return -state.d / state.mu
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_kalman.py::TestKalmanInitState tests/test_kalman.py::TestKalmanProcessAttempt tests/test_kalman.py::TestKalmanMarginalReturn -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/estimators/kalman.py tests/test_kalman.py
git commit -m "feat(kalman): implement init_state, process_attempt, marginal_return"
```

---

### Task 5: drift_info, R re-estimation, population priors, rebuild_state

**Files:**
- Modify: `python/spinlab/estimators/kalman.py`
- Modify: `tests/test_kalman.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_kalman.py`:

```python
import math


class TestKalmanDriftInfo:
    def test_confident_improving(self):
        est = KalmanEstimator()
        # d=-1.0, P_dd small → confident, CI doesn't cross zero
        state = KalmanState(mu=20.0, d=-1.0, P_dd=0.01)
        info = est.drift_info(state)
        assert info["drift"] == -1.0
        assert info["label"] == "improving"
        assert info["confidence"] == "confident"
        assert info["ci_lower"] < -1.0
        assert info["ci_upper"] < 0  # whole CI below zero

    def test_uncertain_drift(self):
        est = KalmanEstimator()
        # d=-0.3, P_dd large → CI crosses zero
        state = KalmanState(mu=20.0, d=-0.3, P_dd=4.0)
        info = est.drift_info(state)
        assert info["confidence"] == "uncertain"

    def test_regressing(self):
        est = KalmanEstimator()
        state = KalmanState(mu=20.0, d=0.5, P_dd=0.01)
        info = est.drift_info(state)
        assert info["label"] == "regressing"


class TestKalmanPopulationPriors:
    def test_computes_mean_from_mature_splits(self):
        est = KalmanEstimator()
        states = [
            KalmanState(d=-0.8, R=20.0, Q_mm=0.1, Q_dd=0.02, n_completed=15),
            KalmanState(d=-0.4, R=30.0, Q_mm=0.1, Q_dd=0.01, n_completed=20),
        ]
        priors = est.get_population_priors(states)
        assert priors["d"] == pytest.approx(-0.6)
        assert priors["R"] == pytest.approx(25.0)

    def test_returns_defaults_when_no_mature_splits(self):
        est = KalmanEstimator()
        states = [KalmanState(n_completed=3), KalmanState(n_completed=5)]
        priors = est.get_population_priors(states)
        assert priors["d"] == DEFAULT_D
        assert priors["R"] == DEFAULT_R


class TestKalmanRebuildState:
    def test_rebuild_matches_sequential_processing(self):
        est = KalmanEstimator()
        times = [20.0, 19.0, None, 18.5, 17.0]

        # Sequential
        state = est.init_state(times[0], priors={})
        for t in times[1:]:
            state = est.process_attempt(state, t)

        # Rebuild
        rebuilt = est.rebuild_state(times)

        assert rebuilt.mu == pytest.approx(state.mu)
        assert rebuilt.d == pytest.approx(state.d)
        assert rebuilt.P_dd == pytest.approx(state.P_dd)
        assert rebuilt.gold == pytest.approx(state.gold)
        assert rebuilt.n_completed == state.n_completed
        assert rebuilt.n_attempts == state.n_attempts
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_kalman.py::TestKalmanDriftInfo tests/test_kalman.py::TestKalmanPopulationPriors tests/test_kalman.py::TestKalmanRebuildState -v`
Expected: FAIL — `NotImplementedError`

- [ ] **Step 3: Implement remaining methods**

Add to `KalmanEstimator` in `python/spinlab/estimators/kalman.py`:

```python
    def _reestimate_R(
        self, state: KalmanState, predicted: KalmanState, observed_time: float
    ) -> KalmanState:
        """Re-estimate observation noise R from latest innovation.

        Simple exponential moving average approach: blend current R
        with the squared innovation, weighted toward recent data.
        """
        innovation_sq = (observed_time - predicted.mu) ** 2
        # Innovation variance S = P_mm_pred + R. So R ≈ innovation² - P_mm_pred
        R_est = innovation_sq - predicted.P_mm
        R_new = max(R_est, R_FLOOR)
        # Blend: 70% old, 30% new estimate for stability
        R_blended = 0.7 * state.R + 0.3 * R_new
        return KalmanState(
            mu=state.mu, d=state.d,
            P_mm=state.P_mm, P_md=state.P_md, P_dm=state.P_dm, P_dd=state.P_dd,
            R=max(R_blended, R_FLOOR),
            Q_mm=state.Q_mm, Q_md=state.Q_md, Q_dm=state.Q_dm, Q_dd=state.Q_dd,
            gold=state.gold, n_completed=state.n_completed, n_attempts=state.n_attempts,
        )

    def drift_info(self, state: KalmanState) -> dict:
        import math

        p_dd_sqrt = math.sqrt(max(state.P_dd, 0.0))
        ci_lower = state.d - 1.96 * p_dd_sqrt
        ci_upper = state.d + 1.96 * p_dd_sqrt

        # Label: based on drift sign
        if state.d < 0:
            label = "improving"
        elif state.d > 0:
            label = "regressing"
        else:
            label = "flat"

        # Confidence: does CI cross zero?
        if ci_lower > 0 or ci_upper < 0:
            confidence = "confident"
        elif p_dd_sqrt < 0.5:
            confidence = "moderate"
        else:
            confidence = "uncertain"

        return {
            "drift": state.d,
            "ci_lower": ci_lower,
            "ci_upper": ci_upper,
            "label": label,
            "confidence": confidence,
        }

    def get_population_priors(self, all_states: list[KalmanState]) -> dict:
        mature = [s for s in all_states if s.n_completed >= R_REESTIMATE_INTERVAL]
        if not mature:
            return {
                "d": DEFAULT_D,
                "R": DEFAULT_R,
                "Q_mm": DEFAULT_Q_MM,
                "Q_dd": DEFAULT_Q_DD,
            }
        n = len(mature)
        return {
            "d": sum(s.d for s in mature) / n,
            "R": sum(s.R for s in mature) / n,
            "Q_mm": sum(s.Q_mm for s in mature) / n,
            "Q_dd": sum(s.Q_dd for s in mature) / n,
        }

    def rebuild_state(self, attempts: list[float | None]) -> KalmanState:
        completed = [t for t in attempts if t is not None]
        if not completed:
            # All incomplete — return default state with attempt count
            state = KalmanState(n_attempts=len(attempts))
            return state
        first_time = completed[0]
        state = self.init_state(first_time, priors={})

        # Find position of first completed attempt and replay from there
        first_idx = attempts.index(first_time)
        # Count incompletes before first completed
        for i in range(first_idx):
            state = self.process_attempt(state, None)

        # Replay remaining attempts
        for t in attempts[first_idx + 1 :]:
            state = self.process_attempt(state, t)

        return state
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_kalman.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/estimators/kalman.py tests/test_kalman.py
git commit -m "feat(kalman): implement drift_info, population priors, rebuild_state"
```

---

### Task 6: Kalman convergence integration test

**Files:**
- Modify: `tests/test_kalman.py`

- [ ] **Step 1: Write integration test for realistic scenario**

Append to `tests/test_kalman.py`:

```python
class TestKalmanConvergence:
    def test_improving_runner_detected(self):
        """Simulate a runner improving from 20s to ~15s over 30 runs."""
        est = KalmanEstimator()
        import random

        random.seed(42)
        state = est.init_state(first_time=20.0, priors={})

        # Simulate improvement: true mean drops by 0.2s per run
        for run in range(29):
            true_mean = 20.0 - 0.2 * (run + 1)
            observed = true_mean + random.gauss(0, 2.0)  # noise σ=2s
            state = est.process_attempt(state, observed)

        # After 30 runs, filter should detect negative drift
        assert state.d < 0, "Should detect improvement"
        info = est.drift_info(state)
        assert info["label"] == "improving"
        assert est.marginal_return(state) > 0

    def test_flat_runner_near_zero_drift(self):
        """Simulate a runner with no improvement — drift should stay near zero."""
        est = KalmanEstimator()
        import random

        random.seed(99)
        state = est.init_state(first_time=15.0, priors={})

        for _ in range(29):
            observed = 15.0 + random.gauss(0, 2.0)
            state = est.process_attempt(state, observed)

        # Drift should be close to zero
        assert abs(state.d) < 1.0, f"Drift should be near zero, got {state.d}"
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/test_kalman.py::TestKalmanConvergence -v`
Expected: PASS (this is a validation test, not red-green — the implementation is already done)

- [ ] **Step 3: Commit**

```bash
git add tests/test_kalman.py
git commit -m "test(kalman): add convergence integration tests"
```

---

## Chunk 2: Allocator Module + DB Schema

### Task 7: Allocator ABC, SplitWithModel, and registry

**Files:**
- Create: `python/spinlab/allocators/__init__.py`

- [ ] **Step 1: Write the allocator ABC and SplitWithModel**

```python
"""Allocator abstract base class, SplitWithModel, and registry."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from spinlab.estimators import EstimatorState


@dataclass
class SplitWithModel:
    """Split metadata combined with estimator output."""

    # Split metadata (from splits table)
    split_id: str
    game_id: str
    level_number: int
    room_id: int | None
    goal: str
    description: str
    strat_version: int
    reference_time_ms: int | None
    state_path: str | None
    active: bool
    # Estimator output
    estimator_state: EstimatorState | None = None
    marginal_return: float = 0.0
    drift_info: dict = field(default_factory=dict)
    n_completed: int = 0
    n_attempts: int = 0
    gold_ms: int | None = None


class Allocator(ABC):
    """Abstract allocator that picks next split to practice."""

    name: str

    @abstractmethod
    def pick_next(self, split_states: list[SplitWithModel]) -> str | None:
        """Pick next split_id to practice, or None if list is empty."""
        ...

    @abstractmethod
    def peek_next_n(self, split_states: list[SplitWithModel], n: int) -> list[str]:
        """Preview next N split_ids without side effects."""
        ...


# Registry: name -> Allocator class
_ALLOCATOR_REGISTRY: dict[str, type[Allocator]] = {}


def register_allocator(cls: type[Allocator]) -> type[Allocator]:
    """Decorator to register an allocator class."""
    _ALLOCATOR_REGISTRY[cls.name] = cls
    return cls


def get_allocator(name: str) -> Allocator:
    """Instantiate an allocator by name."""
    if name not in _ALLOCATOR_REGISTRY:
        raise ValueError(
            f"Unknown allocator: {name!r}. "
            f"Available: {list(_ALLOCATOR_REGISTRY.keys())}"
        )
    return _ALLOCATOR_REGISTRY[name]()


def list_allocators() -> list[str]:
    """Return list of registered allocator names."""
    return list(_ALLOCATOR_REGISTRY.keys())
```

- [ ] **Step 2: Commit**

```bash
git add python/spinlab/allocators/__init__.py
git commit -m "feat(allocator): add Allocator ABC, SplitWithModel, and registry"
```

---

### Task 8: Greedy allocator

**Files:**
- Create: `python/spinlab/allocators/greedy.py`
- Create: `tests/test_allocators.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for allocator implementations."""
import pytest
from spinlab.allocators import SplitWithModel
from spinlab.allocators.greedy import GreedyAllocator


def _make_split(split_id: str, marginal_return: float) -> SplitWithModel:
    return SplitWithModel(
        split_id=split_id,
        game_id="test",
        level_number=1,
        room_id=None,
        goal="normal",
        description="test",
        strat_version=1,
        reference_time_ms=None,
        state_path=None,
        active=True,
        marginal_return=marginal_return,
    )


class TestGreedyAllocator:
    def test_picks_highest_marginal_return(self):
        alloc = GreedyAllocator()
        splits = [_make_split("a", 0.05), _make_split("b", 0.10), _make_split("c", 0.02)]
        assert alloc.pick_next(splits) == "b"

    def test_peek_returns_sorted_order(self):
        alloc = GreedyAllocator()
        splits = [_make_split("a", 0.05), _make_split("b", 0.10), _make_split("c", 0.02)]
        result = alloc.peek_next_n(splits, 2)
        assert result == ["b", "a"]

    def test_empty_list_returns_none(self):
        alloc = GreedyAllocator()
        assert alloc.pick_next([]) is None

    def test_peek_empty_returns_empty(self):
        alloc = GreedyAllocator()
        assert alloc.peek_next_n([], 5) == []

    def test_peek_more_than_available(self):
        alloc = GreedyAllocator()
        splits = [_make_split("a", 0.05)]
        assert alloc.peek_next_n(splits, 5) == ["a"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_allocators.py::TestGreedyAllocator -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement greedy allocator**

```python
"""Greedy allocator: picks split with highest marginal return."""
from __future__ import annotations

from spinlab.allocators import Allocator, SplitWithModel, register_allocator


@register_allocator
class GreedyAllocator(Allocator):
    name = "greedy"

    def pick_next(self, split_states: list[SplitWithModel]) -> str | None:
        if not split_states:
            return None
        best = max(split_states, key=lambda s: s.marginal_return)
        return best.split_id

    def peek_next_n(self, split_states: list[SplitWithModel], n: int) -> list[str]:
        sorted_splits = sorted(split_states, key=lambda s: s.marginal_return, reverse=True)
        return [s.split_id for s in sorted_splits[:n]]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_allocators.py::TestGreedyAllocator -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/allocators/greedy.py tests/test_allocators.py
git commit -m "feat(allocator): implement greedy allocator"
```

---

### Task 9: Random and Round Robin allocators

**Files:**
- Create: `python/spinlab/allocators/random.py`
- Create: `python/spinlab/allocators/round_robin.py`
- Modify: `tests/test_allocators.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_allocators.py`:

```python
from spinlab.allocators.random import RandomAllocator
from spinlab.allocators.round_robin import RoundRobinAllocator


class TestRandomAllocator:
    def test_picks_from_available(self):
        alloc = RandomAllocator()
        splits = [_make_split("a", 0.0), _make_split("b", 0.0)]
        result = alloc.pick_next(splits)
        assert result in ("a", "b")

    def test_empty_returns_none(self):
        alloc = RandomAllocator()
        assert alloc.pick_next([]) is None

    def test_peek_no_replacement(self):
        alloc = RandomAllocator()
        splits = [_make_split("a", 0.0), _make_split("b", 0.0), _make_split("c", 0.0)]
        result = alloc.peek_next_n(splits, 3)
        assert len(result) == 3
        assert len(set(result)) == 3  # no duplicates


class TestRoundRobinAllocator:
    def test_cycles_through_all(self):
        alloc = RoundRobinAllocator()
        splits = [_make_split("a", 0.0), _make_split("b", 0.0), _make_split("c", 0.0)]
        results = [alloc.pick_next(splits) for _ in range(6)]
        # Should cycle: a, b, c, a, b, c
        assert results == ["a", "b", "c", "a", "b", "c"]

    def test_empty_returns_none(self):
        alloc = RoundRobinAllocator()
        assert alloc.pick_next([]) is None

    def test_peek_returns_upcoming(self):
        alloc = RoundRobinAllocator()
        splits = [_make_split("a", 0.0), _make_split("b", 0.0), _make_split("c", 0.0)]
        result = alloc.peek_next_n(splits, 2)
        assert result == ["a", "b"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_allocators.py::TestRandomAllocator tests/test_allocators.py::TestRoundRobinAllocator -v`
Expected: FAIL

- [ ] **Step 3: Implement random allocator**

`python/spinlab/allocators/random.py`:

```python
"""Random allocator: uniform random selection."""
from __future__ import annotations

import random as _random

from spinlab.allocators import Allocator, SplitWithModel, register_allocator


@register_allocator
class RandomAllocator(Allocator):
    name = "random"

    def pick_next(self, split_states: list[SplitWithModel]) -> str | None:
        if not split_states:
            return None
        return _random.choice(split_states).split_id

    def peek_next_n(self, split_states: list[SplitWithModel], n: int) -> list[str]:
        sample_size = min(n, len(split_states))
        return [s.split_id for s in _random.sample(split_states, sample_size)]
```

- [ ] **Step 4: Implement round robin allocator**

`python/spinlab/allocators/round_robin.py`:

```python
"""Round robin allocator: cycles through splits in stable order."""
from __future__ import annotations

from spinlab.allocators import Allocator, SplitWithModel, register_allocator


@register_allocator
class RoundRobinAllocator(Allocator):
    name = "round_robin"

    def __init__(self) -> None:
        self._index = 0

    def pick_next(self, split_states: list[SplitWithModel]) -> str | None:
        if not split_states:
            return None
        idx = self._index % len(split_states)
        self._index += 1
        return split_states[idx].split_id

    def peek_next_n(self, split_states: list[SplitWithModel], n: int) -> list[str]:
        if not split_states:
            return []
        result = []
        for i in range(min(n, len(split_states))):
            idx = (self._index + i) % len(split_states)
            result.append(split_states[idx].split_id)
        return result
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_allocators.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/allocators/random.py python/spinlab/allocators/round_robin.py tests/test_allocators.py
git commit -m "feat(allocator): implement random and round robin allocators"
```

---

### Task 10: DB schema migration

**Files:**
- Modify: `python/spinlab/db.py`
- Modify: `tests/test_db_dashboard.py`

- [ ] **Step 1: Write failing test for migration**

Add to `tests/test_db_dashboard.py`:

```python
class TestSchemaMigration:
    def test_old_schedule_table_dropped_on_init(self, tmp_path):
        """If old DB has 'schedule' table, it gets dropped and replaced."""
        import sqlite3

        db_path = tmp_path / "test.db"
        # Create old-schema DB with schedule table
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE schedule (split_id TEXT PRIMARY KEY, ease_factor REAL)")
        conn.execute("INSERT INTO schedule VALUES ('s1', 2.5)")
        conn.commit()
        conn.close()

        # Init with new code should drop schedule, create model_state
        from spinlab.db import Database
        db = Database(str(db_path))

        # schedule table should be gone
        cur = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schedule'")
        assert cur.fetchone() is None

        # model_state and allocator_config should exist
        cur = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='model_state'")
        assert cur.fetchone() is not None
        cur = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='allocator_config'")
        assert cur.fetchone() is not None


class TestModelStateDB:
    def test_save_and_load_model_state(self, tmp_path):
        from spinlab.db import Database
        db = Database(str(tmp_path / "test.db"))
        db.conn.execute("INSERT INTO games (id, name, category) VALUES ('g1', 'Game', 'any%')")
        db.conn.execute(
            "INSERT INTO splits (id, game_id, level_number, goal, description, strat_version, active) "
            "VALUES ('s1', 'g1', 1, 'normal', 'test', 1, 1)"
        )
        db.conn.commit()

        db.save_model_state("s1", "kalman", '{"mu": 15.0}', 0.05)
        row = db.load_model_state("s1")
        assert row is not None
        assert row["estimator"] == "kalman"
        assert row["state_json"] == '{"mu": 15.0}'
        assert row["marginal_return"] == pytest.approx(0.05)

    def test_load_missing_returns_none(self, tmp_path):
        from spinlab.db import Database
        db = Database(str(tmp_path / "test.db"))
        assert db.load_model_state("nonexistent") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db_dashboard.py::TestSchemaMigration tests/test_db_dashboard.py::TestModelStateDB -v`
Expected: FAIL — no `model_state` table, no `save_model_state`/`load_model_state`

- [ ] **Step 3: Modify db.py schema and add migration logic**

In `python/spinlab/db.py`, in `_init_schema()`:

1. After creating all existing tables, add migration logic:

```python
        # --- Migration: drop old SM-2 schedule table ---
        cur = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schedule'"
        )
        if cur.fetchone() is not None:
            self.conn.execute("DROP TABLE schedule")

        # Drop the schedule index if it exists
        self.conn.execute("DROP INDEX IF EXISTS idx_schedule_next")

        # --- New tables ---
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS model_state (
                split_id TEXT PRIMARY KEY REFERENCES splits(id),
                estimator TEXT NOT NULL,
                state_json TEXT NOT NULL,
                marginal_return REAL,
                updated_at TEXT NOT NULL
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS allocator_config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Make attempts.rating nullable (it was NOT NULL before)
        # SQLite doesn't support ALTER COLUMN, but the column was already
        # created without NOT NULL in our schema, so just ensure new inserts
        # can pass None for rating.
```

2. Remove the old `CREATE TABLE schedule` and `CREATE INDEX idx_schedule_next` statements from the schema string.

3. Add new DB methods:

```python
    def save_model_state(
        self, split_id: str, estimator: str, state_json: str, marginal_return: float
    ) -> None:
        now = datetime.utcnow().isoformat() + "Z"
        self.conn.execute(
            """INSERT INTO model_state (split_id, estimator, state_json, marginal_return, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(split_id) DO UPDATE SET
                 estimator=excluded.estimator,
                 state_json=excluded.state_json,
                 marginal_return=excluded.marginal_return,
                 updated_at=excluded.updated_at""",
            (split_id, estimator, state_json, marginal_return, now),
        )
        self.conn.commit()

    def load_model_state(self, split_id: str) -> dict | None:
        cur = self.conn.execute(
            "SELECT split_id, estimator, state_json, marginal_return, updated_at "
            "FROM model_state WHERE split_id = ?",
            (split_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "split_id": row[0],
            "estimator": row[1],
            "state_json": row[2],
            "marginal_return": row[3],
            "updated_at": row[4],
        }

    def load_all_model_states(self, game_id: str) -> list[dict]:
        cur = self.conn.execute(
            """SELECT m.split_id, m.estimator, m.state_json, m.marginal_return, m.updated_at
               FROM model_state m
               JOIN splits s ON m.split_id = s.id
               WHERE s.game_id = ? AND s.active = 1
               ORDER BY m.marginal_return DESC""",
            (game_id,),
        )
        cols = ["split_id", "estimator", "state_json", "marginal_return", "updated_at"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def save_allocator_config(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO allocator_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def load_allocator_config(self, key: str) -> str | None:
        cur = self.conn.execute(
            "SELECT value FROM allocator_config WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        return row[0] if row else None
```

4. Remove these old methods (no longer needed):
   - `ensure_schedule`
   - `get_due_splits`
   - `get_next_due`
   - `update_schedule`
   - `reset_schedule`
   - `get_all_scheduled_split_ids` (queries dropped `schedule` table)
   - `get_all_splits_with_schedule` (replaced by `get_all_splits_with_model` below)
   - `get_split_with_schedule` (queries dropped `schedule` table)
   - `get_splits_summary_by_ids` (joins dropped `schedule` table)

5. Replace `get_all_splits_with_schedule` with `get_all_splits_with_model`:

```python
    def get_all_splits_with_model(self, game_id: str) -> list[dict]:
        """Get all active splits LEFT JOIN model_state."""
        cur = self.conn.execute(
            """SELECT s.id, s.game_id, s.level_number, s.room_id, s.goal,
                      s.description, s.strat_version, s.reference_time_ms,
                      s.state_path, s.active,
                      m.estimator, m.state_json, m.marginal_return
               FROM splits s
               LEFT JOIN model_state m ON s.id = m.split_id
               WHERE s.game_id = ? AND s.active = 1
               ORDER BY s.level_number, s.room_id""",
            (game_id,),
        )
        cols = [
            "id", "game_id", "level_number", "room_id", "goal",
            "description", "strat_version", "reference_time_ms",
            "state_path", "active",
            "estimator", "state_json", "marginal_return",
        ]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db_dashboard.py::TestSchemaMigration tests/test_db_dashboard.py::TestModelStateDB -v`
Expected: PASS

- [ ] **Step 5: Run all existing tests to check nothing is broken**

Run: `pytest tests/ -v`
Expected: Some old tests that depend on `schedule` table or `Schedule` model may fail — that's expected and will be fixed in Task 11.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/db.py tests/test_db_dashboard.py
git commit -m "feat(db): migrate schema — drop schedule, add model_state + allocator_config"
```

---

### Task 11: Clean up old SM-2 references in models.py

**Files:**
- Modify: `python/spinlab/models.py`

- [ ] **Step 1: Remove Rating enum and Schedule dataclass**

In `python/spinlab/models.py`:
- Delete the `Rating` enum (lines 9–25)
- Delete the `Schedule` dataclass (lines 54–92)
- Add `auto_advance_delay_ms` field to `SplitCommand`:

```python
@dataclass
class SplitCommand:
    id: str
    state_path: str
    goal: str
    description: str
    reference_time_ms: int | None
    auto_advance_delay_ms: int = 2000

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "state_path": self.state_path,
            "goal": self.goal,
            "description": self.description,
            "reference_time_ms": self.reference_time_ms,
            "auto_advance_delay_ms": self.auto_advance_delay_ms,
        }
```

- Remove the `difficulty` field from `SplitCommand` (no longer computed from ease_factor).
- Make `rating` optional in `Attempt`:

```python
@dataclass
class Attempt:
    split_id: str
    session_id: str
    completed: bool
    time_ms: int
    goal_matched: bool
    rating: str | None = None  # no longer populated
    strat_version: int = 1
    source: str = "practice"
```

- [ ] **Step 2: Fix any import errors in other files**

Check files that import `Rating` or `Schedule`:
- `scheduler.py` — will be rewritten in Task 12, skip for now
- `orchestrator.py` — imports `Rating`, `Schedule` — remove those imports (full orchestrator rewrite is Task 14)
- `db.py` — imports `Schedule` in `update_schedule` — already removed in Task 10

For now, just fix `models.py`. Other files will be updated in their own tasks.

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/models.py
git commit -m "refactor(models): drop Rating/Schedule, add auto_advance_delay_ms to SplitCommand"
```

---

## Chunk 3: Scheduler Coordinator + Orchestrator

### Task 12: Rewrite scheduler.py as thin coordinator

**Files:**
- Rewrite: `python/spinlab/scheduler.py`
- Create: `tests/test_scheduler_kalman.py`
- Delete: `tests/test_scheduler_peek.py`

- [ ] **Step 1: Write failing tests for the coordinator**

`tests/test_scheduler_kalman.py`:

```python
"""Tests for the scheduler coordinator (estimator + allocator)."""
import json
import pytest
from spinlab.db import Database
from spinlab.scheduler import Scheduler


@pytest.fixture
def db_with_splits(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Game", "any%")
    from spinlab.models import Split
    for i, goal in enumerate(["normal", "key", "secret"], start=1):
        split = Split(
            id=f"g1:{i}:1:{goal}",
            game_id="g1",
            level_number=i,
            room_id=1,
            goal=goal,
            description=f"Level {i}",
            state_path=f"/states/{i}.mss",
            reference_time_ms=10000 + i * 1000,
            strat_version=1,
        )
        db.upsert_split(split)
    return db


class TestSchedulerPickNext:
    def test_pick_next_returns_split_with_model(self, db_with_splits):
        sched = Scheduler(db_with_splits, "g1")
        result = sched.pick_next()
        # No attempts yet, all marginal returns are equal (default d/mu)
        assert result is not None
        assert result.split_id.startswith("g1:")

    def test_pick_next_no_splits_returns_none(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.upsert_game("g1", "Game", "any%")
        sched = Scheduler(db, "g1")
        assert sched.pick_next() is None


class TestSchedulerProcessAttempt:
    def test_process_attempt_creates_model_state(self, db_with_splits):
        sched = Scheduler(db_with_splits, "g1")
        split_id = "g1:1:1:normal"
        sched.process_attempt(split_id, time_ms=12000, completed=True)
        row = db_with_splits.load_model_state(split_id)
        assert row is not None
        state = json.loads(row["state_json"])
        assert state["mu"] == pytest.approx(12.0)  # 12000ms → 12.0s
        assert state["n_completed"] == 1

    def test_process_attempt_incomplete(self, db_with_splits):
        sched = Scheduler(db_with_splits, "g1")
        split_id = "g1:1:1:normal"
        # First: completed attempt to init state
        sched.process_attempt(split_id, time_ms=12000, completed=True)
        # Second: incomplete attempt
        sched.process_attempt(split_id, time_ms=5000, completed=False)
        row = db_with_splits.load_model_state(split_id)
        state = json.loads(row["state_json"])
        assert state["n_completed"] == 1  # unchanged
        assert state["n_attempts"] == 2   # incremented


class TestSchedulerPeek:
    def test_peek_next_n(self, db_with_splits):
        sched = Scheduler(db_with_splits, "g1")
        results = sched.peek_next_n(3)
        assert len(results) == 3
        assert all(isinstance(r, str) for r in results)


class TestSchedulerSwitch:
    def test_switch_allocator(self, db_with_splits):
        sched = Scheduler(db_with_splits, "g1")
        sched.switch_allocator("random")
        assert sched.allocator.name == "random"

    def test_switch_unknown_allocator_raises(self, db_with_splits):
        sched = Scheduler(db_with_splits, "g1")
        with pytest.raises(ValueError):
            sched.switch_allocator("nonexistent")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_scheduler_kalman.py -v`
Expected: FAIL — old `Scheduler` API doesn't match

- [ ] **Step 3: Rewrite scheduler.py**

```python
"""Scheduler coordinator: wires an estimator + allocator together.

Exposes pick_next(), process_attempt(), peek_next_n() to the orchestrator.
Same interface surface as the old SM-2 scheduler, different internals.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from spinlab.allocators import SplitWithModel, get_allocator, list_allocators
from spinlab.allocators.greedy import GreedyAllocator  # ensure registered
from spinlab.allocators.random import RandomAllocator
from spinlab.allocators.round_robin import RoundRobinAllocator
from spinlab.estimators import get_estimator, list_estimators
from spinlab.estimators.kalman import KalmanEstimator  # ensure registered

if TYPE_CHECKING:
    from spinlab.allocators import Allocator
    from spinlab.db import Database
    from spinlab.estimators import Estimator


class Scheduler:
    def __init__(
        self,
        db: "Database",
        game_id: str,
        estimator_name: str = "kalman",
        allocator_name: str = "greedy",
    ) -> None:
        self.db = db
        self.game_id = game_id
        self.estimator: Estimator = get_estimator(estimator_name)
        self.allocator: Allocator = get_allocator(allocator_name)

    def _load_splits_with_model(self) -> list[SplitWithModel]:
        """Load all active splits and hydrate with estimator state."""
        rows = self.db.get_all_splits_with_model(self.game_id)
        all_states = []
        splits = []

        for row in rows:
            state = None
            mr = 0.0
            di = {}
            n_completed = 0
            n_attempts = 0
            gold_ms = None

            if row["state_json"]:
                from spinlab.estimators.kalman import KalmanState

                state = KalmanState.from_dict(json.loads(row["state_json"]))
                mr = self.estimator.marginal_return(state)
                di = self.estimator.drift_info(state)
                n_completed = state.n_completed
                n_attempts = state.n_attempts
                gold_ms = int(state.gold * 1000) if state.gold != float("inf") else None
                all_states.append(state)

            splits.append(
                SplitWithModel(
                    split_id=row["id"],
                    game_id=row["game_id"],
                    level_number=row["level_number"],
                    room_id=row["room_id"],
                    goal=row["goal"],
                    description=row["description"],
                    strat_version=row["strat_version"],
                    reference_time_ms=row["reference_time_ms"],
                    state_path=row["state_path"],
                    active=bool(row["active"]),
                    estimator_state=state,
                    marginal_return=mr,
                    drift_info=di,
                    n_completed=n_completed,
                    n_attempts=n_attempts,
                    gold_ms=gold_ms,
                )
            )
        return splits

    def pick_next(self) -> SplitWithModel | None:
        """Pick next split to practice."""
        splits = self._load_splits_with_model()
        if not splits:
            return None
        split_id = self.allocator.pick_next(splits)
        if split_id is None:
            return None
        return next((s for s in splits if s.split_id == split_id), None)

    def process_attempt(
        self, split_id: str, time_ms: int, completed: bool
    ) -> None:
        """Process a completed or incomplete attempt."""
        observed_time = time_ms / 1000.0 if completed else None

        # Load existing state
        row = self.db.load_model_state(split_id)
        if row and row["state_json"]:
            from spinlab.estimators.kalman import KalmanState

            state = KalmanState.from_dict(json.loads(row["state_json"]))
            state = self.estimator.process_attempt(state, observed_time)
        else:
            if observed_time is not None:
                # First completed attempt — initialize
                # Get population priors
                all_rows = self.db.load_all_model_states(self.game_id)
                from spinlab.estimators.kalman import KalmanState

                all_states = [
                    KalmanState.from_dict(json.loads(r["state_json"]))
                    for r in all_rows
                    if r["state_json"]
                ]
                priors = self.estimator.get_population_priors(all_states)
                state = self.estimator.init_state(observed_time, priors)
            else:
                # First attempt is incomplete — create minimal state
                from spinlab.estimators.kalman import KalmanState

                state = KalmanState(n_attempts=1)
                self.db.save_model_state(
                    split_id,
                    self.estimator.name,
                    json.dumps(state.to_dict()),
                    0.0,
                )
                return

        mr = self.estimator.marginal_return(state)
        self.db.save_model_state(
            split_id, self.estimator.name, json.dumps(state.to_dict()), mr
        )

    def peek_next_n(self, n: int) -> list[str]:
        """Preview next N split IDs."""
        splits = self._load_splits_with_model()
        return self.allocator.peek_next_n(splits, n)

    def get_all_model_states(self) -> list[SplitWithModel]:
        """Get all splits with model state for dashboard."""
        return self._load_splits_with_model()

    def switch_allocator(self, name: str) -> None:
        self.allocator = get_allocator(name)
        self.db.save_allocator_config("allocator", name)

    def switch_estimator(self, name: str) -> None:
        self.estimator = get_estimator(name)
        self.db.save_allocator_config("estimator", name)

    def rebuild_all_states(self) -> None:
        """Replay all attempts to reconstruct model_state table."""
        splits = self.db.get_all_splits_with_model(self.game_id)
        for row in splits:
            split_id = row["id"]
            # Get all attempts for this split, ordered by created_at
            attempts_raw = self.db.get_split_attempts(split_id)
            if not attempts_raw:
                continue
            times = [
                a["time_ms"] / 1000.0 if a["completed"] else None
                for a in attempts_raw
            ]
            state = self.estimator.rebuild_state(times)
            mr = self.estimator.marginal_return(state)
            self.db.save_model_state(
                split_id, self.estimator.name, json.dumps(state.to_dict()), mr
            )
```

- [ ] **Step 4: Add `get_split_attempts` to db.py**

This method is needed by `rebuild_all_states`. Add to `python/spinlab/db.py`:

```python
    def get_split_attempts(self, split_id: str) -> list[dict]:
        """Get all attempts for a split, ordered by created_at."""
        cur = self.conn.execute(
            "SELECT split_id, completed, time_ms, created_at "
            "FROM attempts WHERE split_id = ? ORDER BY created_at",
            (split_id,),
        )
        cols = ["split_id", "completed", "time_ms", "created_at"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
```

- [ ] **Step 5: Delete old test file**

```bash
rm tests/test_scheduler_peek.py
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_scheduler_kalman.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/scheduler.py python/spinlab/db.py tests/test_scheduler_kalman.py
git rm tests/test_scheduler_peek.py
git commit -m "feat(scheduler): rewrite as estimator+allocator coordinator"
```

---

### Task 13: Update config.yaml with new fields

**Files:**
- Modify: `config.yaml`

- [ ] **Step 1: Add estimator, allocator, and auto_advance_delay_s**

Replace the `scheduler:` section:

```yaml
scheduler:
  estimator: kalman
  allocator: greedy
  auto_advance_delay_s: 2.0
```

Remove `algorithm`, `base_interval_minutes`, `auto_rate_passive` (SM-2 specific).

- [ ] **Step 2: Commit**

```bash
git add config.yaml
git commit -m "config: update scheduler section for Kalman + allocator"
```

---

### Task 14: Update orchestrator to drop ratings and auto-advance

**Files:**
- Modify: `python/spinlab/orchestrator.py`

- [ ] **Step 1: Update orchestrator.run()**

Key changes in `python/spinlab/orchestrator.py`:

1. Remove `Rating` and `Schedule` imports. Remove the `scheduler.init_schedules()` call (no longer exists). Update the `seed_db_from_manifest` docstring to remove the schedule reference.

2. Update `write_state_file` to include allocator/estimator info:

```python
def write_state_file(path, session_id, started_at, current_split_id, queue,
                     allocator="greedy", estimator="kalman"):
    state = {
        "session_id": session_id,
        "started_at": started_at,
        "current_split_id": current_split_id,
        "queue": queue,
        "allocator": allocator,
        "estimator": estimator,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    # ... rest unchanged
```

3. In `run()`, update Scheduler instantiation:

```python
    sched_cfg = config.get("scheduler", {})
    scheduler = Scheduler(
        db, game_id,
        estimator_name=sched_cfg.get("estimator", "kalman"),
        allocator_name=sched_cfg.get("allocator", "greedy"),
    )
    auto_advance_delay_s = sched_cfg.get("auto_advance_delay_s", 2.0)
    auto_advance_delay_ms = int(auto_advance_delay_s * 1000)
```

4. Update the SplitCommand construction — `pick_next()` now returns `SplitWithModel`. Build `SplitCommand` from it:

```python
    picked = scheduler.pick_next()
    if picked is None:
        break
    cmd = SplitCommand(
        id=picked.split_id,
        state_path=picked.state_path,
        goal=picked.goal,
        description=picked.description,
        reference_time_ms=picked.reference_time_ms,
        auto_advance_delay_ms=auto_advance_delay_ms,
    )
```

5. After receiving `attempt_result`, drop the rating processing:

```python
    # Old: rating = Rating.from_lua_string(result["rating"])
    #      scheduler.process_rating(split_id, rating)
    # New:
    scheduler.process_attempt(
        result["split_id"],
        time_ms=result["time_ms"],
        completed=result["completed"],
    )
```

6. Update state file writes to include allocator/estimator:

```python
    write_state_file(
        state_file, session_id, started_at, cmd.id,
        scheduler.peek_next_n(3),
        allocator=scheduler.allocator.name,
        estimator=scheduler.estimator.name,
    )
```

- [ ] **Step 2: Run existing orchestrator tests**

Run: `pytest tests/test_orchestrator.py tests/test_orchestrator_state.py -v`
Expected: PASS (these test buffer parsing and state file, which are mostly unchanged)

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/orchestrator.py
git commit -m "feat(orchestrator): drop ratings, use Kalman coordinator, auto-advance"
```

---

## Chunk 4: Lua Changes

### Task 15: Lua — drop RATING state, add RESULT state

**Files:**
- Modify: `lua/spinlab.lua`

- [ ] **Step 1: Replace RATING state with RESULT**

In `lua/spinlab.lua`:

1. Change state constants (around line 62):

```lua
-- Old:
-- PSTATE_RATING  = "RATING"
-- New:
PSTATE_RESULT  = "RESULT"
```

2. Add auto-advance tracking variables (around line 72):

```lua
practice_result_start_ms = 0          -- when RESULT state began
practice_auto_advance_ms = 2000       -- delay before auto-advancing (from load command)
```

3. Remove rating input variables:

```lua
-- Delete: rating_input_last = { ... }
```

4. In `parse_practice_split()` (line 140), add `auto_advance_delay_ms`:

```lua
function parse_practice_split(json_str)
    return {
        id = json_get_str(json_str, "id"),
        state_path = json_get_str(json_str, "state_path"),
        goal = json_get_str(json_str, "goal"),
        description = json_get_str(json_str, "description"),
        reference_time_ms = json_get_num(json_str, "reference_time_ms"),
        auto_advance_delay_ms = json_get_num(json_str, "auto_advance_delay_ms") or 2000,
    }
end
```

5. Delete `check_rating_input()` function entirely (lines 151–170).

6. Update `handle_practice()` (line 364) — replace RATING logic:

```lua
function handle_practice(curr)
    if practice_state == PSTATE_LOADING then
        if not pending_load then
            practice_state = PSTATE_PLAYING
            practice_start_ms = ts_ms()
            died_flag = false
            log("Practice: PLAYING split " .. practice_split.id)
        end

    elseif practice_state == PSTATE_PLAYING then
        -- Death: reload
        if curr.player_anim == 9 and not died_flag then
            died_flag = true
            pending_load = practice_split.state_path
        end
        -- Fresh entrance after death reload
        if died_flag and curr.game_mode == 18 then
            died_flag = false
            practice_start_ms = ts_ms()
        end
        -- Exit detected (clear or abort)
        if prev.exit_mode == 0 and curr.exit_mode ~= 0 then
            practice_elapsed_ms = ts_ms() - practice_start_ms
            local goal = goal_type(curr)
            practice_completed = (goal ~= "abort")
            practice_state = PSTATE_RESULT
            practice_result_start_ms = ts_ms()
            log("Practice: RESULT (" .. goal .. ") — " .. practice_elapsed_ms .. "ms")
        end

    elseif practice_state == PSTATE_RESULT then
        -- Auto-advance after delay
        local elapsed_in_result = ts_ms() - practice_result_start_ms
        if elapsed_in_result >= practice_auto_advance_ms then
            -- Send result to orchestrator
            local result = to_json({
                event = "attempt_result",
                split_id = practice_split.id,
                completed = practice_completed,
                time_ms = math.floor(practice_elapsed_ms),
                goal = practice_split.goal,
            })
            if client then
                client:send(result .. "\n")
            end
            -- Reset state
            practice_state = PSTATE_IDLE
            practice_mode = false
            practice_split = nil
            log("Practice: auto-advanced, sent result")
        end
    end
end
```

7. In the `practice_load` TCP handler (line 482), store the auto-advance delay:

```lua
    practice_auto_advance_ms = practice_split.auto_advance_delay_ms or 2000
```

8. Update `draw_practice_overlay()` — replace RATING display with RESULT:

```lua
-- In the overlay function, replace the RATING section:
    if practice_state == PSTATE_RESULT then
        local label = practice_completed and "Clear!" or "Abort"
        local time_str = ms_to_display(practice_elapsed_ms)
        local ref_str = practice_split.reference_time_ms
            and ms_to_display(practice_split.reference_time_ms)
            or "—"

        draw_text(x, y, label, 0xFF00FF00, bg)
        y = y + 10
        draw_text(x, y, time_str .. " / " .. ref_str, fg, bg)
        y = y + 10

        -- Countdown bar
        local remaining = practice_auto_advance_ms - (ts_ms() - practice_result_start_ms)
        local secs = string.format("%.1f", math.max(0, remaining / 1000))
        draw_text(x, y, "Next in " .. secs .. "s", 0xFF888888, bg)
    end
```

9. Remove the old difficulty color logic from overlay (lines that reference `practice_split.difficulty`).

- [ ] **Step 2: Test manually** (Lua testing is manual per CLAUDE.md)

Launch Mesen2 with the script loaded. Verify:
- Practice mode loads a state
- On exit, shows "Clear! X.Xs / Y.Ys" with countdown
- Auto-advances after delay
- No rating prompt appears
- `attempt_result` JSON no longer contains `rating` field

- [ ] **Step 3: Commit**

```bash
git add lua/spinlab.lua
git commit -m "feat(lua): replace RATING with RESULT, auto-advance, drop R+D-pad input"
```

---

## Chunk 5: Dashboard Rework

### Task 16: Dashboard API updates

**Files:**
- Modify: `python/spinlab/dashboard.py`

- [ ] **Step 1: Update existing endpoints and add new ones**

In `python/spinlab/dashboard.py`, update `create_app()`:

```python
def create_app(db, game_id, state_file):
    from spinlab.scheduler import Scheduler

    app = FastAPI()
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # Lazy-init scheduler for API calls that need it
    _scheduler = None

    def _get_scheduler():
        nonlocal _scheduler
        if _scheduler is None:
            _scheduler = Scheduler(db, game_id)
        return _scheduler

    @app.get("/")
    async def index():
        return FileResponse(static_dir / "index.html")

    @app.get("/api/state")
    async def get_state():
        # Existing logic, but replace schedule fields with model fields
        state = _read_state_file(state_file)
        if state:
            current = db.load_model_state(state.get("current_split_id"))
            # ... build response with estimator/allocator info
        # ... (keep existing reference/idle mode detection)

    @app.get("/api/model")
    async def get_model():
        """All splits with full estimator state for Model tab."""
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
    async def switch_allocator(body: dict):
        name = body.get("name")
        sched = _get_scheduler()
        sched.switch_allocator(name)
        return {"allocator": name}

    @app.post("/api/estimator")
    async def switch_estimator(body: dict):
        name = body.get("name")
        sched = _get_scheduler()
        sched.switch_estimator(name)
        return {"estimator": name}

    @app.get("/api/splits")
    async def get_splits():
        """All splits with model state."""
        rows = db.get_all_splits_with_model(game_id)
        return {"splits": rows}

    @app.get("/api/sessions")
    async def get_sessions():
        return {"sessions": db.get_session_history(game_id)}

    return app
```

- [ ] **Step 2: Run existing dashboard tests**

Run: `pytest tests/test_db_dashboard.py -v`
Expected: PASS (DB layer tests should still work)

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/dashboard.py
git commit -m "feat(dashboard): add /api/model, /api/allocator, /api/estimator endpoints"
```

---

### Task 17: Dashboard frontend — 3-tab layout + Model tab

**Files:**
- Modify: `python/spinlab/static/index.html`
- Modify: `python/spinlab/static/app.js`
- Modify: `python/spinlab/static/style.css`

- [ ] **Step 1: Update index.html with tab structure**

Replace the content of `python/spinlab/static/index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SpinLab</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <header>
    <h1>SpinLab</h1>
    <span id="session-timer" class="dim"></span>
  </header>

  <nav id="tabs">
    <button class="tab active" data-tab="live">Live</button>
    <button class="tab" data-tab="model">Model</button>
    <button class="tab" data-tab="manage">Manage</button>
  </nav>

  <main>
    <!-- Live Tab -->
    <section id="tab-live" class="tab-content active">
      <div id="mode-idle">
        <p class="dim">No active session</p>
      </div>
      <div id="mode-reference" style="display:none">
        <h2>Reference Run</h2>
        <p id="ref-sections">Sections: 0</p>
      </div>
      <div id="mode-practice" style="display:none">
        <div class="card" id="current-split">
          <div class="split-header">
            <span id="current-goal" class="goal-label"></span>
            <span id="current-attempts" class="dim"></span>
          </div>
          <div id="insight" class="insight-card"></div>
        </div>
        <div class="allocator-row">
          <label>Allocator:</label>
          <select id="allocator-select">
            <option value="greedy">Greedy</option>
            <option value="random">Random</option>
            <option value="round_robin">Round Robin</option>
          </select>
        </div>
        <h3>Up Next</h3>
        <ul id="queue"></ul>
        <h3>Recent</h3>
        <ul id="recent"></ul>
        <footer id="session-stats" class="dim"></footer>
      </div>
    </section>

    <!-- Model Tab -->
    <section id="tab-model" class="tab-content">
      <div class="model-header">
        <h2>Model State</h2>
        <select id="estimator-select">
          <option value="kalman">Kalman</option>
        </select>
      </div>
      <table id="model-table">
        <thead>
          <tr>
            <th>Split</th>
            <th>μ (s)</th>
            <th>Drift</th>
            <th>Conf.</th>
            <th>m<sub>i</sub></th>
            <th>Runs</th>
            <th>Gold</th>
          </tr>
        </thead>
        <tbody id="model-body"></tbody>
      </table>
    </section>

    <!-- Manage Tab (placeholder) -->
    <section id="tab-manage" class="tab-content">
      <p class="dim">Coming soon</p>
    </section>
  </main>

  <script src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Update app.js with tab logic and Model tab rendering**

Replace `python/spinlab/static/app.js`:

```javascript
const POLL_MS = 1000;

// === Tab switching ===
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'model') fetchModel();
  });
});

// === Allocator switch ===
document.getElementById('allocator-select').addEventListener('change', async (e) => {
  await fetch('/api/allocator', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: e.target.value }),
  });
});

// === Estimator switch ===
document.getElementById('estimator-select').addEventListener('change', async (e) => {
  await fetch('/api/estimator', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: e.target.value }),
  });
  fetchModel();
});

// === Live tab polling ===
async function poll() {
  try {
    const res = await fetch('/api/state');
    const data = await res.json();
    updateLive(data);
  } catch (_) {}
  setTimeout(poll, POLL_MS);
}

function updateLive(data) {
  const idle = document.getElementById('mode-idle');
  const ref = document.getElementById('mode-reference');
  const practice = document.getElementById('mode-practice');

  if (data.mode === 'practice') {
    idle.style.display = 'none';
    ref.style.display = 'none';
    practice.style.display = 'block';

    const cs = data.current_split;
    if (cs) {
      document.getElementById('current-goal').textContent = cs.goal || '';
      document.getElementById('current-attempts').textContent =
        'Attempt ' + (cs.attempt_count || 0);

      // Insight card
      const insight = document.getElementById('insight');
      if (cs.drift_info) {
        const arrow = cs.drift_info.drift < 0 ? '↓' : cs.drift_info.drift > 0 ? '↑' : '→';
        const rate = Math.abs(cs.drift_info.drift).toFixed(2);
        insight.innerHTML =
          '<span class="drift-' + cs.drift_info.label + '">' +
          arrow + ' ' + rate + ' s/run</span>' +
          ' <span class="dim">(' + cs.drift_info.confidence + ')</span>';
      } else {
        insight.textContent = 'No data yet';
      }
    }

    // Queue
    const queue = document.getElementById('queue');
    queue.innerHTML = '';
    (data.queue || []).forEach(q => {
      const li = document.createElement('li');
      li.textContent = q.description || q.split_id || q;
      queue.appendChild(li);
    });

    // Recent
    const recent = document.getElementById('recent');
    recent.innerHTML = '';
    (data.recent || []).forEach(r => {
      const li = document.createElement('li');
      const time = formatTime(r.time_ms);
      const refTime = r.reference_time_ms ? formatTime(r.reference_time_ms) : '—';
      const cls = r.reference_time_ms && r.time_ms <= r.reference_time_ms ? 'ahead' : 'behind';
      li.innerHTML = '<span class="' + cls + '">' + time + '</span> / ' + refTime +
        ' <span class="dim">' + (r.goal || '') + '</span>';
      recent.appendChild(li);
    });

    // Session stats
    const stats = document.getElementById('session-stats');
    if (data.session) {
      stats.textContent = (data.session.splits_completed || 0) + '/' +
        (data.session.splits_attempted || 0) + ' cleared | ' +
        elapsedStr(data.session.started_at);
    }

    // Allocator dropdown
    if (data.allocator) {
      document.getElementById('allocator-select').value = data.allocator;
    }

  } else if (data.mode === 'reference') {
    idle.style.display = 'none';
    ref.style.display = 'block';
    practice.style.display = 'none';
    document.getElementById('ref-sections').textContent =
      'Sections: ' + (data.sections_captured || 0);

  } else {
    idle.style.display = 'block';
    ref.style.display = 'none';
    practice.style.display = 'none';
  }

  // Session timer
  if (data.session && data.session.started_at) {
    document.getElementById('session-timer').textContent = elapsedStr(data.session.started_at);
  }
}

// === Model tab ===
async function fetchModel() {
  try {
    const res = await fetch('/api/model');
    const data = await res.json();
    updateModel(data);
  } catch (_) {}
}

function updateModel(data) {
  const body = document.getElementById('model-body');
  body.innerHTML = '';
  (data.splits || []).forEach(s => {
    const tr = document.createElement('tr');
    const driftClass = s.drift_info?.label || 'flat';
    const arrow = s.drift !== null
      ? (s.drift < 0 ? '↓' : s.drift > 0 ? '↑' : '→')
      : '—';
    tr.className = 'drift-row-' + driftClass;
    const conf = s.drift_info?.confidence || '—';
    tr.innerHTML =
      '<td>' + (s.description || s.goal) + '</td>' +
      '<td>' + (s.mu !== null ? s.mu.toFixed(1) : '—') + '</td>' +
      '<td class="drift-' + driftClass + '">' + arrow + ' ' +
        (s.drift !== null ? Math.abs(s.drift).toFixed(2) : '—') + '</td>' +
      '<td>' + conf + '</td>' +
      '<td>' + (s.marginal_return ? s.marginal_return.toFixed(4) : '—') + '</td>' +
      '<td>' + s.n_completed + '</td>' +
      '<td>' + (s.gold_ms !== null ? formatTime(s.gold_ms) : '—') + '</td>';
    body.appendChild(tr);
  });

  if (data.estimator) {
    document.getElementById('estimator-select').value = data.estimator;
  }
}

// === Utilities ===
function formatTime(ms) {
  if (ms == null) return '—';
  const s = ms / 1000;
  return s.toFixed(1) + 's';
}

function elapsedStr(startedAt) {
  if (!startedAt) return '';
  const start = new Date(startedAt.endsWith('Z') ? startedAt : startedAt + 'Z');
  const diff = Math.floor((Date.now() - start.getTime()) / 1000);
  const m = Math.floor(diff / 60);
  const s = diff % 60;
  return m + ':' + String(s).padStart(2, '0');
}

// === Init ===
poll();
```

- [ ] **Step 3: Update style.css with tab styles and model table**

Append to `python/spinlab/static/style.css`:

```css
/* Tabs */
nav#tabs {
  display: flex;
  gap: 0;
  border-bottom: 1px solid #0f3460;
  margin-bottom: 12px;
}
.tab {
  background: none;
  border: none;
  color: #8888aa;
  padding: 8px 16px;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  font-size: 14px;
}
.tab.active {
  color: #00d2ff;
  border-bottom-color: #00d2ff;
}
.tab-content { display: none; }
.tab-content.active { display: block; }

/* Model table */
#model-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
#model-table th {
  text-align: left;
  color: #8888aa;
  padding: 4px 8px;
  border-bottom: 1px solid #0f3460;
}
#model-table td {
  padding: 6px 8px;
  border-bottom: 1px solid #16213e;
}

/* Drift colors */
.drift-improving { color: #4caf50; }
.drift-regressing { color: #f44336; }
.drift-flat { color: #8888aa; }

/* Insight card */
.insight-card {
  padding: 8px;
  background: #16213e;
  border-radius: 4px;
  margin-top: 8px;
}

/* Allocator row */
.allocator-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 8px 0;
}
.allocator-row select {
  background: #16213e;
  color: #e0e0e0;
  border: 1px solid #0f3460;
  padding: 4px 8px;
  border-radius: 4px;
}

/* Model header */
.model-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.model-header select {
  background: #16213e;
  color: #e0e0e0;
  border: 1px solid #0f3460;
  padding: 4px 8px;
  border-radius: 4px;
}

/* Result colors */
.ahead { color: #4caf50; }
.behind { color: #f44336; }
```

Also remove old rating/tier CSS classes that reference SM-2 (tier-struggling, tier-normal, tier-strong, rating-again, etc.).

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/static/index.html python/spinlab/static/app.js python/spinlab/static/style.css
git commit -m "feat(dashboard): 3-tab layout, Model tab, allocator dropdown, drop rating UI"
```

---

## Chunk 6: Integration + Cleanup

### Task 18: Fix remaining test breakage

**Files:**
- Modify: `tests/test_db_dashboard.py`
- Modify: `tests/test_orchestrator.py`

- [ ] **Step 1: Update test_db_dashboard.py**

Fix any tests that reference the old `schedule` table, `get_all_splits_with_schedule`, or SM-2 fields:

- Replace `get_all_splits_with_schedule` calls with `get_all_splits_with_model`
- Remove tests for `get_due_splits`, `get_next_due`, `update_schedule`, `reset_schedule`
- Update any assertions checking `ease_factor` or `interval_minutes`

- [ ] **Step 2: Update test_orchestrator.py**

Fix `_parse_attempt_result_from_buffer` test — the expected JSON no longer includes `rating`:

```python
def test_parses_attempt_result(self):
    buf = '{"event":"attempt_result","split_id":"s1","completed":true,"time_ms":12345,"goal":"normal"}\n'
    results = list(_parse_attempt_result_from_buffer(buf))
    assert len(results) == 1
    assert results[0]["split_id"] == "s1"
    assert "rating" not in results[0]
```

- [ ] **Step 3: Run full test suite**

Run: `pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "test: fix all tests for Kalman model migration"
```

---

### Task 19: End-to-end smoke test

This is a manual verification step per CLAUDE.md guidelines.

- [ ] **Step 1: Run all tests**

Run: `pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 2: Manual emulator test**

1. Launch Mesen2 with the Lua script
2. Start the orchestrator: `spinlab practice`
3. Verify: split loads, overlay shows goal + timer
4. Complete a section — verify "Clear!" display with countdown
5. Verify auto-advance to next split after delay
6. Check dashboard Live tab shows current split + insight
7. Check dashboard Model tab shows split with mu/drift/marginal return
8. Switch allocator via dropdown — verify next pick changes

- [ ] **Step 3: Verify no rating prompt appears in Lua overlay**

During practice, confirm:
- No "R+< again R+v hard R+> good R+^ easy" text
- No RATING state (overlay goes PLAYING → RESULT → next split)

- [ ] **Step 4: Commit any remaining fixes**

```bash
git add -A
git commit -m "feat: Kalman model + allocator redesign — complete"
```

---

## Deferred (not in this plan)

These spec items are explicitly deferred to a follow-up:

- **Expandable rows in Model tab** (spec line 308): click-to-expand detail view showing raw P_dd, R, last 5 attempt times. Add after basic Model tab is validated.
- **"Why-picked" reason in Live tab insight card** (spec line 295): requires adding a reason string to the allocator `pick_next` return value. Add when allocator strategies are more varied.
- **Hierarchical prior blending over time** (spec line 80): `weight = min(1, N_i / 20)` blending between local and population R/Q. Currently priors are used at init only. Add when enough splits have data to validate the approach.

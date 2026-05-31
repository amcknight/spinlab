# EMA-Suite Sampler v0 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the per-segment EMA-suite sampler (10 α values × 3 sub-distributions per segment), a live matrix view of its predictions per segment, and an offline replay/test harness to validate it against existing data.

**Architecture:** New Python estimator module that implements the `Estimator` ABC; reuses `EventAttempt`, `DEFAULT_DEATH_PENALTY_MS`, and follows the `death_aware_rolling.py` pattern. New FastAPI route serves the per-segment matrix. New TypeScript component renders the matrix on the per-segment view. Offline replay is a standalone script.

**Tech Stack:** Python 3.11 (dataclasses, ABC, no JAX), FastAPI + Pydantic, TypeScript + Vite + Vitest, Chart.js (for replay plots).

**Spec:** [`docs/superpowers/specs/2026-05-30-em-suite-sampler-design.md`](../specs/2026-05-30-em-suite-sampler-design.md)

---

## Pre-flight (one-time, before any task)

- [ ] **Run full baseline tests.** Per `CLAUDE.md`: every code-changing session begins with a green baseline.

```bash
python -m pytest
```

Expected: all tests pass. If anything is red, stop and surface to Andrew before proceeding.

- [ ] **Confirm frontend builds.** Needed for smoke tests later.

```bash
cd frontend && npm run build && cd ..
```

Expected: build succeeds, output in `python/spinlab/static/`.

---

## Phase A — Sampler core (Python, TDD)

### Task A1: Constants and EMA update primitive

**Files:**
- Create: `python/spinlab/estimators/em_suite_sampler.py`
- Create: `tests/unit/estimators/test_em_suite_sampler.py`

- [ ] **Step A1.1: Write the failing test for `update_ema_array`.**

Create `tests/unit/estimators/test_em_suite_sampler.py`:

```python
"""Unit tests for the EMA-Suite Sampler.

Spec: docs/superpowers/specs/2026-05-30-em-suite-sampler-design.md
"""
import math

import pytest


class TestAlphaGrid:
    def test_grid_has_ten_values_including_endpoints(self):
        from spinlab.estimators.em_suite_sampler import ALPHA_GRID
        assert len(ALPHA_GRID) == 10
        assert ALPHA_GRID[0] == 0.0
        assert ALPHA_GRID[-1] == 1.0
        # Strictly ascending
        assert list(ALPHA_GRID) == sorted(ALPHA_GRID)


class TestUpdateEmaArray:
    def test_seeds_unset_values_at_observation(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, update_ema_array,
        )
        result = update_ema_array([None] * len(ALPHA_GRID), 5.0)
        assert all(v == 5.0 for v in result)

    def test_applies_ema_formula_per_alpha(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, update_ema_array,
        )
        prior = [10.0] * len(ALPHA_GRID)
        result = update_ema_array(prior, 0.0)
        # For each alpha: new = alpha*0 + (1-alpha)*10 = 10*(1-alpha)
        for v, alpha in zip(result, ALPHA_GRID):
            assert math.isclose(v, 10.0 * (1.0 - alpha))

    def test_alpha_zero_never_updates(self):
        from spinlab.estimators.em_suite_sampler import update_ema_array
        result = update_ema_array([10.0, 10.0], 100.0)
        # alpha=0.0 at index 0 means observation is ignored.
        # We can't test full grid without checking shape; just confirm
        # that with alpha=0.0 the value sticks at the prior.
        # NOTE: this test passes through the grid order; index 0 == 0.0.
        assert result[0] == 10.0

    def test_alpha_one_replaces_entirely(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, update_ema_array,
        )
        n = len(ALPHA_GRID)
        result = update_ema_array([10.0] * n, 100.0)
        # alpha=1.0 at the last index → new value is the observation
        assert result[-1] == 100.0
```

- [ ] **Step A1.2: Run the tests; verify they fail.**

```bash
python -m pytest tests/unit/estimators/test_em_suite_sampler.py -v
```

Expected: FAIL with `ModuleNotFoundError: spinlab.estimators.em_suite_sampler`.

- [ ] **Step A1.3: Create the sampler module with constants and `update_ema_array`.**

Create `python/spinlab/estimators/em_suite_sampler.py`:

```python
"""EMA-Suite Sampler — per-segment three-quantity sampler.

See docs/superpowers/specs/2026-05-30-em-suite-sampler-design.md.

Maintains a fixed suite of 10 exponential-moving-average decay rates for each
of three quantities per segment:
- p_die: Bernoulli outcome per attempt (proportion in [0, 1])
- log success_time: gameplay-ms log of successful attempts
- log death_time: gameplay-ms log of fatal attempts

Trend signal = E_fast − E_slow per quantity, in log-space for times and
logit-space for p_die. Predictions are closed-form mean of the geometric
process: E[episode_time] = success_time + (p/(1−p)) * (death_time + reload).
"""
from __future__ import annotations

# Decay-rate grid (locked, strictly ascending). Endpoints (0.0, 1.0) are
# sanity-check anchors that should look obviously broken on the matrix.
ALPHA_GRID: tuple[float, ...] = (
    0.0, 0.01, 0.02, 0.03, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0,
)


def update_ema_array(
    values: list[float | None], observation: float,
) -> list[float | None]:
    """Apply one EMA update to all alphas in the suite, in parallel.

    For each alpha:
      - If existing value is None, seed at observation (no decay yet).
      - Else, value' = alpha * observation + (1 - alpha) * value.

    Returns a new list; does not mutate input.
    """
    return [
        observation if v is None
        else (alpha * observation + (1.0 - alpha) * v)
        for v, alpha in zip(values, ALPHA_GRID)
    ]
```

- [ ] **Step A1.4: Run the tests; verify they pass.**

```bash
python -m pytest tests/unit/estimators/test_em_suite_sampler.py -v
```

Expected: 4 passed.

- [ ] **Step A1.5: Commit.**

```bash
git add python/spinlab/estimators/em_suite_sampler.py tests/unit/estimators/test_em_suite_sampler.py
git commit -m "feat(estimators): add em_suite_sampler module skeleton with EMA update primitive"
```

---

### Task A2: SamplerState container

**Files:**
- Modify: `python/spinlab/estimators/em_suite_sampler.py`
- Modify: `tests/unit/estimators/test_em_suite_sampler.py`

- [ ] **Step A2.1: Write the failing tests for SamplerState.**

Append to `tests/unit/estimators/test_em_suite_sampler.py`:

```python
class TestSamplerState:
    def test_default_state_has_unset_emas_and_zero_counts(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, SamplerState,
        )
        s = SamplerState()
        n = len(ALPHA_GRID)
        assert s.log_success_time_emas == [None] * n
        assert s.log_death_time_emas == [None] * n
        assert s.p_die_emas == [None] * n
        assert s.n_successes == 0
        assert s.n_deaths == 0
        assert s.n_attempts_total == 0

    def test_to_dict_from_dict_roundtrip(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, SamplerState,
        )
        n = len(ALPHA_GRID)
        s = SamplerState(
            log_success_time_emas=[1.0] * n,
            log_death_time_emas=[2.0] * n,
            p_die_emas=[0.3] * n,
            n_successes=5,
            n_deaths=2,
            n_attempts_total=7,
        )
        d = s.to_dict()
        s2 = SamplerState.from_dict(d)
        assert s2 == s

    def test_registered_with_estimator_state(self):
        from spinlab.estimators import EstimatorState
        # Importing the module registers SamplerState
        import spinlab.estimators.em_suite_sampler  # noqa: F401
        # Should be in the registry now (attribute is _state_classes; see
        # EstimatorState.register_state in python/spinlab/estimators/__init__.py)
        assert "em_suite_sampler" in EstimatorState._state_classes  # type: ignore[attr-defined]
```

- [ ] **Step A2.2: Run; verify failure.**

```bash
python -m pytest tests/unit/estimators/test_em_suite_sampler.py::TestSamplerState -v
```

Expected: FAIL — `SamplerState` not importable.

- [ ] **Step A2.3: Add SamplerState to the module.**

Insert the following after `update_ema_array` in `python/spinlab/estimators/em_suite_sampler.py`:

```python
from dataclasses import dataclass, field

from spinlab.estimators import EstimatorState


@dataclass
class SamplerState(EstimatorState):
    """Per-segment EMA-suite state.

    Each EMA array is indexed by ALPHA_GRID position. Values are None until
    the segment observes its first attempt of the matching kind. Subsequent
    attempts apply the normal update rule (see update_ema_array).

    n_successes / n_deaths / n_attempts_total drive the prediction-gate
    check (nil-until-2 of each).
    """

    log_success_time_emas: list[float | None] = field(
        default_factory=lambda: [None] * len(ALPHA_GRID),
    )
    log_death_time_emas: list[float | None] = field(
        default_factory=lambda: [None] * len(ALPHA_GRID),
    )
    p_die_emas: list[float | None] = field(
        default_factory=lambda: [None] * len(ALPHA_GRID),
    )
    n_successes: int = 0
    n_deaths: int = 0
    n_attempts_total: int = 0

    def to_dict(self) -> dict:
        return {
            "log_success_time_emas": list(self.log_success_time_emas),
            "log_death_time_emas": list(self.log_death_time_emas),
            "p_die_emas": list(self.p_die_emas),
            "n_successes": self.n_successes,
            "n_deaths": self.n_deaths,
            "n_attempts_total": self.n_attempts_total,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SamplerState":
        return cls(
            log_success_time_emas=list(d["log_success_time_emas"]),
            log_death_time_emas=list(d["log_death_time_emas"]),
            p_die_emas=list(d["p_die_emas"]),
            n_successes=d["n_successes"],
            n_deaths=d["n_deaths"],
            n_attempts_total=d["n_attempts_total"],
        )


EstimatorState.register_state("em_suite_sampler", SamplerState)
```

- [ ] **Step A2.4: Run; verify pass.**

```bash
python -m pytest tests/unit/estimators/test_em_suite_sampler.py::TestSamplerState -v
```

Expected: 3 passed.

- [ ] **Step A2.5: Commit.**

```bash
git add python/spinlab/estimators/em_suite_sampler.py tests/unit/estimators/test_em_suite_sampler.py
git commit -m "feat(estimators): add SamplerState container with to_dict/from_dict roundtrip"
```

---

### Task A3: process_event — per-attempt EMA dispatch

**Files:**
- Modify: `python/spinlab/estimators/em_suite_sampler.py`
- Modify: `tests/unit/estimators/test_em_suite_sampler.py`

- [ ] **Step A3.1: Write the failing tests for `process_event`.**

Append to `tests/unit/estimators/test_em_suite_sampler.py`:

```python
class TestProcessEvent:
    def test_success_updates_success_time_and_p_die_only(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        ev = make_event_attempt(outcome="survived", time_ms=20_000)
        new_state = process_event(state, ev)
        # success_time and p_die seeded; death_time unchanged
        assert all(v is not None for v in new_state.log_success_time_emas)
        assert all(v is not None for v in new_state.p_die_emas)
        assert all(v is None for v in new_state.log_death_time_emas)
        assert new_state.n_successes == 1
        assert new_state.n_deaths == 0
        assert new_state.n_attempts_total == 1
        # outcome bit for success = 0 (death=1)
        assert new_state.p_die_emas[5] == 0.0

    def test_death_updates_death_time_and_p_die_only(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        ev = make_event_attempt(outcome="died", time_ms=5_000)
        new_state = process_event(state, ev)
        assert all(v is not None for v in new_state.log_death_time_emas)
        assert all(v is not None for v in new_state.p_die_emas)
        assert all(v is None for v in new_state.log_success_time_emas)
        assert new_state.n_successes == 0
        assert new_state.n_deaths == 1
        # outcome bit for death = 1
        assert new_state.p_die_emas[5] == 1.0

    def test_invalidated_event_does_not_update_state(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        ev = make_event_attempt(outcome="survived", time_ms=20_000, invalidated=True)
        new_state = process_event(state, ev)
        assert new_state == state

    def test_log_time_stored_in_log_space(self):
        import math

        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        ev = make_event_attempt(outcome="survived", time_ms=20_000)
        new_state = process_event(state, ev)
        # Seeded value is log(20000)
        expected = math.log(20_000)
        assert math.isclose(new_state.log_success_time_emas[5], expected)

    def test_two_successes_apply_ema_update_on_second(self):
        import math

        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, SamplerState, process_event,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        state = process_event(
            state, make_event_attempt(outcome="survived", time_ms=10_000),
        )
        state = process_event(
            state, make_event_attempt(outcome="survived", time_ms=40_000),
        )
        # At alpha=0.5: new = 0.5*log(40000) + 0.5*log(10000)
        idx = ALPHA_GRID.index(0.5)
        expected = 0.5 * math.log(40_000) + 0.5 * math.log(10_000)
        assert math.isclose(state.log_success_time_emas[idx], expected)
        assert state.n_successes == 2
        assert state.n_attempts_total == 2
```

- [ ] **Step A3.2: Run; verify failure.**

```bash
python -m pytest tests/unit/estimators/test_em_suite_sampler.py::TestProcessEvent -v
```

Expected: FAIL — `process_event` not defined.

- [ ] **Step A3.3: Add `process_event` to the module.**

Insert the following in `python/spinlab/estimators/em_suite_sampler.py` after the `EstimatorState.register_state(...)` line:

```python
import math

from spinlab.models import EventAttempt


def process_event(state: SamplerState, event: EventAttempt) -> SamplerState:
    """Update sampler state with one observed event.

    - Invalidated events are no-ops.
    - p_die updates on every attempt (outcome_bit = 1 if died else 0).
    - The matching time EMA updates only on attempts of that outcome.
    - n_attempts_total / n_successes / n_deaths advance accordingly.

    Returns a new state; does not mutate the input.
    """
    if event.invalidated:
        return state

    is_death = event.outcome.value == "died"
    outcome_bit = 1.0 if is_death else 0.0
    log_time = math.log(max(event.time_ms, 1))

    new_p_die = update_ema_array(state.p_die_emas, outcome_bit)
    if is_death:
        new_log_death = update_ema_array(state.log_death_time_emas, log_time)
        new_log_success = state.log_success_time_emas
        new_n_deaths = state.n_deaths + 1
        new_n_successes = state.n_successes
    else:
        new_log_success = update_ema_array(state.log_success_time_emas, log_time)
        new_log_death = state.log_death_time_emas
        new_n_successes = state.n_successes + 1
        new_n_deaths = state.n_deaths

    return SamplerState(
        log_success_time_emas=new_log_success,
        log_death_time_emas=new_log_death,
        p_die_emas=new_p_die,
        n_successes=new_n_successes,
        n_deaths=new_n_deaths,
        n_attempts_total=state.n_attempts_total + 1,
    )
```

- [ ] **Step A3.4: Run; verify pass.**

```bash
python -m pytest tests/unit/estimators/test_em_suite_sampler.py::TestProcessEvent -v
```

Expected: 5 passed.

- [ ] **Step A3.5: Commit.**

```bash
git add python/spinlab/estimators/em_suite_sampler.py tests/unit/estimators/test_em_suite_sampler.py
git commit -m "feat(estimators): add process_event for per-attempt EMA dispatch"
```

---

### Task A4: trend_signal_slopes — gated slope computation

**Files:**
- Modify: `python/spinlab/estimators/em_suite_sampler.py`
- Modify: `tests/unit/estimators/test_em_suite_sampler.py`

- [ ] **Step A4.1: Write the failing tests.**

Append to `tests/unit/estimators/test_em_suite_sampler.py`:

```python
class TestTrendSignalSlopes:
    def test_returns_none_when_gate_fails_n_successes(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event, trend_signal_slopes,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        # 1 success + 2 deaths → fails n_successes >= 2
        state = process_event(state, make_event_attempt(outcome="survived", time_ms=20_000))
        state = process_event(state, make_event_attempt(outcome="died", time_ms=5_000))
        state = process_event(state, make_event_attempt(outcome="died", time_ms=4_000))
        assert trend_signal_slopes(state, fast_idx=5, slow_idx=2) is None

    def test_returns_none_when_gate_fails_n_deaths(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event, trend_signal_slopes,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        # 2 successes + 1 death → fails n_deaths >= 2
        for _ in range(2):
            state = process_event(
                state, make_event_attempt(outcome="survived", time_ms=20_000),
            )
        state = process_event(state, make_event_attempt(outcome="died", time_ms=5_000))
        assert trend_signal_slopes(state, fast_idx=5, slow_idx=2) is None

    def test_returns_slopes_when_gate_passes(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event, trend_signal_slopes,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        for t in (10_000, 8_000):
            state = process_event(
                state, make_event_attempt(outcome="survived", time_ms=t),
            )
        for t in (5_000, 4_000):
            state = process_event(
                state, make_event_attempt(outcome="died", time_ms=t),
            )
        slopes = trend_signal_slopes(state, fast_idx=8, slow_idx=2)
        assert slopes is not None
        slope_log_success, slope_log_death, slope_logit_p_die = slopes
        # All three should be floats
        assert isinstance(slope_log_success, float)
        assert isinstance(slope_log_death, float)
        assert isinstance(slope_logit_p_die, float)
```

- [ ] **Step A4.2: Run; verify failure.**

```bash
python -m pytest tests/unit/estimators/test_em_suite_sampler.py::TestTrendSignalSlopes -v
```

Expected: FAIL — `trend_signal_slopes` not defined.

- [ ] **Step A4.3: Add slope computation to the module.**

Insert in `python/spinlab/estimators/em_suite_sampler.py` (after `process_event`):

```python
# Numerical defense for logit at the [0, 1] edges from same-outcome streaks.
LOGIT_EPS = 1e-6


def _logit(p: float) -> float:
    clamped = max(LOGIT_EPS, min(1.0 - LOGIT_EPS, p))
    return math.log(clamped / (1.0 - clamped))


def _logistic(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _gate_passes(state: SamplerState) -> bool:
    return (
        state.n_successes >= 2
        and state.n_deaths >= 2
        and state.n_attempts_total >= 2
    )


def trend_signal_slopes(
    state: SamplerState, fast_idx: int, slow_idx: int,
) -> tuple[float, float, float] | None:
    """Compute (slope_log_success, slope_log_death, slope_logit_p_die).

    Returns None when the prediction gate fails (nil-until-2 of each kind).

    Each slope is E_fast - E_slow, in log-space for times and logit-space
    for p_die.
    """
    if not _gate_passes(state):
        return None
    s_fast = state.log_success_time_emas[fast_idx]
    s_slow = state.log_success_time_emas[slow_idx]
    d_fast = state.log_death_time_emas[fast_idx]
    d_slow = state.log_death_time_emas[slow_idx]
    p_fast = state.p_die_emas[fast_idx]
    p_slow = state.p_die_emas[slow_idx]
    assert s_fast is not None and s_slow is not None
    assert d_fast is not None and d_slow is not None
    assert p_fast is not None and p_slow is not None
    return (
        s_fast - s_slow,
        d_fast - d_slow,
        _logit(p_fast) - _logit(p_slow),
    )
```

- [ ] **Step A4.4: Run; verify pass.**

```bash
python -m pytest tests/unit/estimators/test_em_suite_sampler.py::TestTrendSignalSlopes -v
```

Expected: 3 passed.

- [ ] **Step A4.5: Commit.**

```bash
git add python/spinlab/estimators/em_suite_sampler.py tests/unit/estimators/test_em_suite_sampler.py
git commit -m "feat(estimators): add gated trend signal computation"
```

---

### Task A5: expected_episode_time_ms — closed-form geometric mean

**Files:**
- Modify: `python/spinlab/estimators/em_suite_sampler.py`
- Modify: `tests/unit/estimators/test_em_suite_sampler.py`

- [ ] **Step A5.1: Write the failing tests.**

Append to `tests/unit/estimators/test_em_suite_sampler.py`:

```python
class TestExpectedEpisodeTime:
    def _populated_state(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event,
        )
        from tests.factories import make_event_attempt
        state = SamplerState()
        # 3 successes around 20s, 3 deaths around 5s
        for t in (20_000, 21_000, 19_000):
            state = process_event(
                state, make_event_attempt(outcome="survived", time_ms=t),
            )
        for t in (5_000, 4_500, 5_500):
            state = process_event(
                state, make_event_attempt(outcome="died", time_ms=t),
            )
        return state

    def test_returns_none_when_gate_fails(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, expected_episode_time_ms,
        )
        state = SamplerState()  # no attempts yet
        assert expected_episode_time_ms(state, 5, 2, apply_slope=False) is None
        assert expected_episode_time_ms(state, 5, 2, apply_slope=True) is None

    def test_baseline_matches_geometric_formula(self):
        import math

        from spinlab.estimators.em_suite_sampler import (
            DEFAULT_DEATH_PENALTY_MS, expected_episode_time_ms,
        )
        state = self._populated_state()
        fast_idx = 8  # alpha=0.5
        result = expected_episode_time_ms(
            state, fast_idx, fast_idx, apply_slope=False,
        )
        assert result is not None
        # Reconstruct the formula to compare
        s = math.exp(state.log_success_time_emas[fast_idx])
        d = math.exp(state.log_death_time_emas[fast_idx])
        p = state.p_die_emas[fast_idx]
        expected = s + (p / (1.0 - p)) * (d + DEFAULT_DEATH_PENALTY_MS)
        assert math.isclose(result, expected, rel_tol=1e-9)

    def test_slope_shifts_prediction(self):
        from spinlab.estimators.em_suite_sampler import (
            expected_episode_time_ms,
        )
        state = self._populated_state()
        baseline = expected_episode_time_ms(
            state, 8, 2, apply_slope=False,
        )
        sloped = expected_episode_time_ms(
            state, 8, 2, apply_slope=True,
        )
        assert baseline is not None
        assert sloped is not None
        # The two should differ unless the data is exactly stationary
        assert not math.isclose(baseline, sloped, rel_tol=1e-9)

    def test_returns_none_when_p_die_too_close_to_one(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event, expected_episode_time_ms,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        # Make sure both gates pass minimally
        for t in (20_000, 21_000):
            state = process_event(
                state, make_event_attempt(outcome="survived", time_ms=t),
            )
        # Manually mark p_die as effectively 1 at index 8 to simulate
        # an all-death streak
        from spinlab.estimators.em_suite_sampler import ALPHA_GRID
        state.p_die_emas[8] = 1.0 - 1e-9
        for t in (5_000, 4_500):
            state = process_event(
                state, make_event_attempt(outcome="died", time_ms=t),
            )
        # n_successes=2, n_deaths=2 (gate passes), p ≈ 1 → diverges
        assert expected_episode_time_ms(state, 8, 2, apply_slope=False) is None
```

- [ ] **Step A5.2: Run; verify failure.**

```bash
python -m pytest tests/unit/estimators/test_em_suite_sampler.py::TestExpectedEpisodeTime -v
```

Expected: FAIL — `expected_episode_time_ms` not defined.

- [ ] **Step A5.3: Add the closed-form computation.**

Insert in `python/spinlab/estimators/em_suite_sampler.py` (after `trend_signal_slopes`):

```python
from spinlab.models import DEFAULT_DEATH_PENALTY_MS


def expected_episode_time_ms(
    state: SamplerState, fast_idx: int, slow_idx: int,
    *, apply_slope: bool,
    reload_penalty_ms: int = DEFAULT_DEATH_PENALTY_MS,
) -> float | None:
    """Closed-form mean of the geometric episode-time process.

    Formula:
      E[episode] = success_time + (p / (1 - p)) * (death_time + reload)

    Where:
      success_time = exp(log_E_fast_success [+ slope_log_success if apply_slope])
      death_time   = exp(log_E_fast_death   [+ slope_log_death   if apply_slope])
      p            = p_E_fast               [shifted in logit space if apply_slope]

    Returns None when the prediction gate fails or when p is too close to 1
    (the geometric mean diverges as p → 1).
    """
    if not _gate_passes(state):
        return None

    s_fast = state.log_success_time_emas[fast_idx]
    d_fast = state.log_death_time_emas[fast_idx]
    p_fast = state.p_die_emas[fast_idx]
    assert s_fast is not None and d_fast is not None and p_fast is not None

    if apply_slope:
        slopes = trend_signal_slopes(state, fast_idx, slow_idx)
        assert slopes is not None  # gate already passed
        slope_log_success, slope_log_death, slope_logit_p_die = slopes
        success_time = math.exp(s_fast + slope_log_success)
        death_time = math.exp(d_fast + slope_log_death)
        p = _logistic(_logit(p_fast) + slope_logit_p_die)
    else:
        success_time = math.exp(s_fast)
        death_time = math.exp(d_fast)
        p = p_fast

    if p >= 1.0 - LOGIT_EPS:
        return None  # geometric mean diverges
    return success_time + (p / (1.0 - p)) * (death_time + reload_penalty_ms)
```

- [ ] **Step A5.4: Run; verify pass.**

```bash
python -m pytest tests/unit/estimators/test_em_suite_sampler.py::TestExpectedEpisodeTime -v
```

Expected: 4 passed (or 5 — the first one ran already).

- [ ] **Step A5.5: Commit.**

```bash
git add python/spinlab/estimators/em_suite_sampler.py tests/unit/estimators/test_em_suite_sampler.py
git commit -m "feat(estimators): add closed-form geometric mean episode-time prediction"
```

---

### Task A6: build_matrix — full upper-triangular grid

**Files:**
- Modify: `python/spinlab/estimators/em_suite_sampler.py`
- Modify: `tests/unit/estimators/test_em_suite_sampler.py`

- [ ] **Step A6.1: Write the failing tests.**

Append to `tests/unit/estimators/test_em_suite_sampler.py`:

```python
class TestBuildMatrix:
    def _populated_state(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event,
        )
        from tests.factories import make_event_attempt
        state = SamplerState()
        for t in (20_000, 21_000, 19_000):
            state = process_event(
                state, make_event_attempt(outcome="survived", time_ms=t),
            )
        for t in (5_000, 4_500, 5_500):
            state = process_event(
                state, make_event_attempt(outcome="died", time_ms=t),
            )
        return state

    def test_matrix_shape_and_alpha_grid(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, build_matrix,
        )
        result = build_matrix(self._populated_state())
        n = len(ALPHA_GRID)
        assert result["alpha_grid"] == list(ALPHA_GRID)
        assert len(result["baseline"]) == n
        assert len(result["matrix"]) == n
        assert all(len(row) == n for row in result["matrix"])

    def test_matrix_is_upper_triangular(self):
        # Convention: cell [fast_idx][slow_idx] is non-None iff fast_idx > slow_idx.
        # Diagonal and below are None.
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, build_matrix,
        )
        result = build_matrix(self._populated_state())
        n = len(ALPHA_GRID)
        for fast_idx in range(n):
            for slow_idx in range(n):
                if slow_idx >= fast_idx:
                    assert result["matrix"][fast_idx][slow_idx] is None, (
                        f"cell [{fast_idx}][{slow_idx}] should be None (fast<=slow)"
                    )
                else:
                    # Non-None iff the gate passes for this state
                    assert result["matrix"][fast_idx][slow_idx] is not None

    def test_baseline_contains_no_slope_predictions(self):
        import math

        from spinlab.estimators.em_suite_sampler import (
            build_matrix, expected_episode_time_ms,
        )
        state = self._populated_state()
        result = build_matrix(state)
        for fast_idx, baseline_value in enumerate(result["baseline"]):
            expected = expected_episode_time_ms(
                state, fast_idx, fast_idx, apply_slope=False,
            )
            assert (baseline_value is None and expected is None) or (
                baseline_value is not None
                and expected is not None
                and math.isclose(baseline_value, expected, rel_tol=1e-9)
            )

    def test_returns_none_for_empty_state(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, build_matrix,
        )
        result = build_matrix(SamplerState())
        assert all(v is None for v in result["baseline"])
        assert all(all(v is None for v in row) for row in result["matrix"])
```

- [ ] **Step A6.2: Run; verify failure.**

```bash
python -m pytest tests/unit/estimators/test_em_suite_sampler.py::TestBuildMatrix -v
```

Expected: FAIL — `build_matrix` not defined.

- [ ] **Step A6.3: Add the matrix builder.**

Insert in `python/spinlab/estimators/em_suite_sampler.py` (after `expected_episode_time_ms`):

```python
def build_matrix(
    state: SamplerState, *,
    reload_penalty_ms: int = DEFAULT_DEATH_PENALTY_MS,
) -> dict:
    """Compute the full per-segment prediction matrix.

    Returns a dict with:
      - alpha_grid: list[float]    — the suite's alpha values in order
      - baseline: list[float|None] — sample(0) per alpha (no slope; one number per row)
      - matrix: list[list[float|None]] — sample(1) per (fast_idx, slow_idx).
        Upper-triangular: cell [fast][slow] is non-None iff fast > slow.

    None cells appear when either the prediction gate fails (insufficient data)
    or when fast_idx <= slow_idx.
    """
    n = len(ALPHA_GRID)
    baseline: list[float | None] = []
    matrix: list[list[float | None]] = []
    for fast_idx in range(n):
        baseline.append(
            expected_episode_time_ms(
                state, fast_idx, fast_idx, apply_slope=False,
                reload_penalty_ms=reload_penalty_ms,
            )
        )
        row: list[float | None] = []
        for slow_idx in range(n):
            if slow_idx >= fast_idx:
                row.append(None)
            else:
                row.append(
                    expected_episode_time_ms(
                        state, fast_idx, slow_idx, apply_slope=True,
                        reload_penalty_ms=reload_penalty_ms,
                    )
                )
        matrix.append(row)
    return {
        "alpha_grid": list(ALPHA_GRID),
        "baseline": baseline,
        "matrix": matrix,
    }
```

- [ ] **Step A6.4: Run; verify pass.**

```bash
python -m pytest tests/unit/estimators/test_em_suite_sampler.py::TestBuildMatrix -v
```

Expected: 4 passed.

- [ ] **Step A6.5: Commit.**

```bash
git add python/spinlab/estimators/em_suite_sampler.py tests/unit/estimators/test_em_suite_sampler.py
git commit -m "feat(estimators): add build_matrix for upper-triangular prediction grid"
```

---

### Task A7: Estimator ABC integration + registration

**Files:**
- Modify: `python/spinlab/estimators/em_suite_sampler.py`
- Modify: `tests/unit/estimators/test_em_suite_sampler.py`

- [ ] **Step A7.1: Write the failing tests.**

Append to `tests/unit/estimators/test_em_suite_sampler.py`:

```python
class TestEstimatorIntegration:
    def test_registered_in_estimator_registry(self):
        from spinlab.estimators import list_estimators, get_estimator
        assert "em_suite_sampler" in list_estimators()
        est = get_estimator("em_suite_sampler")
        assert est.name == "em_suite_sampler"
        assert est.display_name == "EMA-Suite Sampler"

    def test_declared_params_is_empty(self):
        from spinlab.estimators import get_estimator
        est = get_estimator("em_suite_sampler")
        assert est.declared_params() == []

    def test_rebuild_state_replays_all_events(self):
        from spinlab.estimators import get_estimator
        from tests.factories import make_event_attempt

        events = [
            make_event_attempt(outcome="survived", time_ms=20_000),
            make_event_attempt(outcome="died", time_ms=5_000),
            make_event_attempt(outcome="survived", time_ms=21_000),
            make_event_attempt(outcome="died", time_ms=4_500),
        ]
        est = get_estimator("em_suite_sampler")
        state = est.rebuild_state(attempts=[], events=events)
        assert state.n_attempts_total == 4
        assert state.n_successes == 2
        assert state.n_deaths == 2

    def test_init_state_returns_empty(self):
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record

        est = get_estimator("em_suite_sampler")
        state = est.init_state(
            first_attempt=make_attempt_record(time_ms=10_000, completed=True),
            priors={},
        )
        assert state.n_attempts_total == 0

    def test_model_output_returns_none_estimates_for_now(self):
        from spinlab.estimators import get_estimator
        from tests.factories import make_event_attempt

        events = [
            make_event_attempt(outcome="survived", time_ms=20_000),
            make_event_attempt(outcome="died", time_ms=5_000),
        ]
        est = get_estimator("em_suite_sampler")
        state = est.rebuild_state(attempts=[], events=events)
        output = est.model_output(state, all_attempts=[], events=events)
        # v0: this estimator does not drive the legacy expected_ms display;
        # the matrix is served via a dedicated endpoint.
        assert output.total.expected_ms is None
        assert output.clean.expected_ms is None
```

- [ ] **Step A7.2: Run; verify failure.**

```bash
python -m pytest tests/unit/estimators/test_em_suite_sampler.py::TestEstimatorIntegration -v
```

Expected: FAIL — `em_suite_sampler` not registered.

- [ ] **Step A7.3: Add the estimator class and register it.**

Append to `python/spinlab/estimators/em_suite_sampler.py`:

```python
from spinlab.estimators import (
    Estimator, ParamDef, register_estimator,
)
from spinlab.models import AttemptRecord, Estimate, ModelOutput


@register_estimator
class EmSuiteSamplerEstimator(Estimator):
    """ABC adapter — registers the EMA-suite sampler with the estimator pipeline.

    v0: this estimator does NOT populate ModelOutput.total/clean
    (it doesn't drive the legacy expected_ms display in the existing UI).
    The per-segment matrix is served via a dedicated route. ModelOutput
    fields are kept None to make this explicit.

    Why register at all: rebuild_state is the canonical entry point for
    replaying historical events through the sampler — used by both the
    matrix endpoint and the offline replay script.
    """

    name = "em_suite_sampler"
    display_name = "EMA-Suite Sampler"

    def declared_params(self) -> list[ParamDef]:
        # No tunable params for v0; the alpha suite is fixed.
        return []

    def init_state(
        self, first_attempt: AttemptRecord, priors: dict,
        params: dict | None = None,
    ) -> SamplerState:
        return SamplerState()

    def process_attempt(  # type: ignore[override]
        self, state: SamplerState, new_attempt: AttemptRecord,
        all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list[EventAttempt] | None = None,
    ) -> SamplerState:
        # Rebuild from full event list on every call. Matches the
        # death_aware_rolling pattern: state contains no history, all
        # computation is on the event log.
        if events is None:
            return state
        return self.rebuild_state(all_attempts, params=params, events=events)

    def model_output(  # type: ignore[override]
        self, state: SamplerState, all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list[EventAttempt] | None = None,
    ) -> ModelOutput:
        none_estimate = Estimate(
            expected_ms=None, ms_per_attempt=None, floor_ms=None,
        )
        return ModelOutput(total=none_estimate, clean=none_estimate, extras=None)

    def rebuild_state(  # type: ignore[override]
        self, attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list[EventAttempt] | None = None,
    ) -> SamplerState:
        # Event-level EMAs come from replaying events.
        state = SamplerState()
        if events is not None:
            for event in events:
                state = process_event(state, event)
        # Episode-level counters (n_completed/n_attempts) come from the
        # AttemptRecord list — these are the fields the scheduler reads
        # generically, and they're at episode (not event) granularity.
        # Pattern matches DeathAwareRollingEstimator.rebuild_state.
        state.n_completed = sum(1 for a in attempts if a.completed)
        state.n_attempts = len(attempts)
        return state
```

- [ ] **Step A7.4: Run; verify pass.**

```bash
python -m pytest tests/unit/estimators/test_em_suite_sampler.py::TestEstimatorIntegration -v
```

Expected: 5 passed.

- [ ] **Step A7.5: Run full sampler tests + lint.**

```bash
python -m pytest tests/unit/estimators/test_em_suite_sampler.py -v
ruff check python/spinlab/estimators/em_suite_sampler.py
npx pyright python/spinlab/estimators/em_suite_sampler.py
```

Expected: all sampler tests pass; ruff clean; pyright shows no new errors.

- [ ] **Step A7.6: Commit.**

```bash
git add python/spinlab/estimators/em_suite_sampler.py tests/unit/estimators/test_em_suite_sampler.py
git commit -m "feat(estimators): integrate EmSuiteSamplerEstimator with the ABC + registry"
```

---

## Phase B — Live matrix UI (API + frontend)

### Task B1: Pydantic schema for the matrix response

**Files:**
- Modify: `python/spinlab/api_schemas.py`

- [ ] **Step B1.1: Find the right location in `api_schemas.py`.**

```bash
python -c "import inspect; import spinlab.api_schemas as s; print(inspect.getsourcefile(s))"
```

- [ ] **Step B1.2: Add the matrix response schema.**

Append the following block near other segment-scoped schemas in `python/spinlab/api_schemas.py`:

```python
class EmSuiteMatrixResponse(BaseModel):
    """Per-segment prediction matrix served by /api/segments/{id}/em-suite-matrix.

    The matrix is upper triangular: matrix[fast_idx][slow_idx] is non-None
    iff fast_idx > slow_idx. Diagonals are reserved for the baseline row
    (no slope, one number per alpha).

    All times are milliseconds. None values mean either:
      - insufficient data (n_successes/n_deaths/n_attempts < 2), or
      - geometric mean diverges (p_die at the suite alpha is ~ 1).
    """

    segment_id: str
    alpha_grid: list[float]
    baseline: list[float | None]
    matrix: list[list[float | None]]
    n_attempts_total: int
    n_successes: int
    n_deaths: int
```

(`BaseModel` is already imported in `api_schemas.py`; if not, add `from pydantic import BaseModel` at the top.)

- [ ] **Step B1.3: Regenerate frontend types.**

```bash
cd frontend && npm run gen-types && cd ..
```

Expected: `frontend/openapi.json` regenerates, `frontend/src/api-types.ts` updates.

- [ ] **Step B1.4: Verify the type appears.**

```bash
grep "EmSuiteMatrixResponse" frontend/src/api-types.ts
```

Expected: one or more matches.

- [ ] **Step B1.5: Commit.**

```bash
git add python/spinlab/api_schemas.py frontend/openapi.json frontend/src/api-types.ts
git commit -m "feat(api): add EmSuiteMatrixResponse schema + regenerate frontend types"
```

---

### Task B2: GET endpoint for the matrix

**Files:**
- Modify: `python/spinlab/routes/model.py` (or wherever segment-scoped GETs live; verify with `grep`)
- Create: `tests/unit/routes/test_em_suite_matrix.py`

- [ ] **Step B2.1: Locate the right route module.**

```bash
grep -rn "segments/{segment_id}" python/spinlab/routes/ | head -10
```

Use the same module that hosts segment-scoped reads (likely `routes/model.py`). The new endpoint joins that file.

- [ ] **Step B2.2: Write the failing endpoint test.**

Create `tests/unit/routes/test_em_suite_matrix.py`:

```python
"""Tests for the /api/segments/{id}/em-suite-matrix endpoint."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(test_app):  # uses the existing test_app fixture from conftest
    return TestClient(test_app)


class TestEmSuiteMatrixEndpoint:
    def test_returns_404_for_unknown_segment(self, client):
        resp = client.get("/api/segments/does-not-exist/em-suite-matrix")
        assert resp.status_code == 404

    def test_returns_empty_matrix_for_segment_with_no_events(
        self, client, seeded_segment_no_events,
    ):
        seg_id = seeded_segment_no_events
        resp = client.get(f"/api/segments/{seg_id}/em-suite-matrix")
        assert resp.status_code == 200
        body = resp.json()
        assert body["segment_id"] == seg_id
        assert body["n_attempts_total"] == 0
        assert all(v is None for v in body["baseline"])
        for row in body["matrix"]:
            assert all(v is None for v in row)

    def test_returns_populated_matrix_after_enough_events(
        self, client, seeded_segment_with_events,
    ):
        seg_id, _ = seeded_segment_with_events
        resp = client.get(f"/api/segments/{seg_id}/em-suite-matrix")
        assert resp.status_code == 200
        body = resp.json()
        assert body["n_attempts_total"] >= 4
        # At least one cell should be non-None
        assert any(
            v is not None
            for row in body["matrix"]
            for v in row
        )
```

**Implementation note:** The fixtures `seeded_segment_no_events` and `seeded_segment_with_events` may need to be created if they don't already exist in `tests/conftest.py`. Inspect the existing `test_app` fixture and segment-related fixtures first; reuse patterns from `tests/unit/routes/test_history.py` (or similar). If a sibling test already creates segments + events via DB inserts, mirror it.

- [ ] **Step B2.3: Run; verify failure.**

```bash
python -m pytest tests/unit/routes/test_em_suite_matrix.py -v
```

Expected: FAIL — endpoint not defined.

- [ ] **Step B2.4: Implement the endpoint.**

Add to the chosen routes module (e.g. `python/spinlab/routes/model.py`):

```python
from spinlab.api_schemas import EmSuiteMatrixResponse
from spinlab.estimators import get_estimator


@router.get(
    "/segments/{segment_id}/em-suite-matrix",
    response_model=EmSuiteMatrixResponse,
)
def get_em_suite_matrix(
    segment_id: str,
    db: Database = Depends(get_db),
) -> EmSuiteMatrixResponse:
    """Return the per-segment EMA-suite prediction matrix.

    Replays the segment's event log through the EmSuiteSamplerEstimator
    and computes the closed-form geometric mean for each (alpha_fast,
    alpha_slow) pair. See docs/superpowers/specs/2026-05-30-em-suite-sampler-design.md.
    """
    seg = db.get_segment_by_id(segment_id)
    if seg is None:
        raise HTTPException(
            status_code=404, detail=f"Segment not found: {segment_id}",
        )

    event_rows = db.get_segment_event_rows(segment_id)
    events = _events_from_rows(event_rows)

    est = get_estimator("em_suite_sampler")
    state = est.rebuild_state(attempts=[], events=events)

    # Inline import to avoid pulling sampler internals into routes header
    from spinlab.estimators.em_suite_sampler import build_matrix

    grid = build_matrix(state)
    return EmSuiteMatrixResponse(
        segment_id=segment_id,
        alpha_grid=grid["alpha_grid"],
        baseline=grid["baseline"],
        matrix=grid["matrix"],
        n_attempts_total=state.n_attempts_total,
        n_successes=state.n_successes,
        n_deaths=state.n_deaths,
    )
```

(`HTTPException`, `Depends`, `_events_from_rows`, etc. are already imported in `model.py`; if not, add them.)

- [ ] **Step B2.5: Run; verify pass.**

```bash
python -m pytest tests/unit/routes/test_em_suite_matrix.py -v
```

Expected: 3 passed (after creating any missing fixtures).

- [ ] **Step B2.6: Commit.**

```bash
git add python/spinlab/routes/model.py tests/unit/routes/test_em_suite_matrix.py tests/conftest.py
git commit -m "feat(api): add GET /segments/{id}/em-suite-matrix endpoint"
```

(Include `tests/conftest.py` only if you added fixtures there.)

---

### Task B3: Frontend matrix view component

**Files:**
- Create: `frontend/src/em-suite-matrix.ts`
- Create: `frontend/src/em-suite-matrix.test.ts`
- Modify: `frontend/src/types.ts` (export the new type)

- [ ] **Step B3.1: Export the type.**

In `frontend/src/types.ts`, after the existing re-exports, add:

```typescript
export type EmSuiteMatrixResponse = S["EmSuiteMatrixResponse"];
```

- [ ] **Step B3.2: Write the failing component test.**

Create `frontend/src/em-suite-matrix.test.ts`:

```typescript
import { describe, expect, it } from "vitest";

import { formatMatrixCell, isAlphaPairValid } from "./em-suite-matrix";

describe("formatMatrixCell", () => {
  it("formats ms to one-decimal seconds with 's' suffix", () => {
    expect(formatMatrixCell(25_600)).toBe("25.6s");
    expect(formatMatrixCell(1_234)).toBe("1.2s");
  });

  it("returns em-dash for null", () => {
    expect(formatMatrixCell(null)).toBe("—");
  });

  it("returns em-dash for non-finite values", () => {
    expect(formatMatrixCell(Number.POSITIVE_INFINITY)).toBe("—");
    expect(formatMatrixCell(Number.NaN)).toBe("—");
  });
});

describe("isAlphaPairValid", () => {
  it("returns true when fast > slow", () => {
    expect(isAlphaPairValid(5, 2)).toBe(true);
  });

  it("returns false when fast == slow", () => {
    expect(isAlphaPairValid(3, 3)).toBe(false);
  });

  it("returns false when fast < slow", () => {
    expect(isAlphaPairValid(1, 4)).toBe(false);
  });
});
```

- [ ] **Step B3.3: Run; verify failure.**

```bash
cd frontend && npm test em-suite-matrix && cd ..
```

Expected: FAIL — module not found.

- [ ] **Step B3.4: Implement the component module.**

Create `frontend/src/em-suite-matrix.ts`:

```typescript
import type { EmSuiteMatrixResponse } from "./types";

const PLACEHOLDER = "—";

export function formatMatrixCell(value_ms: number | null): string {
  if (value_ms === null || !Number.isFinite(value_ms)) {
    return PLACEHOLDER;
  }
  return `${(value_ms / 1000).toFixed(1)}s`;
}

export function isAlphaPairValid(fastIdx: number, slowIdx: number): boolean {
  return fastIdx > slowIdx;
}

/** Render the matrix into a host element. Idempotent: clears + redraws. */
export function renderEmSuiteMatrix(
  host: HTMLElement,
  data: EmSuiteMatrixResponse,
): void {
  host.innerHTML = "";

  const wrapper = document.createElement("div");
  wrapper.className = "em-suite-matrix";

  const header = document.createElement("div");
  header.className = "em-suite-matrix__header";
  header.textContent = `EMA-suite matrix — n=${data.n_attempts_total} (${data.n_successes}S / ${data.n_deaths}D)`;
  wrapper.appendChild(header);

  // Baseline row (sample(0), no slope)
  const baselineRow = document.createElement("div");
  baselineRow.className = "em-suite-matrix__baseline";
  const baselineLabel = document.createElement("span");
  baselineLabel.className = "em-suite-matrix__label";
  baselineLabel.textContent = "sample(0)";
  baselineRow.appendChild(baselineLabel);
  for (const value of data.baseline) {
    const cell = document.createElement("span");
    cell.className = "em-suite-matrix__cell em-suite-matrix__cell--baseline";
    cell.textContent = formatMatrixCell(value);
    baselineRow.appendChild(cell);
  }
  wrapper.appendChild(baselineRow);

  // Grid: matrix[fastIdx][slowIdx]
  const grid = document.createElement("div");
  grid.className = "em-suite-matrix__grid";
  grid.style.gridTemplateColumns = `auto repeat(${data.alpha_grid.length}, 1fr)`;

  // Top header: alpha_slow values
  const corner = document.createElement("span");
  corner.className = "em-suite-matrix__corner";
  corner.textContent = "fast \\ slow";
  grid.appendChild(corner);
  for (const alpha of data.alpha_grid) {
    const head = document.createElement("span");
    head.className = "em-suite-matrix__col-header";
    head.textContent = alpha.toString();
    grid.appendChild(head);
  }

  for (let fastIdx = 0; fastIdx < data.alpha_grid.length; fastIdx++) {
    const rowLabel = document.createElement("span");
    rowLabel.className = "em-suite-matrix__row-header";
    rowLabel.textContent = data.alpha_grid[fastIdx].toString();
    grid.appendChild(rowLabel);
    for (let slowIdx = 0; slowIdx < data.alpha_grid.length; slowIdx++) {
      const cell = document.createElement("span");
      cell.className = "em-suite-matrix__cell";
      if (!isAlphaPairValid(fastIdx, slowIdx)) {
        cell.classList.add("em-suite-matrix__cell--blank");
        cell.textContent = "";
      } else {
        cell.textContent = formatMatrixCell(data.matrix[fastIdx][slowIdx]);
      }
      grid.appendChild(cell);
    }
  }

  wrapper.appendChild(grid);
  host.appendChild(wrapper);
}
```

- [ ] **Step B3.5: Run; verify pass.**

```bash
cd frontend && npm test em-suite-matrix && cd ..
```

Expected: 6 passed.

- [ ] **Step B3.6: Type-check.**

```bash
cd frontend && npm run typecheck && cd ..
```

Expected: no errors related to the new file.

- [ ] **Step B3.7: Commit.**

```bash
git add frontend/src/em-suite-matrix.ts frontend/src/em-suite-matrix.test.ts frontend/src/types.ts
git commit -m "feat(frontend): add EM-suite matrix rendering component"
```

---

### Task B4: Wire matrix into the per-segment view + CSS

**Files:**
- Modify: `frontend/src/segment-detail.ts`
- Modify: `frontend/style.css`

- [ ] **Step B4.1: Add CSS for the matrix.**

Append to `frontend/style.css`:

```css
.em-suite-matrix {
  margin-top: 16px;
  padding: 12px;
  border: 1px solid var(--border-color, #444);
  border-radius: 4px;
  background: var(--card-bg, #1a1a1a);
}

.em-suite-matrix__header {
  font-weight: 600;
  margin-bottom: 8px;
}

.em-suite-matrix__baseline {
  display: flex;
  gap: 4px;
  margin-bottom: 8px;
  padding-bottom: 8px;
  border-bottom: 1px dashed var(--border-color, #444);
  align-items: center;
  font-family: var(--mono-font, monospace);
  font-size: 0.85rem;
}

.em-suite-matrix__label {
  font-weight: 600;
  padding-right: 8px;
}

.em-suite-matrix__grid {
  display: grid;
  gap: 2px;
  font-family: var(--mono-font, monospace);
  font-size: 0.85rem;
}

.em-suite-matrix__corner,
.em-suite-matrix__col-header,
.em-suite-matrix__row-header {
  font-weight: 600;
  text-align: center;
  padding: 4px;
  background: var(--header-bg, #2a2a2a);
}

.em-suite-matrix__cell {
  text-align: center;
  padding: 4px;
  background: var(--cell-bg, #222);
}

.em-suite-matrix__cell--baseline {
  background: var(--cell-baseline-bg, #1e2a1e);
}

.em-suite-matrix__cell--blank {
  background: transparent;
}
```

- [ ] **Step B4.2: Add the fetch + render call in `segment-detail.ts`.**

Find where the segment detail page is populated (after `fetchJSON<SegmentHistory>(...)`). Add:

```typescript
import { renderEmSuiteMatrix } from "./em-suite-matrix";
import type { EmSuiteMatrixResponse } from "./types";

// ...inside the function that builds the segment detail view, after rendering existing content:
async function loadAndRenderMatrix(segmentId: string, host: HTMLElement) {
  try {
    const response = await fetch(
      `/api/segments/${encodeURIComponent(segmentId)}/em-suite-matrix`,
    );
    if (!response.ok) {
      host.innerHTML = `<div class="em-suite-matrix__error">Matrix unavailable (${response.status})</div>`;
      return;
    }
    const data: EmSuiteMatrixResponse = await response.json();
    renderEmSuiteMatrix(host, data);
  } catch (err) {
    host.innerHTML = `<div class="em-suite-matrix__error">Matrix fetch failed: ${err}</div>`;
  }
}

// Append a container under the existing segment detail and trigger load:
const matrixHost = document.createElement("div");
matrixHost.id = "em-suite-matrix-host";
detailContainer.appendChild(matrixHost);  // or wherever the per-segment card lives
await loadAndRenderMatrix(segmentId, matrixHost);
```

(Adjust variable names — `detailContainer`, `segmentId` — to match the existing surrounding code in `segment-detail.ts`. Read the file first to find the right insertion point.)

- [ ] **Step B4.3: Build the frontend.**

```bash
cd frontend && npm run build && cd ..
```

Expected: build succeeds.

- [ ] **Step B4.4: Type-check.**

```bash
cd frontend && npm run typecheck && cd ..
```

Expected: clean.

- [ ] **Step B4.5: Commit.**

```bash
git add frontend/src/segment-detail.ts frontend/style.css
git commit -m "feat(frontend): wire EM-suite matrix into per-segment detail view"
```

---

### Task B5: End-to-end smoke test

**Files:** No new files. Verifies the live system.

- [ ] **Step B5.1: Start the dashboard.**

```bash
spinlab dashboard --foreground
```

(Run in a separate terminal. Wait for "Uvicorn running on http://...:8000" log line.)

- [ ] **Step B5.2: Open a segment in the browser.**

Navigate to `http://localhost:8000` and click into a segment with ≥ 2 successes and ≥ 2 deaths.

- [ ] **Step B5.3: Verify the matrix renders.**

You should see:
- A "EMA-suite matrix" card under the existing per-segment content.
- A baseline row with one value per α (or "—" if gated).
- A 10×10 grid with the upper-triangle populated.
- Cells obviously degenerate at α=0.0 and α=1.0 endpoints.

If anything is wrong (matrix empty, NaNs, layout broken), capture screenshots and report before continuing.

- [ ] **Step B5.4: Verify live update (optional, requires emulator).**

If RetroArch is available, start a practice session on this segment and confirm the matrix changes after each attempt. (Reload the page if the dashboard doesn't auto-refresh — auto-refresh is out of v0 scope; manual reload is fine for now.)

- [ ] **Step B5.5: Stop the dashboard.**

```bash
# In the dashboard terminal: Ctrl+C
```

---

## Phase C — Offline replay / test script

### Task C1: Script skeleton + event loading

**Files:**
- Create: `scripts/em_suite_replay.py`

- [ ] **Step C1.1: Create the script with DB load and argparse.**

```python
#!/usr/bin/env python3
"""Offline replay of the EMA-suite sampler against historical event data.

For each segment with sufficient history, walks the event log forward,
recomputes the prediction matrix at each step, and writes per-segment
one-step-ahead loss heatmaps + a summary plot showing whether the
slope-augmented predictor beats the no-slope baseline.

Usage:
    python scripts/em_suite_replay.py [--player <player>] [--out-dir <path>]

See docs/superpowers/specs/2026-05-30-em-suite-sampler-design.md §Offline replay mode.
"""
from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Iterable
from pathlib import Path

from spinlab.config import load_config
from spinlab.db import Database
from spinlab.estimators.em_suite_sampler import (
    ALPHA_GRID,
    SamplerState,
    expected_episode_time_ms,
    process_event,
)
from spinlab.models import EventAttempt


def _load_events_for_segment(db: Database, segment_id: str) -> list[EventAttempt]:
    rows = db.get_segment_event_rows(segment_id)
    # Reuse the same converter that the API uses; import locally to keep
    # this script's dependencies narrow.
    from spinlab.routes.model import _events_from_rows
    return list(_events_from_rows(rows))


def _walk_events(
    events: Iterable[EventAttempt],
) -> Iterable[tuple[SamplerState, EventAttempt]]:
    """Yield (state_before, next_event) pairs for one-step-ahead evaluation."""
    state = SamplerState()
    for event in events:
        yield state, event
        state = process_event(state, event)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="out/em_suite_replay", help="Output directory for plots/CSVs")
    parser.add_argument("--min-attempts", type=int, default=10, help="Skip segments with fewer events")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    config = load_config()
    db = Database(config.db_path)

    # Resolve game_id from config. SpinLab DB methods that scope by game
    # require this; there is no global "list all segments" method.
    game_id = config.game_id
    segments = db.get_all_segments_with_model(game_id)
    print(f"Found {len(segments)} segments in game {game_id}")
    for seg in segments:
        events = _load_events_for_segment(db, seg.id)
        if len(events) < args.min_attempts:
            continue
        # TODO: per-step prediction sweep, MAE-log, plotting (next tasks)
        print(f"  {seg.id}: {len(events)} events")

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step C1.2: Run the script as a smoke test.**

```bash
python scripts/em_suite_replay.py --min-attempts 1
```

Expected: prints segment list with event counts; exits cleanly. (DB schema methods like `list_all_segments` must exist — if not, adjust to use the actual method. Inspect `python/spinlab/db/__init__.py` or relevant DB module first.)

- [ ] **Step C1.3: Commit.**

```bash
git add scripts/em_suite_replay.py
git commit -m "feat(scripts): em_suite_replay skeleton — load events per segment"
```

---

### Task C2: Per-step MAE-log computation

**Files:**
- Modify: `scripts/em_suite_replay.py`

- [ ] **Step C2.1: Add one-step-ahead scoring.**

Replace the `# TODO:` block in the main loop with the following helper-and-loop body:

```python
def _episode_times_for_segment(events: list[EventAttempt]) -> list[float]:
    """Group events into episodes; return one episode_total_ms per completed episode.

    Episode total includes per-death reload penalty:
      total_ms = sum(event.time_ms) + DEFAULT_DEATH_PENALTY_MS * deaths
    """
    from spinlab.estimators._episode_helpers import _group_into_episodes
    from spinlab.models import DEFAULT_DEATH_PENALTY_MS

    episodes = _group_into_episodes(events)
    totals: list[float] = []
    for ep in episodes:
        if ep.outcome != "completed":
            continue
        deaths = sum(1 for ev in ep.events if ev.outcome.value == "died")
        totals.append(
            sum(ev.time_ms for ev in ep.events)
            + DEFAULT_DEATH_PENALTY_MS * deaths
        )
    return totals


def _score_pair(
    events: list[EventAttempt], fast_idx: int, slow_idx: int,
    *, apply_slope: bool,
) -> tuple[float, int] | None:
    """One-step-ahead MAE-log across all episode boundaries in the segment.

    For each completed episode, predict its total time using the state from
    BEFORE that episode started (i.e. all events up to but not including the
    episode's first event). MAE-log = mean of |log(actual) - log(predicted)|.

    Returns (mae_log, n_scored) or None if the segment never reaches a
    state where the predictor can fire.
    """
    from spinlab.estimators._episode_helpers import _group_into_episodes

    episodes = _group_into_episodes(events)
    state = SamplerState()
    errors: list[float] = []
    cursor = 0  # index into `events`
    for ep in episodes:
        ep_start_idx = cursor
        # Predict from current state (before this episode's events apply)
        predicted = expected_episode_time_ms(
            state, fast_idx, slow_idx, apply_slope=apply_slope,
        )
        # Advance state through this episode's events
        for ev in ep.events:
            state = process_event(state, ev)
        cursor += len(ep.events)

        if ep.outcome != "completed":
            continue
        deaths = sum(1 for ev in ep.events if ev.outcome.value == "died")
        actual = (
            sum(ev.time_ms for ev in ep.events)
            + 3200 * deaths  # DEFAULT_DEATH_PENALTY_MS
        )
        if predicted is None or actual <= 0:
            continue
        errors.append(abs(math.log(actual) - math.log(predicted)))

    if not errors:
        return None
    return sum(errors) / len(errors), len(errors)
```

And replace the inner loop body in `main()`:

```python
        # Score every valid (fast, slow) pair, both with and without slope.
        results = []  # (fast_idx, slow_idx, mae_slope, mae_flat, n_scored)
        for fast_idx in range(len(ALPHA_GRID)):
            # Baseline (no slope, single alpha)
            base = _score_pair(events, fast_idx, fast_idx, apply_slope=False)
            for slow_idx in range(fast_idx):
                sloped = _score_pair(events, fast_idx, slow_idx, apply_slope=True)
                if base is None or sloped is None:
                    continue
                results.append((
                    ALPHA_GRID[fast_idx],
                    ALPHA_GRID[slow_idx],
                    sloped[0],
                    base[0],
                    sloped[1],
                ))

        # Write per-segment CSV for now; plots come in the next task.
        csv_path = out_dir / f"{seg.id}.csv"
        with csv_path.open("w") as f:
            f.write("alpha_fast,alpha_slow,mae_log_slope,mae_log_flat,n_scored\n")
            for row in results:
                f.write(",".join(str(x) for x in row) + "\n")
        print(f"  wrote {csv_path}")
```

- [ ] **Step C2.2: Run on Beto's segments.**

```bash
python scripts/em_suite_replay.py --min-attempts 10
```

Expected: produces one CSV per qualifying segment in `out/em_suite_replay/`.

- [ ] **Step C2.3: Spot-check one CSV.**

```bash
head out/em_suite_replay/*.csv | head -50
```

Sanity: MAE-log values are positive small numbers (typically 0.1–0.5); n_scored is small but non-zero.

- [ ] **Step C2.4: Commit.**

```bash
git add scripts/em_suite_replay.py
git commit -m "feat(scripts): em_suite_replay one-step-ahead MAE-log scoring per pair"
```

---

### Task C3: Summary plots — slope vs flat

**Files:**
- Modify: `scripts/em_suite_replay.py`

- [ ] **Step C3.1: Add plotting using matplotlib.**

Append at the bottom of `scripts/em_suite_replay.py`, before `if __name__`:

```python
def _plot_segment(csv_path: Path, plot_path: Path) -> None:
    """Plot two heatmaps side-by-side: slope MAE-log vs flat MAE-log."""
    import csv

    import numpy as np
    import matplotlib.pyplot as plt

    n = len(ALPHA_GRID)
    alpha_to_idx = {a: i for i, a in enumerate(ALPHA_GRID)}
    slope = np.full((n, n), np.nan)
    flat = np.full((n, n), np.nan)
    with csv_path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            f_idx = alpha_to_idx[float(row["alpha_fast"])]
            s_idx = alpha_to_idx[float(row["alpha_slow"])]
            slope[f_idx, s_idx] = float(row["mae_log_slope"])
            flat[f_idx, s_idx] = float(row["mae_log_flat"])

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    for ax, data, title in (
        (axes[0], slope, "slope MAE-log"),
        (axes[1], flat, "flat MAE-log"),
    ):
        im = ax.imshow(data, origin="lower")
        ax.set_xticks(range(n))
        ax.set_yticks(range(n))
        ax.set_xticklabels([str(a) for a in ALPHA_GRID], rotation=45)
        ax.set_yticklabels([str(a) for a in ALPHA_GRID])
        ax.set_xlabel("alpha_slow")
        ax.set_ylabel("alpha_fast")
        ax.set_title(title)
        fig.colorbar(im, ax=ax)
    fig.suptitle(csv_path.stem)
    fig.tight_layout()
    fig.savefig(plot_path, dpi=100)
    plt.close(fig)
```

And in `main()`, after writing each CSV:

```python
        plot_path = out_dir / f"{seg.id}.png"
        try:
            _plot_segment(csv_path, plot_path)
            print(f"  wrote {plot_path}")
        except Exception as exc:
            print(f"  plot failed for {seg.id}: {exc}")
```

- [ ] **Step C3.2: Run.**

```bash
python scripts/em_suite_replay.py --min-attempts 10
```

Expected: PNGs alongside CSVs in `out/em_suite_replay/`.

- [ ] **Step C3.3: Open one plot.**

```bash
# On Windows, open the file directly; cross-platform:
python -c "import webbrowser; webbrowser.open(r'out\em_suite_replay')"
```

Inspect: does the slope heatmap show lower (better) MAE-log than the flat heatmap in some region?

- [ ] **Step C3.4: Commit.**

```bash
git add scripts/em_suite_replay.py
git commit -m "feat(scripts): em_suite_replay summary heatmaps (slope vs flat)"
```

---

## Phase D — Review with Andrew

### Task D1: Surface results + decision gate

- [ ] **Step D1.1: Run the replay end-to-end.**

```bash
python scripts/em_suite_replay.py --min-attempts 10
```

- [ ] **Step D1.2: Compile a brief findings summary.**

Write a short (under-200-word) note in the PR description or as a markdown file at `out/em_suite_replay/SUMMARY.md` covering:

- Which segments produced enough data to score.
- For each scored segment: best slope-MAE-log, best flat-MAE-log, the winning (α_fast, α_slow) pair for slope.
- Aggregate: across segments, does the slope mechanism reliably beat flat? By what margin in log-space?
- Any anomalies (segments where slope is dramatically worse, segments where neither converges).

- [ ] **Step D1.3: Decision gate — surface to Andrew.**

Ask Andrew to review the summary and decide:

- **Slope earns its keep** → keep the matrix view as built (Phase B already shipped the slope-augmented form).
- **Slope is noise** → modify the frontend to hide the matrix grid and only show the baseline row (a single line of α-indexed `E_fast` values).

If the decision is "hide the grid," create a follow-up task at this point — don't autonomously rip out code without confirmation.

- [ ] **Step D1.4: Run full test suite as final gate.**

Per `CLAUDE.md`: full pytest before declaring done.

```bash
python -m pytest
cd frontend && npm test && npm run typecheck && cd ..
```

Expected: all green. If anything is red, fix or surface to Andrew before merging.

---

## Out of scope (v0 explicit non-goals)

These are NOT in this plan; they are explicit deferrals per the spec:

- Hot vs cold partitioning of distributions.
- Location-aware death modeling.
- Outer Monte Carlo allocator (§1–§5 of the original spec).
- Stochastic per-attempt time draws (sample() is deterministic-given-outcome in v0).
- Fitted α grid / per-segment α selection (manual selection only).
- Per-quantity sub-views (showing trend on each underlying quantity).
- Within-episode learning during simulation.
- Live websocket push to the matrix view (manual refresh OK for v0).

If any of these come up during implementation as "we should also do X," create a follow-up task and surface to Andrew instead of expanding scope.

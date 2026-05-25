# Death-Aware Rolling Calculations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new `death_aware_rolling` estimator that consumes event-level data, tracks decayed rolling stats per segment, and emits death-rate, death-time, and completion-time information alongside a geometric-formula expected-attempt-time. Drop-in for the existing greedy allocator.

**Architecture:** Three additive changes — (1) new `DeathExtras` dataclass + optional field on `ModelOutput`, (2) optional `events: list[EventAttempt] | None` kwarg on the `Estimator` ABC, (3) new `death_aware_rolling.py` estimator. Scheduler change is a one-line addition that pulls events via the existing `db.get_segment_event_rows`. No DB schema changes. Frontend types regenerate via the existing OpenAPI codegen pipeline.

**Tech Stack:** Python 3.11+, pydantic dataclasses, pytest, sqlite (existing), TypeScript+Vite (frontend types codegen only).

**Source spec:** [`docs/superpowers/specs/2026-05-24-death-aware-rolling-design.md`](../specs/2026-05-24-death-aware-rolling-design.md)

---

## File Structure

**Created:**
- `python/spinlab/estimators/death_aware_rolling.py` — estimator implementation (state, math helpers, registered class)
- `tests/unit/estimators/test_death_aware_rolling.py` — unit tests for math + estimator wiring
- `tests/integration/test_death_aware_rolling_e2e.py` — multi-death practice flow integration test

**Modified:**
- `python/spinlab/models.py` — add `DeathExtras` pydantic dataclass; add `extras: DeathExtras | None = None` to `ModelOutput`; update `to_dict`/`from_dict`
- `python/spinlab/estimators/__init__.py` — add `events: list[EventAttempt] | None = None` kwarg to `Estimator.process_attempt` / `model_output` / `rebuild_state`; register `death_aware_rolling`
- `python/spinlab/estimators/rolling_mean.py` — accept (and ignore) new `events` kwarg
- `python/spinlab/estimators/exp_decay.py` — accept (and ignore) new `events` kwarg
- `python/spinlab/estimators/kalman.py` — accept (and ignore) new `events` kwarg
- `python/spinlab/scheduler.py` — load events via `db.get_segment_event_rows(segment_id)`; pass to estimator method calls (3 call sites)
- `python/spinlab/api_schemas.py` — re-export `DeathExtras` for OpenAPI codegen
- `tests/factories.py` — add `make_event_attempt` helper

**Auto-regenerated (do not hand-edit):**
- `frontend/openapi.json` — via `python scripts/dump_openapi.py`
- `frontend/src/api-types.ts` — via `npm run gen-types`

---

## Task 1: Add `DeathExtras` dataclass and `extras` field on `ModelOutput`

**Files:**
- Modify: `python/spinlab/models.py`
- Test: `tests/unit/test_models.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_models.py`:

```python
class TestDeathExtras:
    def test_default_extras_is_none(self):
        from spinlab.models import ModelOutput, Estimate
        out = ModelOutput(
            total=Estimate(expected_ms=10000.0, ms_per_attempt=None, floor_ms=None),
            clean=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
        )
        assert out.extras is None

    def test_extras_round_trip(self):
        from spinlab.models import DeathExtras, Estimate, ModelOutput
        extras = DeathExtras(
            halflife_attempts=20,
            n_attempts_effective=12.5,
            n_episodes_with_death_eff=8.0,
            n_episodes_completed_eff=10.0,
            p_die_per_attempt=0.64,
            n_lives_died_effective=14.0,
            n_lives_survived_effective=10.0,
            p_die_per_life=0.583,
            death_samples=[(5000, 1.0), (4500, 0.5)],
            completion_samples=[(8000, 1.0)],
            expected_death_time_ms=4833.3,
            expected_completion_time_ms=8000.0,
        )
        out = ModelOutput(
            total=Estimate(expected_ms=20000.0, ms_per_attempt=None, floor_ms=4000.0),
            clean=Estimate(expected_ms=8000.0, ms_per_attempt=None, floor_ms=6500.0),
            extras=extras,
        )
        roundtripped = ModelOutput.from_dict(out.to_dict())
        assert roundtripped.extras is not None
        assert roundtripped.extras.halflife_attempts == 20
        assert roundtripped.extras.p_die_per_life == pytest.approx(0.583)
        assert roundtripped.extras.death_samples == [(5000, 1.0), (4500, 0.5)]

    def test_round_trip_without_extras(self):
        from spinlab.models import Estimate, ModelOutput
        out = ModelOutput(
            total=Estimate(expected_ms=10000.0, ms_per_attempt=None, floor_ms=None),
            clean=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
        )
        roundtripped = ModelOutput.from_dict(out.to_dict())
        assert roundtripped.extras is None
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/unit/test_models.py::TestDeathExtras -v
```

Expected: FAIL — `DeathExtras` not defined; `ModelOutput` has no `extras` attribute.

- [ ] **Step 3: Add `DeathExtras` dataclass to `models.py`**

In `python/spinlab/models.py`, after the existing `ModelOutput` class definition, add:

```python
@pydantic_dataclass(config=ConfigDict(extra="allow"))
class DeathExtras:
    """Death-aware fields published by death_aware_rolling.

    Carried on ModelOutput.extras when the active estimator is death-aware.
    Legacy estimators leave ModelOutput.extras = None.

    Two granularities:
      * Life-level (n_lives_*, p_die_per_life): each EventAttempt is one life.
        Drives total.expected_ms via the geometric formula.
      * Episode-level (n_attempts_*, n_episodes_*, p_die_per_attempt): each
        episode_id is one player attempt. Surfaced for player intuition.

    n_episodes_with_death_eff and n_episodes_completed_eff are NOT
    complementary — an episode can both contain deaths and complete. Their
    sum can exceed n_attempts_effective.
    """
    halflife_attempts: int

    # Episode-level (player intuition)
    n_attempts_effective: float
    n_episodes_with_death_eff: float
    n_episodes_completed_eff: float
    p_die_per_attempt: float | None

    # Life-level (drives geometric formula)
    n_lives_died_effective: float
    n_lives_survived_effective: float
    p_die_per_life: float | None

    # Distributions (life-level samples)
    death_samples: list[tuple[int, float]]
    completion_samples: list[tuple[int, float]]
    expected_death_time_ms: float | None
    expected_completion_time_ms: float | None

    def to_dict(self) -> dict:
        return {
            "halflife_attempts": self.halflife_attempts,
            "n_attempts_effective": self.n_attempts_effective,
            "n_episodes_with_death_eff": self.n_episodes_with_death_eff,
            "n_episodes_completed_eff": self.n_episodes_completed_eff,
            "p_die_per_attempt": self.p_die_per_attempt,
            "n_lives_died_effective": self.n_lives_died_effective,
            "n_lives_survived_effective": self.n_lives_survived_effective,
            "p_die_per_life": self.p_die_per_life,
            "death_samples": [list(s) for s in self.death_samples],
            "completion_samples": [list(s) for s in self.completion_samples],
            "expected_death_time_ms": self.expected_death_time_ms,
            "expected_completion_time_ms": self.expected_completion_time_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DeathExtras":
        return cls(
            halflife_attempts=d["halflife_attempts"],
            n_attempts_effective=d["n_attempts_effective"],
            n_episodes_with_death_eff=d["n_episodes_with_death_eff"],
            n_episodes_completed_eff=d["n_episodes_completed_eff"],
            p_die_per_attempt=d["p_die_per_attempt"],
            n_lives_died_effective=d["n_lives_died_effective"],
            n_lives_survived_effective=d["n_lives_survived_effective"],
            p_die_per_life=d["p_die_per_life"],
            death_samples=[tuple(s) for s in d["death_samples"]],
            completion_samples=[tuple(s) for s in d["completion_samples"]],
            expected_death_time_ms=d["expected_death_time_ms"],
            expected_completion_time_ms=d["expected_completion_time_ms"],
        )
```

Then modify the existing `ModelOutput` to add the `extras` field and round-trip it:

```python
@pydantic_dataclass(config=ConfigDict(extra="allow"))
class ModelOutput:
    """What every estimator produces — predictions for total time and clean tail.

    Pydantic dataclass: see ``Estimate`` for rationale.
    """
    total: Estimate
    clean: Estimate
    extras: DeathExtras | None = None

    def to_dict(self) -> dict:
        return {
            "total": self.total.to_dict(),
            "clean": self.clean.to_dict(),
            "extras": self.extras.to_dict() if self.extras is not None else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelOutput":
        extras_d = d.get("extras")
        return cls(
            total=Estimate.from_dict(d["total"]),
            clean=Estimate.from_dict(d["clean"]),
            extras=DeathExtras.from_dict(extras_d) if extras_d is not None else None,
        )
```

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/unit/test_models.py::TestDeathExtras -v
```

Expected: 3 passed.

- [ ] **Step 5: Run the full unit suite as a sanity check**

```
pytest -m "not emulator" -q
```

Expected: all green (no other test should care about `extras` since default is `None`).

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/models.py tests/unit/test_models.py
git commit -m "feat(models): add DeathExtras and ModelOutput.extras field"
```

---

## Task 2: Extend `Estimator` ABC with optional `events` kwarg

**Files:**
- Modify: `python/spinlab/estimators/__init__.py`
- Modify: `python/spinlab/estimators/rolling_mean.py`
- Modify: `python/spinlab/estimators/exp_decay.py`
- Modify: `python/spinlab/estimators/kalman.py`
- Test: `tests/unit/estimators/test_estimator_sanity.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/estimators/test_estimator_sanity.py`:

```python
class TestEstimatorEventsKwarg:
    """The Estimator ABC accepts an optional events kwarg on its three
    work methods. Legacy estimators ignore it; the kwarg defaults to None
    so existing call sites keep working."""

    def test_rolling_mean_accepts_events_kwarg(self):
        from spinlab.estimators.rolling_mean import RollingMeanEstimator
        from tests.factories import make_attempt_record
        est = RollingMeanEstimator()
        a = make_attempt_record(10000, True, clean_tail_ms=10000)
        state = est.init_state(a, priors={})
        # The kwarg is accepted and ignored; output is identical to omitting it.
        state_with = est.process_attempt(state, a, [a], events=None)
        state_without = est.process_attempt(state, a, [a])
        assert state_with.n_completed == state_without.n_completed
        out_with = est.model_output(state, [a], events=None)
        out_without = est.model_output(state, [a])
        assert out_with.to_dict() == out_without.to_dict()
        state_rebuilt = est.rebuild_state([a], events=None)
        assert state_rebuilt.n_completed == 1

    def test_exp_decay_accepts_events_kwarg(self):
        from spinlab.estimators.exp_decay import ExpDecayEstimator
        from tests.factories import make_attempt_record
        est = ExpDecayEstimator()
        attempts = [make_attempt_record(t, True, clean_tail_ms=t) for t in [12000, 11000, 10000]]
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts, events=None)
        out = est.model_output(state, attempts, events=None)
        assert out.total.expected_ms is not None

    def test_kalman_accepts_events_kwarg(self):
        from spinlab.estimators.kalman import KalmanEstimator
        from tests.factories import make_attempt_record
        est = KalmanEstimator()
        a = make_attempt_record(10000, True, clean_tail_ms=10000)
        state = est.init_state(a, priors={})
        state = est.process_attempt(state, a, [a], events=None)
        out = est.model_output(state, [a], events=None)
        assert out is not None
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/unit/estimators/test_estimator_sanity.py::TestEstimatorEventsKwarg -v
```

Expected: FAIL — `process_attempt() got an unexpected keyword argument 'events'`.

- [ ] **Step 3: Update the `Estimator` ABC**

In `python/spinlab/estimators/__init__.py`, update the abstract method signatures. Add a TYPE_CHECKING import for `EventAttempt` and update three methods:

```python
if TYPE_CHECKING:
    from spinlab.db import Database
    from spinlab.models import AttemptRecord, EventAttempt, ModelOutput
```

Then on the `Estimator` class:

```python
    @abstractmethod
    def init_state(
        self, first_attempt: "AttemptRecord", priors: dict,
        params: dict | None = None,
    ) -> EstimatorState:
        """Initialize state from the first completed attempt."""
        ...

    @abstractmethod
    def process_attempt(
        self,
        state: EstimatorState,
        new_attempt: "AttemptRecord",
        all_attempts: list["AttemptRecord"],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> EstimatorState:
        """Process one attempt. Uses new_attempt and/or all_attempts as needed.

        ``events`` is the per-segment event list (one row per died/survived
        event), passed by the scheduler when available. Legacy estimators
        ignore it; new estimators that consume event-level data read it here.
        """
        ...

    @abstractmethod
    def model_output(
        self, state: EstimatorState, all_attempts: list["AttemptRecord"],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> "ModelOutput":
        """Produce standardized ModelOutput from current state.

        ``params`` carries tunable estimator parameters (see ``declared_params``).
        Estimators that don't read params at output time can ignore it.
        ``events`` is optional event-level input; see process_attempt.
        """
        ...

    @abstractmethod
    def rebuild_state(
        self, attempts: list["AttemptRecord"],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> EstimatorState:
        """Rebuild state by replaying all attempts."""
        ...
```

- [ ] **Step 4: Update `rolling_mean.py`, `exp_decay.py`, `kalman.py` to accept the new kwarg**

For each of the three files, update the three method signatures to accept `events: list["EventAttempt"] | None = None`. The body does NOT need to change — they ignore the kwarg.

In `python/spinlab/estimators/rolling_mean.py`:

```python
    def process_attempt(  # type: ignore[override]
        self, state: RollingMeanState, new_attempt: AttemptRecord,
        all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> RollingMeanState:
        n_completed = state.n_completed + (1 if new_attempt.completed else 0)
        return RollingMeanState(n_completed=n_completed, n_attempts=state.n_attempts + 1)

    def model_output(  # type: ignore[override]
        self, state: RollingMeanState, all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> ModelOutput:
        # ... existing body unchanged ...

    def rebuild_state(
        self, attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> RollingMeanState:
        n_completed = sum(1 for a in attempts if a.completed)
        return RollingMeanState(n_completed=n_completed, n_attempts=len(attempts))
```

Add `EventAttempt` to the TYPE_CHECKING block at the top of each file:

```python
if TYPE_CHECKING:
    from spinlab.models import EventAttempt
```

Apply the same pattern to `exp_decay.py` and `kalman.py` — change the three method signatures only; bodies unchanged.

- [ ] **Step 5: Run the new tests to verify they pass**

```
pytest tests/unit/estimators/test_estimator_sanity.py::TestEstimatorEventsKwarg -v
```

Expected: 3 passed.

- [ ] **Step 6: Run the full estimator suite to confirm no regressions**

```
pytest tests/unit/estimators/ -q
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/estimators/ tests/unit/estimators/test_estimator_sanity.py
git commit -m "feat(estimators): add optional events kwarg to Estimator ABC"
```

---

## Task 3: `DeathAwareRollingEstimator` skeleton — registration and declared_params

**Files:**
- Create: `python/spinlab/estimators/death_aware_rolling.py`
- Modify: `python/spinlab/estimators/__init__.py`
- Create: `tests/unit/estimators/test_death_aware_rolling.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/estimators/test_death_aware_rolling.py`:

```python
"""Tests for the Death-Aware Rolling estimator."""
import json
import pytest


class TestRegistration:
    def test_registered_in_registry(self):
        from spinlab.estimators import list_estimators, get_estimator
        assert "death_aware_rolling" in list_estimators()
        est = get_estimator("death_aware_rolling")
        assert est.name == "death_aware_rolling"
        assert est.display_name == "Death-Aware Rolling"

    def test_declared_params_has_halflife(self):
        from spinlab.estimators import get_estimator
        est = get_estimator("death_aware_rolling")
        names = {p.name for p in est.declared_params()}
        assert "halflife" in names
        halflife_param = next(p for p in est.declared_params() if p.name == "halflife")
        assert halflife_param.default == 20.0
        assert halflife_param.min_val == 1.0
        assert halflife_param.max_val == 200.0


class TestEmptyEvents:
    def test_empty_events_returns_none_output(self):
        from spinlab.estimators import get_estimator
        est = get_estimator("death_aware_rolling")
        from tests.factories import make_attempt_record
        a = make_attempt_record(10000, True, clean_tail_ms=10000)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=[])
        assert out.total.expected_ms is None
        assert out.total.ms_per_attempt is None
        assert out.total.floor_ms is None
        assert out.clean.expected_ms is None
        assert out.extras is None
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/unit/estimators/test_death_aware_rolling.py -v
```

Expected: FAIL — `'death_aware_rolling'` not in registry (no file yet).

- [ ] **Step 3: Create the skeleton estimator file**

Create `python/spinlab/estimators/death_aware_rolling.py`:

```python
"""Death-aware rolling estimator.

Tracks decayed rolling statistics per segment: death rate (per-life and
per-attempt), death-time distribution, and completion-time distribution.
Populates ModelOutput.total and ModelOutput.clean via the geometric
expected-time formula, plus a DeathExtras payload carrying the
distribution samples and the two p_die quantities.

State is recomputed from events on every call (rolling_mean style); only
the n_completed / n_attempts counters live in state_json. The halflife
knob lives in declared_params.

See docs/superpowers/specs/2026-05-24-death-aware-rolling-design.md for
the math and the geometric/recursive derivation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from spinlab.estimators import Estimator, EstimatorState, ParamDef, register_estimator
from spinlab.models import AttemptRecord, DeathExtras, Estimate, ModelOutput

if TYPE_CHECKING:
    from spinlab.models import EventAttempt

# Default halflife in episodes. ~20 ≈ a recent month of casual practice;
# tuneable per-estimator via declared_params.
DEFAULT_HALFLIFE = 20

# Effective window: episodes beyond this many halflives contribute weight
# < 0.001 and are dropped before computing stats. Outputs unchanged within
# float precision.
EFFECTIVE_WINDOW_HALFLIVES = 5


@dataclass
class DeathAwareRollingState(EstimatorState):
    """Minimal bookkeeping. Stats recompute from events each call."""

    def to_dict(self) -> dict:
        return {"n_completed": self.n_completed, "n_attempts": self.n_attempts}

    @classmethod
    def from_dict(cls, d: dict) -> "DeathAwareRollingState":
        return cls(
            n_completed=d.get("n_completed", 0),
            n_attempts=d.get("n_attempts", 0),
        )


EstimatorState.register_state("death_aware_rolling", DeathAwareRollingState)


def _resolve_halflife(params: dict | None) -> int:
    if not params or "halflife" not in params:
        return DEFAULT_HALFLIFE
    raw = params["halflife"]
    try:
        n = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"halflife must be an int, got {raw!r}") from exc
    if n < 1 or n > 200:
        raise ValueError(f"halflife must be in [1, 200], got {n}")
    return n


def _empty_output() -> ModelOutput:
    """Output shape for empty / fully-invalidated event lists."""
    none_estimate = Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None)
    return ModelOutput(total=none_estimate, clean=none_estimate, extras=None)


@register_estimator
class DeathAwareRollingEstimator(Estimator):
    name = "death_aware_rolling"
    display_name = "Death-Aware Rolling"

    def declared_params(self) -> list[ParamDef]:
        return [
            ParamDef(
                "halflife", "Halflife (episodes)",
                float(DEFAULT_HALFLIFE), 1.0, 200.0, 1.0,
                "Number of episodes for the rolling weight to halve. "
                "20 ≈ recent month of casual practice; lower = more "
                "responsive to recent changes, higher = more stable.",
            ),
        ]

    def init_state(
        self, first_attempt: AttemptRecord, priors: dict,
        params: dict | None = None,
    ) -> DeathAwareRollingState:
        return DeathAwareRollingState(n_completed=1, n_attempts=1)

    def process_attempt(  # type: ignore[override]
        self, state: DeathAwareRollingState, new_attempt: AttemptRecord,
        all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> DeathAwareRollingState:
        n_completed = state.n_completed + (1 if new_attempt.completed else 0)
        return DeathAwareRollingState(
            n_completed=n_completed, n_attempts=state.n_attempts + 1,
        )

    def model_output(  # type: ignore[override]
        self, state: DeathAwareRollingState, all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> ModelOutput:
        if not events:
            return _empty_output()
        # Full math wired in Task 7.
        return _empty_output()

    def rebuild_state(  # type: ignore[override]
        self, attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> DeathAwareRollingState:
        n_completed = sum(1 for a in attempts if a.completed)
        return DeathAwareRollingState(
            n_completed=n_completed, n_attempts=len(attempts),
        )
```

- [ ] **Step 4: Register the new estimator in `_register_all`**

In `python/spinlab/estimators/__init__.py`, update the `_register_all` function:

```python
def _register_all():
    """Import all estimator modules to trigger @register_estimator decorators."""
    from . import death_aware_rolling, kalman, rolling_mean
    try:
        from . import exp_decay
    except ImportError:
        pass

_register_all()
```

- [ ] **Step 5: Run tests to verify they pass**

```
pytest tests/unit/estimators/test_death_aware_rolling.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/estimators/death_aware_rolling.py python/spinlab/estimators/__init__.py tests/unit/estimators/test_death_aware_rolling.py
git commit -m "feat(estimators): register DeathAwareRollingEstimator skeleton"
```

---

## Task 4: Episode grouping and weighting helpers

**Files:**
- Modify: `python/spinlab/estimators/death_aware_rolling.py`
- Modify: `tests/unit/estimators/test_death_aware_rolling.py`
- Modify: `tests/factories.py`

- [ ] **Step 1: Add an `EventAttempt` factory to `tests/factories.py`**

Append to `tests/factories.py`:

```python
def make_event_attempt(
    segment_id: str = "seg1",
    episode_id: str = "ep1",
    outcome: str = "died",
    time_ms: int = 5000,
    session_id: str | None = "_default_test_session",
    capture_run_id: str | None = None,
    invalidated: bool = False,
    created_at: str = "2026-01-01T00:00:00",
):
    """Create an EventAttempt for unit tests."""
    from datetime import datetime
    from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt
    if session_id is None and capture_run_id is None:
        capture_run_id = "_default_test_capture_run"
    return EventAttempt(
        segment_id=segment_id,
        episode_id=episode_id,
        outcome=AttemptOutcome(outcome),
        time_ms=time_ms,
        session_id=session_id,
        capture_run_id=capture_run_id,
        source=AttemptSource.PRACTICE,
        invalidated=invalidated,
        created_at=datetime.fromisoformat(created_at),
    )
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/unit/estimators/test_death_aware_rolling.py`:

```python
class TestEpisodeGrouping:
    def test_groups_events_by_episode_id(self):
        from spinlab.estimators.death_aware_rolling import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=7500),
        ]
        episodes = _group_into_episodes(events)
        assert len(episodes) == 2
        ep1, ep2 = episodes
        assert ep1.episode_id == "ep1"
        assert len(ep1.events) == 2
        assert ep1.outcome == "completed"
        assert ep1.had_any_death is True
        assert ep2.episode_id == "ep2"
        assert ep2.outcome == "completed"
        assert ep2.had_any_death is False

    def test_aborted_episode_outcome_is_died(self):
        from spinlab.estimators.death_aware_rolling import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2500),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2000),
        ]
        episodes = _group_into_episodes(events)
        assert len(episodes) == 1
        assert episodes[0].outcome == "died"
        assert episodes[0].had_any_death is True

    def test_invalidated_event_drops_whole_episode(self):
        from spinlab.estimators.death_aware_rolling import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000, invalidated=True),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=7500),
        ]
        episodes = _group_into_episodes(events)
        assert len(episodes) == 1
        assert episodes[0].episode_id == "ep2"


class TestWeighting:
    def test_most_recent_episode_has_weight_one(self):
        from spinlab.estimators.death_aware_rolling import _compute_weights
        weights = _compute_weights(n_episodes=10, halflife=5)
        assert weights[-1] == pytest.approx(1.0)

    def test_halflife_ago_has_weight_half(self):
        from spinlab.estimators.death_aware_rolling import _compute_weights
        weights = _compute_weights(n_episodes=10, halflife=5)
        # Index 4 is 5 episodes back from index 9 (the most-recent).
        assert weights[4] == pytest.approx(0.5)

    def test_five_halflives_ago_has_weight_below_threshold(self):
        from spinlab.estimators.death_aware_rolling import _compute_weights
        weights = _compute_weights(n_episodes=100, halflife=10)
        # Index 49 is 50 episodes (5 halflives) back.
        assert weights[49] < 0.05
```

- [ ] **Step 3: Run tests to verify they fail**

```
pytest tests/unit/estimators/test_death_aware_rolling.py::TestEpisodeGrouping tests/unit/estimators/test_death_aware_rolling.py::TestWeighting -v
```

Expected: FAIL — `_group_into_episodes`, `_compute_weights`, `_truncate_to_window` not found.

- [ ] **Step 4: Implement the helpers**

In `python/spinlab/estimators/death_aware_rolling.py`, after the constants and before the state dataclass, add:

```python
@dataclass
class _Episode:
    """Per-episode aggregated view used by the math layer."""
    episode_id: str
    events: list["EventAttempt"]
    outcome: str       # "completed" if any event is survived, else "died"
    had_any_death: bool
    closing_id: int    # closing event's row id, for chronological ordering


def _group_into_episodes(events: list["EventAttempt"]) -> list[_Episode]:
    """Group events by episode_id and drop any episode with an invalidated event.

    Episodes are ordered by the id of their closing (max id) event so the
    chronological order matches the production roll-up in
    spinlab.db.attempts._roll_up_episode.
    """
    by_id: dict[str, list["EventAttempt"]] = {}
    for ev in events:
        by_id.setdefault(ev.episode_id, []).append(ev)

    episodes: list[_Episode] = []
    for ep_id, ev_list in by_id.items():
        if any(ev.invalidated for ev in ev_list):
            continue
        had_any_death = any(
            ev.outcome.value == "died" for ev in ev_list
        )
        any_survived = any(
            ev.outcome.value == "survived" for ev in ev_list
        )
        outcome = "completed" if any_survived else "died"
        # EventAttempt rows from the DB carry their row id via the events-mixin
        # adapter; tests using the factory don't have ids, so fall back to
        # creation order via the list index. The closing event is always the
        # last event in the episode by insertion order.
        closing_id = id(ev_list[-1])  # stable per-process; sufficient for ordering
        episodes.append(_Episode(
            episode_id=ep_id, events=ev_list,
            outcome=outcome, had_any_death=had_any_death,
            closing_id=closing_id,
        ))
    # Order by closing_id ascending (oldest first).
    episodes.sort(key=lambda e: e.closing_id)
    return episodes


def _compute_weights(n_episodes: int, halflife: int) -> list[float]:
    """Return exponentially-decayed weights, one per episode.

    weights[i] = 2 ** (-(n_episodes - 1 - i) / halflife)

    The most-recent episode (index n_episodes - 1) has weight 1.0. An episode
    halflife steps back has weight 0.5.
    """
    return [
        2.0 ** (-(n_episodes - 1 - i) / halflife)
        for i in range(n_episodes)
    ]


```

Truncation to the effective window (~5×halflife episodes) is performed inline
in `_compute_aggregates` (Task 5). Episodes beyond that contribute weight
< 0.03 and don't move the output within float precision.

- [ ] **Step 5: Run tests to verify they pass**

```
pytest tests/unit/estimators/test_death_aware_rolling.py::TestEpisodeGrouping tests/unit/estimators/test_death_aware_rolling.py::TestWeighting -v
```

Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/estimators/death_aware_rolling.py tests/unit/estimators/test_death_aware_rolling.py tests/factories.py
git commit -m "feat(death_aware_rolling): episode grouping and decay weighting helpers"
```

---

## Task 5: Life-level and episode-level aggregates

**Files:**
- Modify: `python/spinlab/estimators/death_aware_rolling.py`
- Modify: `tests/unit/estimators/test_death_aware_rolling.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/estimators/test_death_aware_rolling.py`:

```python
class TestLifeLevelAggregates:
    def test_p_die_per_life_pure_deaths(self):
        from spinlab.estimators.death_aware_rolling import _compute_aggregates
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep2", outcome="died", time_ms=3500),
        ]
        agg = _compute_aggregates(events, halflife=20)
        assert agg.p_die_per_life == pytest.approx(1.0)
        assert agg.expected_completion_time_ms is None
        assert agg.expected_death_time_ms == pytest.approx(3250.0)

    def test_p_die_per_life_pure_completions(self):
        from spinlab.estimators.death_aware_rolling import _compute_aggregates
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=7000),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=8000),
        ]
        agg = _compute_aggregates(events, halflife=20)
        assert agg.p_die_per_life == pytest.approx(0.0)
        assert agg.expected_death_time_ms is None
        assert agg.expected_completion_time_ms == pytest.approx(7500.0)

    def test_p_die_per_life_mixed(self):
        """3 lives died, 1 life survived → p_die_per_life = 0.75.

        All four lives have the same episode weight (one episode each, halflife
        large enough that weights are essentially 1.0).
        """
        from spinlab.estimators.death_aware_rolling import _compute_aggregates
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2000),
            make_event_attempt(episode_id="ep2", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep3", outcome="died", time_ms=4000),
            make_event_attempt(episode_id="ep4", outcome="survived", time_ms=7000),
        ]
        agg = _compute_aggregates(events, halflife=100)
        assert agg.p_die_per_life == pytest.approx(0.75, abs=0.01)
        assert agg.expected_death_time_ms == pytest.approx(3000.0, abs=10)
        assert agg.expected_completion_time_ms == pytest.approx(7000.0, abs=10)

    def test_multi_death_episode_counts_each_life(self):
        """Episode [died, died, survived] contributes 2 death samples and 1 completion."""
        from spinlab.estimators.death_aware_rolling import _compute_aggregates
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2500),
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=7000),
        ]
        agg = _compute_aggregates(events, halflife=20)
        assert len(agg.death_samples) == 2
        assert len(agg.completion_samples) == 1
        # All three lives share the same episode weight, so p_die_per_life = 2/3.
        assert agg.p_die_per_life == pytest.approx(2 / 3, abs=0.01)

    def test_aborted_episode_contributes_deaths_only(self):
        """[died, died, died] gives 3 death samples and zero completion samples."""
        from spinlab.estimators.death_aware_rolling import _compute_aggregates
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2000),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2500),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
        ]
        agg = _compute_aggregates(events, halflife=20)
        assert len(agg.death_samples) == 3
        assert agg.completion_samples == []
        assert agg.p_die_per_life == pytest.approx(1.0)
        assert agg.expected_completion_time_ms is None


class TestEpisodeLevelAggregates:
    def test_p_die_per_attempt_clean_runs(self):
        from spinlab.estimators.death_aware_rolling import _compute_aggregates
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id=f"ep{i}", outcome="survived", time_ms=7000)
            for i in range(5)
        ]
        agg = _compute_aggregates(events, halflife=20)
        assert agg.p_die_per_attempt == pytest.approx(0.0)
        assert agg.n_attempts_effective == pytest.approx(agg.n_episodes_completed_eff)
        assert agg.n_episodes_with_death_eff == pytest.approx(0.0)

    def test_p_die_per_attempt_multi_death_then_survive_counts_as_death(self):
        """An episode with deaths-and-completion counts toward
        n_episodes_with_death_eff AND n_episodes_completed_eff —
        their sum can exceed n_attempts_effective."""
        from spinlab.estimators.death_aware_rolling import _compute_aggregates
        from tests.factories import make_event_attempt
        # 2 episodes: ep1 = [died, survived]; ep2 = [survived].
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=7000),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=6500),
        ]
        agg = _compute_aggregates(events, halflife=100)
        # 2 attempts; 1 has a death; both complete.
        assert agg.n_attempts_effective == pytest.approx(2.0, abs=0.01)
        assert agg.n_episodes_with_death_eff == pytest.approx(1.0, abs=0.01)
        assert agg.n_episodes_completed_eff == pytest.approx(2.0, abs=0.01)
        assert agg.p_die_per_attempt == pytest.approx(0.5, abs=0.01)
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/unit/estimators/test_death_aware_rolling.py::TestLifeLevelAggregates tests/unit/estimators/test_death_aware_rolling.py::TestEpisodeLevelAggregates -v
```

Expected: FAIL — `_compute_aggregates` not defined.

- [ ] **Step 3: Implement `_compute_aggregates`**

Append to `python/spinlab/estimators/death_aware_rolling.py`:

```python
@dataclass
class _Aggregates:
    """Holds all the rolling statistics for one segment.

    Internal — not part of the public API. Used to compose ModelOutput +
    DeathExtras in model_output().
    """
    halflife: int
    # Episode-level
    n_attempts_effective: float
    n_episodes_with_death_eff: float
    n_episodes_completed_eff: float
    p_die_per_attempt: float | None
    # Life-level
    n_lives_died_effective: float
    n_lives_survived_effective: float
    p_die_per_life: float | None
    # Distributions
    death_samples: list[tuple[int, float]]
    completion_samples: list[tuple[int, float]]
    expected_death_time_ms: float | None
    expected_completion_time_ms: float | None


def _weighted_mean(samples: list[tuple[int, float]]) -> float | None:
    if not samples:
        return None
    total_w = sum(w for _, w in samples)
    if total_w == 0:
        return None
    return sum(t * w for t, w in samples) / total_w


def _compute_aggregates(
    events: list["EventAttempt"], halflife: int,
) -> _Aggregates:
    """Compute all rolling statistics for a segment from its event list.

    Drops invalidated episodes, truncates the working set to ~5×halflife
    episodes, then computes both life-level and episode-level aggregates
    from the same weighted dataset.
    """
    episodes = _group_into_episodes(events)
    if not episodes:
        return _Aggregates(
            halflife=halflife,
            n_attempts_effective=0.0,
            n_episodes_with_death_eff=0.0,
            n_episodes_completed_eff=0.0,
            p_die_per_attempt=None,
            n_lives_died_effective=0.0,
            n_lives_survived_effective=0.0,
            p_die_per_life=None,
            death_samples=[],
            completion_samples=[],
            expected_death_time_ms=None,
            expected_completion_time_ms=None,
        )

    # Truncate to effective window.
    max_kept = EFFECTIVE_WINDOW_HALFLIVES * halflife
    if len(episodes) > max_kept:
        episodes = episodes[-max_kept:]

    weights = _compute_weights(n_episodes=len(episodes), halflife=halflife)

    n_attempts_effective = sum(weights)
    n_episodes_with_death_eff = sum(
        w for w, ep in zip(weights, episodes) if ep.had_any_death
    )
    n_episodes_completed_eff = sum(
        w for w, ep in zip(weights, episodes) if ep.outcome == "completed"
    )
    p_die_per_attempt = (
        n_episodes_with_death_eff / n_attempts_effective
        if n_attempts_effective > 0 else None
    )

    death_samples: list[tuple[int, float]] = []
    completion_samples: list[tuple[int, float]] = []
    for w, ep in zip(weights, episodes):
        for ev in ep.events:
            sample = (int(ev.time_ms), w)
            if ev.outcome.value == "died":
                death_samples.append(sample)
            else:
                completion_samples.append(sample)

    n_lives_died_effective = sum(w for _, w in death_samples)
    n_lives_survived_effective = sum(w for _, w in completion_samples)
    total_life_weight = n_lives_died_effective + n_lives_survived_effective
    p_die_per_life = (
        n_lives_died_effective / total_life_weight
        if total_life_weight > 0 else None
    )

    return _Aggregates(
        halflife=halflife,
        n_attempts_effective=n_attempts_effective,
        n_episodes_with_death_eff=n_episodes_with_death_eff,
        n_episodes_completed_eff=n_episodes_completed_eff,
        p_die_per_attempt=p_die_per_attempt,
        n_lives_died_effective=n_lives_died_effective,
        n_lives_survived_effective=n_lives_survived_effective,
        p_die_per_life=p_die_per_life,
        death_samples=death_samples,
        completion_samples=completion_samples,
        expected_death_time_ms=_weighted_mean(death_samples),
        expected_completion_time_ms=_weighted_mean(completion_samples),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/unit/estimators/test_death_aware_rolling.py::TestLifeLevelAggregates tests/unit/estimators/test_death_aware_rolling.py::TestEpisodeLevelAggregates -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/estimators/death_aware_rolling.py tests/unit/estimators/test_death_aware_rolling.py
git commit -m "feat(death_aware_rolling): life-level + episode-level aggregates"
```

---

## Task 6: Geometric `total.expected_ms` formula

**Files:**
- Modify: `python/spinlab/estimators/death_aware_rolling.py`
- Modify: `tests/unit/estimators/test_death_aware_rolling.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/estimators/test_death_aware_rolling.py`:

```python
class TestGeometricFormula:
    def test_zero_p_die_returns_completion_time(self):
        from spinlab.estimators.death_aware_rolling import _expected_total_ms
        result = _expected_total_ms(
            p_die_per_life=0.0,
            e_death_time_ms=None,
            e_completion_time_ms=8000.0,
            respawn_penalty_ms=3200,
        )
        assert result == pytest.approx(8000.0)

    def test_half_p_die_means_one_extra_death_life(self):
        """p_die_per_life=0.5 → E[death lives] = 1, so total = (death+penalty) + completion."""
        from spinlab.estimators.death_aware_rolling import _expected_total_ms
        result = _expected_total_ms(
            p_die_per_life=0.5,
            e_death_time_ms=3000.0,
            e_completion_time_ms=8000.0,
            respawn_penalty_ms=3200,
        )
        # (0.5 / 0.5) * (3000 + 3200) + 8000 = 6200 + 8000 = 14200
        assert result == pytest.approx(14200.0)

    def test_eighty_percent_p_die_means_four_death_lives(self):
        from spinlab.estimators.death_aware_rolling import _expected_total_ms
        result = _expected_total_ms(
            p_die_per_life=0.8,
            e_death_time_ms=3000.0,
            e_completion_time_ms=8000.0,
            respawn_penalty_ms=3200,
        )
        # (0.8 / 0.2) * (3000 + 3200) + 8000 = 4 * 6200 + 8000 = 32800
        assert result == pytest.approx(32800.0)

    def test_p_die_one_returns_none(self):
        """No completions observed → cannot project an expected attempt time."""
        from spinlab.estimators.death_aware_rolling import _expected_total_ms
        result = _expected_total_ms(
            p_die_per_life=1.0,
            e_death_time_ms=3000.0,
            e_completion_time_ms=None,
            respawn_penalty_ms=3200,
        )
        assert result is None

    def test_none_p_die_returns_none(self):
        from spinlab.estimators.death_aware_rolling import _expected_total_ms
        result = _expected_total_ms(
            p_die_per_life=None,
            e_death_time_ms=None,
            e_completion_time_ms=None,
            respawn_penalty_ms=3200,
        )
        assert result is None

    def test_no_completion_time_returns_none(self):
        from spinlab.estimators.death_aware_rolling import _expected_total_ms
        result = _expected_total_ms(
            p_die_per_life=0.3,
            e_death_time_ms=3000.0,
            e_completion_time_ms=None,
            respawn_penalty_ms=3200,
        )
        assert result is None

    def test_no_death_time_with_nonzero_p_die_returns_none(self):
        """If p_die > 0 but we have no death-time samples, can't compute total."""
        from spinlab.estimators.death_aware_rolling import _expected_total_ms
        result = _expected_total_ms(
            p_die_per_life=0.3,
            e_death_time_ms=None,
            e_completion_time_ms=8000.0,
            respawn_penalty_ms=3200,
        )
        assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/unit/estimators/test_death_aware_rolling.py::TestGeometricFormula -v
```

Expected: FAIL — `_expected_total_ms` not defined.

- [ ] **Step 3: Implement the formula**

Append to `python/spinlab/estimators/death_aware_rolling.py`:

```python
def _expected_total_ms(
    p_die_per_life: float | None,
    e_death_time_ms: float | None,
    e_completion_time_ms: float | None,
    respawn_penalty_ms: int,
) -> float | None:
    """Geometric formula for expected attempt time.

    E[attempt] = (p / (1-p)) * (E[death] + penalty) + E[completion]

    where p = p_die_per_life. Each life is modeled as independent Bernoulli;
    player retries until completion.

    Returns None when:
      - p_die_per_life is None (no events)
      - p_die_per_life is 1.0 (no completions ⇒ can't project completion time)
      - e_completion_time_ms is None (haven't seen a completion yet)
      - p_die_per_life > 0 but e_death_time_ms is None (inconsistent input;
        shouldn't happen in practice but guard anyway)
    """
    if p_die_per_life is None or e_completion_time_ms is None:
        return None
    if p_die_per_life >= 1.0:
        return None
    if p_die_per_life > 0 and e_death_time_ms is None:
        return None
    if p_die_per_life == 0:
        return e_completion_time_ms
    # p_die_per_life ∈ (0, 1) here, and e_death_time_ms is not None.
    q = 1.0 - p_die_per_life
    e_n_death_lives = p_die_per_life / q
    return e_n_death_lives * (e_death_time_ms + respawn_penalty_ms) + e_completion_time_ms
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/unit/estimators/test_death_aware_rolling.py::TestGeometricFormula -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/estimators/death_aware_rolling.py tests/unit/estimators/test_death_aware_rolling.py
git commit -m "feat(death_aware_rolling): geometric expected-time formula"
```

---

## Task 7: Wire `model_output` to emit full `ModelOutput` + `DeathExtras`

**Files:**
- Modify: `python/spinlab/estimators/death_aware_rolling.py`
- Modify: `tests/unit/estimators/test_death_aware_rolling.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/estimators/test_death_aware_rolling.py`:

```python
class TestModelOutputWiring:
    def _events_one_episode_one_completion(self):
        from tests.factories import make_event_attempt
        return [make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000)]

    def _events_mixed_aborted_and_completed(self):
        from tests.factories import make_event_attempt
        return [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2000),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2500),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=8000),
            make_event_attempt(episode_id="ep3", outcome="died", time_ms=2200),
            make_event_attempt(episode_id="ep3", outcome="survived", time_ms=7500),
        ]

    def test_single_completion_no_deaths(self):
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record
        est = get_estimator("death_aware_rolling")
        a = make_attempt_record(8000, True, clean_tail_ms=8000)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=self._events_one_episode_one_completion())
        assert out.total.expected_ms == pytest.approx(8000.0)
        assert out.clean.expected_ms == pytest.approx(8000.0)
        assert out.total.floor_ms == pytest.approx(8000.0)
        assert out.clean.floor_ms == pytest.approx(8000.0)
        assert out.extras is not None
        assert out.extras.p_die_per_life == pytest.approx(0.0)
        assert out.extras.death_samples == []
        assert out.extras.completion_samples == [(8000, pytest.approx(1.0))]

    def test_geometric_total_uses_aborted_episodes(self):
        """The 3 deaths in ep1 (aborted) push p_die_per_life up;
        total.expected_ms should be larger than the mean of completed-episode
        totals would suggest."""
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record
        est = get_estimator("death_aware_rolling")
        a = make_attempt_record(8000, True, clean_tail_ms=8000)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=self._events_mixed_aborted_and_completed())
        assert out.extras is not None
        # 4 died lives + 2 completed lives → p_die_per_life = 4/6 ≈ 0.667
        assert out.extras.p_die_per_life == pytest.approx(4 / 6, abs=0.01)
        # E[death] = mean(2000, 2500, 3000, 2200) = 2425
        assert out.extras.expected_death_time_ms == pytest.approx(2425.0, abs=10)
        # E[completion] = mean(8000, 7500) = 7750
        assert out.extras.expected_completion_time_ms == pytest.approx(7750.0, abs=10)
        # Expected total: (4/2) * (2425 + 3200) + 7750 = 2 * 5625 + 7750 = 19000
        assert out.total.expected_ms == pytest.approx(19000.0, abs=50)
        # Mean of just completed-episode totals (ep2 = 8000, ep3 = 2200+7500+3200 = 12900)
        # would give 10450, which is < 19000.
        assert out.total.expected_ms > 10450.0

    def test_floor_ms_is_min_across_all_completed_episodes(self):
        """floor_ms = best episode_total observed, regardless of decay window."""
        from spinlab.estimators import get_estimator
        from spinlab.models import AttemptOutcome
        from tests.factories import make_attempt_record, make_event_attempt
        events = (
            # Old great clean episode (best ever).
            [make_event_attempt(episode_id="old_great", outcome="survived", time_ms=5000)]
            # Plus 50 newer mediocre clean episodes at 9000ms.
            + [
                make_event_attempt(episode_id=f"new{i}", outcome="survived", time_ms=9000)
                for i in range(50)
            ]
        )
        est = get_estimator("death_aware_rolling")
        a = make_attempt_record(9000, True, clean_tail_ms=9000)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=events, params={"halflife": 5})
        assert out.total.floor_ms == pytest.approx(5000.0)
        assert out.clean.floor_ms == pytest.approx(5000.0)

    def test_single_completion_ms_per_attempt_none(self):
        """One data point ⇒ no slope."""
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record
        est = get_estimator("death_aware_rolling")
        a = make_attempt_record(8000, True, clean_tail_ms=8000)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=self._events_one_episode_one_completion())
        assert out.total.ms_per_attempt is None
        assert out.clean.ms_per_attempt is None

    def test_improving_completion_times_positive_ms_per_attempt(self):
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record, make_event_attempt
        events = [
            make_event_attempt(episode_id=f"ep{i}", outcome="survived", time_ms=t)
            for i, t in enumerate([12000, 11500, 11000, 10500, 10000, 9500, 9000, 8500])
        ]
        est = get_estimator("death_aware_rolling")
        a = make_attempt_record(8500, True, clean_tail_ms=8500)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=events)
        # Positive = improving (earlier slower than later).
        assert out.total.ms_per_attempt is not None
        assert out.total.ms_per_attempt > 0
        assert out.clean.ms_per_attempt is not None
        assert out.clean.ms_per_attempt > 0

    def test_halflife_param_applied(self):
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record, make_event_attempt
        events = [
            # 5 old episodes at 5000ms.
            *[make_event_attempt(episode_id=f"old{i}", outcome="survived", time_ms=5000) for i in range(5)],
            # 3 recent episodes at 10000ms.
            *[make_event_attempt(episode_id=f"new{i}", outcome="survived", time_ms=10000) for i in range(3)],
        ]
        est = get_estimator("death_aware_rolling")
        a = make_attempt_record(10000, True, clean_tail_ms=10000)
        state = est.init_state(a, priors={})
        # Short halflife should give heavier weight to the recent 10000s.
        out_short = est.model_output(state, [a], events=events, params={"halflife": 1})
        # Long halflife mixes old and new more evenly.
        out_long = est.model_output(state, [a], events=events, params={"halflife": 100})
        assert out_short.extras is not None and out_long.extras is not None
        assert out_short.extras.expected_completion_time_ms is not None
        assert out_long.extras.expected_completion_time_ms is not None
        assert out_short.extras.expected_completion_time_ms > out_long.extras.expected_completion_time_ms

    def test_halflife_out_of_bounds_raises(self):
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record, make_event_attempt
        est = get_estimator("death_aware_rolling")
        a = make_attempt_record(8000, True, clean_tail_ms=8000)
        state = est.init_state(a, priors={})
        events = [make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000)]
        with pytest.raises(ValueError, match="halflife"):
            est.model_output(state, [a], events=events, params={"halflife": 0})
        with pytest.raises(ValueError, match="halflife"):
            est.model_output(state, [a], events=events, params={"halflife": 500})

    def test_all_deaths_no_completions_total_is_none(self):
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record, make_event_attempt
        est = get_estimator("death_aware_rolling")
        a = make_attempt_record(0, False, clean_tail_ms=None)
        state = est.init_state(a, priors={})
        events = [
            make_event_attempt(episode_id=f"ep{i}", outcome="died", time_ms=3000)
            for i in range(5)
        ]
        out = est.model_output(state, [a], events=events)
        assert out.total.expected_ms is None
        assert out.total.floor_ms is None
        assert out.clean.expected_ms is None
        assert out.clean.floor_ms is None
        assert out.extras is not None
        assert out.extras.p_die_per_life == pytest.approx(1.0)
        assert len(out.extras.death_samples) == 5
        assert out.extras.completion_samples == []
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/unit/estimators/test_death_aware_rolling.py::TestModelOutputWiring -v
```

Expected: FAIL — `model_output` currently returns the empty placeholder.

- [ ] **Step 3: Replace `model_output`'s body with the full wiring**

In `python/spinlab/estimators/death_aware_rolling.py`, add a helper for slope computation and replace `_empty_output`'s caller. Add this above the estimator class:

```python
from spinlab.models import DEFAULT_DEATH_PENALTY_MS


def _weighted_half_split_slope(
    samples: list[tuple[int, float]],
) -> float | None:
    """Crude slope estimator: (mean_first_half - mean_second_half) / half_n.

    Positive ⇒ improving (earlier samples slower than later).
    Returns None when fewer than 2 samples (no slope).
    """
    if len(samples) < 2:
        return None
    half = max(len(samples) // 2, 1)
    first = samples[:half]
    second = samples[half:]
    m_first = _weighted_mean(first)
    m_second = _weighted_mean(second)
    if m_first is None or m_second is None:
        return None
    return (m_first - m_second) / half


def _floor_over_completed_episode_totals(
    episodes: list[_Episode], respawn_penalty_ms: int,
) -> float | None:
    """Best (min) episode_total time across ALL completed episodes.

    episode_total_time_ms = sum(event.time_ms) + respawn_penalty_ms × n_deaths.
    Matches the production roll-up in spinlab.db.attempts._roll_up_episode.

    Not windowed — best-ever is sticky info.
    """
    best: float | None = None
    for ep in episodes:
        if ep.outcome != "completed":
            continue
        deaths = sum(1 for ev in ep.events if ev.outcome.value == "died")
        total = sum(ev.time_ms for ev in ep.events) + respawn_penalty_ms * deaths
        if best is None or total < best:
            best = float(total)
    return best


def _floor_over_survived_event_times(episodes: list[_Episode]) -> float | None:
    """Best (min) survived-event time_ms across ALL survived events.

    Not windowed.
    """
    best: float | None = None
    for ep in episodes:
        for ev in ep.events:
            if ev.outcome.value != "survived":
                continue
            if best is None or ev.time_ms < best:
                best = float(ev.time_ms)
    return best
```

Then replace the `model_output` body on the estimator class:

```python
    def model_output(  # type: ignore[override]
        self, state: DeathAwareRollingState, all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> ModelOutput:
        if not events:
            return _empty_output()
        halflife = _resolve_halflife(params)

        # All episodes (for floor_ms across full history — not windowed).
        all_episodes = _group_into_episodes(events)
        if not all_episodes:
            return _empty_output()

        agg = _compute_aggregates(events, halflife=halflife)

        total_expected_ms = _expected_total_ms(
            p_die_per_life=agg.p_die_per_life,
            e_death_time_ms=agg.expected_death_time_ms,
            e_completion_time_ms=agg.expected_completion_time_ms,
            respawn_penalty_ms=DEFAULT_DEATH_PENALTY_MS,
        )
        clean_expected_ms = agg.expected_completion_time_ms

        # ms_per_attempt: slope over completion_samples in chronological order.
        # The samples list is already chronological because episodes are
        # ordered chronologically in _compute_aggregates and each episode's
        # events are inserted in order.
        clean_mpa = _weighted_half_split_slope(agg.completion_samples)
        # For total, slope is over the same completion samples — total tracks
        # completion-time learning + death-rate trends together; a richer
        # slope estimator is a follow-up.
        total_mpa = clean_mpa

        total_floor = _floor_over_completed_episode_totals(
            all_episodes, respawn_penalty_ms=DEFAULT_DEATH_PENALTY_MS,
        )
        clean_floor = _floor_over_survived_event_times(all_episodes)

        extras = DeathExtras(
            halflife_attempts=halflife,
            n_attempts_effective=agg.n_attempts_effective,
            n_episodes_with_death_eff=agg.n_episodes_with_death_eff,
            n_episodes_completed_eff=agg.n_episodes_completed_eff,
            p_die_per_attempt=agg.p_die_per_attempt,
            n_lives_died_effective=agg.n_lives_died_effective,
            n_lives_survived_effective=agg.n_lives_survived_effective,
            p_die_per_life=agg.p_die_per_life,
            death_samples=agg.death_samples,
            completion_samples=agg.completion_samples,
            expected_death_time_ms=agg.expected_death_time_ms,
            expected_completion_time_ms=agg.expected_completion_time_ms,
        )
        return ModelOutput(
            total=Estimate(
                expected_ms=total_expected_ms,
                ms_per_attempt=total_mpa,
                floor_ms=total_floor,
            ),
            clean=Estimate(
                expected_ms=clean_expected_ms,
                ms_per_attempt=clean_mpa,
                floor_ms=clean_floor,
            ),
            extras=extras,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/unit/estimators/test_death_aware_rolling.py::TestModelOutputWiring -v
```

Expected: 8 passed.

- [ ] **Step 5: Run all death-aware unit tests**

```
pytest tests/unit/estimators/test_death_aware_rolling.py -v
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/estimators/death_aware_rolling.py tests/unit/estimators/test_death_aware_rolling.py
git commit -m "feat(death_aware_rolling): wire model_output to emit ModelOutput + DeathExtras"
```

---

## Task 8: State serialization round-trip

**Files:**
- Modify: `tests/unit/estimators/test_death_aware_rolling.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/estimators/test_death_aware_rolling.py`:

```python
class TestStateRoundTrip:
    def test_state_serializes_minimally(self):
        from spinlab.estimators.death_aware_rolling import DeathAwareRollingState
        s = DeathAwareRollingState(n_completed=7, n_attempts=12)
        d = s.to_dict()
        assert d == {"n_completed": 7, "n_attempts": 12}
        s2 = DeathAwareRollingState.from_dict(d)
        assert s2.n_completed == 7
        assert s2.n_attempts == 12

    def test_state_deserialize_via_registry(self):
        from spinlab.estimators import EstimatorState
        state_json = json.dumps({"n_completed": 4, "n_attempts": 6})
        s = EstimatorState.deserialize("death_aware_rolling", state_json)
        assert s.n_completed == 4
        assert s.n_attempts == 6

    def test_rebuild_state_from_attempts(self):
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record, make_incomplete
        est = get_estimator("death_aware_rolling")
        attempts = [
            make_attempt_record(12000, True, clean_tail_ms=12000),
            make_incomplete(),
            make_attempt_record(11000, True, clean_tail_ms=11000),
        ]
        state = est.rebuild_state(attempts)
        assert state.n_completed == 2
        assert state.n_attempts == 3

    def test_rebuild_state_empty(self):
        from spinlab.estimators import get_estimator
        est = get_estimator("death_aware_rolling")
        state = est.rebuild_state([])
        assert state.n_completed == 0
        assert state.n_attempts == 0
```

- [ ] **Step 2: Run tests to verify they pass**

```
pytest tests/unit/estimators/test_death_aware_rolling.py::TestStateRoundTrip -v
```

Expected: 4 passed (the state class already implements this from Task 3).

- [ ] **Step 3: Run the full unit suite to confirm no regressions across the codebase**

```
pytest -m "not emulator" -q
```

Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/estimators/test_death_aware_rolling.py
git commit -m "test(death_aware_rolling): state round-trip via registry"
```

---

## Task 9: Scheduler integration — pull events and pass to estimators

**Files:**
- Modify: `python/spinlab/scheduler.py`
- Create: `tests/unit/test_scheduler_death_aware.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_scheduler_death_aware.py`:

```python
"""Scheduler-level test: events flow through to the new estimator."""
import json

import pytest


def _seed_segment_with_events(db, segment_id: str, game_id: str):
    """Create a segment with one completed episode (1 death + 1 survival)."""
    from spinlab.models import (
        Attempt, AttemptOutcome, AttemptSource, EndpointType, EventAttempt,
        Segment,
    )
    db.upsert_game(game_id, "FakeGame", "any%")
    db.upsert_segment(Segment(
        id=segment_id, game_id=game_id, level_number=1,
        start_type=EndpointType.ENTRANCE, start_ordinal=0,
        end_type=EndpointType.GOAL, end_ordinal=0,
        description="seg1",
    ))
    session_id = f"{game_id}:sess"
    db.create_session(session_id, game_id)
    # One episode: died at 3000ms, then survived at 8000ms.
    from datetime import datetime
    episode_id = "ep1"
    common = dict(
        segment_id=segment_id, episode_id=episode_id,
        session_id=session_id, capture_run_id=None,
        source=AttemptSource.PRACTICE,
        chosen_allocator=None, invalidated=False,
        created_at=datetime.fromisoformat("2026-05-24T00:00:00"),
    )
    db.log_event_attempt(EventAttempt(outcome=AttemptOutcome.DIED, time_ms=3000, **common))
    db.log_event_attempt(EventAttempt(outcome=AttemptOutcome.SURVIVED, time_ms=8000, **common))
    return session_id


class TestSchedulerPassesEvents:
    def test_death_aware_estimator_receives_events_and_emits_extras(self, tmp_path):
        from spinlab.db import Database
        from spinlab.scheduler import Scheduler

        db = Database(tmp_path / "test.db")
        db.migrate()
        game_id = "test_game"
        segment_id = "seg1"
        _seed_segment_with_events(db, segment_id, game_id)

        sched = Scheduler(db, game_id, estimator_name="death_aware_rolling")
        sched.update_state_after_episode(segment_id)

        row = db.load_model_state(segment_id, "death_aware_rolling")
        assert row is not None
        output_payload = json.loads(row["output_json"])
        assert output_payload["extras"] is not None
        assert output_payload["extras"]["p_die_per_life"] == pytest.approx(0.5)
        assert len(output_payload["extras"]["death_samples"]) == 1
        assert len(output_payload["extras"]["completion_samples"]) == 1

    def test_existing_estimators_unaffected_when_scheduler_loads_events(self, tmp_path):
        """rolling_mean still produces the same outputs even though the
        scheduler is now also loading events on its behalf."""
        from spinlab.db import Database
        from spinlab.scheduler import Scheduler

        db = Database(tmp_path / "test.db")
        db.migrate()
        game_id = "test_game"
        segment_id = "seg1"
        _seed_segment_with_events(db, segment_id, game_id)

        sched = Scheduler(db, game_id, estimator_name="rolling_mean")
        sched.update_state_after_episode(segment_id)

        row = db.load_model_state(segment_id, "rolling_mean")
        assert row is not None
        output_payload = json.loads(row["output_json"])
        # rolling_mean does not populate extras.
        assert output_payload["extras"] is None
        # And total.expected_ms still computes from AttemptRecord (the rolled-up
        # episode time = 3000 + 8000 + 3200 = 14200ms).
        assert output_payload["total"]["expected_ms"] == pytest.approx(14200.0)
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/unit/test_scheduler_death_aware.py -v
```

Expected: FAIL — the new estimator receives no events because the scheduler doesn't pass them yet.

- [ ] **Step 3: Update `scheduler.py` to load events and pass them through**

In `python/spinlab/scheduler.py`, modify `_process_attempt_for_estimator` to load events once per call and pass them via the new kwarg. Locate the method (around line 220) and update:

```python
    def _process_attempt_for_estimator(
        self, est: "Estimator", segment_id: str,
        new_attempt: AttemptRecord, all_attempts: list[AttemptRecord],
        *, completed: bool, time_ms: int,
    ) -> None:
        params = self._load_estimator_params(est.name)
        row = self.db.load_model_state(segment_id, est.name)
        # Load per-event rows once; pass to the estimator. Legacy estimators
        # ignore the kwarg; death_aware_rolling and future event-level
        # estimators read it.
        event_rows = self.db.get_segment_event_rows(segment_id)
        events = _events_from_rows(event_rows)

        if row and row["state_json"]:
            state = EstimatorState.deserialize(est.name, row["state_json"])
            if state.n_completed == 0 and completed and time_ms is not None:
                prior_n_attempts = state.n_attempts
                priors = est.get_priors(self.db, self.game_id)
                state = est.init_state(new_attempt, priors, params=params)
                state.n_attempts += prior_n_attempts
            else:
                state = est.process_attempt(
                    state, new_attempt, all_attempts,
                    params=params, events=events,
                )
        else:
            if completed and time_ms is not None:
                priors = est.get_priors(self.db, self.game_id)
                state = est.init_state(new_attempt, priors, params=params)
            else:
                state = est.rebuild_state(
                    [new_attempt], params=params, events=events,
                )
                output = est.model_output(
                    state, all_attempts, params=params, events=events,
                )
                self.db.save_model_state(
                    segment_id, est.name,
                    json.dumps(state.to_dict()), json.dumps(output.to_dict()),
                )
                return

        output = est.model_output(
            state, all_attempts, params=params, events=events,
        )
        self.db.save_model_state(
            segment_id, est.name,
            json.dumps(state.to_dict()), json.dumps(output.to_dict()),
        )
```

Also locate the other `est.rebuild_state(all_attempts, params=params)` call site (around line 303) and update:

```python
                    event_rows = self.db.get_segment_event_rows(segment_id)
                    events = _events_from_rows(event_rows)
                    state = est.rebuild_state(all_attempts, params=params, events=events)
                    output = est.model_output(state, all_attempts, params=params, events=events)
```

Add an `_events_from_rows` helper near the top of `scheduler.py`, alongside the existing `_attempts_from_rows`:

```python
def _events_from_rows(rows: list[dict]) -> list["EventAttempt"]:
    """Convert raw event_attempt rows (dicts from get_segment_event_rows)
    into EventAttempt dataclass instances for estimator consumption."""
    from datetime import datetime
    from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt
    out: list[EventAttempt] = []
    for r in rows:
        out.append(EventAttempt(
            segment_id=r["segment_id"],
            episode_id=r["episode_id"],
            outcome=AttemptOutcome(r["outcome"]),
            time_ms=r["time_ms"],
            session_id=r.get("session_id"),
            capture_run_id=r.get("capture_run_id"),
            source=AttemptSource(r["source"]),
            chosen_allocator=r.get("chosen_allocator"),
            invalidated=bool(r.get("invalidated", 0)),
            created_at=datetime.fromisoformat(r["created_at"]),
        ))
    return out
```

If `EventAttempt` isn't already imported at the module top, add it to the TYPE_CHECKING block:

```python
if TYPE_CHECKING:
    from spinlab.estimators import Estimator
    from spinlab.models import EventAttempt
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/unit/test_scheduler_death_aware.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Run the full scheduler-area suite**

```
pytest tests/unit/test_scheduler_kalman.py tests/unit/test_scheduler_fallback.py tests/unit/test_scheduler_death_aware.py -v
```

Expected: all green.

- [ ] **Step 6: Run the full unit suite**

```
pytest -m "not emulator" -q
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/scheduler.py tests/unit/test_scheduler_death_aware.py
git commit -m "feat(scheduler): pass per-segment events to estimators"
```

---

## Task 10: API schemas re-export + frontend type regeneration

**Files:**
- Modify: `python/spinlab/api_schemas.py`
- Regenerate: `frontend/openapi.json`, `frontend/src/api-types.ts`

- [ ] **Step 1: Add `DeathExtras` to the existing re-export import**

In `python/spinlab/api_schemas.py`, locate this line (around line 45):

```python
from spinlab.models import ConditionMap, Mode, ModelOutput, Status  # noqa: E402, I001 — kept beside its explanatory block above
```

Edit to add `DeathExtras`:

```python
from spinlab.models import ConditionMap, DeathExtras, Mode, ModelOutput, Status  # noqa: E402, I001 — kept beside its explanatory block above
```

This explicit re-export ensures the OpenAPI schema definition for `DeathExtras` appears as a named component even though pydantic may also pick it up transitively through `ModelOutput.extras`. Named components survive codegen better than inlined anonymous types.

- [ ] **Step 2: Regenerate the OpenAPI JSON and the TS types**

```
python scripts/dump_openapi.py
cd frontend && npm run gen-types
```

- [ ] **Step 3: Verify the generated TypeScript file has `DeathExtras`**

```
grep -n "DeathExtras\|extras" frontend/src/api-types.ts | head -20
```

Expected: at least one match for `DeathExtras` in the generated types, plus an `extras` field on the `ModelOutput` type.

- [ ] **Step 4: Run the frontend tests**

```
cd frontend && npm run typecheck && npm test
```

Expected: typecheck passes, tests green. The frontend doesn't render `extras` yet, but the new optional field should not break any existing code.

- [ ] **Step 5: Build the frontend to be sure nothing else broke**

```
cd frontend && npm run build
```

Expected: clean build.

- [ ] **Step 6: Run the smoke tests that depend on the built frontend**

```
pytest tests/integration/test_frontend_smoke.py -v
```

Expected: green.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/api_schemas.py frontend/openapi.json frontend/src/api-types.ts
git commit -m "feat(api): expose DeathExtras in OpenAPI + regenerate frontend types"
```

---

## Task 11: Integration test — multi-death practice flow end-to-end

**Files:**
- Create: `tests/integration/test_death_aware_rolling_e2e.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_death_aware_rolling_e2e.py`:

```python
"""End-to-end pipeline test for the death-aware rolling estimator.

Drives several multi-death practice episodes through the real DB +
scheduler + estimator stack and asserts the persisted output is
populated with sensible values.

No emulator — uses the DB directly and the existing PracticeTiming
adapter is NOT required (we write events directly via log_event_attempt
the same way PracticeSession.receive_event_attempt does).
"""
import json
from datetime import datetime

import pytest


def _log_episode(db, segment_id, session_id, episode_id, outcomes_and_times):
    """Persist one episode as a series of event rows. Returns the closing id."""
    from spinlab.models import (
        AttemptOutcome, AttemptSource, EventAttempt,
    )
    last_id = None
    for outcome, time_ms in outcomes_and_times:
        last_id = db.log_event_attempt(EventAttempt(
            segment_id=segment_id,
            episode_id=episode_id,
            outcome=AttemptOutcome(outcome),
            time_ms=time_ms,
            session_id=session_id,
            capture_run_id=None,
            source=AttemptSource.PRACTICE,
            chosen_allocator=None,
            invalidated=False,
            created_at=datetime.now(),
        ))
    return last_id


class TestDeathAwareE2E:
    def test_multi_death_flow_persists_sensible_outputs(self, tmp_path):
        from spinlab.db import Database
        from spinlab.models import EndpointType, Segment
        from spinlab.scheduler import Scheduler

        db = Database(tmp_path / "spinlab.db")
        db.migrate()
        game_id = "test_game"
        segment_id = "seg_e2e"
        session_id = f"{game_id}:sess"
        db.upsert_game(game_id, "FakeGame", "any%")
        db.upsert_segment(Segment(
            id=segment_id, game_id=game_id, level_number=1,
            start_type=EndpointType.ENTRANCE, start_ordinal=0,
            end_type=EndpointType.GOAL, end_ordinal=0,
            description="e2e segment",
        ))
        db.create_session(session_id, game_id)

        sched = Scheduler(db, game_id, estimator_name="death_aware_rolling")

        # Episode 1: died at 3000, then survived at 8000.
        _log_episode(db, segment_id, session_id, "ep1", [
            ("died", 3000), ("survived", 8000),
        ])
        sched.update_state_after_episode(segment_id)

        # Episode 2: clean survival at 7500.
        _log_episode(db, segment_id, session_id, "ep2", [
            ("survived", 7500),
        ])
        sched.update_state_after_episode(segment_id)

        # Episode 3: aborted after three deaths.
        _log_episode(db, segment_id, session_id, "ep3", [
            ("died", 2500), ("died", 2800), ("died", 3100),
        ])
        sched.update_state_after_episode(segment_id)

        # Episode 4: died once then survived.
        _log_episode(db, segment_id, session_id, "ep4", [
            ("died", 2200), ("survived", 8200),
        ])
        sched.update_state_after_episode(segment_id)

        # Inspect persisted state.
        row = db.load_model_state(segment_id, "death_aware_rolling")
        assert row is not None
        output = json.loads(row["output_json"])

        # Total expected ms is populated (we have completions).
        assert output["total"]["expected_ms"] is not None
        assert output["total"]["expected_ms"] > 0
        assert output["clean"]["expected_ms"] is not None

        # Floors reflect the best episode totals / completion times.
        assert output["total"]["floor_ms"] is not None
        assert output["clean"]["floor_ms"] is not None
        # Best clean survived time in this dataset is 7500.
        assert output["clean"]["floor_ms"] == pytest.approx(7500.0)

        # Extras populated.
        extras = output["extras"]
        assert extras is not None
        assert 0.0 < extras["p_die_per_life"] < 1.0
        # 4 attempts; 3 of them had deaths (ep1, ep3, ep4) — ep3 aborted.
        assert extras["n_attempts_effective"] == pytest.approx(4.0, abs=0.01)
        # Total died events: ep1(1) + ep3(3) + ep4(1) = 5.
        assert len(extras["death_samples"]) == 5
        # Total survived events: ep1(1) + ep2(1) + ep4(1) = 3.
        assert len(extras["completion_samples"]) == 3

    def test_invalidated_episode_excluded_e2e(self, tmp_path):
        from spinlab.db import Database
        from spinlab.models import EndpointType, Segment
        from spinlab.scheduler import Scheduler

        db = Database(tmp_path / "spinlab.db")
        db.migrate()
        game_id = "test_game"
        segment_id = "seg_inv"
        session_id = f"{game_id}:sess"
        db.upsert_game(game_id, "FakeGame", "any%")
        db.upsert_segment(Segment(
            id=segment_id, game_id=game_id, level_number=1,
            start_type=EndpointType.ENTRANCE, start_ordinal=0,
            end_type=EndpointType.GOAL, end_ordinal=0,
            description="invalidation test",
        ))
        db.create_session(session_id, game_id)

        sched = Scheduler(db, game_id, estimator_name="death_aware_rolling")

        last_id_bad = _log_episode(db, segment_id, session_id, "ep_bad", [
            ("died", 100), ("died", 200), ("died", 300),
        ])
        # Mark the bad episode invalidated by flipping the flag on one event.
        db.set_attempt_invalidated(last_id_bad, True)

        _log_episode(db, segment_id, session_id, "ep_good", [
            ("survived", 8000),
        ])
        sched.update_state_after_episode(segment_id)

        row = db.load_model_state(segment_id, "death_aware_rolling")
        output = json.loads(row["output_json"])
        extras = output["extras"]
        # Invalidated episode does not contribute — only the clean one counts.
        assert extras["p_die_per_life"] == pytest.approx(0.0)
        assert len(extras["death_samples"]) == 0
        assert len(extras["completion_samples"]) == 1
```

- [ ] **Step 2: Run the integration test**

```
pytest tests/integration/test_death_aware_rolling_e2e.py -v
```

Expected: 2 passed.

- [ ] **Step 3: Run the full test suite as a final check**

```
python -m pytest
```

Expected: all green (unit + emulator + frontend smoke). Per CLAUDE.md, full pytest is the merge gate — every test must pass and `SKIPPED` is not "passing" unless explicitly accepted with a reason.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_death_aware_rolling_e2e.py
git commit -m "test(integration): end-to-end multi-death practice flow for death_aware_rolling"
```

---

## Task 12: Type check and lint sweep

**Files:** none (verification only)

- [ ] **Step 1: Run pyright over the modified Python**

```
npx pyright python/spinlab/estimators/death_aware_rolling.py python/spinlab/estimators/__init__.py python/spinlab/scheduler.py python/spinlab/models.py python/spinlab/api_schemas.py
```

Expected: zero new errors. The CLAUDE.md guidance is "don't introduce new errors; existing errors are tracked." If pyright surfaces problems in the new file specifically, fix them inline before the next commit. Pre-existing errors elsewhere are not in scope for this task.

- [ ] **Step 2: Run ruff over the modified Python**

```
ruff check python/spinlab/estimators/death_aware_rolling.py python/spinlab/estimators/__init__.py python/spinlab/scheduler.py python/spinlab/models.py
```

Expected: zero issues. Fix any flagged unused imports or dead code.

- [ ] **Step 3: If any fixes were made, commit them**

```bash
git add python/spinlab/
git commit -m "chore(death_aware_rolling): pyright + ruff cleanup"
```

(Skip if both step 1 and step 2 were already clean.)

---

## Done

The death-aware rolling estimator is now:
- Registered alongside the existing three estimators
- Receiving event-level data through the scheduler
- Emitting `ModelOutput.total` / `ModelOutput.clean` via the geometric formula plus a `DeathExtras` payload
- Round-tripping through state serialization, API schemas, and frontend type codegen
- Covered by unit tests for every math piece and an end-to-end integration test

**Not done (deferred per the spec's Out-of-scope list):**
- Death-aware allocator (existing greedy still uses `total.expected_ms` unchanged)
- Frontend death-curve visualization (data is on the wire, no renderer yet)
- Population priors (`get_priors` returns `{}` by default)
- Screen-awareness / per-screen breakdown
- Learning-curve / asymptote projections

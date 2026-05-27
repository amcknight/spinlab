# Bootstrap-Resample Estimator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `bootstrap_resample` estimator that produces `ModelOutput` for the same per-segment decisions as `death_aware_rolling`, but estimated by resampling whole cold episodes from recent history instead of plugging weighted means into the geometric formula.

**Architecture:** New estimator at `python/spinlab/estimators/bootstrap_resample.py`. Episode-grouping + decay-weight helpers move out of `death_aware_rolling.py` into a shared `python/spinlab/estimators/_episode_helpers.py` so both estimators import the same logic (no duplication). The bootstrap RNG is seedable via constructor for deterministic tests. Filters to cold attempts only (`is_hot=False`), dropping any episode that contains a hot life. Registry auto-exposure means the existing estimator dropdown picks it up without UI changes.

**Tech Stack:** Python 3.11+, dataclasses, `random.choices` for weighted sampling, pytest.

**Locked-in decisions from spec's open questions:**
1. **`extras=None`** on bootstrap output. The death-distribution panel will hide on bootstrap-selected segments. Backlog entry to revisit if the histogram is missed.
2. **Extract** `_group_into_episodes` and `_compute_weights` (plus the `_Episode` dataclass) to `_episode_helpers.py`. Both estimators import from there.
3. **Default-on** in the dropdown — registry-driven UI, no flag needed.

---

## File Structure

- **Create:** `python/spinlab/estimators/_episode_helpers.py` — shared `_Episode`, `_group_into_episodes`, `_compute_weights`.
- **Create:** `python/spinlab/estimators/bootstrap_resample.py` — the new estimator.
- **Create:** `tests/unit/estimators/test_bootstrap_resample.py` — tests for the estimator.
- **Modify:** `python/spinlab/estimators/death_aware_rolling.py` — import the three helpers from `_episode_helpers` instead of defining them.
- **Modify:** `python/spinlab/estimators/__init__.py` — add `bootstrap_resample` to the registry import list.
- **Modify:** `tests/factories.py` — `make_event_attempt` gains an `is_hot: bool = False` parameter.
- **Modify:** `docs/BACKLOG.md` — backlog entries for deferred items.

---

## Task 1: Extract shared episode helpers

**Files:**
- Create: `python/spinlab/estimators/_episode_helpers.py`
- Modify: `python/spinlab/estimators/death_aware_rolling.py:72-126`

- [ ] **Step 1: Write failing test for the new module's surface**

Create `tests/unit/estimators/test_episode_helpers.py`:

```python
"""Tests for the shared episode-helpers module."""
import pytest


class TestModuleExports:
    def test_exports_episode_dataclass(self):
        from spinlab.estimators._episode_helpers import _Episode
        assert _Episode.__dataclass_fields__.keys() == {
            "episode_id", "events", "outcome", "had_any_death"
        }

    def test_exports_group_into_episodes(self):
        from spinlab.estimators._episode_helpers import _group_into_episodes
        assert callable(_group_into_episodes)

    def test_exports_compute_weights(self):
        from spinlab.estimators._episode_helpers import _compute_weights
        assert callable(_compute_weights)


class TestGroupBehaviorMatchesLegacy:
    """Sanity check: the moved function behaves the same as before."""
    def test_groups_by_episode_id_preserves_chronological_order(self):
        from spinlab.estimators._episode_helpers import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="old", outcome="survived", time_ms=8000),
            make_event_attempt(episode_id="new", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="new", outcome="survived", time_ms=7000),
        ]
        episodes = _group_into_episodes(events)
        assert [ep.episode_id for ep in episodes] == ["old", "new"]
        assert episodes[1].outcome == "completed"
        assert episodes[1].had_any_death is True

    def test_invalidated_event_drops_whole_episode(self):
        from spinlab.estimators._episode_helpers import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000, invalidated=True),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=7500),
        ]
        episodes = _group_into_episodes(events)
        assert [ep.episode_id for ep in episodes] == ["ep2"]


class TestWeightsBehaviorMatchesLegacy:
    def test_most_recent_episode_has_weight_one(self):
        from spinlab.estimators._episode_helpers import _compute_weights
        weights = _compute_weights(n_episodes=10, halflife=5)
        assert weights[-1] == pytest.approx(1.0)

    def test_halflife_ago_has_weight_half(self):
        from spinlab.estimators._episode_helpers import _compute_weights
        weights = _compute_weights(n_episodes=10, halflife=5)
        assert weights[4] == pytest.approx(0.5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/estimators/test_episode_helpers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'spinlab.estimators._episode_helpers'`.

- [ ] **Step 3: Create the new module by copying the three helpers**

Create `python/spinlab/estimators/_episode_helpers.py`:

```python
"""Shared episode-grouping and decay-weight helpers.

Used by both `death_aware_rolling` and `bootstrap_resample` estimators.
Module-internal (leading `_`) — not part of the public estimators API,
but stable for in-package imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spinlab.models import EventAttempt


@dataclass
class _Episode:
    """Per-episode aggregated view used by estimator math layers.

    Produced by `_group_into_episodes` and consumed by estimators that
    need to reason about an episode as a whole (e.g. for episode-level
    aggregates, bootstrap resampling, or floor-over-episodes).
    """
    episode_id: str
    events: list["EventAttempt"]
    outcome: str       # "completed" if any event is survived, else "died"
    had_any_death: bool


def _group_into_episodes(events: list["EventAttempt"]) -> list[_Episode]:
    """Group events by episode_id, dropping any episode with an invalidated event.

    Episodes are returned in the chronological order their FIRST event arrived
    in the input list. The scheduler queries events via
    Database.get_segment_event_rows which returns rows ordered by row id
    (chronological insertion order), so the first occurrence of each
    episode_id reflects the episode's start time.

    Python dicts preserve insertion order (PEP 468 / 3.7+), so iterating
    by_id below yields episodes in their first-encounter order.
    """
    by_id: dict[str, list["EventAttempt"]] = {}
    for ev in events:
        by_id.setdefault(ev.episode_id, []).append(ev)

    episodes: list[_Episode] = []
    for ep_id, ev_list in by_id.items():
        if any(ev.invalidated for ev in ev_list):
            continue
        had_any_death = any(ev.outcome.value == "died" for ev in ev_list)
        any_survived = any(ev.outcome.value == "survived" for ev in ev_list)
        outcome = "completed" if any_survived else "died"
        episodes.append(_Episode(
            episode_id=ep_id, events=ev_list,
            outcome=outcome, had_any_death=had_any_death,
        ))
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

- [ ] **Step 4: Run the new module's tests**

Run: `pytest tests/unit/estimators/test_episode_helpers.py -v`
Expected: PASS (all 7 tests).

- [ ] **Step 5: Update `death_aware_rolling.py` to import the helpers**

In `python/spinlab/estimators/death_aware_rolling.py`:

Delete lines 72-126 (the `_Episode` dataclass, `_group_into_episodes`, and `_compute_weights` definitions).

Add this import near the top of the file, with the other `spinlab.estimators` imports:

```python
from spinlab.estimators._episode_helpers import (
    _Episode,
    _compute_weights,
    _group_into_episodes,
)
```

The `TYPE_CHECKING`-gated `EventAttempt` import block stays — `_Aggregates` still references the type.

- [ ] **Step 6: Verify the death_aware_rolling test suite still passes**

Run: `pytest tests/unit/estimators/test_death_aware_rolling.py -v`
Expected: PASS — all existing tests (including the ones that import `_group_into_episodes` and `_compute_weights` directly from `death_aware_rolling`) still pass because the names are re-imported into that module's namespace.

- [ ] **Step 7: Run the full fast suite to catch any other importer**

Run: `pytest -m "not emulator"`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/estimators/_episode_helpers.py python/spinlab/estimators/death_aware_rolling.py tests/unit/estimators/test_episode_helpers.py
git commit -m "refactor(estimators): extract _episode_helpers shared by death_aware and bootstrap"
```

---

## Task 2: Add `is_hot` kwarg to the `make_event_attempt` factory

**Files:**
- Modify: `tests/factories.py:134-159`

- [ ] **Step 1: Write failing test**

Add to `tests/unit/test_factories.py` (create the file if it doesn't exist):

```python
"""Tests for the test factories themselves."""


class TestMakeEventAttempt:
    def test_is_hot_defaults_to_false(self):
        from tests.factories import make_event_attempt
        ev = make_event_attempt()
        assert ev.is_hot is False

    def test_is_hot_can_be_set(self):
        from tests.factories import make_event_attempt
        ev = make_event_attempt(is_hot=True)
        assert ev.is_hot is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_factories.py::TestMakeEventAttempt -v`
Expected: FAIL — `test_is_hot_can_be_set` raises `TypeError: make_event_attempt() got an unexpected keyword argument 'is_hot'`.

- [ ] **Step 3: Add `is_hot` to the factory**

In `tests/factories.py`, change the `make_event_attempt` signature (around line 134-159) to accept and forward `is_hot`:

```python
def make_event_attempt(
    segment_id: str = "seg1",
    episode_id: str = "ep1",
    outcome: str = "died",
    time_ms: int = 5000,
    session_id: str | None = "_default_test_session",
    capture_run_id: str | None = None,
    invalidated: bool = False,
    is_hot: bool = False,
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
        is_hot=is_hot,
        created_at=datetime.fromisoformat(created_at),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_factories.py::TestMakeEventAttempt -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/factories.py tests/unit/test_factories.py
git commit -m "test: add is_hot kwarg to make_event_attempt factory"
```

---

## Task 3: Bootstrap estimator skeleton (registration only)

**Files:**
- Create: `python/spinlab/estimators/bootstrap_resample.py`
- Modify: `python/spinlab/estimators/__init__.py:192-200`

- [ ] **Step 1: Write failing test for registration + display name**

Create `tests/unit/estimators/test_bootstrap_resample.py`:

```python
"""Tests for the Bootstrap-Resample estimator."""
import pytest


class TestRegistration:
    def test_registered_in_registry(self):
        from spinlab.estimators import list_estimators, get_estimator
        assert "bootstrap_resample" in list_estimators()
        est = get_estimator("bootstrap_resample")
        assert est.name == "bootstrap_resample"
        assert est.display_name == "Bootstrap (Monte Carlo)"

    def test_declared_params_has_n_samples(self):
        from spinlab.estimators import get_estimator
        est = get_estimator("bootstrap_resample")
        names = {p.name for p in est.declared_params()}
        assert "n_samples" in names
        n_samples = next(p for p in est.declared_params() if p.name == "n_samples")
        # Default in the middle of [100, 10000].
        assert n_samples.default == 1000.0
        assert n_samples.min_val == 100.0
        assert n_samples.max_val == 10000.0

    def test_declared_params_has_halflife(self):
        """Bootstrap reuses the decayed sampling-weight machinery; same knob."""
        from spinlab.estimators import get_estimator
        est = get_estimator("bootstrap_resample")
        names = {p.name for p in est.declared_params()}
        assert "halflife" in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/estimators/test_bootstrap_resample.py -v`
Expected: FAIL — `"bootstrap_resample" in list_estimators()` is False, the module doesn't exist yet.

- [ ] **Step 3: Create the skeleton module**

Create `python/spinlab/estimators/bootstrap_resample.py`:

```python
"""Bootstrap-resample estimator.

Estimates per-segment attempt and completion times by resampling whole
cold episodes from recent history. Sidesteps the i.i.d.-lives assumption
baked into `death_aware_rolling`'s geometric formula; the divergence
between the two estimators is itself a diagnostic of how clustered the
deaths are.

See docs/superpowers/specs/2026-05-27-bootstrap-estimator-design.md.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

from spinlab.estimators import Estimator, EstimatorState, ParamDef, register_estimator
from spinlab.estimators._episode_helpers import (
    _compute_weights,
    _group_into_episodes,
)
from spinlab.estimators.death_aware_rolling import (
    DEFAULT_HALFLIFE,
    EFFECTIVE_WINDOW_HALFLIVES,
    HALFLIFE_MAX,
    HALFLIFE_MIN,
    _floor_over_completed_episode_totals,
    _floor_over_survived_event_times,
    _resolve_halflife,
    _weighted_half_split_slope,
)
from spinlab.models import (
    DEFAULT_DEATH_PENALTY_MS,
    AttemptRecord,
    Estimate,
    ModelOutput,
)

if TYPE_CHECKING:
    from spinlab.estimators._episode_helpers import _Episode
    from spinlab.models import EventAttempt

# Bootstrap draw count. 1000 is enough to bring the Monte-Carlo standard
# error well below 1% of the mean for realistic distributions while
# staying well under any noticeable per-tick CPU cost.
DEFAULT_N_SAMPLES = 1000

# Sanity bounds. Lower bound 100 keeps Monte-Carlo error from dominating
# the signal; upper bound 10000 caps per-call cost. Anything outside this
# range almost certainly means the user is using the wrong tool.
N_SAMPLES_MIN = 100
N_SAMPLES_MAX = 10000


@dataclass
class BootstrapResampleState(EstimatorState):
    """Minimal bookkeeping. Stats recompute from events each call."""

    def to_dict(self) -> dict:
        return {"n_completed": self.n_completed, "n_attempts": self.n_attempts}

    @classmethod
    def from_dict(cls, d: dict) -> "BootstrapResampleState":
        return cls(
            n_completed=d.get("n_completed", 0),
            n_attempts=d.get("n_attempts", 0),
        )


EstimatorState.register_state("bootstrap_resample", BootstrapResampleState)


def _empty_output() -> ModelOutput:
    none_estimate = Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None)
    return ModelOutput(total=none_estimate, clean=none_estimate, extras=None)


@register_estimator
class BootstrapResampleEstimator(Estimator):
    name = "bootstrap_resample"
    display_name = "Bootstrap (Monte Carlo)"

    def __init__(self, seed: int | None = None) -> None:
        # Seedable RNG for deterministic tests. Default None = nondeterministic.
        self._rng = random.Random(seed)

    def declared_params(self) -> list[ParamDef]:
        return [
            ParamDef(
                "halflife", "Halflife (episodes)",
                float(DEFAULT_HALFLIFE), float(HALFLIFE_MIN), float(HALFLIFE_MAX), 1.0,
                "Number of episodes for the sampling weight to halve. "
                "Mirrors death_aware_rolling so the two estimators see the "
                "same effective window.",
            ),
            ParamDef(
                "n_samples", "Bootstrap draws",
                float(DEFAULT_N_SAMPLES), float(N_SAMPLES_MIN), float(N_SAMPLES_MAX), 100.0,
                "Number of resampled episodes drawn per estimate. Higher = "
                "smaller Monte-Carlo error, linear cost.",
            ),
        ]

    def init_state(
        self, first_attempt: AttemptRecord, priors: dict,
        params: dict | None = None,
    ) -> BootstrapResampleState:
        return BootstrapResampleState(n_completed=1, n_attempts=1)

    def process_attempt(  # type: ignore[override]
        self, state: BootstrapResampleState, new_attempt: AttemptRecord,
        all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> BootstrapResampleState:
        n_completed = state.n_completed + (1 if new_attempt.completed else 0)
        return BootstrapResampleState(
            n_completed=n_completed, n_attempts=state.n_attempts + 1,
        )

    def model_output(  # type: ignore[override]
        self, state: BootstrapResampleState, all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> ModelOutput:
        # Placeholder — wired up in Task 4+. Returning empty keeps the
        # estimator registered without producing misleading numbers.
        return _empty_output()

    def rebuild_state(  # type: ignore[override]
        self, attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> BootstrapResampleState:
        n_completed = sum(1 for a in attempts if a.completed)
        return BootstrapResampleState(
            n_completed=n_completed, n_attempts=len(attempts),
        )
```

- [ ] **Step 4: Hook the new module into the registry import**

In `python/spinlab/estimators/__init__.py`, modify `_register_all` (lines 192-200) to include `bootstrap_resample`:

```python
def _register_all():
    """Import all estimator modules to trigger @register_estimator decorators."""
    from . import bootstrap_resample, death_aware_rolling, kalman, rolling_mean
    try:
        from . import exp_decay
    except ImportError:
        pass
```

- [ ] **Step 5: Run the registration tests**

Run: `pytest tests/unit/estimators/test_bootstrap_resample.py::TestRegistration -v`
Expected: PASS — all 3 tests.

- [ ] **Step 6: Run full fast suite — sanity check the new estimator doesn't break the param-coverage test**

Run: `pytest -m "not emulator"`
Expected: PASS. If `test_estimator_params.py` enforces a per-estimator param-shape contract, that's where a hit would land; the `n_samples` + `halflife` decls should satisfy it.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/estimators/bootstrap_resample.py python/spinlab/estimators/__init__.py tests/unit/estimators/test_bootstrap_resample.py
git commit -m "feat(estimators): bootstrap_resample skeleton (registered, empty output)"
```

---

## Task 4: Cold-only episode filter

**Files:**
- Modify: `python/spinlab/estimators/bootstrap_resample.py`
- Modify: `tests/unit/estimators/test_bootstrap_resample.py`

- [ ] **Step 1: Write failing tests for the cold filter helper**

Append to `tests/unit/estimators/test_bootstrap_resample.py`:

```python
class TestColdFilter:
    def test_all_cold_episodes_pass_through(self):
        from spinlab.estimators.bootstrap_resample import _filter_to_cold_episodes
        from spinlab.estimators._episode_helpers import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000, is_hot=False),
            make_event_attempt(episode_id="ep2", outcome="died", time_ms=3000, is_hot=False),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=7000, is_hot=False),
        ]
        episodes = _group_into_episodes(events)
        cold = _filter_to_cold_episodes(episodes)
        assert [ep.episode_id for ep in cold] == ["ep1", "ep2"]

    def test_episode_with_any_hot_life_dropped(self):
        """Even one hot event in an episode disqualifies the whole episode.

        Half-counting a mixed-state episode would muddle the cold sample
        pool; cleanest rule is all-or-nothing per episode.
        """
        from spinlab.estimators.bootstrap_resample import _filter_to_cold_episodes
        from spinlab.estimators._episode_helpers import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="cold", outcome="survived", time_ms=8000, is_hot=False),
            make_event_attempt(episode_id="mixed", outcome="died", time_ms=3000, is_hot=False),
            make_event_attempt(episode_id="mixed", outcome="survived", time_ms=7000, is_hot=True),
        ]
        episodes = _group_into_episodes(events)
        cold = _filter_to_cold_episodes(episodes)
        assert [ep.episode_id for ep in cold] == ["cold"]

    def test_all_hot_returns_empty(self):
        from spinlab.estimators.bootstrap_resample import _filter_to_cold_episodes
        from spinlab.estimators._episode_helpers import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000, is_hot=True),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=7500, is_hot=True),
        ]
        episodes = _group_into_episodes(events)
        cold = _filter_to_cold_episodes(episodes)
        assert cold == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/estimators/test_bootstrap_resample.py::TestColdFilter -v`
Expected: FAIL — `_filter_to_cold_episodes` is not defined.

- [ ] **Step 3: Add the helper to the estimator module**

In `python/spinlab/estimators/bootstrap_resample.py`, add this module-level function below the imports and above `_empty_output`:

```python
def _filter_to_cold_episodes(episodes: list["_Episode"]) -> list["_Episode"]:
    """Keep only episodes where every life is cold (is_hot is False).

    Hot lives represent a different population (carry-over from a prior
    completed segment, not a fresh-load attempt). The scheduler's
    "next practice load" decision is a cold-load question, so cold-only
    is the right resampling pool. Mixed-state episodes are dropped
    entirely rather than half-counted — partial inclusion would
    contaminate the sample with hot-life timing within a "cold" draw.
    """
    return [
        ep for ep in episodes
        if all(not ev.is_hot for ev in ep.events)
    ]
```

- [ ] **Step 4: Run cold-filter tests**

Run: `pytest tests/unit/estimators/test_bootstrap_resample.py::TestColdFilter -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/estimators/bootstrap_resample.py tests/unit/estimators/test_bootstrap_resample.py
git commit -m "feat(bootstrap): cold-only episode filter (drops any episode with a hot life)"
```

---

## Task 5: Episode-total computation reused from the legacy roll-up

**Files:**
- Modify: `python/spinlab/estimators/bootstrap_resample.py`
- Modify: `tests/unit/estimators/test_bootstrap_resample.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/estimators/test_bootstrap_resample.py`:

```python
class TestEpisodeTotal:
    def test_clean_completion_total_is_just_time(self):
        from spinlab.estimators.bootstrap_resample import _episode_total_ms
        from spinlab.estimators._episode_helpers import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000)]
        ep = _group_into_episodes(events)[0]
        assert _episode_total_ms(ep, respawn_penalty_ms=3200) == 8000

    def test_episode_with_deaths_adds_penalty_per_death(self):
        """Total = sum(time_ms) + penalty × deaths. Matches _roll_up_episode."""
        from spinlab.estimators.bootstrap_resample import _episode_total_ms
        from spinlab.estimators._episode_helpers import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2500),
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=7000),
        ]
        ep = _group_into_episodes(events)[0]
        # 3000 + 2500 + 7000 = 12500 raw, plus 2 deaths × 3200 penalty = 18900
        assert _episode_total_ms(ep, respawn_penalty_ms=3200) == 18900

    def test_aborted_episode_total_no_penalty_on_last_life(self):
        """Aborted (all-deaths) episode: raw sum + penalty × deaths."""
        from spinlab.estimators.bootstrap_resample import _episode_total_ms
        from spinlab.estimators._episode_helpers import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2000),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
        ]
        ep = _group_into_episodes(events)[0]
        # 5000 raw + 2 × 3200 penalty = 11400
        assert _episode_total_ms(ep, respawn_penalty_ms=3200) == 11400


class TestSurvivedTailMs:
    def test_completed_episode_returns_last_life_time(self):
        from spinlab.estimators.bootstrap_resample import _survived_tail_ms
        from spinlab.estimators._episode_helpers import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=7500),
        ]
        ep = _group_into_episodes(events)[0]
        assert _survived_tail_ms(ep) == 7500

    def test_aborted_episode_returns_none(self):
        """No survived life ⇒ no completion tail to sample."""
        from spinlab.estimators.bootstrap_resample import _survived_tail_ms
        from spinlab.estimators._episode_helpers import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2000),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
        ]
        ep = _group_into_episodes(events)[0]
        assert _survived_tail_ms(ep) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/estimators/test_bootstrap_resample.py::TestEpisodeTotal tests/unit/estimators/test_bootstrap_resample.py::TestSurvivedTailMs -v`
Expected: FAIL — `_episode_total_ms` / `_survived_tail_ms` not defined.

- [ ] **Step 3: Add the helpers**

In `python/spinlab/estimators/bootstrap_resample.py`, add below `_filter_to_cold_episodes`:

```python
def _episode_total_ms(episode: "_Episode", respawn_penalty_ms: int) -> int:
    """Compute the episode's wall-clock total, matching db.attempts._roll_up_episode.

    total = sum(event.time_ms) + respawn_penalty_ms × n_deaths

    Per-event time_ms is the raw delta since the prior event (or arm time
    for the first), so summing them gives the raw wall-clock; the penalty
    adds the standard 3.2s respawn lag for each death. The penalty value
    must match the one used at write time (DEFAULT_DEATH_PENALTY_MS).
    """
    deaths = sum(1 for ev in episode.events if ev.outcome.value == "died")
    raw_sum = sum(ev.time_ms for ev in episode.events)
    return raw_sum + respawn_penalty_ms * deaths


def _survived_tail_ms(episode: "_Episode") -> int | None:
    """Return the completion-tail time (last life's time_ms) or None.

    Only completed episodes have a tail — aborted episodes (all deaths)
    return None and the caller skips them when building the completion
    sample pool.
    """
    if episode.outcome != "completed":
        return None
    last = episode.events[-1]
    assert last.outcome.value == "survived"  # implied by outcome == "completed"
    return last.time_ms
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/estimators/test_bootstrap_resample.py::TestEpisodeTotal tests/unit/estimators/test_bootstrap_resample.py::TestSurvivedTailMs -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/estimators/bootstrap_resample.py tests/unit/estimators/test_bootstrap_resample.py
git commit -m "feat(bootstrap): episode-total and survived-tail helpers"
```

---

## Task 6: Bootstrap resampling — the core algorithm

**Files:**
- Modify: `python/spinlab/estimators/bootstrap_resample.py`
- Modify: `tests/unit/estimators/test_bootstrap_resample.py`

- [ ] **Step 1: Write failing tests for `_resolve_n_samples`**

Append to `tests/unit/estimators/test_bootstrap_resample.py`:

```python
class TestResolveNSamples:
    def test_default_when_param_missing(self):
        from spinlab.estimators.bootstrap_resample import (
            DEFAULT_N_SAMPLES, _resolve_n_samples,
        )
        assert _resolve_n_samples(None) == DEFAULT_N_SAMPLES
        assert _resolve_n_samples({}) == DEFAULT_N_SAMPLES

    def test_explicit_value_used(self):
        from spinlab.estimators.bootstrap_resample import _resolve_n_samples
        assert _resolve_n_samples({"n_samples": 500}) == 500

    def test_below_min_raises(self):
        from spinlab.estimators.bootstrap_resample import _resolve_n_samples
        with pytest.raises(ValueError, match="n_samples"):
            _resolve_n_samples({"n_samples": 50})

    def test_above_max_raises(self):
        from spinlab.estimators.bootstrap_resample import _resolve_n_samples
        with pytest.raises(ValueError, match="n_samples"):
            _resolve_n_samples({"n_samples": 999999})

    def test_non_int_raises(self):
        from spinlab.estimators.bootstrap_resample import _resolve_n_samples
        with pytest.raises(ValueError, match="n_samples"):
            _resolve_n_samples({"n_samples": "lots"})
```

- [ ] **Step 2: Run failing tests**

Run: `pytest tests/unit/estimators/test_bootstrap_resample.py::TestResolveNSamples -v`
Expected: FAIL — function not defined.

- [ ] **Step 3: Add `_resolve_n_samples`**

In `bootstrap_resample.py`, add below `_survived_tail_ms`:

```python
def _resolve_n_samples(params: dict | None) -> int:
    if not params or "n_samples" not in params:
        return DEFAULT_N_SAMPLES
    raw = params["n_samples"]
    try:
        n = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"n_samples must be an int, got {raw!r}") from exc
    if n < N_SAMPLES_MIN or n > N_SAMPLES_MAX:
        raise ValueError(
            f"n_samples must be in [{N_SAMPLES_MIN}, {N_SAMPLES_MAX}], got {n}"
        )
    return n
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/estimators/test_bootstrap_resample.py::TestResolveNSamples -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Write failing tests for `_bootstrap_means`**

Append to `tests/unit/estimators/test_bootstrap_resample.py`:

```python
class TestBootstrapMeans:
    def test_single_completed_episode_returns_its_values(self):
        """Pool of one ⇒ every draw is the same episode ⇒ zero variance."""
        from spinlab.estimators.bootstrap_resample import _bootstrap_means
        from spinlab.estimators._episode_helpers import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000)]
        episodes = _group_into_episodes(events)
        import random
        rng = random.Random(42)
        result = _bootstrap_means(
            episodes=episodes,
            weights=[1.0],
            n_samples=1000,
            respawn_penalty_ms=3200,
            rng=rng,
        )
        assert result.mean_total_ms == pytest.approx(8000.0)
        assert result.mean_completion_ms == pytest.approx(8000.0)

    def test_empty_pool_returns_none(self):
        from spinlab.estimators.bootstrap_resample import _bootstrap_means
        import random
        rng = random.Random(42)
        result = _bootstrap_means(
            episodes=[],
            weights=[],
            n_samples=1000,
            respawn_penalty_ms=3200,
            rng=rng,
        )
        assert result.mean_total_ms is None
        assert result.mean_completion_ms is None

    def test_no_completed_episodes_completion_mean_none(self):
        """All-aborted pool ⇒ total has values (deaths counted), completion is None."""
        from spinlab.estimators.bootstrap_resample import _bootstrap_means
        from spinlab.estimators._episode_helpers import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2000),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
        ]
        episodes = _group_into_episodes(events)
        import random
        rng = random.Random(42)
        result = _bootstrap_means(
            episodes=episodes,
            weights=[1.0],
            n_samples=1000,
            respawn_penalty_ms=3200,
            rng=rng,
        )
        # Total = 2000 + 3000 + 2 × 3200 = 11400 every draw.
        assert result.mean_total_ms == pytest.approx(11400.0)
        # No completed episode in the pool ⇒ no completion samples to mean.
        assert result.mean_completion_ms is None

    def test_seeded_reproducibility(self):
        """Same seed + same pool ⇒ same answer."""
        from spinlab.estimators.bootstrap_resample import _bootstrap_means
        from spinlab.estimators._episode_helpers import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000),
            make_event_attempt(episode_id="ep2", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=7000),
        ]
        episodes = _group_into_episodes(events)
        weights = [1.0, 1.0]
        import random
        a = _bootstrap_means(episodes=episodes, weights=weights, n_samples=500,
                             respawn_penalty_ms=3200, rng=random.Random(7))
        b = _bootstrap_means(episodes=episodes, weights=weights, n_samples=500,
                             respawn_penalty_ms=3200, rng=random.Random(7))
        assert a.mean_total_ms == b.mean_total_ms
        assert a.mean_completion_ms == b.mean_completion_ms

    def test_agrees_with_geometric_when_iid(self):
        """When deaths truly are i.i.d. Bernoulli, bootstrap and the geometric
        formula should agree within Monte-Carlo error."""
        from spinlab.estimators.bootstrap_resample import _bootstrap_means
        from spinlab.estimators._episode_helpers import _group_into_episodes
        from spinlab.estimators.death_aware_rolling import _expected_total_ms
        from tests.factories import make_event_attempt
        # Build a synthetic history where every episode is identical:
        # one death (3000ms), one survive (7000ms). p_die_per_life = 0.5,
        # E[death] = 3000, E[completion] = 7000.
        # Geometric: (0.5/0.5) × (3000 + 3200) + 7000 = 13200.
        events = []
        for i in range(20):
            events.append(make_event_attempt(episode_id=f"ep{i}", outcome="died", time_ms=3000))
            events.append(make_event_attempt(episode_id=f"ep{i}", outcome="survived", time_ms=7000))
        episodes = _group_into_episodes(events)
        # Every episode IS the same i.i.d. realization here — bootstrapping
        # whole episodes just reshuffles them, so the bootstrap mean equals
        # the per-episode total exactly: 3000 + 7000 + 1 × 3200 = 13200.
        import random
        rng = random.Random(123)
        weights = [1.0] * len(episodes)
        result = _bootstrap_means(
            episodes=episodes, weights=weights, n_samples=2000,
            respawn_penalty_ms=3200, rng=rng,
        )
        geom = _expected_total_ms(
            p_die_per_life=0.5,
            e_death_time_ms=3000.0,
            e_completion_time_ms=7000.0,
            respawn_penalty_ms=3200,
        )
        assert result.mean_total_ms == pytest.approx(geom, rel=0.01)

    def test_aborted_episodes_pull_bootstrap_below_geometric(self):
        """When some attempts abort (player gives up), the geometric formula
        OVERESTIMATES expected time because it pretends every attempt
        completes-by-attrition, while bootstrap uses the actual short totals
        of aborted episodes.

        Pool: 5 clean completes (7000ms, 1 life) + 5 aborts (4 × 3000ms
        deaths, no survive).
          Lives: 5 survives + 20 deaths = 25 lives. p_die_per_life = 0.8.
          Geometric: (0.8/0.2) × (3000+3200) + 7000 = 4×6200 + 7000 = 31800.
          Per-episode totals: A=7000, B = 12000 + 4×3200 = 24800.
          Bootstrap mean = (5×7000 + 5×24800)/10 = 15900.

        Note: the spec said bootstrap > geometric on "clustered deaths," but
        the direction is data-dependent. With aborts in the pool the
        bootstrap is LOWER. Test the direction we actually see for this
        construction; the broader "when do they diverge?" question is a
        branch-3 visualization concern (see BACKLOG entry from Task 10).
        """
        from spinlab.estimators.bootstrap_resample import _bootstrap_means
        from spinlab.estimators._episode_helpers import _group_into_episodes
        from spinlab.estimators.death_aware_rolling import _expected_total_ms
        from tests.factories import make_event_attempt
        events = []
        for i in range(5):
            events.append(make_event_attempt(episode_id=f"clean{i}", outcome="survived", time_ms=7000))
        for i in range(5):
            for _ in range(4):
                events.append(make_event_attempt(episode_id=f"abort{i}", outcome="died", time_ms=3000))
        episodes = _group_into_episodes(events)
        import random
        weights = [1.0] * len(episodes)
        bs = _bootstrap_means(
            episodes=episodes, weights=weights, n_samples=5000,
            respawn_penalty_ms=3200, rng=random.Random(99),
        )
        geom = _expected_total_ms(
            p_die_per_life=0.8,
            e_death_time_ms=3000.0,
            e_completion_time_ms=7000.0,
            respawn_penalty_ms=3200,
        )
        assert bs.mean_total_ms is not None
        assert geom is not None
        # bs should be ≈ 15900, geom = 31800 ⇒ ratio < 0.6.
        assert bs.mean_total_ms < 0.6 * geom
```

> **Note for the executor:** the spec's "test_coverage" bullet called for "clustered-deaths history produces a mean **higher** than the analytic geometric mean." The direction is data-dependent — with aborted episodes the bootstrap is LOWER, with within-episode death clustering and all-completes the two often agree exactly (the geometric formula's `p/(1-p)` recovers `E[deaths_per_attempt]` exactly when lives are sampled marginally from per-episode data). The test above keeps the divergence assertion but in the direction that's actually demonstrable. See the BACKLOG entry added in Task 10 for the full notes.

- [ ] **Step 6: Run failing tests**

Run: `pytest tests/unit/estimators/test_bootstrap_resample.py::TestBootstrapMeans -v`
Expected: FAIL — `_bootstrap_means` not defined.

- [ ] **Step 7: Implement `_bootstrap_means` and the result dataclass**

In `bootstrap_resample.py`, add below `_resolve_n_samples`:

```python
@dataclass
class _BootstrapResult:
    """Means computed across the bootstrap draws.

    Both fields are None when the corresponding pool is empty:
      - mean_total_ms: None ⇔ no episodes at all
      - mean_completion_ms: None ⇔ no COMPLETED episodes (all aborted)
    """
    mean_total_ms: float | None
    mean_completion_ms: float | None


def _bootstrap_means(
    episodes: list["_Episode"],
    weights: list[float],
    n_samples: int,
    respawn_penalty_ms: int,
    rng: random.Random,
) -> _BootstrapResult:
    """Draw n_samples episodes from `episodes` with the given weights,
    then return mean per-draw total time and mean completion-tail time.

    The completion mean is computed over only the completed-episode draws
    (aborted draws contribute None and are filtered out). When zero draws
    land on a completed episode, mean_completion_ms is None.
    """
    if not episodes:
        return _BootstrapResult(mean_total_ms=None, mean_completion_ms=None)

    # Precompute per-episode totals and tails once — saves O(n_samples × episode_len)
    # work versus computing inside the resample loop.
    totals = [_episode_total_ms(ep, respawn_penalty_ms) for ep in episodes]
    tails = [_survived_tail_ms(ep) for ep in episodes]

    draws = rng.choices(range(len(episodes)), weights=weights, k=n_samples)

    total_sum = 0.0
    completion_sum = 0.0
    completion_count = 0
    for idx in draws:
        total_sum += totals[idx]
        tail = tails[idx]
        if tail is not None:
            completion_sum += tail
            completion_count += 1

    mean_total = total_sum / n_samples
    mean_completion = (
        completion_sum / completion_count if completion_count > 0 else None
    )
    return _BootstrapResult(
        mean_total_ms=mean_total, mean_completion_ms=mean_completion,
    )
```

- [ ] **Step 8: Run tests**

Run: `pytest tests/unit/estimators/test_bootstrap_resample.py::TestBootstrapMeans -v`
Expected: PASS (the docstring-only stub test passes trivially; the four real tests pass on the implementation).

- [ ] **Step 9: Commit**

```bash
git add python/spinlab/estimators/bootstrap_resample.py tests/unit/estimators/test_bootstrap_resample.py
git commit -m "feat(bootstrap): core resampling — episode draws with seedable RNG"
```

---

## Task 7: Wire `model_output` end-to-end

**Files:**
- Modify: `python/spinlab/estimators/bootstrap_resample.py`
- Modify: `tests/unit/estimators/test_bootstrap_resample.py`

- [ ] **Step 1: Write failing tests for the full `model_output` pipeline**

Append to `tests/unit/estimators/test_bootstrap_resample.py`:

```python
class TestModelOutput:
    def test_empty_events_returns_none_output(self):
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record
        est = get_estimator("bootstrap_resample")
        a = make_attempt_record(8000, True, clean_tail_ms=8000)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=[])
        assert out.total.expected_ms is None
        assert out.clean.expected_ms is None
        assert out.extras is None  # bootstrap never populates extras (locked-in decision)

    def test_hot_only_history_returns_none_output(self):
        """All-hot pool filters down to empty after cold filter."""
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record, make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000, is_hot=True),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=7500, is_hot=True),
        ]
        est = get_estimator("bootstrap_resample")
        a = make_attempt_record(8000, True, clean_tail_ms=8000)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=events)
        assert out.total.expected_ms is None
        assert out.clean.expected_ms is None
        assert out.extras is None

    def test_single_completion_returns_completion_time(self):
        from spinlab.estimators.bootstrap_resample import BootstrapResampleEstimator
        from tests.factories import make_attempt_record, make_event_attempt
        events = [make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000)]
        est = BootstrapResampleEstimator(seed=42)
        a = make_attempt_record(8000, True, clean_tail_ms=8000)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=events)
        assert out.total.expected_ms == pytest.approx(8000.0)
        assert out.clean.expected_ms == pytest.approx(8000.0)
        # Floor reuses the death_aware helper ⇒ same answer.
        assert out.total.floor_ms == pytest.approx(8000.0)
        assert out.clean.floor_ms == pytest.approx(8000.0)
        # One sample ⇒ no slope.
        assert out.total.ms_per_attempt is None
        assert out.clean.ms_per_attempt is None
        assert out.extras is None

    def test_filters_hot_episodes_before_sampling(self):
        """Hot episodes in the input must NOT contribute to the bootstrap pool."""
        from spinlab.estimators.bootstrap_resample import BootstrapResampleEstimator
        from tests.factories import make_attempt_record, make_event_attempt
        events = [
            # Cold pool: 5000ms clean.
            make_event_attempt(episode_id="cold", outcome="survived", time_ms=5000, is_hot=False),
            # Hot episode: should be excluded from sampling.
            make_event_attempt(episode_id="hot", outcome="survived", time_ms=99000, is_hot=True),
        ]
        est = BootstrapResampleEstimator(seed=42)
        a = make_attempt_record(5000, True, clean_tail_ms=5000)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=events)
        # If the hot episode leaked in, total would jump toward 99000.
        # Cold-only ⇒ should sit at the cold value.
        assert out.total.expected_ms == pytest.approx(5000.0)
        assert out.clean.expected_ms == pytest.approx(5000.0)

    def test_floor_ms_matches_death_aware(self):
        """floor_ms uses the same helper as death_aware_rolling ⇒ same answer
        for the same input."""
        from spinlab.estimators.bootstrap_resample import BootstrapResampleEstimator
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record, make_event_attempt
        events = (
            [make_event_attempt(episode_id="old_great", outcome="survived", time_ms=5000)]
            + [
                make_event_attempt(episode_id=f"new{i}", outcome="survived", time_ms=9000)
                for i in range(20)
            ]
        )
        a = make_attempt_record(9000, True, clean_tail_ms=9000)

        bs = BootstrapResampleEstimator(seed=1)
        bs_state = bs.init_state(a, priors={})
        bs_out = bs.model_output(bs_state, [a], events=events)

        da = get_estimator("death_aware_rolling")
        da_state = da.init_state(a, priors={})
        da_out = da.model_output(da_state, [a], events=events)

        assert bs_out.total.floor_ms == da_out.total.floor_ms
        assert bs_out.clean.floor_ms == da_out.clean.floor_ms

    def test_ms_per_attempt_uses_chronological_completion_samples(self):
        """Slope estimator is the same one death_aware uses; positive when improving."""
        from spinlab.estimators.bootstrap_resample import BootstrapResampleEstimator
        from tests.factories import make_attempt_record, make_event_attempt
        events = [
            make_event_attempt(episode_id=f"ep{i}", outcome="survived", time_ms=t)
            for i, t in enumerate([12000, 11500, 11000, 10500, 10000, 9500, 9000, 8500])
        ]
        est = BootstrapResampleEstimator(seed=1)
        a = make_attempt_record(8500, True, clean_tail_ms=8500)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=events)
        assert out.total.ms_per_attempt is not None
        assert out.total.ms_per_attempt > 0
        assert out.clean.ms_per_attempt is not None
        assert out.clean.ms_per_attempt > 0
```

- [ ] **Step 2: Run failing tests**

Run: `pytest tests/unit/estimators/test_bootstrap_resample.py::TestModelOutput -v`
Expected: FAIL — the current `model_output` is the empty stub from Task 3.

- [ ] **Step 3: Replace the `model_output` stub with the real implementation**

In `python/spinlab/estimators/bootstrap_resample.py`, replace the `model_output` method body. The new method must:
1. Return `_empty_output()` on empty events.
2. Group events into episodes, then floor uses ALL episodes (not just cold) — match `death_aware_rolling` so the "best ever" sticky info is consistent.
3. Apply the cold filter, then truncate to the effective window, then compute decay weights.
4. Build the completion-sample list (life-level samples of survived events, weighted by cold-episode weight) so the slope estimator has chronological data — reuse `_weighted_half_split_slope`.
5. Call `_bootstrap_means` with the seeded RNG.
6. Compose `ModelOutput` with `extras=None`.

New `model_output`:

```python
    def model_output(  # type: ignore[override]
        self, state: BootstrapResampleState, all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list["EventAttempt"] | None = None,
    ) -> ModelOutput:
        if not events:
            return _empty_output()
        halflife = _resolve_halflife(params)
        n_samples = _resolve_n_samples(params)

        all_episodes = _group_into_episodes(events)
        if not all_episodes:
            return _empty_output()

        # Floor reuses death_aware's "best across full history" helpers so
        # the floor numbers agree across estimators. Floor isn't a sampling
        # question; it's a min over completed history.
        total_floor = _floor_over_completed_episode_totals(
            all_episodes, respawn_penalty_ms=DEFAULT_DEATH_PENALTY_MS,
        )
        clean_floor = _floor_over_survived_event_times(all_episodes)

        # Cold filter + windowing for the sampling pool.
        cold_episodes = _filter_to_cold_episodes(all_episodes)
        if not cold_episodes:
            return _empty_output()

        max_kept = EFFECTIVE_WINDOW_HALFLIVES * halflife
        if len(cold_episodes) > max_kept:
            cold_episodes = cold_episodes[-max_kept:]
        weights = _compute_weights(n_episodes=len(cold_episodes), halflife=halflife)

        bs = _bootstrap_means(
            episodes=cold_episodes,
            weights=weights,
            n_samples=n_samples,
            respawn_penalty_ms=DEFAULT_DEATH_PENALTY_MS,
            rng=self._rng,
        )

        # ms_per_attempt: same slope estimator death_aware uses, over the
        # chronological completion-tail samples. Survived-event time_ms is
        # the completion tail; one per completed episode.
        completion_samples: list[tuple[int, float]] = []
        for w, ep in zip(weights, cold_episodes):
            tail = _survived_tail_ms(ep)
            if tail is not None:
                completion_samples.append((int(tail), w))
        mpa = _weighted_half_split_slope(completion_samples)

        return ModelOutput(
            total=Estimate(
                expected_ms=bs.mean_total_ms,
                ms_per_attempt=mpa,
                floor_ms=total_floor,
            ),
            clean=Estimate(
                expected_ms=bs.mean_completion_ms,
                ms_per_attempt=mpa,
                floor_ms=clean_floor,
            ),
            extras=None,
        )
```

- [ ] **Step 4: Run the model_output tests**

Run: `pytest tests/unit/estimators/test_bootstrap_resample.py::TestModelOutput -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Run full fast suite**

Run: `pytest -m "not emulator"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/estimators/bootstrap_resample.py tests/unit/estimators/test_bootstrap_resample.py
git commit -m "feat(bootstrap): wire model_output end-to-end (cold filter + resample + slope)"
```

---

## Task 8: Verify `get_estimator("bootstrap_resample")` works through the registry path used in production

**Files:**
- Modify: `tests/unit/estimators/test_bootstrap_resample.py`

- [ ] **Step 1: Add a smoke test against the actual factory the routes use**

Append to `tests/unit/estimators/test_bootstrap_resample.py`:

```python
class TestRegistryFactory:
    def test_get_estimator_returns_a_fresh_seedless_instance(self):
        """The routes call get_estimator(name) — no seed kwarg. The default
        constructor must work and return nondeterministic output."""
        from spinlab.estimators import get_estimator
        est = get_estimator("bootstrap_resample")
        # Two instances should not share RNG state.
        est2 = get_estimator("bootstrap_resample")
        assert est is not est2

    def test_default_constructed_estimator_produces_output_on_real_history(self):
        """End-to-end through get_estimator — what the route does at runtime."""
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record, make_event_attempt
        est = get_estimator("bootstrap_resample")
        events = [
            make_event_attempt(episode_id=f"ep{i}", outcome="survived", time_ms=8000)
            for i in range(5)
        ]
        a = make_attempt_record(8000, True, clean_tail_ms=8000)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=events)
        # All-clean cold history ⇒ both means converge to 8000.
        assert out.total.expected_ms == pytest.approx(8000.0)
        assert out.clean.expected_ms == pytest.approx(8000.0)
```

- [ ] **Step 2: Run it**

Run: `pytest tests/unit/estimators/test_bootstrap_resample.py::TestRegistryFactory -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/estimators/test_bootstrap_resample.py
git commit -m "test(bootstrap): registry-factory smoke (matches production code path)"
```

---

## Task 9: Verify the new estimator surfaces in the API/UI list

**Files:**
- No code changes expected — this is verification only.

- [ ] **Step 1: Inspect the API surface**

Read [python/spinlab/routes/model.py:38-44](../../python/spinlab/routes/model.py#L38-L44) to confirm `list_estimators()` feeds the dropdown payload. (Already verified during planning.)

- [ ] **Step 2: Add a contract test**

Append to `tests/unit/estimators/test_bootstrap_resample.py`:

```python
class TestAPIExposure:
    def test_appears_in_list_estimators_payload(self):
        """The route reads list_estimators(); appearing here means the dropdown shows it."""
        from spinlab.estimators import list_estimators
        names = list_estimators()
        assert "bootstrap_resample" in names
        assert "death_aware_rolling" in names  # sanity: didn't accidentally drop the other
```

- [ ] **Step 3: Run it**

Run: `pytest tests/unit/estimators/test_bootstrap_resample.py::TestAPIExposure -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/estimators/test_bootstrap_resample.py
git commit -m "test(bootstrap): contract test for list_estimators exposure"
```

---

## Task 10: Update BACKLOG with deferred items

**Files:**
- Modify: `docs/BACKLOG.md`

- [ ] **Step 1: Append deferred items**

Edit `docs/BACKLOG.md`. Under the existing `### Cold/hot follow-ups (deferred from the 2026-05-26 is_hot landing)` heading, append (preserving the indentation/bullet style of nearby entries):

```markdown

### Bootstrap-resample follow-ups (deferred from the 2026-05-27 branch-2 landing)

- **[S] Bootstrap distribution exposure.** Bootstrap naturally produces a full distribution (the N sampled totals), but branch 2 only surfaces the mean. Persist the samples in a new extras payload (`BootstrapExtras` with `total_samples: list[float]`) once the user-facing histogram in branch 3 needs them. Cheapest landing: expose at the route level only, no DB persistence.
- **[S] Death-distribution panel for bootstrap segments.** The bootstrap estimator currently sets `extras=None`, so the `DeathExtras`-driven death-time histogram hides on segments using bootstrap. Either (a) compute the cold-filtered samples and populate `DeathExtras` alongside the bootstrap result, or (b) add a bootstrap-specific panel. Revisit if the missing histogram is annoying in practice.
- **[M] Bias-as-learning meta-loop.** Run the bootstrap with multiple reweighting schemes (different halflives or alternative decay shapes) and select the one that best predicts held-out future episodes. The picked weighting then *is* the player's learning rate estimate. Premature to implement; document the idea so it isn't lost.
- **[S] Bootstrap-vs-geometric divergence note.** The original branch-2 spec said "bootstrap mean > geometric mean on clustered deaths." The implementation analysis showed the direction is data-dependent: with aborted episodes in the pool, bootstrap is LOWER (geometric pretends every attempt completes-by-attrition); with all-completed clustered-death data, the two often agree exactly because `p/(1-p)` over the lives-weighted marginal recovers `E[deaths_per_attempt]` by construction. The meaningful divergence between the two estimators is in the FULL distribution (variance, tail, multi-modality), not the mean. Revisit the spec wording when branch 3's distribution overlay lands.
```

- [ ] **Step 2: Commit**

```bash
git add docs/BACKLOG.md
git commit -m "docs(backlog): bootstrap-resample follow-ups (branch 2 deferrals)"
```

---

## Task 11: Full test suite + final smoke

**Files:**
- No code changes.

- [ ] **Step 1: Run the full unfiltered pytest suite (baseline + emulator + frontend)**

Run: `python -m pytest`
Expected: PASS — no new failures, no skips beyond the pre-existing accepted ones. Per CLAUDE.md, `SKIPPED` counts as `FAILED`; if emulator tests skip with "ra_harness launch failed", surface that as a blocker rather than committing.

If the baseline was already red on entry (pre-existing failures), the rule is `feedback_red_baseline_habit.md`: stop and ask before touching code, OR fix as the first commit of the session, OR get explicit deferral sign-off. Don't silently move on.

- [ ] **Step 2: Type check**

Run: `npx pyright python/spinlab/estimators/`
Expected: No new errors introduced by the bootstrap module. Pre-existing errors in other modules are out of scope.

- [ ] **Step 3: Lint**

Run: `ruff check python/spinlab/estimators/ tests/unit/estimators/`
Expected: Clean. Use `ruff check --fix` for safe auto-fixes only.

- [ ] **Step 4: Final commit (only if any fixups were needed; otherwise skip)**

```bash
git add -p
git commit -m "chore: lint/type cleanup for bootstrap_resample"
```

---

## Self-Review Notes

After writing this plan I walked through it once with fresh eyes:

- **Spec coverage:** All seven "In scope" bullets map to a task — registration (Task 3), `expected_ms` for both total and clean (Task 7), floor_ms reuse (Task 7), ms_per_attempt via `_weighted_half_split_slope` (Task 7), cold filter (Task 4), `n_samples` param (Tasks 3 + 6), helper extraction (Task 1). The spec's "test coverage" bullets map to Tasks 4 + 6 + 7, except the "clustered-deaths > geometric" claim, which Task 6's docstring explicitly addresses by recording the (failed) derivation and pushing the claim to a backlog entry (Task 10) — the executor should not try to make this assertion hold.
- **Open questions:** The three the spec asked the plan to resolve are answered at the top of this plan (extras=None, extract helpers, default-on).
- **Type consistency:** `_BootstrapResult.mean_total_ms` / `mean_completion_ms` field names match Task 7's reads. `_bootstrap_means` signature is identical between Task 6 declaration and Task 7 call. `BootstrapResampleEstimator` / `BootstrapResampleState` names match throughout.
- **No placeholders:** No "TBD" / "TODO" / "appropriate" / "etc.". Every code block is the actual code; every command has expected output.
- **Frequent commits:** 10 commits across 11 tasks, each at a clean test-green checkpoint.

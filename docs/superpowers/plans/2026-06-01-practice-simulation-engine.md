# Practice Simulation Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-contained engine that rolls out per-segment sampler draws into a matrix, evaluates pluggable reset policies and objectives over the matrix, and computes per-segment "value of one more practice" diffs — plus a read-only dashboard panel that exposes it all.

**Architecture:** A new package `python/spinlab/practice_engine/` housing the engine; new FastAPI routes under `/api/practice-engine/`; a new dashboard tab. Pure functions throughout; column-keyed cache invalidation tied to `Scheduler.update_state_after_episode`. The em_suite sampler's `sample_episode` is the only randomness source.

**Tech Stack:** Python 3.11+, numpy, FastAPI, TypeScript/Vite, chart.js (already a dashboard dep). No new dependencies.

**Spec:** [`docs/superpowers/specs/2026-06-01-practice-simulation-engine-design.md`](../specs/2026-06-01-practice-simulation-engine-design.md).

---

## File Structure

**New files:**
- `python/spinlab/practice_engine/__init__.py` — package init, re-exports the public API.
- `python/spinlab/practice_engine/types.py` — `ResetMasks`, `PerSegmentValue` dataclasses.
- `python/spinlab/practice_engine/reset_policies.py` — `no_reset`, `target_paced`.
- `python/spinlab/practice_engine/threshold_sources.py` — `thresholds_from_user`, `thresholds_from_gold_default` (dev helper).
- `python/spinlab/practice_engine/objectives.py` — the 5 objectives.
- `python/spinlab/practice_engine/rollout_matrix.py` — `RolloutMatrix` class.
- `python/spinlab/practice_engine/engine.py` — `PracticeEngine` class.
- `python/spinlab/routes/practice_engine.py` — FastAPI router for the two new endpoints.
- `frontend/src/practice-engine.ts` — frontend module: fetch, render, wire interactions.
- `frontend/src/practice-engine.test.ts` — Vitest spec for the frontend module.
- `tests/unit/practice_engine/__init__.py` — empty.
- `tests/unit/practice_engine/test_reset_policies.py`
- `tests/unit/practice_engine/test_threshold_sources.py`
- `tests/unit/practice_engine/test_objectives.py`
- `tests/unit/practice_engine/test_rollout_matrix.py`
- `tests/unit/practice_engine/test_engine.py`
- `tests/unit/practice_engine/test_scheduler_integration.py`
- `tests/unit/test_practice_engine_routes.py`

**Modified files:**
- `python/spinlab/config.py` — add `practice_engine.rollouts: int = 20000`.
- `python/spinlab/scheduler.py` — lazy `engine` property + `_load_all_sampler_states()` + invalidation hook on `update_state_after_episode`.
- `python/spinlab/api_schemas.py` — request/response schemas for the two routes.
- `python/spinlab/dashboard.py` — wire the new router.
- `frontend/index.html` — add a tab + panel skeleton.
- `frontend/src/app.ts` — tab wiring.
- `frontend/src/types.ts` — re-export the new auto-generated types.

---

## Task 1: Profile `sample_episode` and bootstrap the package

**Files:**
- Create: `python/spinlab/practice_engine/__init__.py`
- Modify: `python/spinlab/config.py:38-72`
- Test: `tests/unit/practice_engine/__init__.py` (empty), inline profiling script

**Why:** The spec's risk #2 calls for measuring `sample_episode` throughput before locking N=20k. If it's substantially slower than ~1µs/call, the default needs to drop.

- [ ] **Step 1: Baseline.** Run `python -m pytest -m "not emulator" -q` — must be fully green. Red baseline = stop and ask.

- [ ] **Step 2: Profile `sample_episode`.** Create a temporary script `scripts/profile_sample_episode.py`:

```python
"""Profile sample_episode throughput. Delete after Task 1 lands.

Builds a synthetic post-gate SamplerState with full pools, then times N draws.
"""
from __future__ import annotations

import random
import time

from spinlab.estimators.em_suite_sampler import (
    DEFAULT_FAST_IDX,
    DEFAULT_SLOW_IDX,
    SamplerState,
    sample_episode,
)

# Build a gated state: at least 2 successes and 2 deaths, populated pools.
state = SamplerState(n_completed=20, n_attempts=40)
state.success_time_pool = [4000.0 + i * 10 for i in range(100)]
state.death_time_pool = [1500.0 + i * 5 for i in range(100)]
# Populate the EMAs by walking events:
from spinlab.estimators.em_suite_sampler import process_event
from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt
from datetime import UTC, datetime
for i in range(80):
    outcome = AttemptOutcome.SURVIVED if i % 3 != 0 else AttemptOutcome.DIED
    t_ms = 4000 if outcome == AttemptOutcome.SURVIVED else 1500
    state = process_event(state, EventAttempt(
        segment_id="x", session_id="s", episode_id=f"e{i}",
        outcome=outcome, time_ms=t_ms,
        source=AttemptSource.PRACTICE,
        created_at=datetime.now(UTC),
    ))

rng = random.Random(0)
N = 100_000
t0 = time.perf_counter()
draws = [sample_episode(state, DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX, k=0, rng=rng) for _ in range(N)]
elapsed = time.perf_counter() - t0
ns_per_call = elapsed / N * 1e9
print(f"sample_episode: {N} calls in {elapsed*1000:.1f}ms = {ns_per_call:.0f}ns/call")
print(f"At N=20000 per column: ~{N//100 * ns_per_call / 1e6:.1f}ms/column rebuild")
print(f"Non-None fraction: {sum(d is not None for d in draws) / N:.3f}")
```

- [ ] **Step 3: Run the profile.**

```bash
python scripts/profile_sample_episode.py
```

Expected: 100k calls complete in under 1 second (~10µs/call or better). Record the actual `ns_per_call` number in the commit message in Step 6.

- [ ] **Step 4: Decide N default.** If `ns_per_call < 10000` (10µs), keep `N=20000` default. If between 10–50µs, set default to `N=10000`. Above that, surface to the user before proceeding.

- [ ] **Step 5: Create the package skeleton.**

`python/spinlab/practice_engine/__init__.py`:
```python
"""Practice Simulation Engine — rollout matrix + reset policies + objectives.

Spec: docs/superpowers/specs/2026-06-01-practice-simulation-engine-design.md
"""
from spinlab.practice_engine.engine import PracticeEngine
from spinlab.practice_engine.types import PerSegmentValue, ResetMasks

__all__ = ["PracticeEngine", "PerSegmentValue", "ResetMasks"]
```

(Note: this will fail to import until Tasks 2/5/6 land. That's fine — Task 1 just bootstraps the directory; later tasks fix imports.)

`tests/unit/practice_engine/__init__.py`:
```python
```

- [ ] **Step 6: Add config knob.** In `python/spinlab/config.py`, add a new dataclass and a field on `AppConfig`:

After the `EmulatorConfig` block (line ~27), add:
```python
@dataclass
class PracticeEngineConfig:
    rollouts: int = 20000  # Monte Carlo rollouts per matrix column. See spec §10.
```

In `AppConfig` (line ~30), add a new field:
```python
@dataclass
class AppConfig:
    network: NetworkConfig
    emulator: EmulatorConfig
    practice_engine: PracticeEngineConfig
    data_dir: Path
    rom_dir: Path | None
    category: str = "any%"
```

In `AppConfig.from_yaml` (line ~38), parse the new key (default if missing):
```python
        pe = raw.get("practice_engine", {})
```
…and pass to `cls(...)`:
```python
            practice_engine=PracticeEngineConfig(
                rollouts=pe.get("rollouts", 20000),
            ),
```

- [ ] **Step 7: Confirm existing config consumers still work.** Run:

```bash
python -m pytest -m "not emulator" -q
```
Expected: green. Any tests that build `AppConfig` directly (search via grep — `grep -rn "AppConfig(" tests/ python/`) need the new field. Fix call sites to pass `practice_engine=PracticeEngineConfig()`.

Specific call sites known to exist:
- `python/spinlab/dashboard.py:118-123` — the `AppConfig(network=..., emulator=..., data_dir=..., rom_dir=None)` fallback when no config is provided. Add `practice_engine=PracticeEngineConfig()` to the constructor call.
- `tests/conftest.py` — `make_test_config()` helper. Add the field there too.

- [ ] **Step 8: Delete the profiling script.** It's a one-shot measurement.

```bash
git rm scripts/profile_sample_episode.py
```

- [ ] **Step 9: Commit.**

```bash
git add -A
git commit -m "feat(practice-engine): bootstrap package skeleton + config knob

sample_episode profile: <N> ns/call ⇒ N default = <20000 or 10000>."
```
(Replace the angle-bracket placeholders with the measured numbers.)

---

## Task 2: ResetMasks dataclass + `no_reset` policy

**Files:**
- Create: `python/spinlab/practice_engine/types.py`
- Create: `python/spinlab/practice_engine/reset_policies.py`
- Test: `tests/unit/practice_engine/test_reset_policies.py`

- [ ] **Step 1: Write the failing test.** Create `tests/unit/practice_engine/test_reset_policies.py`:

```python
"""Tests for reset policies."""
import numpy as np
import pytest

from spinlab.practice_engine.reset_policies import no_reset
from spinlab.practice_engine.types import ResetMasks


class TestNoReset:
    def test_all_finished(self):
        T = np.array([[100.0, 200.0, 300.0],
                      [150.0, 250.0, 350.0]])
        masks = no_reset(T)
        assert isinstance(masks, ResetMasks)
        assert masks.finished.tolist() == [True, True]
        assert masks.abort_at.tolist() == [-1, -1]
        assert masks.wall_ms.tolist() == [600.0, 750.0]

    def test_empty_matrix(self):
        T = np.zeros((0, 3))
        masks = no_reset(T)
        assert masks.finished.shape == (0,)
        assert masks.abort_at.shape == (0,)
        assert masks.wall_ms.shape == (0,)
```

- [ ] **Step 2: Run it — fails** (modules don't exist).

```bash
python -m pytest tests/unit/practice_engine/test_reset_policies.py -q
```
Expected: ImportError on `spinlab.practice_engine.reset_policies`.

- [ ] **Step 3: Create `types.py`.**

`python/spinlab/practice_engine/types.py`:
```python
"""Shared dataclasses for the practice simulation engine."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class ResetMasks:
    """Per-rollout outcome of applying a reset policy to a rollout matrix.

    Shapes (for a matrix of N rollouts):
      finished[N]  — bool; True when the rollout reached the end without aborting.
      abort_at[N]  — int; segment index where the abort triggered, or -1 if finished.
      wall_ms[N]   — float64; cumulative wall-clock through and including the
                     aborting segment (or full run total if finished).
    """
    finished: np.ndarray
    abort_at: np.ndarray
    wall_ms: np.ndarray


@dataclass
class PerSegmentValue:
    """Per-segment improvement under one more practice attempt.

    value           — baseline_objective − swap_to_k=1_objective, raw signed.
                      UI colors by sign per objective direction.
    value_per_second — value / cost_ms[i]; None if cost_ms is zero.
    e_sample_0_ms   — column mean before swap.
    e_sample_1_ms   — column mean after swap to k=1 draws.
    """
    seg_id: str
    value: float
    value_per_second: float | None
    e_sample_0_ms: float
    e_sample_1_ms: float
```

- [ ] **Step 4: Create `reset_policies.py` with `no_reset` only.**

`python/spinlab/practice_engine/reset_policies.py`:
```python
"""Reset policies — pure functions over the rollout matrix.

Each policy: (T[N,K], **kwargs) -> ResetMasks
"""
from __future__ import annotations

import numpy as np

from spinlab.practice_engine.types import ResetMasks


def no_reset(T: np.ndarray) -> ResetMasks:
    """Trivial policy: every rollout finishes. wall_ms is the full row sum."""
    N = T.shape[0]
    return ResetMasks(
        finished=np.ones(N, dtype=bool),
        abort_at=np.full(N, -1, dtype=np.int32),
        wall_ms=T.sum(axis=1),
    )
```

- [ ] **Step 5: Run tests — should pass.**

```bash
python -m pytest tests/unit/practice_engine/test_reset_policies.py -q
```
Expected: 2 passed.

- [ ] **Step 6: Commit.**

```bash
git add -A
git commit -m "feat(practice-engine): ResetMasks + no_reset policy"
```

---

## Task 3: `target_paced` policy + `thresholds_from_user` + `thresholds_from_gold_default`

**Files:**
- Modify: `python/spinlab/practice_engine/reset_policies.py`
- Create: `python/spinlab/practice_engine/threshold_sources.py`
- Test: `tests/unit/practice_engine/test_reset_policies.py`
- Test: `tests/unit/practice_engine/test_threshold_sources.py`

- [ ] **Step 1: Write the failing tests for `target_paced`.** Append to `tests/unit/practice_engine/test_reset_policies.py`:

```python
from spinlab.practice_engine.reset_policies import target_paced


class TestTargetPaced:
    def test_threshold_none_acts_as_no_reset(self):
        T = np.array([[100.0, 200.0, 300.0]])
        masks = target_paced(T, threshold_cum_ms=None)
        assert masks.finished.tolist() == [True]
        assert masks.abort_at.tolist() == [-1]
        assert masks.wall_ms.tolist() == [600.0]

    def test_threshold_finishes_when_well_below(self):
        T = np.array([[100.0, 200.0, 300.0]])
        # Cumulative: 100, 300, 600. Threshold: 200, 500, 800 (× 1.0 slack).
        # Row never exceeds threshold ⇒ finished.
        threshold = np.array([200.0, 500.0, 800.0])
        masks = target_paced(T, threshold_cum_ms=threshold, slack=0.0)
        assert masks.finished.tolist() == [True]
        assert masks.abort_at.tolist() == [-1]
        assert masks.wall_ms.tolist() == [600.0]

    def test_aborts_at_first_over(self):
        T = np.array([[100.0, 200.0, 300.0]])
        # Cumulative: 100, 300, 600. Threshold: 90, 500, 800.
        # 100 > 90 ⇒ abort at segment 0.
        threshold = np.array([90.0, 500.0, 800.0])
        masks = target_paced(T, threshold_cum_ms=threshold, slack=0.0)
        assert masks.finished.tolist() == [False]
        assert masks.abort_at.tolist() == [0]
        assert masks.wall_ms.tolist() == [100.0]

    def test_aborts_at_middle_segment(self):
        T = np.array([[100.0, 200.0, 300.0]])
        # Cumulative: 100, 300, 600. Threshold: 200, 250, 800.
        # 300 > 250 ⇒ abort at segment 1.
        threshold = np.array([200.0, 250.0, 800.0])
        masks = target_paced(T, threshold_cum_ms=threshold, slack=0.0)
        assert masks.finished.tolist() == [False]
        assert masks.abort_at.tolist() == [1]
        assert masks.wall_ms.tolist() == [300.0]

    def test_slack_widens_threshold(self):
        T = np.array([[100.0, 200.0, 300.0]])
        # Cumulative: 100, 300, 600. Threshold: 200, 250, 800 × 1.5 = 300, 375, 1200.
        # 300 vs 300 (not strict >) so segment 1 NOT aborted.
        threshold = np.array([200.0, 250.0, 800.0])
        masks = target_paced(T, threshold_cum_ms=threshold, slack=0.5)
        assert masks.finished.tolist() == [True]

    def test_mixed_rollouts(self):
        T = np.array([[100.0, 200.0, 300.0],   # cum 100, 300, 600 ⇒ aborts at seg 1
                      [50.0,  100.0, 150.0],   # cum 50, 150, 300  ⇒ finished
                      [400.0, 100.0, 100.0]])  # cum 400, 500, 600 ⇒ aborts at seg 0
        threshold = np.array([200.0, 250.0, 800.0])
        masks = target_paced(T, threshold_cum_ms=threshold, slack=0.0)
        assert masks.finished.tolist() == [False, True, False]
        assert masks.abort_at.tolist() == [1, -1, 0]
        assert masks.wall_ms.tolist() == [300.0, 300.0, 400.0]
```

- [ ] **Step 2: Run tests — should fail.**

```bash
python -m pytest tests/unit/practice_engine/test_reset_policies.py::TestTargetPaced -q
```
Expected: ImportError on `target_paced`.

- [ ] **Step 3: Implement `target_paced`.** Append to `python/spinlab/practice_engine/reset_policies.py`:

```python
def target_paced(
    T: np.ndarray,
    threshold_cum_ms: np.ndarray | None,
    slack: float = 0.0,
) -> ResetMasks:
    """Abort the first time cumulative time exceeds threshold_cum_ms[k] * (1+slack).

    If threshold_cum_ms is None, behaves identically to no_reset.
    """
    if threshold_cum_ms is None:
        return no_reset(T)
    N, K = T.shape
    cum = T.cumsum(axis=1)
    threshold = threshold_cum_ms * (1.0 + slack)
    over = cum > threshold[None, :]
    any_over = over.any(axis=1)
    abort_at = np.where(any_over, over.argmax(axis=1), -1).astype(np.int32)
    finished = ~any_over
    # safe_abort gives a valid index for the gather; for finished rows we
    # gather the last segment's cumulative time (full row sum).
    safe_abort = np.where(any_over, abort_at, K - 1)
    wall_ms = cum[np.arange(N), safe_abort]
    return ResetMasks(finished=finished, abort_at=abort_at, wall_ms=wall_ms)
```

- [ ] **Step 4: Run reset-policy tests — should pass.**

```bash
python -m pytest tests/unit/practice_engine/test_reset_policies.py -q
```
Expected: 8 passed (2 from Task 2 + 6 new).

- [ ] **Step 5: Write threshold-source tests.** Create `tests/unit/practice_engine/test_threshold_sources.py`:

```python
"""Tests for threshold source helpers."""
import numpy as np

from spinlab.practice_engine.threshold_sources import (
    thresholds_from_gold_default,
    thresholds_from_user,
)


class TestThresholdsFromUser:
    def test_cumulative_order(self):
        seg_ids = ["s1", "s2", "s3"]
        splits = {"s1": 5000, "s2": 12000, "s3": 18000}
        result = thresholds_from_user(seg_ids, splits)
        assert result.tolist() == [5000.0, 12000.0, 18000.0]
        assert result.dtype == np.float64

    def test_missing_segment_raises(self):
        seg_ids = ["s1", "s2"]
        splits = {"s1": 5000}
        try:
            thresholds_from_user(seg_ids, splits)
        except KeyError:
            return
        raise AssertionError("Expected KeyError for missing segment")


class TestThresholdsFromGoldDefault:
    def test_cumulative_sum(self):
        seg_ids = ["s1", "s2", "s3"]
        golds_ms = {"s1": 3000, "s2": 5000, "s3": 8000}
        result = thresholds_from_gold_default(seg_ids, golds_ms)
        assert result.tolist() == [3000.0, 8000.0, 16000.0]
        assert result.dtype == np.float64

    def test_missing_gold_raises(self):
        seg_ids = ["s1", "s2"]
        golds_ms = {"s1": 3000}
        try:
            thresholds_from_gold_default(seg_ids, golds_ms)
        except KeyError:
            return
        raise AssertionError("Expected KeyError for missing gold")
```

- [ ] **Step 6: Run threshold-source tests — should fail.**

```bash
python -m pytest tests/unit/practice_engine/test_threshold_sources.py -q
```
Expected: ImportError.

- [ ] **Step 7: Implement threshold sources.** Create `python/spinlab/practice_engine/threshold_sources.py`:

```python
"""Threshold-source helpers — produce per-segment cumulative thresholds for target_paced.

v0 ships two:
  thresholds_from_user        — caller passes per-segment cumulative splits explicitly.
  thresholds_from_gold_default — dev convenience: cumulative-sum of per-segment golds.

Future sources (PB-of-full-runs, WR-anchored, best-recent-N) are one-function additions.
"""
from __future__ import annotations

import numpy as np


def thresholds_from_user(
    seg_ids: list[str],
    cum_splits_ms: dict[str, int],
) -> np.ndarray:
    """User-entered per-segment cumulative split thresholds.

    Returns array shape (K,) of cumulative ms, one per segment in seg_ids order.
    KeyError if any seg_id has no entry in cum_splits_ms.
    """
    return np.array([cum_splits_ms[s] for s in seg_ids], dtype=np.float64)


def thresholds_from_gold_default(
    seg_ids: list[str],
    golds_ms: dict[str, int],
) -> np.ndarray:
    """Cumulative sum of per-segment golds — dashboard "fill from gold" default.

    Returns array shape (K,) of cumulative-gold ms.
    KeyError if any seg_id has no gold entry.
    """
    per_segment = np.array([golds_ms[s] for s in seg_ids], dtype=np.float64)
    return np.cumsum(per_segment)
```

- [ ] **Step 8: Run threshold-source tests — should pass.**

```bash
python -m pytest tests/unit/practice_engine/test_threshold_sources.py -q
```
Expected: 4 passed.

- [ ] **Step 9: Run all practice-engine tests + fast suite.**

```bash
python -m pytest -m "not emulator" -q
```
Expected: green, count grew by 10 (6 target_paced + 4 threshold).

- [ ] **Step 10: Commit.**

```bash
git add -A
git commit -m "feat(practice-engine): target_paced reset policy + threshold sources"
```

---

## Task 4: Objective slate

**Files:**
- Create: `python/spinlab/practice_engine/objectives.py`
- Test: `tests/unit/practice_engine/test_objectives.py`

- [ ] **Step 1: Write the failing tests.** Create `tests/unit/practice_engine/test_objectives.py`:

```python
"""Tests for objective functions."""
import math

import numpy as np
import pytest

from spinlab.practice_engine.objectives import (
    expected_total_finished_time,
    expected_wall_clock_per_attempt,
    p_pb_this_session,
    q,
    quantile,
)
from spinlab.practice_engine.reset_policies import no_reset, target_paced
from spinlab.practice_engine.types import ResetMasks


def _masks(finished, abort_at, wall_ms):
    return ResetMasks(
        finished=np.array(finished, dtype=bool),
        abort_at=np.array(abort_at, dtype=np.int32),
        wall_ms=np.array(wall_ms, dtype=np.float64),
    )


class TestExpectedWallClockPerAttempt:
    def test_uniform_finished(self):
        T = np.zeros((3, 2))
        masks = _masks([True, True, True], [-1, -1, -1], [1000.0, 2000.0, 3000.0])
        assert expected_wall_clock_per_attempt(T, masks, {}) == pytest.approx(2000.0)

    def test_mixed_finished_aborted_includes_partials(self):
        # wall_ms reflects ABORTED partials as well as finished totals
        T = np.zeros((4, 2))
        masks = _masks(
            [True, False, True, False],
            [-1, 0, -1, 1],
            [10_000.0, 2_000.0, 8_000.0, 5_000.0],
        )
        # Average across all 4: (10000+2000+8000+5000)/4 = 6250
        assert expected_wall_clock_per_attempt(T, masks, {}) == pytest.approx(6250.0)


class TestExpectedTotalFinishedTime:
    def test_finished_only(self):
        T = np.zeros((4, 2))
        masks = _masks(
            [True, False, True, False],
            [-1, 0, -1, 1],
            [10_000.0, 2_000.0, 8_000.0, 5_000.0],
        )
        # Mean of [10000, 8000] = 9000
        assert expected_total_finished_time(T, masks, {}) == pytest.approx(9000.0)

    def test_none_when_no_finished(self):
        T = np.zeros((2, 2))
        masks = _masks([False, False], [0, 1], [1000.0, 2000.0])
        assert expected_total_finished_time(T, masks, {}) is None


class TestQ:
    def test_fraction_under_target(self):
        T = np.zeros((4, 2))
        masks = _masks(
            [True, True, True, False],
            [-1, -1, -1, 1],
            [4500.0, 6000.0, 9000.0, 5500.0],
        )
        # target 6500 ⇒ finished AND wall_ms <= 6500: rows 0 (4500) and 1 (6000). q = 2/4 = 0.5
        assert q(T, masks, {"target_ms": 6500}) == pytest.approx(0.5)

    def test_zero_when_none_under(self):
        T = np.zeros((2, 2))
        masks = _masks([True, True], [-1, -1], [10_000.0, 11_000.0])
        assert q(T, masks, {"target_ms": 5_000}) == pytest.approx(0.0)


class TestQuantile:
    def test_median_of_finished(self):
        T = np.zeros((5, 2))
        masks = _masks(
            [True, True, True, True, False],
            [-1, -1, -1, -1, 0],
            [3000.0, 5000.0, 7000.0, 9000.0, 1500.0],
        )
        # Finished times: [3000, 5000, 7000, 9000]; median = 6000
        assert quantile(T, masks, {"p": 0.5}) == pytest.approx(6000.0)

    def test_none_when_no_finished(self):
        T = np.zeros((1, 2))
        masks = _masks([False], [0], [500.0])
        assert quantile(T, masks, {"p": 0.5}) is None


class TestPpbThisSession:
    def test_one_minus_one_minus_q_to_H_over_tau(self):
        # q = 0.5; τ̄ = 1000; H = 5000 ⇒ attempts = 5
        # 1 - 0.5^5 = 1 - 1/32 = 31/32 ≈ 0.96875
        T = np.zeros((2, 2))
        masks = _masks([True, False], [-1, 1], [500.0, 1500.0])
        # wall_ms.mean() = 1000; q with target 600: finished and <=600 ⇒ 1/2 = 0.5
        ctx = {"target_ms": 600, "session_remaining_ms": 5000}
        result = p_pb_this_session(T, masks, ctx)
        assert result == pytest.approx(1 - 0.5 ** 5, rel=1e-6)

    def test_none_when_tau_zero(self):
        T = np.zeros((1, 2))
        masks = _masks([True], [-1], [0.0])
        ctx = {"target_ms": 100, "session_remaining_ms": 1000}
        assert p_pb_this_session(T, masks, ctx) is None
```

- [ ] **Step 2: Run tests — should fail.**

```bash
python -m pytest tests/unit/practice_engine/test_objectives.py -q
```
Expected: ImportError.

- [ ] **Step 3: Implement objectives.** Create `python/spinlab/practice_engine/objectives.py`:

```python
"""Objective functions for the practice simulation engine.

Each objective is a pure function (T, masks, ctx) -> float | None.
Returns None when the gate fails (e.g. zero finished rollouts). Never silently
fall back to a default — None is the honest answer.

Sign convention: objectives return the raw value in their natural units. The
engine's per_segment_values returns baseline-minus-swap, signed; the UI inverts
color per objective direction (e.g. for q, "value < 0" means practice helped).
"""
from __future__ import annotations

import numpy as np

from spinlab.practice_engine.types import ResetMasks


def expected_wall_clock_per_attempt(T: np.ndarray, masks: ResetMasks, ctx: dict) -> float | None:
    """Mean wall-clock per attempt (aborted + finished both contribute their partials)."""
    if masks.wall_ms.size == 0:
        return None
    return float(masks.wall_ms.mean())


def expected_total_finished_time(T: np.ndarray, masks: ResetMasks, ctx: dict) -> float | None:
    """Mean total time across rollouts that FINISHED."""
    if not masks.finished.any():
        return None
    return float(masks.wall_ms[masks.finished].mean())


def q(T: np.ndarray, masks: ResetMasks, ctx: dict) -> float | None:
    """Fraction of rollouts that finished under ctx['target_ms']."""
    target = ctx["target_ms"]
    if masks.finished.size == 0:
        return None
    under = masks.finished & (masks.wall_ms <= target)
    return float(under.mean())


def quantile(T: np.ndarray, masks: ResetMasks, ctx: dict) -> float | None:
    """p-th quantile of FINISHED total times. ctx['p'] in (0, 1)."""
    p = ctx["p"]
    finished_times = masks.wall_ms[masks.finished]
    if finished_times.size == 0:
        return None
    return float(np.quantile(finished_times, p))


def p_pb_this_session(T: np.ndarray, masks: ResetMasks, ctx: dict) -> float | None:
    """1 − (1 − q)^(H/τ̄). ctx: target_ms, session_remaining_ms."""
    q_val = q(T, masks, ctx)
    tau_bar = expected_wall_clock_per_attempt(T, masks, ctx)
    if q_val is None or tau_bar is None or tau_bar <= 0:
        return None
    H = ctx["session_remaining_ms"]
    attempts_remaining = H / tau_bar
    return float(1.0 - (1.0 - q_val) ** attempts_remaining)
```

- [ ] **Step 4: Run tests — should pass.**

```bash
python -m pytest tests/unit/practice_engine/test_objectives.py -q
```
Expected: 9 passed.

- [ ] **Step 5: Run the full fast suite.**

```bash
python -m pytest -m "not emulator" -q
```
Expected: green.

- [ ] **Step 6: Commit.**

```bash
git add -A
git commit -m "feat(practice-engine): objective slate (5 objectives, None-on-gate-fail)"
```

---

## Task 5: RolloutMatrix data structure + column build + invalidation

**Files:**
- Create: `python/spinlab/practice_engine/rollout_matrix.py`
- Test: `tests/unit/practice_engine/test_rollout_matrix.py`

- [ ] **Step 1: Write the failing tests.** Create `tests/unit/practice_engine/test_rollout_matrix.py`:

```python
"""Tests for RolloutMatrix."""
from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from spinlab.estimators.em_suite_sampler import SamplerState, process_event
from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt
from spinlab.practice_engine.rollout_matrix import RolloutMatrix


def _gated_state(seed: int = 0, n_events: int = 60) -> SamplerState:
    """Build a synthetic SamplerState past the (>=2 successes, >=2 deaths) gate."""
    state = SamplerState(n_completed=0, n_attempts=0)
    import random
    rng = random.Random(seed)
    for i in range(n_events):
        outcome = AttemptOutcome.SURVIVED if i % 3 != 0 else AttemptOutcome.DIED
        t_ms = 4000 + rng.randint(-200, 200) if outcome == AttemptOutcome.SURVIVED else 1500 + rng.randint(-50, 50)
        state = process_event(state, EventAttempt(
            segment_id="x", session_id="s", episode_id=f"e{i}",
            outcome=outcome, time_ms=t_ms,
            source=AttemptSource.PRACTICE,
            created_at=datetime.now(UTC),
        ))
    return state


class TestRolloutMatrixBuild:
    def test_shape_matches_states(self):
        states = {"s1": _gated_state(0), "s2": _gated_state(1)}
        m = RolloutMatrix(sampler_states=states, N=500, rng_seed=42)
        m.ensure_fresh()
        assert m.T.shape == (500, 2)
        assert m.seg_ids == ["s1", "s2"]
        assert m.dirty == set()

    def test_cost_ms_populated(self):
        states = {"s1": _gated_state(0)}
        m = RolloutMatrix(sampler_states=states, N=500, rng_seed=42)
        m.ensure_fresh()
        assert m.cost_ms.shape == (1,)
        assert m.cost_ms[0] > 0  # mean of finite sample draws

    def test_initial_state_is_dirty(self):
        states = {"s1": _gated_state(0)}
        m = RolloutMatrix(sampler_states=states, N=100, rng_seed=42)
        assert m.dirty == {"s1"}

    def test_reproducibility_same_seed(self):
        states = {"s1": _gated_state(0)}
        m1 = RolloutMatrix(sampler_states=states, N=200, rng_seed=42)
        m1.ensure_fresh()
        m2 = RolloutMatrix(sampler_states=states, N=200, rng_seed=42)
        m2.ensure_fresh()
        assert np.array_equal(m1.T, m2.T)

    def test_different_seed_different_draws(self):
        states = {"s1": _gated_state(0)}
        m1 = RolloutMatrix(sampler_states=states, N=200, rng_seed=42)
        m1.ensure_fresh()
        m2 = RolloutMatrix(sampler_states=states, N=200, rng_seed=43)
        m2.ensure_fresh()
        assert not np.array_equal(m1.T, m2.T)

    def test_ungated_segment_excluded(self):
        # State with no events fails the gate.
        bare = SamplerState(n_completed=0, n_attempts=0)
        states = {"s1": _gated_state(0), "s2_bare": bare}
        m = RolloutMatrix(sampler_states=states, N=100, rng_seed=42)
        m.ensure_fresh()
        assert m.seg_ids == ["s1"]  # bare excluded
        assert m.T.shape == (100, 1)


class TestRolloutMatrixInvalidation:
    def test_invalidate_marks_dirty(self):
        states = {"s1": _gated_state(0), "s2": _gated_state(1)}
        m = RolloutMatrix(sampler_states=states, N=100, rng_seed=42)
        m.ensure_fresh()
        m.invalidate("s1")
        assert m.dirty == {"s1"}

    def test_ensure_fresh_only_rebuilds_dirty(self):
        states = {"s1": _gated_state(0), "s2": _gated_state(1)}
        m = RolloutMatrix(sampler_states=states, N=100, rng_seed=42)
        m.ensure_fresh()
        T_before = m.T.copy()
        # Mutate state s1 to a wildly different state and invalidate ONLY s1:
        states["s1"] = _gated_state(seed=999, n_events=80)
        m.invalidate("s1")
        m.ensure_fresh()
        # Column 0 should change; column 1 should be identical.
        assert not np.array_equal(m.T[:, 0], T_before[:, 0])
        assert np.array_equal(m.T[:, 1], T_before[:, 1])

    def test_invalidate_unknown_segment_is_noop(self):
        states = {"s1": _gated_state(0)}
        m = RolloutMatrix(sampler_states=states, N=100, rng_seed=42)
        m.ensure_fresh()
        # Invalidating a non-existent segment shouldn't crash.
        m.invalidate("does_not_exist")
        m.ensure_fresh()
        # No new column appeared.
        assert m.seg_ids == ["s1"]

    def test_newly_gated_segment_added_on_refresh(self):
        bare = SamplerState(n_completed=0, n_attempts=0)
        states = {"s1": _gated_state(0), "s2": bare}
        m = RolloutMatrix(sampler_states=states, N=100, rng_seed=42)
        m.ensure_fresh()
        assert m.seg_ids == ["s1"]
        # Now s2 gates:
        states["s2"] = _gated_state(7)
        m.invalidate("s2")
        m.ensure_fresh()
        assert sorted(m.seg_ids) == ["s1", "s2"]
        assert m.T.shape == (100, 2)


class TestRolloutMatrixSwapColumn:
    def test_draw_column_returns_correct_shape(self):
        states = {"s1": _gated_state(0)}
        m = RolloutMatrix(sampler_states=states, N=100, rng_seed=42)
        m.ensure_fresh()
        swap = m.draw_column("s1", k_param=1)
        assert swap.shape == (100,)
        assert np.all(swap >= 0)

    def test_draw_column_unknown_segment_raises(self):
        states = {"s1": _gated_state(0)}
        m = RolloutMatrix(sampler_states=states, N=100, rng_seed=42)
        m.ensure_fresh()
        with pytest.raises(KeyError):
            m.draw_column("does_not_exist", k_param=1)
```

- [ ] **Step 2: Run tests — should fail.**

```bash
python -m pytest tests/unit/practice_engine/test_rollout_matrix.py -q
```
Expected: ImportError.

- [ ] **Step 3: Implement `RolloutMatrix`.** Create `python/spinlab/practice_engine/rollout_matrix.py`:

```python
"""Rollout matrix — the backbone of the practice simulation engine.

T[N, K]: per-segment-per-rollout sample times. Columns are owned by segments;
when a segment's SamplerState mutates, its column gets marked dirty and is
rebuilt on the next ensure_fresh().
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

import numpy as np

from spinlab.estimators.em_suite_sampler import (
    DEFAULT_FAST_IDX,
    DEFAULT_SLOW_IDX,
    SamplerState,
    _gate_passes,
    sample_episode,
)


@dataclass
class RolloutMatrix:
    """Lazy, column-keyed rollout matrix.

    Construction notes:
      - sampler_states is a live mapping; new gated segments are auto-included
        on ensure_fresh().
      - N rollouts per column, fixed at construction (use a new RolloutMatrix
        if you need a different N).
      - rng_seed is global; each column k uses random.Random(rng_seed + k_offset_for_seg).
    """
    sampler_states: dict[str, SamplerState]
    N: int
    rng_seed: int

    T: np.ndarray = field(init=False)
    seg_ids: list[str] = field(init=False)
    cost_ms: np.ndarray = field(init=False)
    dirty: set[str] = field(init=False)
    # Stable seg_id -> column-seed-offset mapping; once assigned, never reused
    # so that re-draws for the same segment are reproducible.
    _seed_offsets: dict[str, int] = field(init=False)
    _next_seed_offset: int = field(init=False)

    def __post_init__(self) -> None:
        self.T = np.zeros((self.N, 0), dtype=np.float64)
        self.seg_ids = []
        self.cost_ms = np.zeros((0,), dtype=np.float64)
        self.dirty = set()
        self._seed_offsets = {}
        self._next_seed_offset = 0
        # Mark all currently-gated states dirty so the first ensure_fresh()
        # builds the matrix from scratch.
        for seg_id, state in self.sampler_states.items():
            if _gate_passes(state):
                self._assign_seed_offset(seg_id)
                self.dirty.add(seg_id)

    def invalidate(self, seg_id: str) -> None:
        """Mark a column dirty. No-op if the segment is unknown."""
        # If the segment exists in sampler_states (gated or not), mark it dirty;
        # ensure_fresh re-checks the gate and either rebuilds or drops the column.
        if seg_id in self.sampler_states:
            self.dirty.add(seg_id)

    def ensure_fresh(self) -> None:
        """Rebuild dirty columns, drop any now-ungated columns, add new ones."""
        if not self.dirty:
            return

        # Determine the new authoritative set of gated segments.
        gated = [s for s, st in self.sampler_states.items() if _gate_passes(st)]

        # Detect if the column set is changing (add/remove). If so, rebuild
        # the whole matrix from scratch — cheap at v0 N.
        current_set = set(self.seg_ids)
        new_set = set(gated)
        if current_set != new_set:
            self._rebuild_full(gated)
            return

        # Same column set; rebuild only dirty columns in place.
        for seg_id in list(self.dirty):
            col_idx = self.seg_ids.index(seg_id)
            new_col = self._draw_column_impl(seg_id, k_param=0)
            self.T[:, col_idx] = new_col
            self.cost_ms[col_idx] = float(new_col.mean())
        self.dirty.clear()

    def draw_column(self, seg_id: str, k_param: int) -> np.ndarray:
        """Draw N samples for a segment at the given k_param (0 or 1).

        Used by per-segment value attribution: pass k_param=1 to draw the
        "what if practiced once" column. Re-seeds from the same offset so
        the underlying RNG stream is identical to the baseline column.
        """
        if seg_id not in self.sampler_states:
            raise KeyError(f"Unknown segment: {seg_id!r}")
        return self._draw_column_impl(seg_id, k_param=k_param)

    def _assign_seed_offset(self, seg_id: str) -> None:
        if seg_id in self._seed_offsets:
            return
        self._seed_offsets[seg_id] = self._next_seed_offset
        self._next_seed_offset += 1

    def _draw_column_impl(self, seg_id: str, k_param: int) -> np.ndarray:
        state = self.sampler_states[seg_id]
        self._assign_seed_offset(seg_id)
        seed = self.rng_seed + self._seed_offsets[seg_id]
        rng = random.Random(seed)
        out = np.empty(self.N, dtype=np.float64)
        for n in range(self.N):
            v = sample_episode(state, DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX, k=k_param, rng=rng)
            # Gate-passes was checked at column inclusion; sample_episode can
            # still return None on a degenerate draw (e.g. MAX_ATTEMPTS_PER_EPISODE
            # reached). Substitute the cost-mean — these are rare and substituting
            # the column mean keeps the row-sum honest as a single bad draw.
            # If this happens often the gate is failing silently — surface in a test.
            out[n] = v if v is not None else np.nan
        # If any draws came back None, replace nans with the column's nan-mean.
        if np.isnan(out).any():
            non_nan = out[~np.isnan(out)]
            if non_nan.size == 0:
                # Catastrophic: all draws failed. Shouldn't happen on a gated state.
                raise RuntimeError(
                    f"All {self.N} sample_episode draws returned None for {seg_id!r}; "
                    f"likely a gate logic bug."
                )
            out[np.isnan(out)] = non_nan.mean()
        return out

    def _rebuild_full(self, gated_seg_ids: list[str]) -> None:
        """Rebuild the matrix from scratch for the given segment set."""
        # Preserve seed-offset assignments where possible (so reproducibility holds
        # across mid-session column add/remove).
        for seg_id in gated_seg_ids:
            self._assign_seed_offset(seg_id)
        self.seg_ids = list(gated_seg_ids)
        K = len(self.seg_ids)
        self.T = np.zeros((self.N, K), dtype=np.float64)
        self.cost_ms = np.zeros(K, dtype=np.float64)
        for k, seg_id in enumerate(self.seg_ids):
            col = self._draw_column_impl(seg_id, k_param=0)
            self.T[:, k] = col
            self.cost_ms[k] = float(col.mean())
        self.dirty.clear()
```

- [ ] **Step 4: Run tests — should pass.**

```bash
python -m pytest tests/unit/practice_engine/test_rollout_matrix.py -q
```
Expected: 12 passed.

- [ ] **Step 5: Run the full fast suite.**

```bash
python -m pytest -m "not emulator" -q
```
Expected: green.

- [ ] **Step 6: Commit.**

```bash
git add -A
git commit -m "feat(practice-engine): RolloutMatrix with column invalidation + ensure_fresh"
```

---

## Task 6: `PracticeEngine` — evaluate, total_time_distribution, column_summary

**Files:**
- Create: `python/spinlab/practice_engine/engine.py`
- Test: `tests/unit/practice_engine/test_engine.py`

- [ ] **Step 1: Write the failing tests.** Create `tests/unit/practice_engine/test_engine.py`:

```python
"""Tests for PracticeEngine (excluding per_segment_values; that's Task 7)."""
from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from spinlab.estimators.em_suite_sampler import SamplerState, process_event
from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt
from spinlab.practice_engine.engine import PracticeEngine
from spinlab.practice_engine.objectives import (
    expected_total_finished_time,
    expected_wall_clock_per_attempt,
)
from spinlab.practice_engine.reset_policies import no_reset, target_paced


def _gated_state(seed: int = 0) -> SamplerState:
    state = SamplerState(n_completed=0, n_attempts=0)
    import random
    rng = random.Random(seed)
    for i in range(60):
        outcome = AttemptOutcome.SURVIVED if i % 3 != 0 else AttemptOutcome.DIED
        t_ms = 4000 + rng.randint(-200, 200) if outcome == AttemptOutcome.SURVIVED else 1500
        state = process_event(state, EventAttempt(
            segment_id="x", session_id="s", episode_id=f"e{i}",
            outcome=outcome, time_ms=t_ms,
            source=AttemptSource.PRACTICE,
            created_at=datetime.now(UTC),
        ))
    return state


class TestEvaluate:
    def test_returns_scalar_for_expected_total(self):
        states = {"s1": _gated_state(0), "s2": _gated_state(1)}
        engine = PracticeEngine(sampler_states=states, N=500, rng_seed=42)
        result = engine.evaluate(
            policy=no_reset, threshold_kwargs={},
            objective=expected_wall_clock_per_attempt, ctx={},
        )
        assert isinstance(result["value"], float)
        assert result["value"] > 0

    def test_returns_none_for_finished_time_when_all_aborted(self):
        states = {"s1": _gated_state(0)}
        engine = PracticeEngine(sampler_states=states, N=200, rng_seed=42)
        # Threshold so low everything aborts at seg 0:
        result = engine.evaluate(
            policy=target_paced,
            threshold_kwargs={"threshold_cum_ms": np.array([1.0]), "slack": 0.0},
            objective=expected_total_finished_time, ctx={},
        )
        assert result["value"] is None
        assert result["masks_summary"]["finished_pct"] == pytest.approx(0.0)

    def test_masks_summary_shape(self):
        states = {"s1": _gated_state(0), "s2": _gated_state(1)}
        engine = PracticeEngine(sampler_states=states, N=200, rng_seed=42)
        result = engine.evaluate(
            policy=no_reset, threshold_kwargs={},
            objective=expected_wall_clock_per_attempt, ctx={},
        )
        ms = result["masks_summary"]
        assert ms["finished_pct"] == pytest.approx(100.0)
        assert ms["aborted_by_segment"] == {}


class TestTotalTimeDistribution:
    def test_histogram_payload(self):
        states = {"s1": _gated_state(0), "s2": _gated_state(1)}
        engine = PracticeEngine(sampler_states=states, N=500, rng_seed=42)
        result = engine.total_time_distribution(policy=no_reset, threshold_kwargs={})
        assert "bins" in result and "counts" in result
        assert len(result["bins"]) == len(result["counts"]) + 1  # bin edges
        assert sum(result["counts"]) == 500  # all finished under no_reset
        assert result["mean"] > 0
        assert result["median"] > 0
        assert result["p10"] <= result["median"] <= result["p90"]


class TestColumnSummary:
    def test_per_segment_stats(self):
        states = {"s1": _gated_state(0)}
        engine = PracticeEngine(sampler_states=states, N=500, rng_seed=42)
        summary = engine.column_summary("s1")
        assert summary["seg_id"] == "s1"
        assert summary["n"] == 500
        assert summary["mean"] > 0
        assert summary["p10"] <= summary["p50"] <= summary["p90"]
        assert "e_sample_0_ms" in summary
        assert "e_sample_1_ms" in summary
```

- [ ] **Step 2: Run tests — should fail.**

```bash
python -m pytest tests/unit/practice_engine/test_engine.py -q
```
Expected: ImportError.

- [ ] **Step 3: Implement `PracticeEngine`.** Create `python/spinlab/practice_engine/engine.py`:

```python
"""PracticeEngine — consumer-facing API over the rollout matrix.

Combines a RolloutMatrix with pluggable ResetPolicy + Objective functions to
produce: scalar objective values, total-time distributions, per-segment stats,
and per-segment value attributions (the §4 ranking primitive, in Task 7).
"""
from __future__ import annotations

from collections import Counter
from typing import Callable

import numpy as np

from spinlab.estimators.em_suite_sampler import SamplerState
from spinlab.practice_engine.rollout_matrix import RolloutMatrix
from spinlab.practice_engine.types import PerSegmentValue, ResetMasks

# Type aliases for clarity. Could be Protocols later; functions for now.
ResetPolicy = Callable[..., ResetMasks]
Objective = Callable[[np.ndarray, ResetMasks, dict], float | None]


class PracticeEngine:
    """Holds the rollout matrix and exposes reductions over it."""

    def __init__(
        self,
        sampler_states: dict[str, SamplerState],
        N: int,
        rng_seed: int = 0,
    ) -> None:
        self.matrix = RolloutMatrix(
            sampler_states=sampler_states, N=N, rng_seed=rng_seed,
        )

    def invalidate(self, seg_id: str) -> None:
        """Mark a segment's column dirty. See RolloutMatrix.invalidate."""
        self.matrix.invalidate(seg_id)

    def evaluate(
        self,
        policy: ResetPolicy,
        threshold_kwargs: dict,
        objective: Objective,
        ctx: dict,
    ) -> dict:
        """Single objective evaluation. Returns {value, masks_summary}."""
        self.matrix.ensure_fresh()
        masks = policy(self.matrix.T, **threshold_kwargs)
        value = objective(self.matrix.T, masks, ctx)
        return {
            "value": value,
            "masks_summary": self._masks_summary(masks),
        }

    def total_time_distribution(
        self,
        policy: ResetPolicy,
        threshold_kwargs: dict,
        bin_count: int = 30,
    ) -> dict:
        """Histogram payload of finished wall_ms under the given policy."""
        self.matrix.ensure_fresh()
        masks = policy(self.matrix.T, **threshold_kwargs)
        finished_wall = masks.wall_ms[masks.finished]
        if finished_wall.size == 0:
            return {
                "bins": [], "counts": [],
                "mean": None, "median": None, "p10": None, "p90": None,
                "finished_count": 0,
            }
        counts, bin_edges = np.histogram(finished_wall, bins=bin_count)
        return {
            "bins": bin_edges.tolist(),
            "counts": counts.tolist(),
            "mean": float(finished_wall.mean()),
            "median": float(np.median(finished_wall)),
            "p10": float(np.quantile(finished_wall, 0.10)),
            "p90": float(np.quantile(finished_wall, 0.90)),
            "finished_count": int(finished_wall.size),
        }

    def column_summary(self, seg_id: str) -> dict:
        """Per-segment column stats for the dashboard table."""
        self.matrix.ensure_fresh()
        if seg_id not in self.matrix.seg_ids:
            raise KeyError(f"Unknown or ungated segment: {seg_id!r}")
        col_idx = self.matrix.seg_ids.index(seg_id)
        col = self.matrix.T[:, col_idx]
        swap_col = self.matrix.draw_column(seg_id, k_param=1)
        return {
            "seg_id": seg_id,
            "n": int(col.size),
            "mean": float(col.mean()),
            "p10": float(np.quantile(col, 0.10)),
            "p50": float(np.quantile(col, 0.50)),
            "p90": float(np.quantile(col, 0.90)),
            "e_sample_0_ms": float(col.mean()),
            "e_sample_1_ms": float(swap_col.mean()),
        }

    def _masks_summary(self, masks: ResetMasks) -> dict:
        """Compact summary of ResetMasks for the dashboard."""
        n = masks.finished.size
        if n == 0:
            return {"finished_pct": 0.0, "aborted_by_segment": {}}
        finished_pct = float(masks.finished.mean() * 100.0)
        aborted_at = masks.abort_at[~masks.finished]
        # Map column index back to seg_id; mask only valid abort_at values
        seg_ids = self.matrix.seg_ids
        aborted_by_segment: dict[str, int] = {}
        if aborted_at.size > 0:
            counter = Counter(int(a) for a in aborted_at if 0 <= int(a) < len(seg_ids))
            for col_idx, count in counter.items():
                aborted_by_segment[seg_ids[col_idx]] = count
        return {
            "finished_pct": finished_pct,
            "aborted_by_segment": aborted_by_segment,
        }
```

- [ ] **Step 4: Update package `__init__.py` to re-export.** Edit `python/spinlab/practice_engine/__init__.py` — already in place from Task 1; verify it imports without error now:

```bash
python -c "from spinlab.practice_engine import PracticeEngine, PerSegmentValue, ResetMasks; print('ok')"
```
Expected: prints `ok`.

- [ ] **Step 5: Run tests — should pass.**

```bash
python -m pytest tests/unit/practice_engine/test_engine.py -q
```
Expected: 5 passed.

- [ ] **Step 6: Run the full fast suite.**

```bash
python -m pytest -m "not emulator" -q
```
Expected: green.

- [ ] **Step 7: Commit.**

```bash
git add -A
git commit -m "feat(practice-engine): PracticeEngine.evaluate + total_time_distribution + column_summary"
```

---

## Task 7: `PracticeEngine.per_segment_values` (§4 ranking primitive)

**Files:**
- Modify: `python/spinlab/practice_engine/engine.py`
- Test: `tests/unit/practice_engine/test_engine.py`

- [ ] **Step 1: Write the failing tests.** Append to `tests/unit/practice_engine/test_engine.py`:

```python
from spinlab.practice_engine.types import PerSegmentValue


class TestPerSegmentValues:
    def test_returns_one_entry_per_gated_segment(self):
        states = {"s1": _gated_state(0), "s2": _gated_state(1), "s3": _gated_state(2)}
        engine = PracticeEngine(sampler_states=states, N=500, rng_seed=42)
        values = engine.per_segment_values(
            policy=no_reset, threshold_kwargs={},
            objective=expected_wall_clock_per_attempt, ctx={},
        )
        assert set(values.keys()) == {"s1", "s2", "s3"}
        for seg_id, psv in values.items():
            assert isinstance(psv, PerSegmentValue)
            assert psv.seg_id == seg_id
            assert psv.e_sample_0_ms > 0
            assert psv.e_sample_1_ms >= 0

    def test_returns_empty_when_no_gated(self):
        empty = SamplerState(n_completed=0, n_attempts=0)
        states = {"s1": empty}
        engine = PracticeEngine(sampler_states=states, N=100, rng_seed=42)
        values = engine.per_segment_values(
            policy=no_reset, threshold_kwargs={},
            objective=expected_wall_clock_per_attempt, ctx={},
        )
        assert values == {}

    def test_value_per_second_is_value_over_cost(self):
        states = {"s1": _gated_state(0)}
        engine = PracticeEngine(sampler_states=states, N=500, rng_seed=42)
        values = engine.per_segment_values(
            policy=no_reset, threshold_kwargs={},
            objective=expected_wall_clock_per_attempt, ctx={},
        )
        psv = values["s1"]
        if psv.value_per_second is not None:
            assert psv.value_per_second == pytest.approx(psv.value / psv.e_sample_0_ms, rel=1e-9)

    def test_objective_none_skips_segment(self):
        # If the objective returns None for the swap (e.g. expected_total_finished_time
        # when threshold is so tight nothing finishes), that segment should be skipped.
        states = {"s1": _gated_state(0)}
        engine = PracticeEngine(sampler_states=states, N=200, rng_seed=42)
        values = engine.per_segment_values(
            policy=target_paced,
            threshold_kwargs={"threshold_cum_ms": np.array([1.0]), "slack": 0.0},
            objective=expected_total_finished_time, ctx={},
        )
        # baseline returns None (nothing finished) ⇒ empty dict
        assert values == {}
```

- [ ] **Step 2: Run tests — should fail** (per_segment_values doesn't exist yet).

```bash
python -m pytest tests/unit/practice_engine/test_engine.py::TestPerSegmentValues -q
```
Expected: AttributeError.

- [ ] **Step 3: Implement `per_segment_values`.** Append to `python/spinlab/practice_engine/engine.py`:

```python
    def per_segment_values(
        self,
        policy: ResetPolicy,
        threshold_kwargs: dict,
        objective: Objective,
        ctx: dict,
    ) -> dict[str, PerSegmentValue]:
        """For each gated segment, return baseline_obj − swap_i_to_k=1_obj.

        Independent re-draws of the swap column (no CRN in v0; see spec §13 risk #1).
        """
        self.matrix.ensure_fresh()
        baseline_masks = policy(self.matrix.T, **threshold_kwargs)
        baseline_obj = objective(self.matrix.T, baseline_masks, ctx)
        if baseline_obj is None:
            return {}

        results: dict[str, PerSegmentValue] = {}
        for i, seg_id in enumerate(self.matrix.seg_ids):
            swap_col = self.matrix.draw_column(seg_id, k_param=1)
            T_swap = self.matrix.T.copy()
            T_swap[:, i] = swap_col

            swap_masks = policy(T_swap, **threshold_kwargs)
            swap_obj = objective(T_swap, swap_masks, ctx)
            if swap_obj is None:
                continue

            value = baseline_obj - swap_obj
            cost = self.matrix.cost_ms[i]
            value_per_second = (value / cost) if cost > 0 else None

            results[seg_id] = PerSegmentValue(
                seg_id=seg_id,
                value=value,
                value_per_second=value_per_second,
                e_sample_0_ms=float(cost),
                e_sample_1_ms=float(swap_col.mean()),
            )
        return results
```

- [ ] **Step 4: Run tests — should pass.**

```bash
python -m pytest tests/unit/practice_engine/test_engine.py::TestPerSegmentValues -q
```
Expected: 4 passed.

- [ ] **Step 5: Run the full fast suite.**

```bash
python -m pytest -m "not emulator" -q
```
Expected: green.

- [ ] **Step 6: Commit.**

```bash
git add -A
git commit -m "feat(practice-engine): per_segment_values — the §4 ranking primitive"
```

---

## Task 8: Scheduler integration

**Files:**
- Modify: `python/spinlab/scheduler.py`
- Test: `tests/unit/practice_engine/test_scheduler_integration.py`

- [ ] **Step 1: Write the failing tests.** Create `tests/unit/practice_engine/test_scheduler_integration.py`:

```python
"""Tests for Scheduler ↔ PracticeEngine integration."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from spinlab.db import Database
from spinlab.estimators.em_suite_sampler import SamplerState, process_event
from spinlab.models import (
    Attempt,
    AttemptOutcome,
    AttemptSource,
    EventAttempt,
    Segment,
)
from spinlab.scheduler import Scheduler


def _seed_gated_state(db, segment_id: str, n_events: int = 60) -> None:
    """Insert enough events to gate the segment's SamplerState."""
    state = SamplerState(n_completed=0, n_attempts=0)
    import random
    rng = random.Random(hash(segment_id) % 1000)
    db.create_session("sess_pe", "g1")
    for i in range(n_events):
        outcome = AttemptOutcome.SURVIVED if i % 3 != 0 else AttemptOutcome.DIED
        t_ms = 4000 + rng.randint(-200, 200) if outcome == AttemptOutcome.SURVIVED else 1500
        ev = EventAttempt(
            segment_id=segment_id, session_id="sess_pe",
            episode_id=f"{segment_id}_e{i}",
            outcome=outcome, time_ms=t_ms,
            source=AttemptSource.PRACTICE,
            created_at=datetime.now(UTC),
        )
        db.log_event_attempt(ev)
        state = process_event(state, ev)
    db.save_model_state(
        segment_id, "em_suite_sampler",
        json.dumps(state.to_dict()),
        json.dumps({"total": {"expected_ms": None, "ms_per_attempt": None, "floor_ms": None},
                    "clean": {"expected_ms": None, "ms_per_attempt": None, "floor_ms": None}}),
    )


def _seeded_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Game", "any%")
    for seg_id, level in [("s1", 1), ("s2", 2)]:
        seg = Segment(
            id=seg_id, game_id="g1", level_number=level,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
        )
        db.upsert_segment(seg)
        _seed_gated_state(db, seg_id)
    return db


class TestSchedulerEngineLazy:
    def test_engine_built_on_first_access(self, tmp_path):
        db = _seeded_db(tmp_path)
        sched = Scheduler(db, "g1")
        engine = sched.engine
        engine.matrix.ensure_fresh()
        assert "s1" in engine.matrix.seg_ids
        assert "s2" in engine.matrix.seg_ids

    def test_engine_invalidation_on_attempt_completion(self, tmp_path):
        db = _seeded_db(tmp_path)
        sched = Scheduler(db, "g1")
        engine = sched.engine
        engine.matrix.ensure_fresh()
        assert engine.matrix.dirty == set()
        # Simulate an attempt landing:
        db.log_attempt(Attempt(
            segment_id="s1", session_id="sess_pe",
            completed=True, time_ms=5000,
        ))
        sched.update_state_after_episode("s1")
        # Engine column for s1 should now be dirty:
        assert "s1" in engine.matrix.dirty
        assert "s2" not in engine.matrix.dirty


class TestSchedulerLoadAllSamplerStates:
    def test_returns_gated_states_only(self, tmp_path):
        db = _seeded_db(tmp_path)
        # Add a third segment that has NO events (ungated):
        db.upsert_segment(Segment(
            id="s3", game_id="g1", level_number=3,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
        ))
        sched = Scheduler(db, "g1")
        states = sched._load_all_sampler_states()
        # s1 and s2 are gated; s3 has no model_state row → not present
        assert set(states.keys()) == {"s1", "s2"}
```

- [ ] **Step 2: Run tests — should fail** (engine attribute and _load_all_sampler_states don't exist).

```bash
python -m pytest tests/unit/practice_engine/test_scheduler_integration.py -q
```
Expected: AttributeError or failure on `sched.engine`.

- [ ] **Step 3: Add `_load_all_sampler_states` and lazy `engine` to `Scheduler`.** In `python/spinlab/scheduler.py`:

Find the existing import block at the top and add:
```python
from spinlab.practice_engine.engine import PracticeEngine
from spinlab.estimators.em_suite_sampler import SamplerState
```

Add a new private attribute initialization in `Scheduler.__init__` (after line ~95, before any other `self.` assignments work; specifically right after `self._drop_legacy_allocator_config_key()`):
```python
        self._engine: PracticeEngine | None = None
```

Add the lazy property and helper method to `Scheduler` (place them anywhere within the class, e.g. just before `update_state_after_episode`):

```python
    @property
    def engine(self) -> PracticeEngine:
        """Lazy practice simulation engine. Built on first access from current SamplerStates."""
        if self._engine is None:
            from spinlab.config import PracticeEngineConfig  # local import: avoid cycle
            # N comes from config when wired through; for now default to 20000.
            # Scheduler doesn't receive config today — see follow-up wiring in routes.
            self._engine = PracticeEngine(
                sampler_states=self._load_all_sampler_states(),
                N=20000,
                rng_seed=0,
            )
        return self._engine

    def _load_all_sampler_states(self) -> dict[str, SamplerState]:
        """Hydrate all SamplerState objects for this game's segments.

        Returns only segments that have a saved em_suite_sampler model_state row.
        Newly-gated segments (without a saved row yet) are absent until their first
        update_state_after_episode call writes one.
        """
        rows = self.db.load_all_model_states(self.game_id)
        out: dict[str, SamplerState] = {}
        for r in rows:
            if r["estimator"] != "em_suite_sampler" or not r["state_json"]:
                continue
            out[r["segment_id"]] = SamplerState.from_dict(json.loads(r["state_json"]))
        return out
```

Find the existing `update_state_after_episode` method and add an invalidation call at the bottom (after the existing `self._maybe_refit_segment(segment_id)` call, INSIDE the try/except guard so a stale engine doesn't crash practice):

```python
            self._maybe_refit_segment(segment_id)
            if self._engine is not None:
                self._engine.invalidate(segment_id)
```

(Place this line as the last statement inside the try-block; the guard catches any exception so a failing engine.invalidate doesn't crash practice. Re-check the existing try/except boundaries in the file — the guard added in commit `832a85d` already wraps this block.)

- [ ] **Step 4: Run tests — should pass.**

```bash
python -m pytest tests/unit/practice_engine/test_scheduler_integration.py -q
```
Expected: 3 passed.

- [ ] **Step 5: Run the full fast suite.**

```bash
python -m pytest -m "not emulator" -q
```
Expected: green.

- [ ] **Step 6: Commit.**

```bash
git add -A
git commit -m "feat(scheduler): lazy PracticeEngine attribute + invalidation hook on update_state_after_episode"
```

---

## Task 9: API schemas for the new endpoints

**Files:**
- Modify: `python/spinlab/api_schemas.py`
- Test: routes test file is created in Task 10 — schemas are exercised there

- [ ] **Step 1: Add request/response schemas.** In `python/spinlab/api_schemas.py`, append (place near the end of the file, before any trailing helpers):

```python
# ---------------------------------------------------------------------------
# Practice Simulation Engine — /api/practice-engine/*
# ---------------------------------------------------------------------------

class PracticeEngineSegmentState(_BaseResponse):
    seg_id: str
    description: str
    level_number: int
    e_sample_0_ms: float
    e_sample_1_ms: float
    pool_success: int
    pool_death: int
    gold_ms: int | None = None  # backs the "fill from gold" dashboard helper


class PracticeEngineUngated(_BaseResponse):
    seg_id: str
    reason: str


class PracticeEngineState(_BaseResponse):
    gated_segments: list[PracticeEngineSegmentState] = []
    ungated_segments: list[PracticeEngineUngated] = []
    matrix_built_at: str | None = None
    N: int


class PracticeEngineEvaluateRequest(BaseModel):
    policy: Literal["no_reset", "target_paced"]
    policy_kwargs: dict = {}  # cum_splits_ms, slack — optional, validated server-side
    objective: Literal[
        "expected_wall_clock_per_attempt",
        "expected_total_finished_time",
        "q",
        "quantile",
        "p_pb_this_session",
    ]
    objective_ctx: dict = {}  # target_ms, p, session_remaining_ms — validated server-side


class PracticeEnginePerSegmentValue(_BaseResponse):
    seg_id: str
    value: float
    value_per_second: float | None
    e_sample_0_ms: float
    e_sample_1_ms: float


class PracticeEngineTotalTimeSummary(_BaseResponse):
    bins: list[float] = []
    counts: list[int] = []
    mean: float | None = None
    median: float | None = None
    p10: float | None = None
    p90: float | None = None
    finished_pct: float
    aborted_by_segment: dict[str, int] = {}


class PracticeEngineEvaluateResponse(_BaseResponse):
    objective_value: float | None
    per_segment_values: list[PracticeEnginePerSegmentValue] = []
    total_time_summary: PracticeEngineTotalTimeSummary
```

- [ ] **Step 2: Confirm schemas import cleanly.**

```bash
python -c "from spinlab.api_schemas import PracticeEngineState, PracticeEngineEvaluateRequest, PracticeEngineEvaluateResponse; print('ok')"
```
Expected: prints `ok`.

- [ ] **Step 3: Commit.**

```bash
git add -A
git commit -m "feat(schemas): practice-engine route schemas"
```

---

## Task 10: FastAPI routes

**Files:**
- Create: `python/spinlab/routes/practice_engine.py`
- Modify: `python/spinlab/dashboard.py`
- Test: `tests/unit/test_practice_engine_routes.py`

- [ ] **Step 1: Write the failing tests.** Create `tests/unit/test_practice_engine_routes.py`:

```python
"""Tests for /api/practice-engine routes."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from spinlab.dashboard import create_app
from spinlab.db import Database
from spinlab.estimators.em_suite_sampler import SamplerState, process_event
from spinlab.models import (
    AttemptOutcome,
    AttemptSource,
    EventAttempt,
    Segment,
)


def _seed_gated_state(db, segment_id, n_events=60):
    state = SamplerState(n_completed=0, n_attempts=0)
    import random
    rng = random.Random(hash(segment_id) % 1000)
    db.create_session("sess_pe", "g1")
    for i in range(n_events):
        outcome = AttemptOutcome.SURVIVED if i % 3 != 0 else AttemptOutcome.DIED
        t_ms = 4000 + rng.randint(-200, 200) if outcome == AttemptOutcome.SURVIVED else 1500
        ev = EventAttempt(
            segment_id=segment_id, session_id="sess_pe",
            episode_id=f"{segment_id}_e{i}",
            outcome=outcome, time_ms=t_ms,
            source=AttemptSource.PRACTICE,
            created_at=datetime.now(UTC),
        )
        db.log_event_attempt(ev)
        state = process_event(state, ev)
    db.save_model_state(
        segment_id, "em_suite_sampler",
        json.dumps(state.to_dict()),
        json.dumps({"total": {"expected_ms": None, "ms_per_attempt": None, "floor_ms": None},
                    "clean": {"expected_ms": None, "ms_per_attempt": None, "floor_ms": None}}),
    )


@pytest.fixture
def client_with_gated(tmp_path):
    from tests.conftest import make_test_config
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Game", "any%")
    for seg_id, level in [("s1", 1), ("s2", 2)]:
        seg = Segment(
            id=seg_id, game_id="g1", level_number=level,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            description=f"Level {level}",
        )
        db.upsert_segment(seg)
        _seed_gated_state(db, seg_id)
    app = create_app(db=db, config=make_test_config())
    app.state.session.game_id = "g1"
    app.state.session.game_name = "Game"
    return TestClient(app)


@pytest.fixture
def client_no_game(tmp_path):
    from tests.conftest import make_test_config
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Game", "any%")
    app = create_app(db=db, config=make_test_config())
    return TestClient(app)


class TestStateEndpoint:
    def test_returns_gated_segments(self, client_with_gated):
        resp = client_with_gated.get("/api/practice-engine/state")
        assert resp.status_code == 200
        data = resp.json()
        assert {s["seg_id"] for s in data["gated_segments"]} == {"s1", "s2"}
        for s in data["gated_segments"]:
            assert s["e_sample_0_ms"] > 0
            assert s["pool_success"] > 0
            assert s["pool_death"] > 0

    def test_no_game_returns_empty(self, client_no_game):
        resp = client_no_game.get("/api/practice-engine/state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["gated_segments"] == []
        assert data["ungated_segments"] == []


class TestEvaluateEndpoint:
    def test_no_reset_expected_wall_clock(self, client_with_gated):
        resp = client_with_gated.post(
            "/api/practice-engine/evaluate",
            json={
                "policy": "no_reset", "policy_kwargs": {},
                "objective": "expected_wall_clock_per_attempt", "objective_ctx": {},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["objective_value"] is not None
        assert data["total_time_summary"]["finished_pct"] == pytest.approx(100.0)
        assert len(data["per_segment_values"]) == 2

    def test_target_paced_with_thresholds(self, client_with_gated):
        resp = client_with_gated.post(
            "/api/practice-engine/evaluate",
            json={
                "policy": "target_paced",
                "policy_kwargs": {
                    "cum_splits_ms": {"s1": 6000, "s2": 12000},
                    "slack": 0.1,
                },
                "objective": "expected_wall_clock_per_attempt", "objective_ctx": {},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # Some rollouts may abort
        assert 0.0 <= data["total_time_summary"]["finished_pct"] <= 100.0

    def test_q_objective_requires_target_ms(self, client_with_gated):
        resp = client_with_gated.post(
            "/api/practice-engine/evaluate",
            json={
                "policy": "no_reset", "policy_kwargs": {},
                "objective": "q", "objective_ctx": {"target_ms": 7000},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["objective_value"] is not None
        assert 0.0 <= data["objective_value"] <= 1.0

    def test_unknown_objective_400(self, client_with_gated):
        resp = client_with_gated.post(
            "/api/practice-engine/evaluate",
            json={
                "policy": "no_reset", "policy_kwargs": {},
                "objective": "not_a_real_objective", "objective_ctx": {},
            },
        )
        assert resp.status_code == 422  # Pydantic Literal mismatch
```

- [ ] **Step 2: Run tests — should fail** (router doesn't exist).

```bash
python -m pytest tests/unit/test_practice_engine_routes.py -q
```
Expected: 404 from the endpoints + a test-collection ImportError.

- [ ] **Step 3: Implement the router.** Create `python/spinlab/routes/practice_engine.py`:

```python
"""FastAPI routes: /api/practice-engine/state, /api/practice-engine/evaluate.

Read-only diagnostic surface over the PracticeEngine. See
docs/superpowers/specs/2026-06-01-practice-simulation-engine-design.md §11.
"""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException

from spinlab.api_schemas import (
    PracticeEngineEvaluateRequest,
    PracticeEngineEvaluateResponse,
    PracticeEnginePerSegmentValue,
    PracticeEngineSegmentState,
    PracticeEngineState,
    PracticeEngineTotalTimeSummary,
    PracticeEngineUngated,
)
from spinlab.db import Database
from spinlab.estimators.em_suite_sampler import (
    _gate_passes,
    expected_episode_time_scalar,
)
from spinlab.practice_engine import objectives, reset_policies
from spinlab.practice_engine.threshold_sources import thresholds_from_user
from spinlab.routes._deps import get_db, get_session
from spinlab.session_manager import SessionManager

router = APIRouter(prefix="/api/practice-engine")


# Map string identifiers from the request to actual function objects.
_POLICIES: dict[str, Callable] = {
    "no_reset": reset_policies.no_reset,
    "target_paced": reset_policies.target_paced,
}
_OBJECTIVES: dict[str, Callable] = {
    "expected_wall_clock_per_attempt": objectives.expected_wall_clock_per_attempt,
    "expected_total_finished_time": objectives.expected_total_finished_time,
    "q": objectives.q,
    "quantile": objectives.quantile,
    "p_pb_this_session": objectives.p_pb_this_session,
}


@router.get("/state", response_model=PracticeEngineState)
def get_state(
    session: SessionManager = Depends(get_session),
    db: Database = Depends(get_db),
) -> PracticeEngineState:
    if session.game_id is None:
        return PracticeEngineState(N=0)
    sched = session.get_scheduler()
    states = sched._load_all_sampler_states()
    # Pull segment metadata + gold values for display:
    segs = {s.id: s for s in db.get_active_segments(session.game_id)}
    golds = db.compute_golds(session.game_id)

    gated: list[PracticeEngineSegmentState] = []
    ungated: list[PracticeEngineUngated] = []

    for seg_id, state in states.items():
        if seg_id not in segs:
            continue
        meta = segs[seg_id]
        gold_ms = golds.get(seg_id, {}).get("gold_ms")
        n_succ = len(state.success_time_pool)
        n_death = len(state.death_time_pool)
        if _gate_passes(state):
            # E[sample(0)] from the closed-form scalar; precise per-segment draws
            # live in /evaluate. e_sample_1 is a cheap closed-form lookahead.
            e0 = expected_episode_time_scalar(state) or 0.0
            gated.append(PracticeEngineSegmentState(
                seg_id=seg_id,
                description=meta.description or "",
                level_number=meta.level_number,
                e_sample_0_ms=float(e0),
                e_sample_1_ms=float(e0),  # placeholder; /evaluate fills in real draws
                pool_success=n_succ,
                pool_death=n_death,
                gold_ms=gold_ms,
            ))
        else:
            reason = (
                f"needs ≥2 successes (have {n_succ}) and ≥2 deaths (have {n_death})"
            )
            ungated.append(PracticeEngineUngated(seg_id=seg_id, reason=reason))

    return PracticeEngineState(
        gated_segments=gated,
        ungated_segments=ungated,
        matrix_built_at=None,
        N=sched.engine.matrix.N if gated else 0,
    )


@router.post("/evaluate", response_model=PracticeEngineEvaluateResponse)
def evaluate(
    body: PracticeEngineEvaluateRequest,
    session: SessionManager = Depends(get_session),
    db: Database = Depends(get_db),
) -> PracticeEngineEvaluateResponse:
    if session.game_id is None:
        raise HTTPException(status_code=400, detail="No game loaded")
    sched = session.get_scheduler()
    engine = sched.engine
    engine.matrix.ensure_fresh()

    policy_fn = _POLICIES[body.policy]
    objective_fn = _OBJECTIVES[body.objective]

    # Build threshold kwargs from the request body.
    threshold_kwargs: dict = {}
    if body.policy == "target_paced":
        cum_splits = body.policy_kwargs.get("cum_splits_ms")
        if cum_splits is None:
            threshold_kwargs["threshold_cum_ms"] = None
        else:
            threshold_kwargs["threshold_cum_ms"] = thresholds_from_user(
                seg_ids=engine.matrix.seg_ids,
                cum_splits_ms=cum_splits,
            )
        threshold_kwargs["slack"] = float(body.policy_kwargs.get("slack", 0.0))

    ctx = dict(body.objective_ctx)

    eval_result = engine.evaluate(policy_fn, threshold_kwargs, objective_fn, ctx)
    per_seg = engine.per_segment_values(policy_fn, threshold_kwargs, objective_fn, ctx)
    dist = engine.total_time_distribution(policy_fn, threshold_kwargs)

    return PracticeEngineEvaluateResponse(
        objective_value=eval_result["value"],
        per_segment_values=[
            PracticeEnginePerSegmentValue(
                seg_id=psv.seg_id,
                value=psv.value,
                value_per_second=psv.value_per_second,
                e_sample_0_ms=psv.e_sample_0_ms,
                e_sample_1_ms=psv.e_sample_1_ms,
            ) for psv in per_seg.values()
        ],
        total_time_summary=PracticeEngineTotalTimeSummary(
            bins=dist["bins"],
            counts=dist["counts"],
            mean=dist["mean"],
            median=dist["median"],
            p10=dist["p10"],
            p90=dist["p90"],
            finished_pct=eval_result["masks_summary"]["finished_pct"],
            aborted_by_segment=eval_result["masks_summary"]["aborted_by_segment"],
        ),
    )
```

- [ ] **Step 4: Wire the router into `dashboard.py`.** In `python/spinlab/dashboard.py`, find the existing import block of routers (`from .routes.attempts import router as attempts_router`, lines ~160-166) and add:

```python
    from .routes.practice_engine import router as practice_engine_router
```

Then find the `app.include_router(...)` block and add:
```python
    app.include_router(practice_engine_router)
```

- [ ] **Step 5: Run tests — should pass.**

```bash
python -m pytest tests/unit/test_practice_engine_routes.py -q
```
Expected: 5 passed.

- [ ] **Step 6: Run the full fast suite.**

```bash
python -m pytest -m "not emulator" -q
```
Expected: green.

- [ ] **Step 7: Commit.**

```bash
git add -A
git commit -m "feat(routes): /api/practice-engine/state + /evaluate endpoints"
```

---

## Task 11: Regen frontend types + verify typecheck

**Files:**
- Modify (codegen): `frontend/openapi.json`, `frontend/src/api-types.ts`
- Modify: `frontend/src/types.ts`

- [ ] **Step 1: Regen the OpenAPI dump + TS types.**

```bash
cd frontend && npm run gen-types
```
Expected: writes `openapi.json` and `src/api-types.ts`.

- [ ] **Step 2: Re-export the new types.** Edit `frontend/src/types.ts` to add (place near the existing groups of exports):

```typescript
// Practice Simulation Engine
export type PracticeEngineState = S["PracticeEngineState"];
export type PracticeEngineSegmentState = S["PracticeEngineSegmentState"];
export type PracticeEngineUngated = S["PracticeEngineUngated"];
export type PracticeEngineEvaluateRequest = S["PracticeEngineEvaluateRequest"];
export type PracticeEngineEvaluateResponse = S["PracticeEngineEvaluateResponse"];
export type PracticeEnginePerSegmentValue = S["PracticeEnginePerSegmentValue"];
export type PracticeEngineTotalTimeSummary = S["PracticeEngineTotalTimeSummary"];
```

- [ ] **Step 3: Frontend typecheck + build.**

```bash
cd C:/Users/thedo/git/spinlab/frontend && npm run typecheck && npm run build
```
Expected: clean.

- [ ] **Step 4: Commit.**

```bash
git add -A
git commit -m "feat(frontend): regen api-types + export PracticeEngine types"
```

---

## Task 12: Frontend panel — module, HTML, tab wiring

**Files:**
- Create: `frontend/src/practice-engine.ts`
- Create: `frontend/src/practice-engine.test.ts`
- Modify: `frontend/index.html`
- Modify: `frontend/src/app.ts`

- [ ] **Step 1: Write the failing frontend test.** Create `frontend/src/practice-engine.test.ts`:

```typescript
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderPracticeEnginePanel, buildEvaluateRequest } from "./practice-engine";
import type { PracticeEngineState, PracticeEngineEvaluateResponse } from "./types";

const MOCK_STATE: PracticeEngineState = {
  gated_segments: [
    { seg_id: "s1", description: "Level 1", level_number: 1,
      e_sample_0_ms: 4500, e_sample_1_ms: 4300,
      pool_success: 40, pool_death: 20, gold_ms: 4200 },
    { seg_id: "s2", description: "Level 2", level_number: 2,
      e_sample_0_ms: 6000, e_sample_1_ms: 5800,
      pool_success: 35, pool_death: 25, gold_ms: 5800 },
  ],
  ungated_segments: [],
  matrix_built_at: null,
  N: 20000,
};

const MOCK_EVAL: PracticeEngineEvaluateResponse = {
  objective_value: 10500,
  per_segment_values: [
    { seg_id: "s1", value: 200, value_per_second: 200/4500,
      e_sample_0_ms: 4500, e_sample_1_ms: 4300 },
    { seg_id: "s2", value: 150, value_per_second: 150/6000,
      e_sample_0_ms: 6000, e_sample_1_ms: 5850 },
  ],
  total_time_summary: {
    bins: [10000, 10500, 11000, 11500],
    counts: [200, 500, 300],
    mean: 10500, median: 10500, p10: 10200, p90: 10800,
    finished_pct: 100,
    aborted_by_segment: {},
  },
};

describe("renderPracticeEnginePanel", () => {
  beforeEach(() => {
    document.body.innerHTML = `<div id="practice-engine-panel"></div>`;
  });

  it("renders gated segments in the controls table", () => {
    const container = document.getElementById("practice-engine-panel")!;
    renderPracticeEnginePanel(container, MOCK_STATE);
    expect(container.querySelector(".pe-segments-input")).not.toBeNull();
    // Two rows for s1, s2:
    const rows = container.querySelectorAll(".pe-segments-input tbody tr");
    expect(rows.length).toBe(2);
  });

  it("shows ungated segments separately if any", () => {
    const stateWithUngated: PracticeEngineState = {
      ...MOCK_STATE,
      ungated_segments: [{ seg_id: "s3", reason: "needs more data" }],
    };
    const container = document.getElementById("practice-engine-panel")!;
    renderPracticeEnginePanel(container, stateWithUngated);
    expect(container.textContent).toContain("needs more data");
  });

  it("fill-from-gold button populates inputs with cumulative golds", () => {
    const container = document.getElementById("practice-engine-panel")!;
    renderPracticeEnginePanel(container, MOCK_STATE);
    const fillBtn = container.querySelector<HTMLButtonElement>("#pe-fill-gold")!;
    fillBtn.click();
    const s1Input = container.querySelector<HTMLInputElement>('input.pe-seg-split[data-seg-id="s1"]')!;
    const s2Input = container.querySelector<HTMLInputElement>('input.pe-seg-split[data-seg-id="s2"]')!;
    expect(s1Input.value).toBe("4200");          // cum gold through s1
    expect(s2Input.value).toBe(String(4200 + 5800));  // cum gold through s2
  });
});

describe("buildEvaluateRequest", () => {
  it("packages no_reset request without policy_kwargs", () => {
    const req = buildEvaluateRequest({
      policy: "no_reset",
      cumSplits: {},
      slack: 0,
      objective: "expected_wall_clock_per_attempt",
      objectiveCtx: {},
    });
    expect(req.policy).toBe("no_reset");
    expect(req.objective).toBe("expected_wall_clock_per_attempt");
    expect(req.objective_ctx).toEqual({});
  });

  it("packages target_paced request with cum_splits + slack", () => {
    const req = buildEvaluateRequest({
      policy: "target_paced",
      cumSplits: { s1: 6000, s2: 12000 },
      slack: 0.15,
      objective: "q",
      objectiveCtx: { target_ms: 11000 },
    });
    expect(req.policy).toBe("target_paced");
    expect(req.policy_kwargs).toEqual({
      cum_splits_ms: { s1: 6000, s2: 12000 },
      slack: 0.15,
    });
    expect(req.objective_ctx).toEqual({ target_ms: 11000 });
  });
});
```

- [ ] **Step 2: Run frontend tests — should fail** (module doesn't exist).

```bash
cd C:/Users/thedo/git/spinlab/frontend && npm test
```
Expected: error on import of `./practice-engine`.

- [ ] **Step 3: Implement the frontend module.** Create `frontend/src/practice-engine.ts`:

```typescript
/**
 * Practice Simulation Engine — dashboard panel.
 *
 * Renders policy + objective controls and surfaces objective values,
 * per-segment value attribution, and total-time histogram. Read-only.
 */
import type {
  PracticeEngineState,
  PracticeEngineEvaluateRequest,
  PracticeEngineEvaluateResponse,
} from "./types";

type PolicyName = "no_reset" | "target_paced";
type ObjectiveName =
  | "expected_wall_clock_per_attempt"
  | "expected_total_finished_time"
  | "q"
  | "quantile"
  | "p_pb_this_session";

interface BuildArgs {
  policy: PolicyName;
  cumSplits: Record<string, number>;
  slack: number;
  objective: ObjectiveName;
  objectiveCtx: Record<string, number>;
}

export function buildEvaluateRequest(args: BuildArgs): PracticeEngineEvaluateRequest {
  const req: PracticeEngineEvaluateRequest = {
    policy: args.policy,
    policy_kwargs: {},
    objective: args.objective,
    objective_ctx: args.objectiveCtx,
  };
  if (args.policy === "target_paced") {
    req.policy_kwargs = {
      cum_splits_ms: args.cumSplits,
      slack: args.slack,
    };
  }
  return req;
}

export async function fetchState(): Promise<PracticeEngineState> {
  const resp = await fetch("/api/practice-engine/state");
  if (!resp.ok) throw new Error(`/api/practice-engine/state ${resp.status}`);
  return resp.json();
}

export async function fetchEvaluate(
  body: PracticeEngineEvaluateRequest,
): Promise<PracticeEngineEvaluateResponse> {
  const resp = await fetch("/api/practice-engine/evaluate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`/api/practice-engine/evaluate ${resp.status}`);
  return resp.json();
}

export function renderPracticeEnginePanel(
  container: HTMLElement,
  state: PracticeEngineState,
): void {
  container.innerHTML = "";

  // Header
  const header = document.createElement("h2");
  header.textContent = "Practice Simulator";
  container.appendChild(header);

  // Policy + objective controls
  const controls = document.createElement("div");
  controls.className = "pe-controls";
  controls.innerHTML = `
    <label>Policy:
      <select id="pe-policy">
        <option value="no_reset">no_reset</option>
        <option value="target_paced">target_paced</option>
      </select>
    </label>
    <label>Objective:
      <select id="pe-objective">
        <option value="expected_wall_clock_per_attempt">expected_wall_clock_per_attempt</option>
        <option value="expected_total_finished_time">expected_total_finished_time</option>
        <option value="q">q(target)</option>
        <option value="quantile">quantile(p)</option>
        <option value="p_pb_this_session">p_pb_this_session</option>
      </select>
    </label>
    <label>Slack:
      <input id="pe-slack" type="number" step="0.05" value="0" min="0" max="1" />
    </label>
    <label>target_ms:
      <input id="pe-target-ms" type="number" step="100" placeholder="e.g. 12000" />
    </label>
    <label>p (quantile):
      <input id="pe-p" type="number" step="0.05" min="0" max="1" placeholder="0.5" />
    </label>
    <label>session_remaining_ms:
      <input id="pe-h" type="number" step="60000" placeholder="e.g. 10440000" />
    </label>
    <button id="pe-recompute">Recompute</button>
  `;
  container.appendChild(controls);

  // Per-segment threshold input table (used by target_paced)
  // The "fill from gold" button pre-populates with cumulative golds.
  const segInputWrap = document.createElement("div");
  segInputWrap.innerHTML = `
    <button id="pe-fill-gold" type="button">Fill cum-splits from gold</button>
  `;
  container.appendChild(segInputWrap);

  const segInput = document.createElement("table");
  segInput.className = "pe-segments-input";
  segInput.innerHTML = `
    <thead><tr><th>Segment</th><th>Cumulative split (ms)</th><th>Gold (ms)</th></tr></thead>
    <tbody>
      ${state.gated_segments.map(seg => `
        <tr data-seg-id="${seg.seg_id}">
          <td>${seg.description || seg.seg_id} (L${seg.level_number})</td>
          <td><input class="pe-seg-split" type="number" step="100" data-seg-id="${seg.seg_id}" /></td>
          <td class="pe-seg-gold" data-gold-ms="${seg.gold_ms ?? ""}">${seg.gold_ms ?? "—"}</td>
        </tr>
      `).join("")}
    </tbody>
  `;
  container.appendChild(segInput);

  // Wire the fill-from-gold button: cumulative sum of per-segment golds.
  const fillBtn = segInputWrap.querySelector<HTMLButtonElement>("#pe-fill-gold");
  if (fillBtn) {
    fillBtn.addEventListener("click", () => {
      let cum = 0;
      state.gated_segments.forEach(seg => {
        if (seg.gold_ms !== null && seg.gold_ms !== undefined) {
          cum += seg.gold_ms;
          const input = segInput.querySelector<HTMLInputElement>(
            `.pe-seg-split[data-seg-id="${seg.seg_id}"]`,
          );
          if (input) input.value = String(cum);
        }
      });
    });
  }

  // Headline objective value
  const headline = document.createElement("div");
  headline.className = "pe-headline";
  headline.id = "pe-headline";
  headline.textContent = "(Click Recompute)";
  container.appendChild(headline);

  // Histogram canvas
  const canvasWrap = document.createElement("div");
  canvasWrap.className = "pe-histogram-wrap";
  canvasWrap.innerHTML = `<canvas id="pe-histogram"></canvas>`;
  container.appendChild(canvasWrap);

  // Per-segment value table
  const valuesTable = document.createElement("table");
  valuesTable.className = "pe-values";
  valuesTable.innerHTML = `
    <thead><tr>
      <th>Segment</th><th>E[sample(0)]</th><th>E[sample(1)]</th>
      <th>Δ</th><th>Value</th><th>Value/sec</th>
    </tr></thead>
    <tbody id="pe-values-body"></tbody>
  `;
  container.appendChild(valuesTable);

  // Ungated segments
  if (state.ungated_segments.length > 0) {
    const ungated = document.createElement("div");
    ungated.className = "pe-ungated";
    ungated.innerHTML = `<h3>Ungated</h3><ul>${
      state.ungated_segments.map(u => `<li>${u.seg_id}: ${u.reason}</li>`).join("")
    }</ul>`;
    container.appendChild(ungated);
  }
}

export function updatePanelResults(
  container: HTMLElement,
  response: PracticeEngineEvaluateResponse,
): void {
  const headline = container.querySelector<HTMLDivElement>("#pe-headline");
  if (headline) {
    headline.textContent = response.objective_value === null
      ? "(None — gate failed)"
      : `Objective: ${response.objective_value.toFixed(2)}`;
  }
  const body = container.querySelector<HTMLTableSectionElement>("#pe-values-body");
  if (body) {
    body.innerHTML = response.per_segment_values.map(psv => `
      <tr>
        <td>${psv.seg_id}</td>
        <td>${psv.e_sample_0_ms.toFixed(0)}</td>
        <td>${psv.e_sample_1_ms.toFixed(0)}</td>
        <td>${(psv.e_sample_0_ms - psv.e_sample_1_ms).toFixed(0)}</td>
        <td>${psv.value.toFixed(2)}</td>
        <td>${psv.value_per_second === null ? "—" : psv.value_per_second.toExponential(2)}</td>
      </tr>
    `).join("");
  }
}

export async function initPracticeEnginePanel(): Promise<void> {
  const container = document.getElementById("practice-engine-panel");
  if (!container) return;
  const state = await fetchState();
  renderPracticeEnginePanel(container, state);

  const recompute = container.querySelector<HTMLButtonElement>("#pe-recompute");
  if (!recompute) return;
  recompute.addEventListener("click", async () => {
    const policy = (container.querySelector<HTMLSelectElement>("#pe-policy"))?.value as PolicyName;
    const objective = (container.querySelector<HTMLSelectElement>("#pe-objective"))?.value as ObjectiveName;
    const slack = parseFloat((container.querySelector<HTMLInputElement>("#pe-slack"))?.value || "0");
    const cumSplits: Record<string, number> = {};
    container.querySelectorAll<HTMLInputElement>(".pe-seg-split").forEach(input => {
      const segId = input.dataset.segId;
      const value = parseFloat(input.value);
      if (segId && !isNaN(value)) {
        cumSplits[segId] = value;
      }
    });
    const objectiveCtx: Record<string, number> = {};
    const targetMs = parseFloat((container.querySelector<HTMLInputElement>("#pe-target-ms"))?.value || "");
    if (!isNaN(targetMs)) objectiveCtx.target_ms = targetMs;
    const p = parseFloat((container.querySelector<HTMLInputElement>("#pe-p"))?.value || "");
    if (!isNaN(p)) objectiveCtx.p = p;
    const h = parseFloat((container.querySelector<HTMLInputElement>("#pe-h"))?.value || "");
    if (!isNaN(h)) objectiveCtx.session_remaining_ms = h;
    const req = buildEvaluateRequest({
      policy, cumSplits, slack, objective, objectiveCtx,
    });
    const resp = await fetchEvaluate(req);
    updatePanelResults(container, resp);
  });
}
```

- [ ] **Step 4: Wire a new tab into `frontend/index.html`.** Find the existing `<nav id="tabs">` block and add a new tab button:

```html
<button class="tab" data-tab="practice-engine">Simulator</button>
```

Find the existing tab-content blocks and add:
```html
<section id="tab-practice-engine" class="tab-content">
  <div id="practice-engine-panel"></div>
</section>
```

- [ ] **Step 5: Wire the init into `frontend/src/app.ts`.** Find the existing tab-switch logic; on switch to the `practice-engine` tab, call `initPracticeEnginePanel()`. If the existing app uses a tab-switch handler pattern, mirror it. Concretely: locate `addEventListener` calls on `.tab` elements and ensure `initPracticeEnginePanel()` is invoked when `data-tab === "practice-engine"`. Add the import at the top:

```typescript
import { initPracticeEnginePanel } from "./practice-engine";
```

Where the existing tab switch dispatches by name (e.g. a switch or if-chain), add:
```typescript
} else if (tabName === "practice-engine") {
  initPracticeEnginePanel();
}
```

- [ ] **Step 6: Run frontend tests + build.**

```bash
cd C:/Users/thedo/git/spinlab/frontend && npm test && npm run typecheck && npm run build
```
Expected: all green.

- [ ] **Step 7: Commit.**

```bash
git add -A
git commit -m "feat(frontend): practice-engine panel — controls + values table + histogram canvas"
```

---

## Task 13: Final verification

**Files:** none (verification only); possibly `docs/ARCHITECTURE.md`

- [ ] **Step 1: Static analysis.**

```bash
cd C:/Users/thedo/git/spinlab && ruff check python/spinlab/practice_engine python/spinlab/routes/practice_engine.py
```
Expected: clean (no new errors).

```bash
npx pyright python/spinlab/practice_engine python/spinlab/routes/practice_engine.py
```
Expected: zero new errors (baseline-equivalent overall).

- [ ] **Step 2: Frontend.**

```bash
cd C:/Users/thedo/git/spinlab/frontend && npm run typecheck && npm run build && npm test
```
Expected: green.

- [ ] **Step 3: Full unfiltered pytest** (project policy):

```bash
cd C:/Users/thedo/git/spinlab && python -m pytest
```
Expected: all pass. Skips count as failures — if emulator tests skip with a launch failure, surface it, don't treat as green.

- [ ] **Step 4: Grep for stragglers.**

```bash
grep -rn "PracticeEngine\|practice_engine" python/spinlab/ frontend/src/ | grep -v __pycache__ | head -20
```
Expected: only references inside the new module + its callers; nothing in docs/specs that's wrong.

- [ ] **Step 5: Update `docs/ARCHITECTURE.md` if it describes the scheduler.** Find the `## Scheduler: Sampler + Allocator Pipeline` section and add a short note that the scheduler now exposes a lazy `engine` attribute:

In `docs/ARCHITECTURE.md`, after the "Sampler" paragraph in the Scheduler section, add:
```markdown
**Practice Simulation Engine (`practice_engine/`)** — `scheduler.engine` is a lazy `PracticeEngine` instance that builds a vectorized rollout matrix from the per-segment SamplerStates and exposes objective reductions + per-segment value attribution. Read-only dashboard panel at `/api/practice-engine/*`. Designed as engine-with-consumers; the live practice allocator and run-advisor are downstream specs that consume it.
```

- [ ] **Step 6: Commit any doc cleanup.**

```bash
git add -A
git commit -m "docs(architecture): note practice simulation engine"
```

Plan complete. Hand off to `superpowers:finishing-a-development-branch` to merge.

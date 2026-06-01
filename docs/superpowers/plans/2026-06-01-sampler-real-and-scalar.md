# Sampler-Real & Scalar — Implementation Plan (Plan 1 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the EMA-Suite sampler an *actual* sampler — real bootstrap-with-slide episode-time draws, a corrected zero-decay (uniform-mean) α=0 anchor, and a real per-segment scalar emitted through `model_output` so the existing allocator/table work — all *additively*, deleting nothing.

**Architecture:** All changes are confined to `python/spinlab/estimators/em_suite_sampler.py` (plus its test file). We add two recency-weighted draw pools to `SamplerState`, a `draw_from_pool` helper, a `sample_episode` bootstrap generator, and a closed-form scalar that `model_output` now returns. The multi-model machinery, scheduler, allocators, routes, and frontend are untouched — the system stays multi-model and fully green. The destructive purge is Plan 2.

**Tech Stack:** Python 3.11+, dataclasses, pytest. Pure-Python (no JAX/numpy). Existing normalized `(Sum, Denom)` EMA stays.

**Spec:** [`docs/superpowers/specs/2026-06-01-model-purge-sampler-core-design.md`](../specs/2026-06-01-model-purge-sampler-core-design.md)

---

## File Structure

- **Modify:** `python/spinlab/estimators/em_suite_sampler.py` — add pools, draw helper, `sample_episode`, scalar, populate `model_output`; fix `ema_step` for α=0.
- **Modify/Create:** `tests/unit/test_em_suite_sampler.py` — the existing sampler unit test file (verify exact path in Task 0; create if absent). All new behavior is TDD'd here.

No other production files change in Plan 1.

---

## Task 0: Baseline & safety net

**Files:** none (verification + git only).

- [ ] **Step 1: Confirm the full suite is green BEFORE touching code**

Run: `python -m pytest`
Expected: all pass (unit + emulator + frontend). Per project policy a red baseline is **stop-and-ask** — do not proceed over failures; surface them first.

- [ ] **Step 2: Locate the sampler's test file**

Run: `ls tests/unit/test_em_suite_sampler.py` (and `git ls-files "tests/**em_suite**"`)
Expected: find the existing test path. If it does not exist, all new tests below go in `tests/unit/test_em_suite_sampler.py` (create it with the standard import header used by sibling tests: `from spinlab.estimators.em_suite_sampler import ...`).

- [ ] **Step 3: Cut the safety tag on current `main`**

```bash
git tag -a pre-model-purge -m "Pre model-purge snapshot: all six estimators present"
git tag --list pre-model-purge
```
Expected: tag listed. (Branch `model-purge-sampler-core` already holds the spec/plan commits.)

- [ ] **Step 4: Commit (no-op marker not needed — proceed to Task 1).**

---

## Task 1: α=0 means zero-decay (uniform mean), not updatelessness

**Files:**
- Modify: `python/spinlab/estimators/em_suite_sampler.py` (`ema_step`, the `ALPHA_GRID` comment, `_ema` is unchanged)
- Test: `tests/unit/test_em_suite_sampler.py`

**Why:** Under the normalized form, `α=0.0` currently keeps `Denom=0` forever → reads `—`. Its intended meaning is *zero decay* = every observation weighted equally = the unbiased all-time mean. Because the Sum accumulator is driven by the observation and the Denom by `1.0`, the α=0 slot becomes a uniform mean with a single rule: `new = old + driver` (Sum: `S+obs`; Denom: `D+1`).

- [ ] **Step 1: Write the failing test**

```python
import math
from spinlab.estimators.em_suite_sampler import ALPHA_GRID, SamplerState, process_event
from spinlab.models import AttemptOutcome, EventAttempt


def _evt(outcome: str, time_ms: int) -> EventAttempt:
    return EventAttempt(
        segment_id="seg", episode_id="ep",
        outcome=AttemptOutcome(outcome), time_ms=time_ms,
    )


def test_alpha_zero_is_uniform_mean_of_log_times():
    # alpha=0.0 is the first grid entry: the zero-decay / uniform anchor.
    assert ALPHA_GRID[0] == 0.0
    st = SamplerState()
    # Three successes at 1000, 2000, 4000 ms.
    for t in (1000, 2000, 4000):
        st = process_event(st, _evt("survived", t))
    # alpha=0 success-time EMA must equal the unbiased mean of log(time).
    expected = (math.log(1000) + math.log(2000) + math.log(4000)) / 3.0
    got = st.log_success_time_ema(0)
    assert got is not None
    assert math.isclose(got, expected, rel_tol=1e-9)


def test_alpha_zero_p_die_is_uniform_rate():
    st = SamplerState()
    for outcome in ("died", "survived", "died", "died"):
        st = process_event(st, _evt(outcome, 1500))
    # 3 deaths of 4 attempts -> uniform p_die = 0.75 at alpha=0.
    assert math.isclose(st.p_die_ema(0), 0.75, rel_tol=1e-9)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_em_suite_sampler.py::test_alpha_zero_is_uniform_mean_of_log_times -v`
Expected: FAIL — `got` is `None` (α=0 denom stays 0 under the current `ema_step`).

- [ ] **Step 3: Fix `ema_step` to special-case α=0 as uniform accumulation**

Replace the body of `ema_step` (currently the list comprehension `[a * driver + (1.0 - a) * v ...]`) with:

```python
def ema_step(values: list[float], driver: float) -> list[float]:
    """Apply one EMA-style update to all alphas in parallel.

    For alpha > 0: new[i] = alpha * driver + (1 - alpha) * values[i]  (normalized EMA).
    For alpha == 0: new[i] = values[i] + driver  (ZERO DECAY = unbiased
        accumulation). Because the Sum accumulator is driven by the
        observation and the Denom by 1.0, this makes the alpha=0 slot a true
        uniform all-time mean (Sum/Denom = sum(obs)/count), not the degenerate
        frozen-at-zero cell the plain alpha=0 substitution would give.

    Used for both the Sum (driver = observation) and Denom (driver = 1.0)
    accumulators inside the normalized EMA. Lengths must match ALPHA_GRID;
    a mismatch is a programming error (silent zip truncation otherwise).
    """
    if len(values) != len(ALPHA_GRID):
        raise ValueError(
            f"values has length {len(values)}, expected {len(ALPHA_GRID)}",
        )
    return [
        (v + driver) if a == 0.0 else (a * driver + (1.0 - a) * v)
        for v, a in zip(values, ALPHA_GRID)
    ]
```

Also update the `ALPHA_GRID` module comment: replace the note claiming "α=0.0 produces D = 0 forever and so never yields a valid EMA" with: `# alpha=0.0 is the ZERO-DECAY anchor: uniform (equal-weight) all-time mean, handled specially in ema_step. alpha=1.0 is the goldfish anchor (last attempt only).`

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_em_suite_sampler.py -k alpha_zero -v`
Expected: both PASS.

- [ ] **Step 5: Guard against regressions in the rest of the sampler suite**

Run: `python -m pytest tests/unit/test_em_suite_sampler.py -v`
Expected: all PASS (existing matrix/slope tests still green — α>0 math is unchanged).

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/estimators/em_suite_sampler.py tests/unit/test_em_suite_sampler.py
git commit -m "fix(em-suite): alpha=0 is zero-decay uniform mean, not a dead cell"
```

---

## Task 2: Two recency draw pools on `SamplerState`

**Files:**
- Modify: `python/spinlab/estimators/em_suite_sampler.py` (`SamplerState` fields + `to_dict`/`from_dict`; `process_event`; new `POOL_SIZE` constant + `_append_capped` helper)
- Test: `tests/unit/test_em_suite_sampler.py`

**Why:** `sample_episode` draws real past times. We keep two bounded pools of raw `time_ms` per segment — successes and deaths separately, so a death-heavy segment never starves the success pool. `p_die` stays an EMA (no pool — Bernoulli's mean is sufficient).

- [ ] **Step 1: Write the failing test**

```python
from spinlab.estimators.em_suite_sampler import POOL_SIZE, SamplerState, process_event


def test_pools_split_by_outcome():
    st = SamplerState()
    st = process_event(st, _evt("survived", 1000))
    st = process_event(st, _evt("died", 500))
    st = process_event(st, _evt("survived", 1200))
    assert st.success_time_pool == [1000.0, 1200.0]
    assert st.death_time_pool == [500.0]


def test_pool_is_ring_buffered_to_pool_size():
    st = SamplerState()
    for i in range(POOL_SIZE + 50):
        st = process_event(st, _evt("survived", 1000 + i))
    # Oldest 50 dropped; newest POOL_SIZE kept, in order.
    assert len(st.success_time_pool) == POOL_SIZE
    assert st.success_time_pool[0] == float(1000 + 50)
    assert st.success_time_pool[-1] == float(1000 + POOL_SIZE + 49)


def test_pools_roundtrip_through_serialization():
    st = SamplerState()
    st = process_event(st, _evt("survived", 1000))
    st = process_event(st, _evt("died", 500))
    restored = SamplerState.from_dict(st.to_dict())
    assert restored.success_time_pool == [1000.0]
    assert restored.death_time_pool == [500.0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_em_suite_sampler.py -k pool -v`
Expected: FAIL — `SamplerState` has no `success_time_pool` attribute.

- [ ] **Step 3: Add the constant and the capped-append helper**

Near the top of the module, after `ALPHA_GRID`:

```python
# Draw-pool size per segment, per outcome. Sized to cover several effective
# windows of the slowest recency-weighted rate (alpha=0.01 ~ 100-attempt
# memory). Two SEPARATE pools (success, death) so a death-heavy segment never
# starves the success pool. Bump if a segment needs a deeper success history
# than this; it is a one-line change with no structural impact.
POOL_SIZE = 300


def _append_capped(pool: list[float], value: float) -> list[float]:
    """Return a new list with `value` appended, keeping only the last POOL_SIZE
    entries (oldest dropped first). Immutable to match process_event's
    new-state-per-call convention."""
    return (pool + [value])[-POOL_SIZE:]
```

- [ ] **Step 4: Add the pool fields to `SamplerState`**

After the `p_die_denoms` field (before `n_successes`):

```python
    success_time_pool: list[float] = field(default_factory=list)
    death_time_pool: list[float] = field(default_factory=list)
```

In `to_dict`, add (before `"n_successes"`):

```python
            "success_time_pool": list(self.success_time_pool),
            "death_time_pool": list(self.death_time_pool),
```

In `from_dict`, add as constructor kwargs. Use `.get(..., [])` here — and ONLY here — as deliberate cross-version tolerance for state rows serialized before pools existed (they rebuild on the next event); the older fields stay direct-indexed so a genuinely malformed row still surfaces:

```python
            success_time_pool=list(d.get("success_time_pool", [])),
            death_time_pool=list(d.get("death_time_pool", [])),
```

- [ ] **Step 5: Fill the pools in `process_event`**

Inside `process_event`, in the `if is_death:` / `else:` branches, add the matching pool update, and pass both pools to the returned `SamplerState`. Concretely, set defaults before the branch:

```python
    new_success_pool = state.success_time_pool
    new_death_pool = state.death_time_pool
```

In the `if is_death:` branch add: `new_death_pool = _append_capped(state.death_time_pool, float(event.time_ms))`
In the `else:` branch add: `new_success_pool = _append_capped(state.success_time_pool, float(event.time_ms))`

Then in the `return SamplerState(...)` add the two kwargs:

```python
        success_time_pool=new_success_pool,
        death_time_pool=new_death_pool,
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_em_suite_sampler.py -k pool -v`
Expected: all 3 PASS.

- [ ] **Step 7: Full sampler suite green**

Run: `python -m pytest tests/unit/test_em_suite_sampler.py -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/estimators/em_suite_sampler.py tests/unit/test_em_suite_sampler.py
git commit -m "feat(em-suite): two recency draw pools (success/death) on SamplerState"
```

---

## Task 3: `draw_from_pool` — recency-weighted empirical draw

**Files:**
- Modify: `python/spinlab/estimators/em_suite_sampler.py` (new `draw_from_pool`)
- Test: `tests/unit/test_em_suite_sampler.py`

**Why:** A draw from a pool must weight recent entries more, consistent with the EMA's decay — `weight(age k) ∝ α(1−α)^k`, normalized over the pool. α=0 → uniform; α=1 → always the newest. Deterministic via an injected `random.Random`.

- [ ] **Step 1: Write the failing test**

```python
import random
from spinlab.estimators.em_suite_sampler import draw_from_pool


def test_draw_alpha_one_always_returns_newest():
    pool = [10.0, 20.0, 30.0]  # newest is last
    rng = random.Random(0)
    assert all(draw_from_pool(pool, 1.0, rng) == 30.0 for _ in range(20))


def test_draw_alpha_zero_is_roughly_uniform():
    pool = [10.0, 20.0, 30.0]
    rng = random.Random(1)
    draws = [draw_from_pool(pool, 0.0, rng) for _ in range(3000)]
    counts = {v: draws.count(v) for v in pool}
    # Uniform: each ~1000 of 3000. Loose bounds for sampling noise.
    assert all(800 < c < 1200 for c in counts.values())


def test_draw_empty_pool_returns_none():
    assert draw_from_pool([], 0.2, random.Random(0)) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_em_suite_sampler.py -k draw -v`
Expected: FAIL — `draw_from_pool` not defined.

- [ ] **Step 3: Implement `draw_from_pool`**

First add a TYPE_CHECKING import for the rng type (the module never calls `random.*` at runtime — the rng is always injected — so a runtime `import random` would be flagged unused by ruff under the module's `from __future__ import annotations`). Add near the top imports:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import random
```

Then add `draw_from_pool` after `_append_capped`:

```python
def draw_from_pool(
    pool: list[float], alpha: float, rng: "random.Random",
) -> float | None:
    """Draw one value from `pool`, recency-weighted by `alpha`.

    Weight on the entry of age k (k=0 is the newest, the last element) is
    proportional to alpha*(1-alpha)^k, matching the EMA's decay. alpha=0
    gives uniform weights (zero decay); alpha=1 puts all mass on the newest.
    Returns None for an empty pool (callers gate on non-empty pools first).
    """
    n = len(pool)
    if n == 0:
        return None
    if alpha <= 0.0:
        weights = [1.0] * n
    else:
        # Index j has age (n-1-j): the last element (j=n-1) has age 0.
        weights = [alpha * (1.0 - alpha) ** (n - 1 - j) for j in range(n)]
    return rng.choices(pool, weights=weights, k=1)[0]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_em_suite_sampler.py -k draw -v`
Expected: all 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/estimators/em_suite_sampler.py tests/unit/test_em_suite_sampler.py
git commit -m "feat(em-suite): recency-weighted draw_from_pool helper"
```

---

## Task 4: `sample_episode` — bootstrap-with-slide draw

**Files:**
- Modify: `python/spinlab/estimators/em_suite_sampler.py` (new `MAX_ATTEMPTS_PER_EPISODE`, `sample_episode`)
- Test: `tests/unit/test_em_suite_sampler.py`

**Why:** The object the sampler produces — a *drawn* episode time. Loop attempt draws (each gated by `p`) until the first survive, summing gameplay times + reload per death. `k>0` slides p and the per-draw times by the trend slopes. Capped to avoid infinite loops at high `p`.

- [ ] **Step 1: Write the failing test**

```python
import random
from spinlab.estimators.em_suite_sampler import (
    DEFAULT_DEATH_PENALTY_MS, sample_episode, SamplerState, process_event,
)


def _seed_balanced(st: SamplerState) -> SamplerState:
    # >=2 successes and >=2 deaths so the prediction gate passes.
    for outcome, t in [("survived", 2000), ("died", 500), ("survived", 2100),
                       ("died", 600), ("survived", 1900), ("died", 550)]:
        st = process_event(st, _evt(outcome, t))
    return st


def test_sample_episode_returns_none_below_gate():
    st = SamplerState()
    st = process_event(st, _evt("survived", 2000))  # only 1 success, 0 deaths
    assert sample_episode(st, fast_idx=6, slow_idx=4, rng=random.Random(0)) is None


def test_sample_episode_draws_a_positive_time():
    st = _seed_balanced(SamplerState())
    rng = random.Random(0)
    draws = [sample_episode(st, fast_idx=6, slow_idx=4, k=0, rng=rng)
             for _ in range(200)]
    assert all(d is not None and d > 0 for d in draws)
    # A draw with >=1 death must exceed the minimum success time alone.
    assert max(draws) > 2000


def test_sample_episode_gated_out_when_no_deaths():
    st = SamplerState()
    # All successes -> 0 deaths, so the >=2-deaths gate fails and the empty
    # death pool would have nothing to draw: must return None, not a fudge.
    for t in (2000, 2100, 1900):
        st = process_event(st, _evt("survived", t))
    assert sample_episode(st, fast_idx=6, slow_idx=4, rng=random.Random(0)) is None


def test_sample_episode_caps_and_returns_none_when_never_survives(monkeypatch):
    st = _seed_balanced(SamplerState())

    class AlwaysDie:
        def random(self):
            return 0.0  # always < p -> always "died"

        def choices(self, seq, weights, k):
            return [seq[-1]]

    assert sample_episode(st, fast_idx=6, slow_idx=4, rng=AlwaysDie()) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_em_suite_sampler.py -k sample_episode -v`
Expected: FAIL — `sample_episode` not defined.

- [ ] **Step 3: Implement `MAX_ATTEMPTS_PER_EPISODE` and `sample_episode`**

Add the constant near the other constants:

```python
# Inner-loop cap for a single simulated episode. A draw that never survives
# within this many attempts returns None (non-converged) rather than a fudge
# value, per the no-silent-fallback principle. Reachable only at near-certain
# death; normal segments survive in a handful of attempts.
MAX_ATTEMPTS_PER_EPISODE = 100
```

Add the function after `expected_episode_time_ms`:

```python
def sample_episode(
    state: SamplerState, fast_idx: int, slow_idx: int, k: int = 0,
    *, rng: "random.Random",
    reload_penalty_ms: int = DEFAULT_DEATH_PENALTY_MS,
) -> float | None:
    """Draw one episode time (ms): bootstrap-with-slide.

    Repeatedly draw an attempt — died with probability p, else survived —
    drawing the attempt's gameplay time from the matching recency-weighted
    pool, until the first survival. Sum gameplay times + reload_penalty_ms per
    death. k>0 slides p (logit space) and the per-draw times (log space) by
    k * the (alpha_fast, alpha_slow) trend slopes.

    Returns None when the prediction gate fails, when either pool is empty,
    or when the draw does not survive within MAX_ATTEMPTS_PER_EPISODE.
    """
    if not _gate_passes(state):
        return None
    if not state.success_time_pool or not state.death_time_pool:
        return None
    p_fast = state.p_die_ema(fast_idx)
    if p_fast is None:
        return None

    if k != 0:
        slopes = trend_signal_slopes(state, fast_idx, slow_idx)
        if slopes is None:
            return None
        slope_log_success, slope_log_death, slope_logit_p = slopes
        p = _logistic(_logit(p_fast) + k * slope_logit_p)
        slide_success = math.exp(k * slope_log_success)
        slide_death = math.exp(k * slope_log_death)
    else:
        p = p_fast
        slide_success = 1.0
        slide_death = 1.0

    alpha_fast = ALPHA_GRID[fast_idx]
    episode_ms = 0.0
    for _ in range(MAX_ATTEMPTS_PER_EPISODE):
        if rng.random() < p:  # died
            d = draw_from_pool(state.death_time_pool, alpha_fast, rng)
            episode_ms += d * slide_death + reload_penalty_ms
        else:  # survived -> episode ends
            s = draw_from_pool(state.success_time_pool, alpha_fast, rng)
            episode_ms += s * slide_success
            return episode_ms
    return None  # never survived within the cap
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_em_suite_sampler.py -k sample_episode -v`
Expected: all 4 PASS.

- [ ] **Step 5: Full sampler suite green**

Run: `python -m pytest tests/unit/test_em_suite_sampler.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/estimators/em_suite_sampler.py tests/unit/test_em_suite_sampler.py
git commit -m "feat(em-suite): sample_episode bootstrap-with-slide draw + convergence cap"
```

---

## Task 5: Emit a real scalar through `model_output`

**Files:**
- Modify: `python/spinlab/estimators/em_suite_sampler.py` (`DEFAULT_ALPHA_PAIR` constants, `expected_episode_time_scalar`, `EmSuiteSamplerEstimator.model_output`)
- Test: `tests/unit/test_em_suite_sampler.py`

**Why:** Greedy ranks on `out.total.ms_per_attempt`; the segment table shows the expected episode time. Both need one honest number per segment. We use the closed-form mean (variance-free, exact) at a named default α — `sample(0)` (no slope) at `α_fast=0.2`. This makes em_suite functional as the active model *without deleting anything*.

- [ ] **Step 1: Write the failing test**

```python
from spinlab.estimators.em_suite_sampler import (
    DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX, EmSuiteSamplerEstimator,
    expected_episode_time_scalar, expected_episode_time_ms,
)
from spinlab.models import AttemptRecord


def test_default_alpha_pair_indices_map_to_expected_rates():
    assert ALPHA_GRID[DEFAULT_FAST_IDX] == 0.2
    assert ALPHA_GRID[DEFAULT_SLOW_IDX] == 0.05


def test_scalar_matches_closed_form_no_slope_at_default_fast():
    st = _seed_balanced(SamplerState())
    expected = expected_episode_time_ms(
        st, DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX, apply_slope=False,
    )
    assert expected_episode_time_scalar(st) == expected
    assert expected is not None and expected > 0


def test_model_output_total_carries_the_scalar():
    st = _seed_balanced(SamplerState())
    est = EmSuiteSamplerEstimator()
    out = est.model_output(st, [])
    scalar = expected_episode_time_scalar(st)
    assert out.total.expected_ms == scalar
    assert out.total.ms_per_attempt == scalar  # greedy reads this
    assert out.clean.expected_ms is None       # clean unmodeled in Plan 1


def test_model_output_is_none_scalar_below_gate():
    st = SamplerState()  # no data
    out = EmSuiteSamplerEstimator().model_output(st, [])
    assert out.total.expected_ms is None
    assert out.total.ms_per_attempt is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_em_suite_sampler.py -k "scalar or default_alpha or model_output" -v`
Expected: FAIL — `DEFAULT_FAST_IDX` / `expected_episode_time_scalar` not defined; `model_output` still returns all-None.

- [ ] **Step 3: Add the default-α constants and the scalar function**

After the `ALPHA_GRID` definition:

```python
# Default decay pair for the single headline scalar (the segment table's
# "Episode Time" and the greedy allocator's ranking number). fast=0.2 is
# ~5-attempt current skill; slow=0.05 is a ~20-attempt baseline. A named,
# tunable modeling default — NOT a magic pair. The scalar uses the no-slope
# sample(0) value, so only the fast rate affects it; the pair is kept whole so
# the same default feeds slope-aware diagnostics unchanged.
DEFAULT_ALPHA_FAST = 0.2
DEFAULT_ALPHA_SLOW = 0.05
DEFAULT_FAST_IDX = ALPHA_GRID.index(DEFAULT_ALPHA_FAST)
DEFAULT_SLOW_IDX = ALPHA_GRID.index(DEFAULT_ALPHA_SLOW)
```

After `expected_episode_time_ms`:

```python
def expected_episode_time_scalar(state: SamplerState) -> float | None:
    """The single headline expected-episode-time (ms) for a segment, or None
    below the prediction gate. Closed-form mean, no trend slide (sample(0)),
    at the default fast rate — the variance-free number for the table and the
    greedy allocator."""
    return expected_episode_time_ms(
        state, DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX, apply_slope=False,
    )
```

- [ ] **Step 4: Populate `model_output`**

Replace `EmSuiteSamplerEstimator.model_output`'s body (currently returns all-None) with:

```python
    def model_output(  # type: ignore[override]
        self, state: SamplerState, all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list[EventAttempt] | None = None,
    ) -> ModelOutput:
        scalar = expected_episode_time_scalar(state)
        # total carries the headline scalar in BOTH expected_ms (table) and
        # ms_per_attempt (greedy allocator). clean stays unmodeled in Plan 1 —
        # the "Success Attempt" distribution lands with the UI work (Spec #2).
        total = Estimate(
            expected_ms=scalar, ms_per_attempt=scalar, floor_ms=None,
        )
        clean = Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None)
        return ModelOutput(total=total, clean=clean, extras=None)
```

Update the class docstring: replace the "v0: this estimator does NOT populate ModelOutput.total/clean" paragraph with a note that `total` now carries the closed-form scalar (`expected_ms`/`ms_per_attempt`); `clean`/`extras` remain unset pending Spec #2.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_em_suite_sampler.py -k "scalar or default_alpha or model_output" -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/estimators/em_suite_sampler.py tests/unit/test_em_suite_sampler.py
git commit -m "feat(em-suite): model_output emits the closed-form episode-time scalar"
```

---

## Task 6: Plan 1 verification & wrap

**Files:** none (verification only).

- [ ] **Step 1: Type check the changed module**

Run: `npx pyright python/spinlab/estimators/em_suite_sampler.py`
Expected: no *new* errors versus baseline (pre-existing errors are tracked; do not add new ones).

- [ ] **Step 2: Lint**

Run: `ruff check python/spinlab/estimators/em_suite_sampler.py`
Expected: clean. The `import random` lives under `if TYPE_CHECKING:` (Task 3) since the rng is always injected and the module never calls `random.*` at runtime, so ruff sees no unused runtime import.

- [ ] **Step 3: Run the FULL suite (project policy — not a subset)**

Run: `python -m pytest`
Expected: all pass (unit + emulator + frontend). Skips count as failures per project policy — if emulator tests skip with a launch failure, surface it, do not treat as green.

- [ ] **Step 4: Confirm nothing downstream changed behavior unexpectedly**

The multi-model system is intact; em_suite simply now emits a scalar and can sample. Sanity-check that `em_suite_sampler` is NOT the default active estimator (scheduler default is still `kalman`), so production behavior is unchanged unless explicitly switched. Greedy will now function if/when em_suite is selected.

- [ ] **Step 5: Final commit marker (if any docs need updating)**

If `docs/ARCHITECTURE.md` references the sampler as "means only," update that sentence. Otherwise no commit needed.

```bash
git add -A && git commit -m "docs: note em-suite is now a real sampler (Plan 1 complete)" || echo "nothing to commit"
```

---

## Self-Review (completed by plan author)

- **Spec coverage (Plan 1 scope):** α=0 uniform-mean fix (Task 1) ✓; two draw pools (Task 2) ✓; recency draw (Task 3) ✓; `sample_episode` bootstrap-with-slide + cap (Task 4) ✓; default-α scalar + `model_output` (Task 5) ✓. Deferred to **Plan 2** (correctly out of scope here): deleting the 5 models, scheduler/allocator/route collapse, replay-seeding fix, cold-distribution decouple, frontend selector removal, derived-data reset.
- **Placeholders:** none — every code step shows complete code.
- **Type/name consistency:** `SamplerState.success_time_pool`/`death_time_pool`, `_append_capped`, `draw_from_pool`, `sample_episode`, `POOL_SIZE`, `MAX_ATTEMPTS_PER_EPISODE`, `DEFAULT_FAST_IDX`/`DEFAULT_SLOW_IDX`, `expected_episode_time_scalar` are used consistently across tasks. `fast_idx=6`/`slow_idx=4` in Task 4 tests match `ALPHA_GRID.index(0.2)=6` / `index(0.05)=4` asserted in Task 5.
- **Risk:** additive only; default active estimator unchanged; full suite gates each commit.

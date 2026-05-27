# Segment Hazard Plot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Kaplan-Meier-style hazard view to the segment-detail page, sharing a [Histogram] [Hazard] toggle panel with the cold-filtered death histogram.

**Architecture:** Two phases on two branches. **Phase 0** rebases the unmerged `feat/segment-death-histogram` onto current `main`, replaces its DAR-coupled data source with a new `cold_distribution.py` backend module (cold-filtered, adaptive sqrt-rule binning), renames the panel to "Cold distribution", and merges. **Phase 1** stacks `feat/segment-hazard-plot` on top of the merged work, extends `cold_distribution.py` with hazard math, adds a Chart.js hazard renderer, and wires the toggle.

**Tech Stack:** Python 3.11+, FastAPI, SQLite (existing), TypeScript + Vite + Chart.js (existing), Vitest + happy-dom for frontend tests, pytest for backend.

**Spec:** `docs/superpowers/specs/2026-05-27-segment-hazard-plot-design.md`.

**Naming deviation from spec:** the spec used `HazardCurve` / `HazardBin`; this plan uses `ColdDistribution` / `ColdBin` because Phase 0 needs the same backend payload for the *histogram* (no hazard math yet), and naming a histogram bin a "HazardBin" would be misleading. Hazard fields are added to the same schema in Phase 1.

---

## Phase 0 — Histogram branch: rebase, cold-filter, adaptive bins, panel rename

All Phase 0 work happens on `feat/segment-death-histogram`. End state: that branch is merged to `main` with the histogram showing cold-only data in adaptive bins under the "Cold distribution" header.

### Task 0.0: Rebase histogram branch onto current main

**Why this is its own task:** the branch was cut from `27297f0` (2026-05-25), but branches 1 (`is_hot`) and 2 (`bootstrap_resample`) have landed on `main` since then. Several files conflict — `python/spinlab/api_schemas.py`, `python/spinlab/routes/model.py`, `python/spinlab/estimators/death_aware_rolling.py`, `python/spinlab/estimators/__init__.py`. Resolve conflicts before any new work.

**Files:** None created/modified — pure git operation. Conflict resolution touches whatever files conflict.

- [ ] **Step 1: Verify clean tree and baseline tests**

```bash
git status                          # must be clean (or stash)
git checkout main
git pull
python -m pytest                    # baseline; must be green
cd frontend && npm run build && npm test && cd ..
```
Expected: full suite green on main. If red, STOP and surface to user per CLAUDE.md "red baseline" rule.

- [ ] **Step 2: Rebase**

```bash
git checkout feat/segment-death-histogram
git rebase main
```
Expected: conflicts in at least `api_schemas.py`, `routes/model.py`, `_episode_helpers.py` (deleted on histogram branch but exists on main), `estimators/__init__.py`. Resolve by keeping main's version (which has the merged branches 1/2) and re-applying the histogram-branch-only additions (`final_extras` field, `selected_model` field, `_events_from_rows` helper, event loading in the route).

- [ ] **Step 3: Verify post-rebase tests**

```bash
python -m pytest
cd frontend && npm run build && npm test && cd ..
```
Expected: green. If red, the rebase resolution dropped or broke something — fix before proceeding.

- [ ] **Step 4: Commit (rebase produces no new commit by itself; this just records the resolved state)**

The rebase rewrites history. No additional commit needed unless conflict resolution required edits beyond simple "take main"/"take ours" — in which case the conflict-resolution commit is the rebase's natural product.

---

### Task 0.1: Skeleton `cold_distribution.py` with file-level constants

**Files:**
- Create: `python/spinlab/cold_distribution.py`
- Create: `tests/unit/test_cold_distribution.py`

- [ ] **Step 1: Write the failing test for module existence + constants**

```python
# tests/unit/test_cold_distribution.py
from spinlab.cold_distribution import (
    MAX_BINS, MIN_BINS, EFFECTIVE_WINDOW_HALFLIVES, HI_ROUND_MS,
)


def test_bin_count_constants_are_sane():
    assert MIN_BINS == 5
    assert MAX_BINS == 20
    assert MAX_BINS > MIN_BINS
    assert EFFECTIVE_WINDOW_HALFLIVES == 5
    assert HI_ROUND_MS == 1000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_cold_distribution.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'spinlab.cold_distribution'`.

- [ ] **Step 3: Create the module with constants**

```python
# python/spinlab/cold_distribution.py
"""Per-segment cold-attempt distribution computation.

Backs the "Cold distribution" panel on the segment-detail page: both
the histogram view (raw deaths/completions per bin) and the hazard
view (deaths_w / at_risk_w per bin, added in Phase 1).

Operates on a flat list of cold EventAttempts — episode-level
aggregation is irrelevant here (every attempt is its own risk
timeline). Caller is responsible for the cold filter (is_hot=False);
this module trusts its input.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spinlab.models import EventAttempt

# Maximum bin count. 20 matches screen-width comfort at typical viewport
# widths; above this, bars are too thin to read.
MAX_BINS = 20

# Minimum bin count. Below 5 the chart degenerates into a quantile
# summary and loses its shape-as-distribution affordance.
MIN_BINS = 5

# Truncation horizon in halflives. At 5*halflife back, an attempt's
# weight is 2^-5 ≈ 3%, below the noise floor of the binning. Matches
# EFFECTIVE_WINDOW_HALFLIVES in spinlab.estimators.death_aware_rolling.
EFFECTIVE_WINDOW_HALFLIVES = 5

# X-axis upper-edge rounding. One-second rounding gives clean axis
# labels without manual tick configuration.
HI_ROUND_MS = 1000
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_cold_distribution.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/cold_distribution.py tests/unit/test_cold_distribution.py
git commit -m "feat(cold-dist): scaffold cold_distribution module with file-level constants"
```

---

### Task 0.2: `_compute_attempt_weights` and bin-count rule

**Files:**
- Modify: `python/spinlab/cold_distribution.py`
- Modify: `tests/unit/test_cold_distribution.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_cold_distribution.py`:

```python
import math
from spinlab.cold_distribution import (
    _compute_attempt_weights, _bin_count_for,
)


def test_attempt_weights_most_recent_is_one():
    # Three attempts in chronological order. Last should weigh 1.0;
    # one halflife back should weigh 0.5.
    weights = _compute_attempt_weights(n=3, halflife=2)
    assert weights[-1] == 1.0
    assert weights[-3] == 0.25   # 2 halflives back ⇒ 2^-2 = 0.25
    assert weights[-2] == 0.5    # 1 halflife back ⇒ 2^-1


def test_attempt_weights_empty():
    assert _compute_attempt_weights(n=0, halflife=20) == []


def test_bin_count_clamps_low():
    assert _bin_count_for(n=0) == 5
    assert _bin_count_for(n=4) == 5
    assert _bin_count_for(n=25) == 5      # sqrt(25)=5
    assert _bin_count_for(n=26) == 6      # sqrt(26)≈5.099 → ceil 6


def test_bin_count_clamps_high():
    assert _bin_count_for(n=400) == 20    # sqrt(400)=20
    assert _bin_count_for(n=401) == 20    # capped
    assert _bin_count_for(n=10_000) == 20
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_cold_distribution.py -v`
Expected: FAIL — `_compute_attempt_weights` and `_bin_count_for` undefined.

- [ ] **Step 3: Implement the helpers**

Append to `python/spinlab/cold_distribution.py`:

```python
import math


def _compute_attempt_weights(n: int, halflife: int) -> list[float]:
    """Per-attempt exponential decay weights, chronological order.

    weights[i] = 2 ** (-(n - 1 - i) / halflife)

    The most-recent attempt (index n-1) has weight 1.0. An attempt one
    halflife back has weight 0.5. Mirrors _episode_helpers._compute_weights
    but operates at the attempt level (not episode level) since cold-
    filtered analysis treats each attempt as its own risk timeline.
    """
    return [2.0 ** (-(n - 1 - i) / halflife) for i in range(n)]


def _bin_count_for(n: int) -> int:
    """Adaptive bin count via the square-root rule, clamped to [MIN_BINS, MAX_BINS]."""
    if n <= 0:
        return MIN_BINS
    return min(MAX_BINS, max(MIN_BINS, math.ceil(math.sqrt(n))))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_cold_distribution.py -v`
Expected: PASS (all 5 tests).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/cold_distribution.py tests/unit/test_cold_distribution.py
git commit -m "feat(cold-dist): per-attempt decay weights + adaptive sqrt bin count"
```

---

### Task 0.3: `ColdBin` and `ColdDistribution` schemas

**Files:**
- Modify: `python/spinlab/api_schemas.py` (add new classes alongside existing)
- Modify: `tests/unit/test_cold_distribution.py`

Hazard fields are NOT added here — Phase 1 extends the schema. Phase 0 ships histogram-only fields.

- [ ] **Step 1: Write the failing import-shape test**

Append to `tests/unit/test_cold_distribution.py`:

```python
def test_schema_imports():
    from spinlab.api_schemas import ColdBin, ColdDistribution
    bin_ = ColdBin(lo_ms=0.0, hi_ms=500.0, n_deaths=2, n_completions=1)
    dist = ColdDistribution(
        bins=[bin_], n_cold_attempts=3,
        mu_d_ms=200.0, mu_c_ms=400.0,
        p_die_per_attempt=0.5, p_die_per_life=0.5,
    )
    assert dist.bins[0].n_deaths == 2
    assert dist.n_cold_attempts == 3
```

- [ ] **Step 2: Run test to verify failure**

Run: `python -m pytest tests/unit/test_cold_distribution.py::test_schema_imports -v`
Expected: FAIL — `ImportError: cannot import name 'ColdBin'`.

- [ ] **Step 3: Add the schemas**

In `python/spinlab/api_schemas.py`, immediately before the existing `class SegmentHistory(_BaseResponse):` line, add:

```python
class ColdBin(_BaseResponse):
    """One time bin of the cold-attempt distribution.

    Hazard fields (hazard, at_risk_w) are added in Phase 1.
    """
    lo_ms: float
    hi_ms: float
    n_deaths: int        # raw count of cold deaths landing in this bin
    n_completions: int   # raw count of cold completions landing in this bin


class ColdDistribution(_BaseResponse):
    """Per-segment cold-attempt distribution payload.

    Feeds the "Cold distribution" panel on the segment-detail page.
    Histogram view reads bin counts; hazard view (Phase 1) reads
    hazard/at_risk_w (also added in Phase 1) from the same bins.

    Aggregates (mu_*, p_die_*) are cold-only — derived from the same
    cold pool the bins were computed from, NOT from DAR's all-events
    aggregates.
    """
    bins: list[ColdBin]
    n_cold_attempts: int                  # raw cold count after truncation; drives bin layout
    mu_d_ms: float | None                 # weighted mean cold-death time; None when no deaths
    mu_c_ms: float | None                 # weighted mean cold-completion time; None when no completions
    p_die_per_attempt: float | None       # weighted P(any death in attempt); None when n_cold=0
    p_die_per_life: float | None          # weighted P(this life dies); None when no events
```

- [ ] **Step 4: Run test to verify pass**

Run: `python -m pytest tests/unit/test_cold_distribution.py::test_schema_imports -v`
Expected: PASS.

- [ ] **Step 5: Regenerate frontend types and commit**

```bash
cd frontend && npm run gen-types && cd ..
git add python/spinlab/api_schemas.py tests/unit/test_cold_distribution.py frontend/openapi.json frontend/src/api-types.ts
git commit -m "feat(api): ColdBin + ColdDistribution schemas (no hazard fields yet)"
```

---

### Task 0.4: `compute_cold_distribution` — bin counts, aggregates, edge cases

**Files:**
- Modify: `python/spinlab/cold_distribution.py`
- Modify: `tests/unit/test_cold_distribution.py`
- Modify: `tests/factories.py` if needed (to make EventAttempt factory ergonomic)

- [ ] **Step 1: Confirm `make_event_attempt` factory exists**

Run: `grep -n "make_event_attempt" tests/factories.py`
Expected: a factory that accepts `time_ms`, `outcome`, `is_hot`, `episode_id`, etc. Confirmed present per branch 1 commit `bd1c24a` ("add is_hot kwarg to make_event_attempt factory") on main.

- [ ] **Step 2: Write the failing tests**

Append to `tests/unit/test_cold_distribution.py`:

```python
from spinlab.cold_distribution import compute_cold_distribution
from spinlab.models import AttemptOutcome
from tests.factories import make_event_attempt


def _ev(time_ms: int, outcome: AttemptOutcome, ep: str = "e1"):
    return make_event_attempt(time_ms=time_ms, outcome=outcome, episode_id=ep)


def test_compute_empty_inputs_disallowed():
    # Caller (route) substitutes None on empty input; the function itself
    # should never be called with an empty list. Test that this is the
    # documented contract.
    import pytest
    with pytest.raises((AssertionError, IndexError, ValueError)):
        compute_cold_distribution([], halflife=20)


def test_compute_single_death_at_2s():
    # One cold attempt that died at 2000ms.
    events = [_ev(2000, AttemptOutcome.DIED)]
    dist = compute_cold_distribution(events, halflife=20)
    # n_cold_attempts pre-truncation (only 1 here, no truncation)
    assert dist.n_cold_attempts == 1
    # bin count = max(5, ceil(sqrt(1))) = 5
    assert len(dist.bins) == 5
    # X-axis range: 0 to ceil(2000/1000)*1000 = 2000
    assert dist.bins[0].lo_ms == 0
    assert dist.bins[-1].hi_ms == 2000
    # Single death lands in topmost bin (time 2000 == hi → clamped)
    total_deaths = sum(b.n_deaths for b in dist.bins)
    assert total_deaths == 1
    assert dist.mu_d_ms == 2000.0
    assert dist.mu_c_ms is None  # no completions


def test_compute_two_attempts_mixed_outcomes():
    # One died at 2000, one survived at 8000.
    events = [
        _ev(2000, AttemptOutcome.DIED, ep="e1"),
        _ev(8000, AttemptOutcome.SURVIVED, ep="e2"),
    ]
    dist = compute_cold_distribution(events, halflife=20)
    assert dist.n_cold_attempts == 2
    assert len(dist.bins) == 5  # sqrt(2) → ceil 2 → clamped to 5
    # hi rounds up to ceil(8000/1000)*1000 = 8000
    assert dist.bins[-1].hi_ms == 8000
    total_deaths = sum(b.n_deaths for b in dist.bins)
    total_completions = sum(b.n_completions for b in dist.bins)
    assert total_deaths == 1
    assert total_completions == 1
    # Weighted means: only one death, only one completion → equal to the
    # raw times regardless of weight.
    assert dist.mu_d_ms == 2000.0
    assert dist.mu_c_ms == 8000.0


def test_compute_truncates_to_5x_halflife():
    # 200 cold attempts; halflife=20 → window = 100 attempts.
    events = [
        _ev(time_ms=100 + i, outcome=AttemptOutcome.SURVIVED, ep=f"e{i}")
        for i in range(200)
    ]
    dist = compute_cold_distribution(events, halflife=20)
    # n_cold_attempts reflects POST-truncation count
    assert dist.n_cold_attempts == 100
    assert len(dist.bins) == min(20, max(5, 10))  # sqrt(100)=10


def test_compute_p_die_aggregates():
    # Two episodes:
    #   ep1: died at 2000  →  1 death
    #   ep2: died at 1500, then survived at 5000  →  episode "attempted" had a death
    events = [
        _ev(2000, AttemptOutcome.DIED, ep="e1"),
        _ev(1500, AttemptOutcome.DIED, ep="e2"),
        _ev(5000, AttemptOutcome.SURVIVED, ep="e2"),
    ]
    dist = compute_cold_distribution(events, halflife=20)
    # Life-level: 2 deaths / 3 lives = 0.667
    assert dist.p_die_per_life is not None
    assert abs(dist.p_die_per_life - 2.0/3.0) < 0.05  # weighted, so approximate
    # Attempt-level (per episode): 2/2 episodes had a death = 1.0
    assert dist.p_die_per_attempt is not None
    assert dist.p_die_per_attempt > 0.9  # both episodes had a death
```

- [ ] **Step 3: Run tests to verify failure**

Run: `python -m pytest tests/unit/test_cold_distribution.py -v`
Expected: 4-5 FAILs (function undefined).

- [ ] **Step 4: Implement `compute_cold_distribution`**

Append to `python/spinlab/cold_distribution.py`:

```python
from spinlab.api_schemas import ColdBin, ColdDistribution
from spinlab.models import AttemptOutcome, EventAttempt


def compute_cold_distribution(
    cold_events: list[EventAttempt], halflife: int,
) -> ColdDistribution:
    """Bin + summarize cold attempts for the segment-detail panel.

    Caller filters to is_hot=False BEFORE calling this. The function
    does not re-filter; passing any hot event skews the result. Empty
    input is disallowed (caller substitutes None at the route layer).
    """
    if not cold_events:
        raise ValueError("compute_cold_distribution requires non-empty cold_events")

    # 1. Truncate to last 5*halflife events. Recency-decay weights past
    #    this horizon are < 3% — negligible vs. binning noise.
    horizon = EFFECTIVE_WINDOW_HALFLIVES * halflife
    truncated = cold_events[-horizon:] if len(cold_events) > horizon else list(cold_events)
    n = len(truncated)

    # 2. Per-attempt decay weights.
    weights = _compute_attempt_weights(n=n, halflife=halflife)

    # 3. Bin count from sqrt rule.
    bin_count = _bin_count_for(n=n)

    # 4. X-axis range: lo=0; hi = ceil(max_time/1000)*1000 rounded up.
    max_ms = max(ev.time_ms for ev in truncated)
    hi = max(HI_ROUND_MS, ((max_ms + HI_ROUND_MS - 1) // HI_ROUND_MS) * HI_ROUND_MS)
    lo = 0
    bin_width = (hi - lo) / bin_count

    # 5. Initialize bins.
    bins: list[ColdBin] = [
        ColdBin(
            lo_ms=lo + i * bin_width,
            hi_ms=lo + (i + 1) * bin_width,
            n_deaths=0,
            n_completions=0,
        )
        for i in range(bin_count)
    ]

    # 6. Walk events, fill bin counts and weighted aggregates.
    def bin_idx(t: int) -> int:
        if bin_width == 0:
            return 0
        idx = int((t - lo) // bin_width)
        if idx >= bin_count:
            idx = bin_count - 1
        if idx < 0:
            idx = 0
        return idx

    sum_w_d = 0.0   # weighted death count
    sum_w_c = 0.0   # weighted completion count
    sum_wt_d = 0.0  # weighted sum of death times
    sum_wt_c = 0.0  # weighted sum of completion times

    # Episode-level: per-episode "had at least one death" indicator.
    # p_die_per_attempt = weighted fraction of episodes with a death.
    episodes_seen: dict[str, dict] = {}  # episode_id → {"weight": w, "had_death": bool}

    for ev, w in zip(truncated, weights):
        idx = bin_idx(ev.time_ms)
        ep_entry = episodes_seen.setdefault(ev.episode_id, {"weight": w, "had_death": False})
        # Use the LATEST event's weight as the episode's representative weight
        # (chronological order; later overrides earlier).
        ep_entry["weight"] = w
        if ev.outcome == AttemptOutcome.DIED:
            bins[idx].n_deaths += 1
            sum_w_d += w
            sum_wt_d += w * ev.time_ms
            ep_entry["had_death"] = True
        elif ev.outcome == AttemptOutcome.SURVIVED:
            bins[idx].n_completions += 1
            sum_w_c += w
            sum_wt_c += w * ev.time_ms

    # 7. Aggregates.
    mu_d_ms = sum_wt_d / sum_w_d if sum_w_d > 0 else None
    mu_c_ms = sum_wt_c / sum_w_c if sum_w_c > 0 else None
    total_w_life = sum_w_d + sum_w_c
    p_die_per_life = sum_w_d / total_w_life if total_w_life > 0 else None

    total_ep_w = sum(e["weight"] for e in episodes_seen.values())
    had_death_w = sum(e["weight"] for e in episodes_seen.values() if e["had_death"])
    p_die_per_attempt = had_death_w / total_ep_w if total_ep_w > 0 else None

    return ColdDistribution(
        bins=bins, n_cold_attempts=n,
        mu_d_ms=mu_d_ms, mu_c_ms=mu_c_ms,
        p_die_per_attempt=p_die_per_attempt, p_die_per_life=p_die_per_life,
    )
```

- [ ] **Step 5: Run tests to verify pass**

Run: `python -m pytest tests/unit/test_cold_distribution.py -v`
Expected: all 9 tests PASS (4 from prior tasks + 5 new).

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/cold_distribution.py tests/unit/test_cold_distribution.py
git commit -m "feat(cold-dist): compute_cold_distribution — bins, weighted aggregates"
```

---

### Task 0.5: Wire `cold_distribution` into segment_history route

**Files:**
- Modify: `python/spinlab/routes/model.py` (the `segment_history` handler, rebased version with events loading already present)
- Modify: `python/spinlab/api_schemas.py` (add field to `SegmentHistory`)
- Create: `tests/unit/routes/test_segment_history_cold_distribution.py`

- [ ] **Step 1: Add field to schema**

In `python/spinlab/api_schemas.py`, find `class SegmentHistory(_BaseResponse):` and add the new field at the end (before any closing comments):

```python
class SegmentHistory(_BaseResponse):
    # ...existing fields (segment_id ... selected_model)...
    cold_distribution: ColdDistribution | None = None  # NEW
```

- [ ] **Step 2: Regenerate frontend types**

Run: `cd frontend && npm run gen-types && cd ..`
Expected: `frontend/src/api-types.ts` updated.

- [ ] **Step 3: Write the failing route test**

Create `tests/unit/routes/test_segment_history_cold_distribution.py`:

```python
"""Tests for the cold_distribution field on /api/segments/{id}/history."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from spinlab.dashboard import create_app
from spinlab.db import Database
from spinlab.models import AttemptOutcome, Segment
from tests.conftest import make_test_config
from tests.factories import make_event_attempt


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("g1", "Test Game", "any%")
    return d


@pytest.fixture
def client(db):
    app = create_app(db=db, config=make_test_config())
    app.state.session.game_id = "g1"
    app.state.session.game_name = "Test Game"
    return TestClient(app)


def _make_segment(segment_id: str, level: int) -> Segment:
    return Segment(
        id=segment_id, game_id="g1", level_number=level,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
    )


def test_segment_history_returns_cold_distribution(db, client):
    """Segment with cold + hot attempts → cold_distribution computed from cold subset only."""
    seg_id = "seg-test-1"
    db.upsert_segment(_make_segment(seg_id, level=1))
    # 2 cold events (die at 1500, survive at 3000), 1 hot event (die at 9999)
    for ev in [
        make_event_attempt(
            segment_id=seg_id, episode_id="e1",
            outcome=AttemptOutcome.DIED, time_ms=1500, is_hot=False,
        ),
        make_event_attempt(
            segment_id=seg_id, episode_id="e1",
            outcome=AttemptOutcome.SURVIVED, time_ms=3000, is_hot=False,
        ),
        make_event_attempt(
            segment_id=seg_id, episode_id="e2",
            outcome=AttemptOutcome.DIED, time_ms=9999, is_hot=True,
        ),
    ]:
        db.log_event_attempt(ev)

    resp = client.get(f"/api/segments/{seg_id}/history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cold_distribution"] is not None
    cd = data["cold_distribution"]
    # 2 cold attempts, NOT 3 (hot dropped)
    assert cd["n_cold_attempts"] == 2
    total_deaths = sum(b["n_deaths"] for b in cd["bins"])
    total_completions = sum(b["n_completions"] for b in cd["bins"])
    assert total_deaths == 1
    assert total_completions == 1
    # hi from cold max (3000), NOT from hot max (9999)
    assert cd["bins"][-1]["hi_ms"] == 3000


def test_segment_history_returns_null_when_all_hot(db, client):
    seg_id = "seg-test-2"
    db.upsert_segment(_make_segment(seg_id, level=2))
    db.log_event_attempt(make_event_attempt(
        segment_id=seg_id, episode_id="e1",
        outcome=AttemptOutcome.DIED, time_ms=1000, is_hot=True,
    ))
    resp = client.get(f"/api/segments/{seg_id}/history")
    assert resp.status_code == 200
    assert resp.json()["cold_distribution"] is None


def test_segment_history_returns_null_when_no_events(db, client):
    seg_id = "seg-test-3"
    db.upsert_segment(_make_segment(seg_id, level=3))
    resp = client.get(f"/api/segments/{seg_id}/history")
    assert resp.status_code == 200
    assert resp.json()["cold_distribution"] is None
```

Note: the `Segment` constructor and exact `make_event_attempt` parameter names should be verified by reading `python/spinlab/models.py` and `tests/factories.py` before running. The route-test fixture pattern matches `tests/unit/routes/test_dashboard_references.py`.

- [ ] **Step 4: Run tests to verify they fail**

Run: `python -m pytest tests/unit/routes/test_segment_history_cold_distribution.py -v`
Expected: FAIL — `cold_distribution` key missing from response.

- [ ] **Step 5: Wire into the route handler**

In `python/spinlab/routes/model.py`, inside `segment_history`, after the existing `event_rows = db.get_segment_event_rows(segment_id)` / `events = _events_from_rows(event_rows)` lines (added by the rebased histogram branch), add:

```python
# Cold-only distribution for the segment-detail panel (histogram + hazard).
from spinlab.cold_distribution import compute_cold_distribution
from spinlab.estimators.death_aware_rolling import DEFAULT_HALFLIFE

cold_events = [ev for ev in events if not ev.is_hot]

if cold_events:
    # Use the active death_aware_rolling halflife so the cold panel
    # tracks the user's tuned smoothing knob (shared with DAR + bootstrap).
    dar_params_raw = db.load_allocator_config("estimator_params:death_aware_rolling")
    dar_params = json.loads(dar_params_raw) if dar_params_raw else {}
    halflife = int(dar_params.get("halflife", DEFAULT_HALFLIFE))
    cold_distribution = compute_cold_distribution(cold_events, halflife=halflife)
else:
    cold_distribution = None
```

And add to the return dict:

```python
return {
    # ...existing keys...
    "cold_distribution": cold_distribution,
}
```

Note: `json` is already imported in this file (used for estimator params). If not, add `import json` at the top.

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/unit/routes/test_segment_history_cold_distribution.py -v`
Expected: all 3 PASS.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/routes/model.py python/spinlab/api_schemas.py frontend/openapi.json frontend/src/api-types.ts tests/unit/routes/test_segment_history_cold_distribution.py
git commit -m "feat(api): cold_distribution field on segment_history, wired from cold events"
```

---

### Task 0.6: Replace histogram's data source — frontend

**Files:**
- Modify: `frontend/src/death-distribution.ts` (refactor to read from `cold_distribution`, drop dead `final_extras`-based path)
- Modify: `frontend/src/segment-detail.ts` (pass `cold_distribution` to the renderer)
- Modify: `frontend/src/death-distribution.test.ts` (update tests for new API shape)

- [ ] **Step 1: Read the current state**

Run: `git show HEAD:frontend/src/death-distribution.ts` and `git show HEAD:frontend/src/segment-detail.ts`. Identify the call site that builds the histogram (likely a `renderDeathDistribution(container, final_extras)` or similar function). The replacement passes `cold_distribution` instead.

- [ ] **Step 2: Write the failing test**

Update `frontend/src/death-distribution.test.ts` — replace the `binSamples` block with one that takes the new schema:

```typescript
import { describe, it, expect, vi } from "vitest";
import { renderColdHistogram } from "./death-distribution";
import type { ColdDistribution } from "./types";

vi.mock("chart.js", () => ({
  Chart: class {
    data: unknown;
    options: unknown;
    static register() {}
    constructor(_ctx: unknown, config: { data: unknown; options: unknown }) {
      this.data = config.data;
      this.options = config.options;
    }
    destroy() {}
    update() {}
  },
  BarController: class {}, BarElement: class {},
  LinearScale: class {}, CategoryScale: class {},
  Legend: class {}, Tooltip: class {},
}));

describe("renderColdHistogram", () => {
  it("builds two datasets (deaths, completions) with one bar per ColdBin", () => {
    const dist: ColdDistribution = {
      bins: [
        { lo_ms: 0, hi_ms: 500, n_deaths: 2, n_completions: 0 },
        { lo_ms: 500, hi_ms: 1000, n_deaths: 1, n_completions: 1 },
        { lo_ms: 1000, hi_ms: 1500, n_deaths: 0, n_completions: 2 },
        { lo_ms: 1500, hi_ms: 2000, n_deaths: 0, n_completions: 1 },
        { lo_ms: 2000, hi_ms: 2500, n_deaths: 0, n_completions: 0 },
      ],
      n_cold_attempts: 7,
      mu_d_ms: 333,
      mu_c_ms: 1500,
      p_die_per_attempt: 0.5,
      p_die_per_life: 0.43,
    };
    const canvas = document.createElement("canvas");
    const chart = renderColdHistogram(canvas, dist);
    const data = (chart as any).data;
    expect(data.datasets).toHaveLength(2);
    expect(data.datasets[0].label).toMatch(/deaths/i);
    expect(data.datasets[1].label).toMatch(/completions/i);
    expect(data.datasets[0].data).toEqual([2, 1, 0, 0, 0]);
    expect(data.datasets[1].data).toEqual([0, 1, 2, 1, 0]);
  });
});
```

- [ ] **Step 3: Run test to verify failure**

Run: `cd frontend && npm test -- death-distribution`
Expected: FAIL — `renderColdHistogram` not exported.

- [ ] **Step 4: Rewrite `death-distribution.ts`**

Replace the entire contents of `frontend/src/death-distribution.ts` with the new shape. Drop the old `binSamples`, `BIN_COUNT`, `DeathExtras` import path; export `renderColdHistogram(canvas, ColdDistribution): Chart`. Preserve the inline marker plugin (μ_d, μ_c lines) but read those values from `dist.mu_d_ms` / `dist.mu_c_ms` instead of from DeathExtras.

```typescript
// frontend/src/death-distribution.ts
import {
  Chart,
  BarController,
  BarElement,
  LinearScale,
  CategoryScale,
  Legend,
  Tooltip,
} from "chart.js";
import { formatTime } from "./format";
import type { ColdDistribution } from "./types";

Chart.register(BarController, BarElement, LinearScale, CategoryScale, Legend, Tooltip);

// Conventional failure/success colors at moderate opacity so overlapping
// regions blend visibly.
const DEATH_COLOR = "rgba(255, 100, 100, 0.55)";
const DEATH_LINE = "rgba(255, 100, 100, 0.95)";
const COMPLETION_COLOR = "rgba(100, 200, 100, 0.55)";
const COMPLETION_LINE = "rgba(100, 200, 100, 0.95)";

// Pixel offset for the marker label so it doesn't sit on top of the line.
const MARKER_LABEL_X_OFFSET_PX = 3;

export function renderColdHistogram(
  canvas: HTMLCanvasElement, dist: ColdDistribution,
): Chart {
  const labels = dist.bins.map((b) => formatTime(b.lo_ms));
  const deaths = dist.bins.map((b) => b.n_deaths);
  const completions = dist.bins.map((b) => b.n_completions);

  return new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          label: "Deaths",
          data: deaths,
          backgroundColor: DEATH_COLOR,
          borderColor: DEATH_LINE,
          borderWidth: 1,
        },
        {
          label: "Completions",
          data: completions,
          backgroundColor: COMPLETION_COLOR,
          borderColor: COMPLETION_LINE,
          borderWidth: 1,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        y: { beginAtZero: true, title: { display: true, text: "Samples" } },
        x: { title: { display: true, text: "Time" } },
      },
      plugins: {
        legend: { position: "top" },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y}`,
          },
        },
      },
    },
  });
}
```

- [ ] **Step 5: Update `segment-detail.ts` call site**

Find where the histogram panel is rendered (added by the histogram branch). Replace the `final_extras`-based call with:

```typescript
import { renderColdHistogram } from "./death-distribution";

// ...inside the detail-rendering flow, after history fetch:
const dist = history.cold_distribution;
if (dist) {
  _histogramChart = renderColdHistogram(histogramCanvas, dist);
  // Update header stats from dist.p_die_per_attempt, dist.p_die_per_life,
  // dist.mu_d_ms, dist.mu_c_ms (existing rendering code; just point at
  // the new source).
} else {
  // Existing empty-state path (panel showing "No cold data for this segment").
}
```

Remove any remaining references to `final_extras` for histogram rendering. (Estimator-coupled `final_extras` may still be used by other UI; only the histogram path is replaced.)

- [ ] **Step 6: Run frontend tests**

```bash
cd frontend
npm run typecheck
npm test
cd ..
```
Expected: PASS. Fix any test that still references `binSamples` / `BIN_COUNT` / `DeathExtras`-based shape.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/death-distribution.ts frontend/src/death-distribution.test.ts frontend/src/segment-detail.ts
git commit -m "refactor(frontend): histogram reads cold_distribution; drop DeathExtras path"
```

---

### Task 0.7: Panel title rename ("Death distribution" → "Cold distribution")

**Files:**
- Modify: `frontend/src/segment-detail.ts` (or wherever the panel header text lives)
- Modify: any test that asserts the old title string

- [ ] **Step 1: Find every occurrence**

Run: `grep -rn "Death distribution" frontend/src/ tests/`
Expected: 1-3 hits in `segment-detail.ts` / `segment-detail.test.ts` / `death-distribution.test.ts`.

- [ ] **Step 2: Replace all to "Cold distribution"**

Use Edit tool with `replace_all: false` per occurrence (the strings should each be unique in their file).

- [ ] **Step 3: Run frontend tests**

```bash
cd frontend && npm test && cd ..
```
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/segment-detail.ts frontend/src/segment-detail.test.ts frontend/src/death-distribution.test.ts
git commit -m "chore(ui): rename panel 'Death distribution' → 'Cold distribution'"
```

---

### Task 0.8: Full-suite verification + merge

- [ ] **Step 1: Frontend build + tests**

```bash
cd frontend
npm run build
npm test
cd ..
```
Expected: green.

- [ ] **Step 2: Full pytest (NOT `-m "not emulator"`)**

```bash
python -m pytest
```
Expected: green per CLAUDE.md "full suite, all-must-pass" rule. If anything is red — including emulator tests skipping for any reason — STOP and surface to user.

- [ ] **Step 3: Type + lint**

```bash
npx pyright python/spinlab/cold_distribution.py python/spinlab/routes/model.py python/spinlab/api_schemas.py
ruff check python/spinlab/cold_distribution.py python/spinlab/routes/model.py
```
Expected: no NEW errors. Pre-existing errors per CLAUDE.md are allowed.

- [ ] **Step 4: Merge to main**

```bash
git checkout main
git merge --no-ff feat/segment-death-histogram -m "Merge feat/segment-death-histogram: cold-filtered histogram panel"
git log --oneline -5
```

- [ ] **Step 5: Update the histogram-branch spec to record the deviation**

Edit `docs/superpowers/specs/2026-05-25-segment-death-histogram-design.md` — add a short note at the bottom:

```markdown
## Post-merge deviation (2026-05-27)

Folded into the segment-hazard-plot work (see
`docs/superpowers/specs/2026-05-27-segment-hazard-plot-design.md`):

- Data source switched from `EstimatorCurves.final_extras` to a new
  `SegmentHistory.cold_distribution` field, computed cold-only in
  `python/spinlab/cold_distribution.py`.
- Fixed 20-bin layout replaced with adaptive sqrt-rule binning.
- Panel title renamed "Death distribution" → "Cold distribution".

`final_extras` remains on the response but is no longer read by the
histogram view.
```

Commit:

```bash
git add docs/superpowers/specs/2026-05-25-segment-death-histogram-design.md
git commit -m "docs(specs): record histogram-spec deviations folded into hazard work"
```

---

## Phase 1 — Hazard plot

Phase 1 work happens on a fresh branch `feat/segment-hazard-plot`, cut from `main` after Phase 0 merges.

### Task 1.0: Create the branch

- [ ] **Step 1: Cut the branch**

```bash
git checkout main
git pull   # ensure Phase 0 merge is visible
git checkout -b feat/segment-hazard-plot
```

---

### Task 1.1: Extend `ColdBin` and `ColdDistribution` schemas with hazard fields

**Files:**
- Modify: `python/spinlab/api_schemas.py`
- Modify: `tests/unit/test_cold_distribution.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_cold_distribution.py`:

```python
def test_hazard_fields_on_schema():
    from spinlab.api_schemas import ColdBin, ColdDistribution
    bin_ = ColdBin(
        lo_ms=0.0, hi_ms=500.0, n_deaths=1, n_completions=0,
        hazard=0.5, at_risk_w=2.0,
    )
    dist = ColdDistribution(
        bins=[bin_], n_cold_attempts=2,
        mu_d_ms=200.0, mu_c_ms=None,
        p_die_per_attempt=0.5, p_die_per_life=0.5,
        halflife=20,
    )
    assert dist.bins[0].hazard == 0.5
    assert dist.bins[0].at_risk_w == 2.0
    assert dist.halflife == 20
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_cold_distribution.py::test_hazard_fields_on_schema -v`
Expected: FAIL — fields don't exist.

- [ ] **Step 3: Add fields**

In `python/spinlab/api_schemas.py`:

```python
class ColdBin(_BaseResponse):
    lo_ms: float
    hi_ms: float
    n_deaths: int
    n_completions: int
    hazard: float | None = None    # NEW: null when at_risk_w == 0
    at_risk_w: float = 0.0          # NEW: weighted at-risk count entering bin


class ColdDistribution(_BaseResponse):
    bins: list[ColdBin]
    n_cold_attempts: int
    mu_d_ms: float | None
    mu_c_ms: float | None
    p_die_per_attempt: float | None
    p_die_per_life: float | None
    halflife: int = 0               # NEW: echoed for label/debug
```

The defaults preserve backward compatibility — Phase 0 callers that don't populate hazard get `null` / `0.0` / `0`.

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/unit/test_cold_distribution.py::test_hazard_fields_on_schema -v`
Expected: PASS.

- [ ] **Step 5: Regenerate types and commit**

```bash
cd frontend && npm run gen-types && cd ..
git add python/spinlab/api_schemas.py tests/unit/test_cold_distribution.py frontend/openapi.json frontend/src/api-types.ts
git commit -m "feat(api): hazard fields on ColdBin + halflife on ColdDistribution"
```

---

### Task 1.2: Extend `compute_cold_distribution` with hazard math

**Files:**
- Modify: `python/spinlab/cold_distribution.py`
- Modify: `tests/unit/test_cold_distribution.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_hazard_single_death():
    # One cold attempt died at 2000ms. The bin containing 2000ms gets
    # hazard = 1/1 = 1.0; bins after the death have at_risk_w = 0, hazard = None.
    events = [_ev(2000, AttemptOutcome.DIED)]
    dist = compute_cold_distribution(events, halflife=20)
    # Find the bin containing 2000ms
    target_idx = next(
        i for i, b in enumerate(dist.bins) if b.lo_ms <= 2000 <= b.hi_ms
    )
    assert dist.bins[target_idx].hazard == 1.0
    assert dist.bins[target_idx].at_risk_w == 1.0
    # Bins before the death: at_risk_w = 1.0, hazard = 0.0 (no deaths yet)
    for i in range(target_idx):
        assert dist.bins[i].at_risk_w == 1.0
        assert dist.bins[i].hazard == 0.0
    # Bins after the death: at_risk_w = 0, hazard = None
    for i in range(target_idx + 1, len(dist.bins)):
        assert dist.bins[i].at_risk_w == 0.0
        assert dist.bins[i].hazard is None


def test_hazard_one_death_one_completion():
    # Died at 2000, survived at 8000.
    # Bin containing 2000: deaths_w=1, at_risk_w=2 → hazard=0.5
    # Bins between 2000 and 8000: deaths_w=0, at_risk_w=1 → hazard=0
    # Bin containing 8000: deaths_w=0, at_risk_w=1 → hazard=0
    # Bins after 8000: at_risk_w=0 → hazard=None
    events = [
        _ev(2000, AttemptOutcome.DIED, ep="e1"),
        _ev(8000, AttemptOutcome.SURVIVED, ep="e2"),
    ]
    dist = compute_cold_distribution(events, halflife=20)
    bin_at_2s = next(b for b in dist.bins if b.lo_ms <= 2000 <= b.hi_ms)
    assert abs(bin_at_2s.hazard - 0.5) < 1e-9
    assert bin_at_2s.at_risk_w == 2.0


def test_hazard_curve_returns_halflife():
    events = [_ev(2000, AttemptOutcome.DIED)]
    dist = compute_cold_distribution(events, halflife=42)
    assert dist.halflife == 42
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_cold_distribution.py -k hazard -v`
Expected: FAIL — hazard not computed.

- [ ] **Step 3: Extend the implementation**

In `python/spinlab/cold_distribution.py`, modify `compute_cold_distribution`:

```python
# (after computing bins, sum_w_*, and aggregates as in Phase 0...)

# Hazard: for each bin i, compute:
#   deaths_w[i]  = sum(w of cold deaths whose time_ms in [lo_i, hi_i))
#   at_risk_w[i] = sum(w of cold attempts whose time_ms >= lo_i)
# Note: deaths_w is per-bin (event_in_bin), but at_risk is cumulative
# (event_time_ms >= bin_lo, regardless of which bin the event ultimately
# falls into).

deaths_w_per_bin = [0.0] * bin_count
event_weights_at_time: list[tuple[int, float]] = []  # (time_ms, weight) pairs

for ev, w in zip(truncated, weights):
    idx = bin_idx(ev.time_ms)
    if ev.outcome == AttemptOutcome.DIED:
        deaths_w_per_bin[idx] += w
    event_weights_at_time.append((ev.time_ms, w))

# at_risk_w for each bin i: sum of weights of events whose time_ms >= lo_i.
for i, b in enumerate(bins):
    at_risk_w = sum(w for t, w in event_weights_at_time if t >= b.lo_ms)
    b.at_risk_w = at_risk_w
    if at_risk_w > 0:
        b.hazard = deaths_w_per_bin[i] / at_risk_w
    else:
        b.hazard = None

# (at the return, add halflife=halflife)
return ColdDistribution(
    bins=bins, n_cold_attempts=n,
    mu_d_ms=mu_d_ms, mu_c_ms=mu_c_ms,
    p_die_per_attempt=p_die_per_attempt, p_die_per_life=p_die_per_life,
    halflife=halflife,
)
```

Note: ColdBin is a Pydantic model — `b.hazard = None` assigns to a field. If immutable, switch to constructing new ColdBin instances with all fields set. Verify by `npx pyright` after editing.

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/unit/test_cold_distribution.py -v`
Expected: all PASS (Phase 0 tests + 3 new hazard tests).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/cold_distribution.py tests/unit/test_cold_distribution.py
git commit -m "feat(cold-dist): hazard + at_risk_w per bin; echo halflife"
```

---

### Task 1.3: Frontend hazard renderer module

**Files:**
- Create: `frontend/src/hazard-render.ts`
- Create: `frontend/src/hazard-render.test.ts`

- [ ] **Step 1: Write the failing test**

```typescript
// frontend/src/hazard-render.test.ts
import { describe, it, expect, vi } from "vitest";
import { renderHazard } from "./hazard-render";
import type { ColdDistribution } from "./types";

vi.mock("chart.js", () => ({
  Chart: class {
    data: any; options: any;
    static register() {}
    constructor(_ctx: unknown, config: { data: any; options: any }) {
      this.data = config.data; this.options = config.options;
    }
    destroy() {} update() {}
  },
  BarController: class {}, BarElement: class {},
  LinearScale: class {}, CategoryScale: class {},
  Legend: class {}, Tooltip: class {},
}));

const SAMPLE: ColdDistribution = {
  bins: [
    { lo_ms: 0,    hi_ms: 500, n_deaths: 0, n_completions: 0, hazard: 0.1, at_risk_w: 10.0 },
    { lo_ms: 500,  hi_ms: 1000, n_deaths: 0, n_completions: 0, hazard: 0.3, at_risk_w: 5.0 },
    { lo_ms: 1000, hi_ms: 1500, n_deaths: 0, n_completions: 0, hazard: null, at_risk_w: 0.0 },
  ],
  n_cold_attempts: 10, mu_d_ms: null, mu_c_ms: null,
  p_die_per_attempt: null, p_die_per_life: null, halflife: 20,
};

describe("renderHazard", () => {
  it("creates one bar per bin with hazard values", () => {
    const chart = renderHazard(document.createElement("canvas"), SAMPLE);
    const data = (chart as any).data;
    expect(data.datasets).toHaveLength(1);
    // null bin renders as 0-height; chart.js drops nulls cleanly
    expect(data.datasets[0].data).toEqual([0.1, 0.3, null]);
  });

  it("computes per-bar opacity from at_risk_w / bins[0].at_risk_w", () => {
    const chart = renderHazard(document.createElement("canvas"), SAMPLE);
    const bg = (chart as any).data.datasets[0].backgroundColor as string[];
    // bin 0: at_risk 10/10 = 1.0  → full opacity
    // bin 1: at_risk 5/10 = 0.5   → half
    // bin 2: at_risk 0/10 = 0.0   → zero
    expect(bg[0]).toMatch(/rgba\(255,\s*241,\s*118,\s*1(\.0+)?\)/);
    expect(bg[1]).toMatch(/rgba\(255,\s*241,\s*118,\s*0\.5\d*\)/);
    expect(bg[2]).toMatch(/rgba\(255,\s*241,\s*118,\s*0(\.0+)?\)/);
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd frontend && npm test -- hazard-render`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement**

```typescript
// frontend/src/hazard-render.ts
import {
  Chart, BarController, BarElement,
  LinearScale, CategoryScale, Legend, Tooltip,
} from "chart.js";
import { formatTime } from "./format";
import type { ColdDistribution } from "./types";

Chart.register(BarController, BarElement, LinearScale, CategoryScale, Legend, Tooltip);

// Yellow matches the spec's mockup; high enough contrast on dark bg
// without competing with the histogram's red/green.
const HAZARD_RGB = "255, 241, 118";

export function renderHazard(
  canvas: HTMLCanvasElement, dist: ColdDistribution,
): Chart {
  const labels = dist.bins.map((b) => formatTime(b.lo_ms));
  const data = dist.bins.map((b) => b.hazard);  // null preserved; chart.js skips
  const denom = dist.bins.length > 0 ? dist.bins[0]!.at_risk_w : 0;
  const bg = dist.bins.map((b) => {
    const opacity = denom > 0 ? Math.max(0, Math.min(1, b.at_risk_w / denom)) : 0;
    return `rgba(${HAZARD_RGB}, ${opacity})`;
  });

  return new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "Hazard rate",
        data,
        backgroundColor: bg,
        borderWidth: 0,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        y: { min: 0, max: 1, title: { display: true, text: "Hazard rate" } },
        x: { title: { display: true, text: "Time" } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const b = dist.bins[ctx.dataIndex]!;
              const h = b.hazard == null ? "n/a" : b.hazard.toFixed(2);
              return `hazard: ${h} · at_risk: ${b.at_risk_w.toFixed(1)} (effective)`;
            },
          },
        },
      },
    },
  });
}
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd frontend && npm test -- hazard-render`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/hazard-render.ts frontend/src/hazard-render.test.ts
git commit -m "feat(frontend): hazard renderer with opacity-as-confidence"
```

---

### Task 1.4: Wire [Histogram] [Hazard] toggle into segment-detail

**Files:**
- Modify: `frontend/src/segment-detail.ts` (add toggle UI, swap chart on click)
- Modify: `frontend/src/segment-detail.test.ts` (toggle behavior)

- [ ] **Step 1: Find the existing panel rendering code**

Run: `grep -n "Cold distribution\|renderColdHistogram" frontend/src/segment-detail.ts`
Expected: a section that builds the panel header + a `<canvas>` + calls `renderColdHistogram`.

- [ ] **Step 2: Write the failing test**

Append to `frontend/src/segment-detail.test.ts`:

```typescript
import { describe, it, expect } from "vitest";

describe("Cold distribution toggle", () => {
  it("renders Histogram by default and switches to Hazard on click", async () => {
    // ...spin up the detail view with a mock /api/segments/.../history
    // response that has cold_distribution populated...
    // Assert: panel canvas exists, initial chart is histogram (2 datasets).
    // Click [Hazard] button. Assert chart now has 1 dataset titled "Hazard rate".
    // Click [Histogram] again. Assert reverted.
  });

  it("disables Hazard tab when cold_distribution is null", async () => {
    // ...mock response with cold_distribution: null...
    // Assert Hazard button is disabled (or hidden); panel shows empty state.
  });
});
```

Write the actual test bodies referencing the existing `setupDetail` helper in `segment-detail.test.ts` (or create a small helper if absent). Use a happy-dom DOM + a `fetch` mock returning the canned `SegmentHistory`.

- [ ] **Step 3: Run to verify failure**

Run: `cd frontend && npm test -- segment-detail`
Expected: FAIL — toggle doesn't exist yet.

- [ ] **Step 4: Add the toggle**

In `frontend/src/segment-detail.ts`, in the cold-distribution panel header (where the title "Cold distribution" is rendered today), add two buttons next to the title:

```typescript
const tabBar = document.createElement("div");
tabBar.className = "cold-tabs";
const histBtn = document.createElement("button");
histBtn.className = "cold-tab active";
histBtn.textContent = "Histogram";
const hazBtn = document.createElement("button");
hazBtn.className = "cold-tab";
hazBtn.textContent = "Hazard";
if (!history.cold_distribution) hazBtn.disabled = true;
tabBar.appendChild(histBtn);
tabBar.appendChild(hazBtn);
panelHeader.appendChild(tabBar);

let currentChart: Chart | null = null;
const showHistogram = () => {
  currentChart?.destroy();
  currentChart = renderColdHistogram(canvas, history.cold_distribution!);
  histBtn.classList.add("active"); hazBtn.classList.remove("active");
};
const showHazard = () => {
  currentChart?.destroy();
  currentChart = renderHazard(canvas, history.cold_distribution!);
  hazBtn.classList.add("active"); histBtn.classList.remove("active");
};
histBtn.addEventListener("click", showHistogram);
hazBtn.addEventListener("click", showHazard);
showHistogram();  // default view
```

Import: `import { renderHazard } from "./hazard-render";` near the top.

Also extend `destroySegmentDetail()` (or wherever cleanup lives) to call `currentChart?.destroy()`.

- [ ] **Step 5: Run tests + browser smoke-test**

```bash
cd frontend && npm test && npm run build && cd ..
```
Per CLAUDE.md UI rule: start the dashboard, open a segment detail page, click toggle in browser. Confirm visually.

```bash
# Manual smoke (skip if no display available):
spinlab dashboard --config <your config>
# Navigate to a segment with cold data; toggle Histogram/Hazard.
```

Expected: both views render correctly, toggle works.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/segment-detail.ts frontend/src/segment-detail.test.ts
git commit -m "feat(frontend): Histogram/Hazard toggle on segment-detail panel"
```

---

### Task 1.5: Backlog entries for deferred items

**Files:**
- Modify: `docs/BACKLOG.md`

- [ ] **Step 1: Append entries**

Under an appropriate section (or create "Cold/hot follow-ups" if not present):

```markdown
### Cold distribution / hazard follow-ups (post-2026-05-27)

- **Hot-view toggle**: add [Cold]/[Hot] sub-filter to the segment-detail
  distribution panel. Today the panel shows cold only; hot data is rare
  but a per-segment hot view is interesting once it accumulates.
- **Bootstrap filter consistency**: `bootstrap_resample` filters at the
  episode level (drops any episode containing a hot attempt); the
  cold_distribution layer filters at the attempt level. Decide whether
  bootstrap should be brought into line.
- **Confidence intervals on hazard**: opacity-as-confidence is the only
  signal today. KM-style binomial confidence bands are a reasonable next
  iteration if opacity proves insufficient.
- **Toggle persistence**: Histogram/Hazard tab choice resets to Histogram
  on every detail-page open. Persist across the session if useful.
- **`SegmentAttempt` episode aggregate**: do we even need it? Branch 3's
  cold_distribution.py works on a flat list of attempts and never touches
  the episode aggregate. Refactor candidate.
- **Histogram bar weighting**: histogram uses raw counts (n_deaths,
  n_completions). If users find the divergence from hazard's weighted
  view confusing, revisit.
```

- [ ] **Step 2: Commit**

```bash
git add docs/BACKLOG.md
git commit -m "docs(backlog): cold-distribution / hazard follow-ups"
```

---

### Task 1.6: Full-suite verification + merge

- [ ] **Step 1: Frontend build + tests**

```bash
cd frontend && npm run build && npm test && cd ..
```
Expected: green.

- [ ] **Step 2: Full pytest**

```bash
python -m pytest
```
Expected: green per CLAUDE.md "full suite" rule. Includes emulator + integration tests.

- [ ] **Step 3: Type + lint check on new code**

```bash
npx pyright python/spinlab/cold_distribution.py python/spinlab/routes/model.py
ruff check python/spinlab/cold_distribution.py
```
Expected: no NEW errors.

- [ ] **Step 4: Merge to main**

```bash
git checkout main
git merge --no-ff feat/segment-hazard-plot -m "Merge feat/segment-hazard-plot: hazard view + toggle"
git log --oneline -5
```

---

## Self-Review (filled by author at write-time)

**Spec coverage:** every Design Decision in the spec is implemented by a task:
- Panel-shared toggle → Task 1.4
- Cold-only filter at attempt level → Task 0.5 (route filter) + Task 0.4 (function trusts cold input)
- Weighted with shared halflife → Task 0.5 (route reads tuned halflife) + Task 0.4 / 1.2 (math)
- Adaptive sqrt bin count → Task 0.2
- Opacity = at-risk fraction → Task 1.3
- null hazard for at_risk == 0 → Task 1.2 (math) + Task 1.3 (rendering)
- Computed in route, not estimator → Task 0.5
- Histogram inherits cold filter + adaptive binning → Tasks 0.4, 0.6, 0.7

**Spec note:** the spec used `HazardCurve`/`HazardBin` names; this plan uses `ColdDistribution`/`ColdBin`. Justification: Phase 0 ships the same schema with only histogram fields, and naming it "HazardBin" before any hazard math exists is misleading. Hazard fields are added in Phase 1. The spec will be updated post-merge.

**Placeholder scan:** all "TBD"-style language replaced with concrete code or a clear `grep ... adapt to existing patterns` instruction (see Task 0.5 note about fixture names).

**Type consistency:** `ColdBin` / `ColdDistribution` fields are defined in Task 0.3 and extended in Task 1.1; all references in subsequent tasks match (lo_ms, hi_ms, n_deaths, n_completions, hazard, at_risk_w, n_cold_attempts, mu_d_ms, mu_c_ms, p_die_per_attempt, p_die_per_life, halflife). `renderColdHistogram` / `renderHazard` function signatures consistent across tasks.

**Open ambiguity:** Task 0.5's route test uses the per-file `db` + `client` fixture pattern from `tests/unit/routes/test_dashboard_references.py`, which is the prevailing pattern in that directory. `db.upsert_segment` and `db.log_event_attempt` are the actual method names (verified). The plan still asks the engineer to spot-check `Segment` and `make_event_attempt` parameter names since they evolve.

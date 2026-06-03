# D-Live-FE2 + BE-2: Live View Shell + Session Snapshot — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the D-Live v1 shell — replace the legacy practice insight card with a 3-section live practice view (route bar + segment summary + episode-graph slot) backed by an in-memory practice-session snapshot that powers "Practice saved" / colored session diffs / floor-improvement stats.

**Architecture:** BE-2 adds a `SessionSnapshot` (per-segment `SamplerState` clones + per-segment running-best clean clear + a route aggregate) captured by `SessionManager` at practice/hyper-play start. The existing closed-form reducers (`live_segment_view`, `route_summary`) take an optional baseline and return diff fields. FE2 mounts a 3-section layout — `route-bar.ts` + `segment-summary.ts` + the already-shipped `episode-graph.ts` — wired through a `live-view.ts` coordinator that fetches both endpoints per SSE push (mirroring `loadAndRenderImprovementView`). Legacy `#current-goal` / `#current-attempts` / `#insight` + `renderPracticeInsight` go away. The climbing dot (frame-by-frame liveliness) is deferred to a follow-up sub-plan (D-Live-FE3) since it needs an attempt-start timestamp the AppState doesn't carry yet.

**Tech Stack:** Python 3.11 dataclasses, FastAPI, TypeScript/Vite, inline SVG, vitest (happy-dom), pytest. No new deps.

**Spec:** [`docs/superpowers/specs/2026-06-02-live-practice-view-design.md`](../specs/2026-06-02-live-practice-view-design.md).

**Prior chunks shipped:** D-Live-BE (closed-form payloads, `08a3550`); D-Live-FE1 (`episode-graph.ts`, `d3f3300`).

---

## Key facts about existing code (verified)

- `python/spinlab/estimators/live_view.py` already defines `live_segment_view(state, episodes)` returning a `LiveSegmentView` dataclass and `route_summary(states)` returning `RouteSummary`. Neither knows about session baselines yet. Both are pure functions over `SamplerState` + observed attempts.
- `python/spinlab/api_schemas.py:505-528` defines `LiveSegmentViewResponse` and `RouteSummaryResponse`. The route bar payload (`RouteSummaryResponse`) has `game_id, exp_run_ms, exp_deaths, n_estimable, n_skipped`. The per-segment payload (`LiveSegmentViewResponse`) has `segment_id, ready, expected_episode_ms, practice_gain_ms, death_rate, floor_ms, last_episode_ms, last_clean_ms, last_deaths, last_rank, series, n_successes, n_deaths`.
- `python/spinlab/routes/model.py:228-280` serves `GET /api/segments/{segment_id}/live` and `GET /api/games/{game_id}/live-summary`. Both replay events to `SamplerState`, call the reducer, and return a dict.
- `python/spinlab/session_manager.py:57+` holds the `SessionManager`. Practice start runs through `practice_session = ...` (a `PracticeSession` set on start, cleared on stop). Hyper-play start does the same via `hyper_play_session`. Scheduler is lazy at `self.scheduler` with `_scheduler_lock`.
- `frontend/src/episode-graph.ts` exports `renderEpisodeGraph(host, data: LiveSegmentView)`. Pure render, no fetch.
- `frontend/src/improvement-view.ts` is the canonical "fetch on SSE push + render into a fixed host" pattern (`loadAndRenderImprovementView(segmentId, host)`) — the new live view coordinator mirrors it but fetches two endpoints.
- `frontend/src/model.ts:72-111` is `updatePracticeCard(data)` — the function that runs per SSE push and calls `renderPracticeInsight` (TO DELETE), `renderRecentList`, `renderSessionStats`, `renderSavingsPanel`, `loadAndRenderImprovementView`, `loadAndRenderEmSuitePanel`.
- `frontend/src/model-render.ts:233-252` is `renderPracticeInsight` — the legacy card to DELETE.
- `frontend/index.html:44-71` is the `#practice-card` block. Legacy hosts to remove: `#current-goal`, `#current-attempts`, `#insight`, the wrapping `<div class="card">` at line 54. New hosts to add: `#live-route-bar`, `#live-segment-summary`, `#live-graph-slot`.
- `SessionInfo` (in AppState) carries `started_at` (epoch seconds), used by `renderSessionStats`. Practice start sets it. We will not change `SessionInfo` itself — the new payloads carry `session_started_at` independently.
- The episode-graph payload's `series` arrives as `list[dict]` from the BE; the FE has `EpisodePoint` (in `types.ts`) for the item shape.

## File Structure

**New BE files:**
- `python/spinlab/estimators/session_snapshot.py` — `SessionSnapshot` dataclass + pure helpers.
- `tests/unit/estimators/test_session_snapshot.py` — unit tests for the pure helpers.

**Modified BE files:**
- `python/spinlab/estimators/live_view.py` — add optional `baseline` arg to `live_segment_view` and `route_summary`; populate diff fields.
- `python/spinlab/session_manager.py` — hold `practice_session_snapshot`; capture at practice/hyper-play start; clear at stop.
- `python/spinlab/api_schemas.py` — add diff fields to `LiveSegmentViewResponse` and `RouteSummaryResponse`.
- `python/spinlab/routes/model.py` — thread the snapshot through both routes.
- `tests/unit/estimators/test_live_view.py` (if exists; else create) — diff-field coverage.

**New FE files:**
- `frontend/src/route-bar.ts` — pure render (title, Practice saved, stat columns).
- `frontend/src/route-bar.test.ts`
- `frontend/src/segment-summary.ts` — pure render (header + 4-col stat cluster + headline + decomposition).
- `frontend/src/segment-summary.test.ts`
- `frontend/src/stat-stack.ts` — shared label/value/colored-diff renderer used by both above.
- `frontend/src/stat-stack.test.ts`
- `frontend/src/live-view.ts` — coordinator: fetch `/segments/{id}/live` + `/games/{id}/live-summary`, render all three slots, manage the session-elapsed tick.
- `frontend/src/live-view.test.ts`

**Modified FE files:**
- `frontend/index.html` — remove legacy hosts + `<div class="card">`; add three new hosts.
- `frontend/src/model.ts` — remove `renderPracticeInsight` call; mount `loadAndRenderLiveView` per push; teardown on mode change.
- `frontend/src/model-render.ts` — delete `renderPracticeInsight` export.
- `frontend/src/types.ts` — regen + add re-exports as needed.
- `frontend/style.css` — append CSS for aligned stat columns + section spacing.
- `tests/integration/test_practice_smoke.py` (or the existing frontend smoke that asserts `#insight`) — update.

---

## Task 1: `SessionSnapshot` dataclass + pure helpers (Red)

**Files:**
- Create: `tests/unit/estimators/test_session_snapshot.py`

- [ ] **Step 1: Write the failing test.** Create `tests/unit/estimators/test_session_snapshot.py`:

```python
"""SessionSnapshot — practice-session baseline for live-view diffs.

The snapshot captures (a) per-segment closed-form metrics at session start
(expected episode ms, practice gain ms, death rate, floor ms = current
running-min clean clear), and (b) a route aggregate (exp_run_ms, exp_deaths).
diff helpers return positive values for improvement (regardless of metric sign).
"""
from __future__ import annotations

import copy

import pytest

from spinlab.estimators.em_suite_sampler import (
    DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX, SamplerState, replay_with_history,
)
from spinlab.estimators.live_view import live_segment_view, route_summary
from spinlab.estimators.session_snapshot import (
    SegmentBaseline, SessionSnapshot, RouteBaseline,
    snapshot_from_segments, segment_diff, route_diff,
)


def _state_after(events):
    s, _ = replay_with_history(events)
    return s


def _seg_with_events(events, episodes):
    return _state_after(events), episodes


def test_snapshot_captures_started_at_and_per_segment_baselines():
    # two synthetic gated segments
    states = [SamplerState(seg_id=f"s{i}", level_number=0, alpha_grid=(0.1,)) for i in range(2)]
    for s in states:
        # force a gated state via direct mutation paths used elsewhere in tests.
        # If your sampler exposes a public helper, prefer it.
        s.n_successes = 3
        s.n_deaths = 3
    snapshot = snapshot_from_segments(
        started_at=1717_000_000.0,
        segments=[(s, []) for s in states],
    )
    assert snapshot.started_at == 1717_000_000.0
    assert set(snapshot.segments.keys()) == {"s0", "s1"}
    assert isinstance(snapshot.route, RouteBaseline)


def test_segment_diff_returns_none_when_either_side_missing():
    base = SegmentBaseline(
        expected_episode_ms=20_000.0, practice_gain_ms=500.0,
        death_rate=0.5, floor_ms=15_000.0,
    )
    # current expected None → diff None
    assert segment_diff(base, current_expected_ms=None,
                        current_gain_ms=None, current_death_rate=0.5,
                        current_floor_ms=None) == {
        "expected_episode_diff_ms": None,
        "practice_gain_diff_ms": None,
        "death_rate_diff": 0.0,
        "floor_diff_ms": None,
    }


def test_segment_diff_signs_improvement_positive():
    """Expected ms dropped → improvement; floor dropped → improvement;
    death rate dropped → improvement (negative delta in the rate itself,
    but reported as positive 'improvement' isn't what we do — we report the
    raw delta (current − baseline). UI colors by sign + metric semantics)."""
    base = SegmentBaseline(
        expected_episode_ms=20_000.0, practice_gain_ms=500.0,
        death_rate=0.5, floor_ms=15_000.0,
    )
    d = segment_diff(
        base,
        current_expected_ms=18_000.0,
        current_gain_ms=300.0,
        current_death_rate=0.4,
        current_floor_ms=14_000.0,
    )
    # expected: current − baseline (negative = faster); UI inverts for color
    assert d["expected_episode_diff_ms"] == pytest.approx(-2_000.0)
    assert d["floor_diff_ms"] == pytest.approx(-1_000.0)
    assert d["death_rate_diff"] == pytest.approx(-0.1)
    assert d["practice_gain_diff_ms"] == pytest.approx(-200.0)


def test_route_diff_returns_none_when_either_aggregate_missing():
    base = RouteBaseline(exp_run_ms=120_000.0, exp_deaths=4.0)
    assert route_diff(base, current_exp_run_ms=None, current_exp_deaths=None) == {
        "exp_run_diff_ms": None,
        "exp_deaths_diff": None,
        "practice_saved_ms": None,
    }


def test_route_diff_practice_saved_is_drop_in_exp_run():
    base = RouteBaseline(exp_run_ms=120_000.0, exp_deaths=4.0)
    d = route_diff(base, current_exp_run_ms=115_000.0, current_exp_deaths=3.5)
    assert d["exp_run_diff_ms"] == pytest.approx(-5_000.0)  # current − baseline
    assert d["exp_deaths_diff"] == pytest.approx(-0.5)
    # practice_saved = baseline − current = +5000 ms saved
    assert d["practice_saved_ms"] == pytest.approx(5_000.0)


def test_snapshot_state_clone_is_independent():
    """If we later mutate the live SamplerState, the baseline must not move."""
    s = SamplerState(seg_id="s0", level_number=0, alpha_grid=(0.1,))
    s.n_successes = 3
    s.n_deaths = 3
    snapshot = snapshot_from_segments(started_at=1.0, segments=[(s, [])])
    s.n_successes = 99  # mutate live
    # baseline was a snapshot, not a reference
    baseline = snapshot.segments[s.seg_id]
    # The baseline holds DERIVED scalars (not the state); the floor was captured
    # from observed episodes. The mutation above must not move any baseline field.
    assert isinstance(baseline, SegmentBaseline)
```

- [ ] **Step 2: Run — Red.**

Run: `python -m pytest tests/unit/estimators/test_session_snapshot.py -v`
Expected: FAIL with `ImportError: cannot import name 'SessionSnapshot' from 'spinlab.estimators.session_snapshot'`.

---

## Task 2: `SessionSnapshot` implementation (Green)

**Files:**
- Create: `python/spinlab/estimators/session_snapshot.py`

- [ ] **Step 1: Implement.** Create `python/spinlab/estimators/session_snapshot.py`:

```python
"""Practice-session baseline snapshot for the live view's session diffs.

A snapshot is taken once at practice/hyper-play start:
  - per-segment: derived scalars (expected_episode_ms, practice_gain_ms,
    death_rate, floor_ms = running-min clean clear at session start),
  - route aggregate: exp_run_ms, exp_deaths.

The reducers (live_segment_view, route_summary) accept the snapshot as an
optional baseline and emit diff fields. The snapshot stores DERIVED scalars
rather than SamplerState clones — the live reducer recomputes 'current'
from the live state on every request, so we only need the comparison anchors.

`segment_diff` and `route_diff` report `current − baseline` (raw deltas).
The UI inverts sign per metric (expected/floor lower = improvement → green
when delta < 0; death_rate lower = improvement; exp_run lower = improvement;
practice_saved is the explicit `baseline − current` of exp_run so positive
always means 'time saved this session').
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from spinlab.estimators.em_suite_sampler import (
    DEFAULT_DEATH_PENALTY_MS, DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX, LOGIT_EPS,
    SamplerState, _gate_passes, expected_episode_time_ms,
    expected_episode_time_scalar,
)


@dataclass(frozen=True)
class SegmentBaseline:
    expected_episode_ms: float | None
    practice_gain_ms: float | None
    death_rate: float
    floor_ms: float | None


@dataclass(frozen=True)
class RouteBaseline:
    exp_run_ms: float | None
    exp_deaths: float | None


@dataclass(frozen=True)
class SessionSnapshot:
    """Taken at practice/hyper-play start. Read-only thereafter."""
    started_at: float  # epoch seconds (time.time())
    segments: Mapping[str, SegmentBaseline]
    route: RouteBaseline


def _baseline_for_segment(
    state: SamplerState,
    episodes: Sequence[Mapping[str, Any]],
    *,
    reload_penalty_ms: int = DEFAULT_DEATH_PENALTY_MS,
) -> SegmentBaseline:
    if not _gate_passes(state):
        return SegmentBaseline(
            expected_episode_ms=None, practice_gain_ms=None,
            death_rate=0.0, floor_ms=_running_min_clean(episodes),
        )
    expected = expected_episode_time_scalar(state)
    slid = expected_episode_time_ms(
        state, DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX,
        apply_slope=True, reload_penalty_ms=reload_penalty_ms,
    )
    gain = (expected - slid) if (expected is not None and slid is not None) else None
    p = state.p_die_ema(DEFAULT_FAST_IDX)
    return SegmentBaseline(
        expected_episode_ms=expected,
        practice_gain_ms=gain,
        death_rate=float(p) if p is not None else 0.0,
        floor_ms=_running_min_clean(episodes),
    )


def _running_min_clean(episodes: Sequence[Mapping[str, Any]]) -> float | None:
    floor: float | None = None
    for e in episodes:
        if not e.get("completed") or e.get("invalidated"):
            continue
        clean = e.get("clean_tail_ms")
        if clean is None:
            continue
        floor = float(clean) if floor is None else min(floor, float(clean))
    return floor


def _route_baseline(
    items: Sequence[tuple[SamplerState, Sequence[Mapping[str, Any]]]],
) -> RouteBaseline:
    run_ms = 0.0
    deaths = 0.0
    n_est = 0
    for state, _episodes in items:
        if not _gate_passes(state):
            continue
        exp = expected_episode_time_scalar(state)
        p = state.p_die_ema(DEFAULT_FAST_IDX)
        if exp is None or p is None or p >= 1.0 - LOGIT_EPS:
            continue
        run_ms += exp
        deaths += p / (1.0 - p)
        n_est += 1
    if n_est == 0:
        return RouteBaseline(exp_run_ms=None, exp_deaths=None)
    return RouteBaseline(exp_run_ms=run_ms, exp_deaths=deaths)


def snapshot_from_segments(
    *,
    started_at: float,
    segments: Sequence[tuple[SamplerState, Sequence[Mapping[str, Any]]]],
) -> SessionSnapshot:
    """Build a session snapshot from current sampler states + observed episodes."""
    per_seg: dict[str, SegmentBaseline] = {}
    for state, episodes in segments:
        per_seg[state.seg_id] = _baseline_for_segment(state, episodes)
    return SessionSnapshot(
        started_at=started_at,
        segments=per_seg,
        route=_route_baseline(segments),
    )


def segment_diff(
    baseline: SegmentBaseline,
    *,
    current_expected_ms: float | None,
    current_gain_ms: float | None,
    current_death_rate: float,
    current_floor_ms: float | None,
) -> dict[str, float | None]:
    """Compute current − baseline for each comparable scalar. None when either side missing."""
    def _delta(c: float | None, b: float | None) -> float | None:
        if c is None or b is None:
            return None
        return float(c - b)
    return {
        "expected_episode_diff_ms": _delta(current_expected_ms, baseline.expected_episode_ms),
        "practice_gain_diff_ms": _delta(current_gain_ms, baseline.practice_gain_ms),
        # death_rate is always a float [0,1]; delta is always defined.
        "death_rate_diff": float(current_death_rate - baseline.death_rate),
        "floor_diff_ms": _delta(current_floor_ms, baseline.floor_ms),
    }


def route_diff(
    baseline: RouteBaseline,
    *,
    current_exp_run_ms: float | None,
    current_exp_deaths: float | None,
) -> dict[str, float | None]:
    """Compute route diffs. practice_saved_ms = baseline − current (positive = saved)."""
    def _delta(c: float | None, b: float | None) -> float | None:
        if c is None or b is None:
            return None
        return float(c - b)
    saved: float | None = None
    if baseline.exp_run_ms is not None and current_exp_run_ms is not None:
        saved = float(baseline.exp_run_ms - current_exp_run_ms)
    return {
        "exp_run_diff_ms": _delta(current_exp_run_ms, baseline.exp_run_ms),
        "exp_deaths_diff": _delta(current_exp_deaths, baseline.exp_deaths),
        "practice_saved_ms": saved,
    }
```

- [ ] **Step 2: Run — Green.**

Run: `python -m pytest tests/unit/estimators/test_session_snapshot.py -v`
Expected: all PASS.

- [ ] **Step 3: Commit.**

```bash
git add python/spinlab/estimators/session_snapshot.py tests/unit/estimators/test_session_snapshot.py
git commit -m "feat(session-snapshot): baseline + diff helpers for live-view session overlay"
```

---

## Task 3: Extend `live_view.py` reducers with optional baselines (TDD)

**Files:**
- Modify: `python/spinlab/estimators/live_view.py`
- Modify: `tests/unit/estimators/test_live_view.py` (create if missing)

- [ ] **Step 1: Write the failing test.** Append to (or create) `tests/unit/estimators/test_live_view.py`:

```python
import pytest

from spinlab.estimators.em_suite_sampler import SamplerState
from spinlab.estimators.live_view import LiveSegmentView, live_segment_view, RouteSummary, route_summary
from spinlab.estimators.session_snapshot import (
    SegmentBaseline, RouteBaseline, SessionSnapshot,
)


def _gated_state(seg_id="s0"):
    s = SamplerState(seg_id=seg_id, level_number=0, alpha_grid=(0.1,))
    s.n_successes = 3
    s.n_deaths = 3
    return s


def test_live_segment_view_emits_null_diffs_when_baseline_absent():
    v = live_segment_view(_gated_state(), [], baseline=None)
    assert v.expected_episode_diff_ms is None
    assert v.practice_gain_diff_ms is None
    assert v.floor_diff_ms is None
    assert v.death_rate_diff is None


def test_live_segment_view_emits_diffs_against_baseline():
    state = _gated_state()
    base = SegmentBaseline(
        expected_episode_ms=20_000.0, practice_gain_ms=500.0,
        death_rate=0.5, floor_ms=15_000.0,
    )
    v = live_segment_view(state, [], baseline=base)
    # 'current' values are computed from the state; deltas are current − baseline.
    # We can't pin exact values for a synthetic empty sampler, but the fields
    # must be present and numeric when both sides exist.
    if v.expected_episode_ms is not None:
        assert v.expected_episode_diff_ms == pytest.approx(v.expected_episode_ms - 20_000.0)
    assert v.death_rate_diff is not None  # rate is always defined


def test_route_summary_emits_null_diffs_when_baseline_absent():
    r = route_summary([_gated_state(), _gated_state("s1")], baseline=None)
    assert r.exp_run_diff_ms is None
    assert r.exp_deaths_diff is None
    assert r.practice_saved_ms is None


def test_route_summary_practice_saved_is_baseline_minus_current():
    states = [_gated_state(), _gated_state("s1")]
    base = RouteBaseline(exp_run_ms=200_000.0, exp_deaths=10.0)
    r = route_summary(states, baseline=base)
    if r.exp_run_ms is not None:
        assert r.practice_saved_ms == pytest.approx(200_000.0 - r.exp_run_ms)
```

- [ ] **Step 2: Run — Red.**

Run: `python -m pytest tests/unit/estimators/test_live_view.py -v`
Expected: FAIL (`baseline` kwarg unknown, `expected_episode_diff_ms` etc. missing).

- [ ] **Step 3: Implement.** Replace the contents of `python/spinlab/estimators/live_view.py` with:

```python
"""Closed-form live-view reducers — the data behind the live practice view.

Everything here is EXACT closed form (no Monte-Carlo): valid because the live
view uses only the additive total-run-time objective under no_reset. See the
D-Live spec's Computation Sources table. The Monte-Carlo engine stays the
Simulator's. Optional `baseline` arguments thread a `SessionSnapshot` through
so the reducer emits per-request diffs vs the session-start values.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from spinlab.estimators.em_suite_sampler import (
    DEFAULT_DEATH_PENALTY_MS,
    DEFAULT_FAST_IDX,
    DEFAULT_SLOW_IDX,
    LOGIT_EPS,
    SamplerState,
    _gate_passes,
    expected_episode_time_ms,
    expected_episode_time_scalar,
)
from spinlab.estimators.session_snapshot import (
    RouteBaseline, SegmentBaseline, route_diff, segment_diff,
)


@dataclass
class LiveSegmentView:
    """Closed-form per-segment payload for the live view. ms fields None below gate."""
    ready: bool
    expected_episode_ms: float | None
    practice_gain_ms: float | None
    death_rate: float
    floor_ms: float | None
    last_episode_ms: float | None
    last_clean_ms: float | None
    last_deaths: int | None
    last_rank: int | None
    series: list[dict] = field(default_factory=list)
    # Session diffs (None when no baseline / either side missing).
    expected_episode_diff_ms: float | None = None
    practice_gain_diff_ms: float | None = None
    floor_diff_ms: float | None = None
    death_rate_diff: float | None = None


def _valid_completed(
    episodes: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return [e for e in episodes if e["completed"] and not e["invalidated"]
            and e["time_ms"] is not None]


def live_segment_view(
    state: SamplerState,
    episodes: Sequence[Mapping[str, Any]],
    *,
    reload_penalty_ms: int = DEFAULT_DEATH_PENALTY_MS,
    baseline: SegmentBaseline | None = None,
) -> LiveSegmentView:
    if not _gate_passes(state):
        return LiveSegmentView(
            ready=False, expected_episode_ms=None, practice_gain_ms=None,
            death_rate=0.0, floor_ms=None, last_episode_ms=None,
            last_clean_ms=None, last_deaths=None, last_rank=None, series=[],
        )

    expected = expected_episode_time_scalar(state)
    slid = expected_episode_time_ms(
        state, DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX,
        apply_slope=True, reload_penalty_ms=reload_penalty_ms,
    )
    practice_gain = (expected - slid) if (expected is not None and slid is not None) else None

    p_die = state.p_die_ema(DEFAULT_FAST_IDX)
    death_rate = float(p_die) if p_die is not None else 0.0

    valid = _valid_completed(episodes)
    floor_ms: float | None = None
    series: list[dict] = []
    for e in valid:
        clean = e["clean_tail_ms"]
        if clean is not None:
            floor_ms = float(clean) if floor_ms is None else min(floor_ms, float(clean))
        series.append({
            "episode_ms": float(e["time_ms"]),
            "deaths": int(e["deaths"]),
            "clean_ms": float(clean) if clean is not None else None,
            "running_floor_ms": floor_ms,
        })

    if valid:
        last = valid[-1]
        last_episode_ms = float(last["time_ms"])
        last_clean_ms = float(last["clean_tail_ms"]) if last["clean_tail_ms"] is not None else None
        last_deaths = int(last["deaths"])
        totals = sorted(float(e["time_ms"]) for e in valid)
        last_rank = totals.index(last_episode_ms) + 1
    else:
        last_episode_ms = last_clean_ms = last_deaths = last_rank = None

    diffs: dict[str, float | None] = {
        "expected_episode_diff_ms": None,
        "practice_gain_diff_ms": None,
        "floor_diff_ms": None,
        "death_rate_diff": None,
    }
    if baseline is not None:
        diffs = segment_diff(
            baseline,
            current_expected_ms=expected,
            current_gain_ms=practice_gain,
            current_death_rate=death_rate,
            current_floor_ms=floor_ms,
        )

    return LiveSegmentView(
        ready=True, expected_episode_ms=expected, practice_gain_ms=practice_gain,
        death_rate=death_rate, floor_ms=floor_ms, last_episode_ms=last_episode_ms,
        last_clean_ms=last_clean_ms, last_deaths=last_deaths, last_rank=last_rank,
        series=series,
        expected_episode_diff_ms=diffs["expected_episode_diff_ms"],
        practice_gain_diff_ms=diffs["practice_gain_diff_ms"],
        floor_diff_ms=diffs["floor_diff_ms"],
        death_rate_diff=diffs["death_rate_diff"],
    )


@dataclass
class RouteSummary:
    """Closed-form whole-run aggregate. None when no segment is estimable."""
    exp_run_ms: float | None
    exp_deaths: float | None
    n_estimable: int
    n_skipped: int
    # Session diffs (None when no baseline / either side missing).
    exp_run_diff_ms: float | None = None
    exp_deaths_diff: float | None = None
    practice_saved_ms: float | None = None


def route_summary(
    states: list[SamplerState],
    *,
    baseline: RouteBaseline | None = None,
) -> RouteSummary:
    run_ms = 0.0
    deaths = 0.0
    n_est = 0
    n_skip = 0
    for state in states:
        exp = expected_episode_time_scalar(state)
        p = state.p_die_ema(DEFAULT_FAST_IDX) if _gate_passes(state) else None
        if exp is None or p is None or p >= 1.0 - LOGIT_EPS:
            n_skip += 1
            continue
        run_ms += exp
        deaths += p / (1.0 - p)
        n_est += 1
    if n_est == 0:
        cur_run: float | None = None
        cur_deaths: float | None = None
    else:
        cur_run = run_ms
        cur_deaths = deaths
    diffs = (
        route_diff(baseline, current_exp_run_ms=cur_run, current_exp_deaths=cur_deaths)
        if baseline is not None
        else {"exp_run_diff_ms": None, "exp_deaths_diff": None, "practice_saved_ms": None}
    )
    return RouteSummary(
        exp_run_ms=cur_run, exp_deaths=cur_deaths,
        n_estimable=n_est, n_skipped=n_skip,
        exp_run_diff_ms=diffs["exp_run_diff_ms"],
        exp_deaths_diff=diffs["exp_deaths_diff"],
        practice_saved_ms=diffs["practice_saved_ms"],
    )
```

- [ ] **Step 4: Run — Green.**

Run: `python -m pytest tests/unit/estimators/test_live_view.py tests/unit/estimators/test_session_snapshot.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit.**

```bash
git add python/spinlab/estimators/live_view.py tests/unit/estimators/test_live_view.py
git commit -m "feat(live-view): optional baseline arg + diff fields on segment + route reducers"
```

---

## Task 4: `SessionManager` wires the snapshot

**Files:**
- Modify: `python/spinlab/session_manager.py`
- Create: `tests/unit/test_session_manager_snapshot.py`

- [ ] **Step 1: Write the failing test.** Create `tests/unit/test_session_manager_snapshot.py`:

```python
"""SessionManager — practice/hyper-play start captures a session snapshot;
stop clears it. The snapshot is taken from the current sampler states + the
observed attempts for each segment, with started_at = time.time()."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from spinlab.session_manager import SessionManager


def _make_sm_with_segments(monkeypatch, seg_ids):
    """Build a SessionManager with a fake scheduler that exposes per-segment
    sampler states + attempts. Stays out of the emu/db plumbing."""
    sm = SessionManager.__new__(SessionManager)  # bypass __init__
    sm.practice_session_snapshot = None

    class FakeState:
        def __init__(self, sid):
            self.seg_id = sid
            self.n_successes = 3
            self.n_deaths = 3

    sm._snapshot_inputs = lambda: [(FakeState(sid), []) for sid in seg_ids]  # type: ignore[attr-defined]
    return sm


def test_take_session_snapshot_records_started_at_and_segments(monkeypatch):
    sm = _make_sm_with_segments(monkeypatch, ["s0", "s1"])
    monkeypatch.setattr(time, "time", lambda: 1717_000_000.0)
    sm._take_session_snapshot()  # type: ignore[attr-defined]
    snap = sm.practice_session_snapshot
    assert snap is not None
    assert snap.started_at == 1717_000_000.0
    assert set(snap.segments.keys()) == {"s0", "s1"}


def test_clear_session_snapshot_resets_to_none(monkeypatch):
    sm = _make_sm_with_segments(monkeypatch, ["s0"])
    sm._take_session_snapshot()  # type: ignore[attr-defined]
    assert sm.practice_session_snapshot is not None
    sm._clear_session_snapshot()  # type: ignore[attr-defined]
    assert sm.practice_session_snapshot is None
```

- [ ] **Step 2: Run — Red.**

Run: `python -m pytest tests/unit/test_session_manager_snapshot.py -v`
Expected: FAIL — `_take_session_snapshot` / `_clear_session_snapshot` / `practice_session_snapshot` don't exist on `SessionManager`.

- [ ] **Step 3: Edit `python/spinlab/session_manager.py`.** Add to `SessionManager.__init__` (right after `self.hyper_play_task: asyncio.Task | None = None`):

```python
        # Practice-session snapshot — captured at practice/hyper-play start
        # to anchor the live view's session diffs. Cleared on stop.
        self.practice_session_snapshot = None  # SessionSnapshot | None
```

Then add new helper methods to the class (near other private helpers):

```python
    def _snapshot_inputs(self):
        """Sequence of (SamplerState, episodes) for every active segment.

        Called by _take_session_snapshot. Pulls per-segment SamplerStates from
        the scheduler and observed attempts from the DB. Tests can override.
        """
        from spinlab.estimators.em_suite_sampler import replay_with_history

        if self.scheduler is None or self.state.game_id is None:
            return []
        out = []
        for seg in self.db.get_active_segments(self.state.game_id):
            events = self.scheduler._events_from_rows(  # type: ignore[attr-defined]
                self.db.get_segment_event_rows(seg.id)
            )
            state, _hist = replay_with_history(events)
            episodes = self.db.get_segment_attempts(seg.id)
            out.append((state, episodes))
        return out

    def _take_session_snapshot(self) -> None:
        """Capture an in-memory baseline of every active segment + the route
        aggregate. Called from practice_start / hyper_play_start."""
        import time as _time

        from spinlab.estimators.session_snapshot import snapshot_from_segments

        self.practice_session_snapshot = snapshot_from_segments(
            started_at=_time.time(),
            segments=self._snapshot_inputs(),
        )

    def _clear_session_snapshot(self) -> None:
        self.practice_session_snapshot = None
```

Finally — wire the calls. Find the two methods that begin a practice/hyper-play session (commonly named `start_practice` / `start_hyper_play` or similar; grep first if unsure). Right after the session object is assigned and BEFORE returning success, add `self._take_session_snapshot()`. In the corresponding `stop_*` methods, after the session object is cleared, add `self._clear_session_snapshot()`.

If `_events_from_rows` is not a public Scheduler method, route via the same path the routes use — `python/spinlab/routes/model.py` imports `_events_from_rows` locally; mirror that import:

```python
from spinlab.routes.model import _events_from_rows
```

(verify the import path before commit; if it has moved, search for the function and import from its current home).

- [ ] **Step 4: Run — Green.**

Run: `python -m pytest tests/unit/test_session_manager_snapshot.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the broader SessionManager + practice tests** to confirm no regression in start/stop wiring:

Run: `python -m pytest -m "not emulator" -q tests/unit/test_session_manager.py tests/unit/test_practice.py tests/unit/test_hyper_play.py 2>&1 | tail -20`
Expected: all PASS (no new failures).

- [ ] **Step 6: Commit.**

```bash
git add python/spinlab/session_manager.py tests/unit/test_session_manager_snapshot.py
git commit -m "feat(session-manager): capture practice-session snapshot at start, clear at stop"
```

---

## Task 5: Thread the snapshot through the live routes

**Files:**
- Modify: `python/spinlab/api_schemas.py`
- Modify: `python/spinlab/routes/model.py`
- Modify: `tests/unit/test_api_segments_live.py` (create if missing)

- [ ] **Step 1: Extend the schemas.** In `python/spinlab/api_schemas.py`, change `LiveSegmentViewResponse` (around line 505) to include diff fields:

```python
class LiveSegmentViewResponse(_BaseResponse):
    """Closed-form live-view payload for one segment. See live_segment_view()."""
    segment_id: str
    ready: bool
    expected_episode_ms: float | None
    practice_gain_ms: float | None
    death_rate: float
    floor_ms: float | None
    last_episode_ms: float | None
    last_clean_ms: float | None
    last_deaths: int | None
    last_rank: int | None
    series: list[dict] = []
    n_successes: int
    n_deaths: int
    # Session diffs — None when no active practice session OR either side missing.
    expected_episode_diff_ms: float | None = None
    practice_gain_diff_ms: float | None = None
    floor_diff_ms: float | None = None
    death_rate_diff: float | None = None
```

And `RouteSummaryResponse` (around line 522):

```python
class RouteSummaryResponse(_BaseResponse):
    """Closed-form whole-run aggregate for the route bar. See route_summary()."""
    game_id: str
    exp_run_ms: float | None
    exp_deaths: float | None
    n_estimable: int
    n_skipped: int
    # Session-overlay fields — None when no active practice session.
    session_started_at: float | None = None  # epoch seconds
    exp_run_diff_ms: float | None = None
    exp_deaths_diff: float | None = None
    practice_saved_ms: float | None = None
    floor_improvement_ms: float | None = None  # Σ over segments of (baseline_floor − current_floor), positive = improved
```

- [ ] **Step 2: Update the routes.** In `python/spinlab/routes/model.py`, replace `get_segment_live` (line 228+) with:

```python
@router.get("/segments/{segment_id}/live", response_model=LiveSegmentViewResponse)
def get_segment_live(
    segment_id: str,
    db: Database = Depends(get_db),
    sm: SessionManager = Depends(get_session_manager),
):
    """Closed-form live-view payload for one segment (episode-time trend, floor,
    expected, practice gain, deaths). Pure read; no Monte-Carlo. Diff fields are
    populated only when a practice session is active."""
    from spinlab.estimators.em_suite_sampler import replay_with_history
    from spinlab.estimators.live_view import live_segment_view

    seg = db.get_segment_by_id(segment_id)
    if seg is None:
        logger.warning("get_segment_live: unknown segment %r", segment_id)
        raise HTTPException(status_code=404, detail=f"Segment not found: {segment_id}")

    events = _events_from_rows(db.get_segment_event_rows(segment_id))
    state, _history = replay_with_history(events)
    episodes = db.get_segment_attempts(segment_id)

    snap = sm.practice_session_snapshot
    baseline = snap.segments.get(segment_id) if snap is not None else None
    v = live_segment_view(state, episodes, baseline=baseline)

    return {
        "segment_id": segment_id,
        "ready": v.ready,
        "expected_episode_ms": v.expected_episode_ms,
        "practice_gain_ms": v.practice_gain_ms,
        "death_rate": v.death_rate,
        "floor_ms": v.floor_ms,
        "last_episode_ms": v.last_episode_ms,
        "last_clean_ms": v.last_clean_ms,
        "last_deaths": v.last_deaths,
        "last_rank": v.last_rank,
        "series": v.series,
        "n_successes": state.n_successes,
        "n_deaths": state.n_deaths,
        "expected_episode_diff_ms": v.expected_episode_diff_ms,
        "practice_gain_diff_ms": v.practice_gain_diff_ms,
        "floor_diff_ms": v.floor_diff_ms,
        "death_rate_diff": v.death_rate_diff,
    }
```

And `get_route_summary` (line 261+):

```python
@router.get("/games/{game_id}/live-summary", response_model=RouteSummaryResponse)
def get_route_summary(
    game_id: str,
    db: Database = Depends(get_db),
    sm: SessionManager = Depends(get_session_manager),
):
    """Closed-form whole-run aggregate for the route bar. Pure read; no MC.
    Session-overlay fields populated only when a practice session is active."""
    from spinlab.estimators.em_suite_sampler import replay_with_history
    from spinlab.estimators.live_view import route_summary

    snap = sm.practice_session_snapshot

    states = []
    floor_improvement_ms: float | None = None
    if snap is not None:
        floor_improvement_ms = 0.0
    for seg in db.get_active_segments(game_id):
        events = _events_from_rows(db.get_segment_event_rows(seg.id))
        state, _history = replay_with_history(events)
        states.append(state)
        # Aggregate floor improvement vs baseline. Per-segment improvement =
        # baseline floor − current running-min clean. None on either side -> skip.
        if snap is not None and floor_improvement_ms is not None:
            base = snap.segments.get(seg.id)
            if base is not None and base.floor_ms is not None:
                episodes = db.get_segment_attempts(seg.id)
                cur = _running_min_clean_for_route(episodes)
                if cur is not None:
                    floor_improvement_ms += max(0.0, base.floor_ms - cur)

    s = route_summary(states, baseline=snap.route if snap else None)
    return {
        "game_id": game_id,
        "exp_run_ms": s.exp_run_ms,
        "exp_deaths": s.exp_deaths,
        "n_estimable": s.n_estimable,
        "n_skipped": s.n_skipped,
        "session_started_at": snap.started_at if snap else None,
        "exp_run_diff_ms": s.exp_run_diff_ms,
        "exp_deaths_diff": s.exp_deaths_diff,
        "practice_saved_ms": s.practice_saved_ms,
        "floor_improvement_ms": floor_improvement_ms,
    }


def _running_min_clean_for_route(episodes):
    """Helper for the route-bar floor_improvement aggregation."""
    floor: float | None = None
    for e in episodes:
        if not e.get("completed") or e.get("invalidated"):
            continue
        clean = e.get("clean_tail_ms")
        if clean is None:
            continue
        floor = float(clean) if floor is None else min(floor, float(clean))
    return floor
```

If `get_session_manager` is not already imported at the top, add it (search for how `get_db` is imported and mirror — likely from `_deps` in routes).

- [ ] **Step 3: Write the route test.** Create `tests/unit/test_api_segments_live.py` (or extend `tests/unit/test_routes_model.py` if one exists):

```python
"""GET /api/segments/{id}/live and /api/games/{id}/live-summary — session-diff fields.

When no practice session is active, all *_diff_* and practice_saved_ms /
session_started_at / floor_improvement_ms fields must be None. When a session
is active, the live-view payload's *_diff_* fields populate (specifics tested in
test_live_view.py).
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_segments_live_diffs_null_without_active_session(client: TestClient, seeded_segment_id: str):
    r = client.get(f"/api/segments/{seeded_segment_id}/live")
    assert r.status_code == 200
    body = r.json()
    for k in ("expected_episode_diff_ms", "practice_gain_diff_ms",
              "floor_diff_ms", "death_rate_diff"):
        assert body[k] is None, f"{k} should be None without a session"


def test_route_summary_session_fields_null_without_active_session(client: TestClient, seeded_game_id: str):
    r = client.get(f"/api/games/{seeded_game_id}/live-summary")
    assert r.status_code == 200
    body = r.json()
    for k in ("session_started_at", "exp_run_diff_ms", "exp_deaths_diff",
              "practice_saved_ms", "floor_improvement_ms"):
        assert body[k] is None, f"{k} should be None without a session"
```

If `client` / `seeded_segment_id` / `seeded_game_id` fixtures don't already exist in `tests/unit/conftest.py`, look at how nearby tests for `/api/segments/{id}/progress` set theirs up (`tests/unit/test_routes_model.py` — search for it) and reuse the pattern.

- [ ] **Step 4: Run — Green.**

Run: `python -m pytest tests/unit/test_api_segments_live.py tests/unit/estimators/ -v`
Expected: all PASS.

- [ ] **Step 5: Regen OpenAPI.**

Run: `python scripts/dump_openapi.py`
Expected: `frontend/openapi.json` updated.

- [ ] **Step 6: Commit.**

```bash
git add python/spinlab/api_schemas.py python/spinlab/routes/model.py tests/unit/test_api_segments_live.py frontend/openapi.json
git commit -m "feat(routes): live + live-summary carry session diffs when practice session active"
```

---

## Task 6: Regen FE types + add `LiveSegmentView`/`RouteSummary` re-exports + frontend convenience type

**Files:**
- Modify: `frontend/src/api-types.ts` (generated)
- Modify: `frontend/src/types.ts`

- [ ] **Step 1: Regen.**

Run: `cd frontend && npm run gen-types`
Expected: `api-types.ts` regenerated with the new fields on `LiveSegmentViewResponse` and `RouteSummaryResponse`.

- [ ] **Step 2: Verify the re-exports.** `frontend/src/types.ts` should already re-export `LiveSegmentView` and `RouteSummary` from FE1. Confirm they pick up the new fields automatically (they're typed structurally). No edit needed unless typecheck flags missing names.

- [ ] **Step 3: Typecheck.**

Run: `cd frontend && npm run typecheck`
Expected: clean.

- [ ] **Step 4: Commit.**

```bash
git add frontend/src/api-types.ts frontend/openapi.json
git commit -m "chore(frontend): regen types with live + route session-diff fields"
```

---

## Task 7: `stat-stack.ts` — shared label/value/colored-diff renderer (Red+Green+Commit)

**Files:**
- Create: `frontend/src/stat-stack.ts`
- Create: `frontend/src/stat-stack.test.ts`

- [ ] **Step 1: Write failing test.** Create `frontend/src/stat-stack.test.ts`:

```typescript
import { describe, it, expect } from "vitest";
import { renderStatStack, type StatStack } from "./stat-stack";

describe("renderStatStack", () => {
  it("renders label / value / diff with correct color for improvement", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    const stack: StatStack = {
      label: "Expected",
      value: "18.7s",
      diff: { text: "-2.1s", sign: "good" },
    };
    renderStatStack(host, stack);
    expect(host.querySelector(".ss-label")!.textContent).toBe("Expected");
    expect(host.querySelector(".ss-value")!.textContent).toBe("18.7s");
    const diff = host.querySelector(".ss-diff")!;
    expect(diff.textContent).toBe("-2.1s");
    expect(diff.classList.contains("good")).toBe(true);
  });
  it("hides the diff row entirely when diff is null", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderStatStack(host, { label: "Floor", value: "12.8s", diff: null });
    expect(host.querySelector(".ss-diff")).toBeNull();
  });
  it("renders an em-dash for null value", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderStatStack(host, { label: "Expected", value: null, diff: null });
    expect(host.querySelector(".ss-value")!.textContent).toBe("—");
  });
});
```

- [ ] **Step 2: Run — Red.** `cd frontend && npm test -- stat-stack` → FAIL (module missing).

- [ ] **Step 3: Implement.** Create `frontend/src/stat-stack.ts`:

```typescript
/**
 * Right-aligned label / value / colored-diff vertical stack used by the route
 * bar and segment summary. Pure render — caller formats strings and decides
 * diff sign ("good" = improvement = green; "bad" = regression = red).
 */

export interface StatDiff {
  text: string;
  sign: "good" | "bad" | "neutral";
}

export interface StatStack {
  label: string;
  value: string | null;
  diff: StatDiff | null;
}

export function renderStatStack(host: HTMLElement, s: StatStack): void {
  const label = `<div class="ss-label">${s.label}</div>`;
  const valueText = s.value ?? "—";
  const value = `<div class="ss-value">${valueText}</div>`;
  const diff = s.diff
    ? `<div class="ss-diff ${s.diff.sign}">${s.diff.text}</div>`
    : "";
  host.innerHTML = `<div class="ss-stack">${label}${value}${diff}</div>`;
}
```

- [ ] **Step 4: Run — Green.** `cd frontend && npm test -- stat-stack` → all pass.

- [ ] **Step 5: Commit.**

```bash
git add frontend/src/stat-stack.ts frontend/src/stat-stack.test.ts
git commit -m "feat(live-view): stat-stack — shared label/value/colored-diff renderer"
```

---

## Task 8: `route-bar.ts` (Red+Green+Commit)

**Files:**
- Create: `frontend/src/route-bar.ts`
- Create: `frontend/src/route-bar.test.ts`

- [ ] **Step 1: Write failing test.** Create `frontend/src/route-bar.test.ts`:

```typescript
import { describe, it, expect } from "vitest";
import { renderRouteBar, formatRate, type RouteBarData } from "./route-bar";

const NOW_S = 1717_000_000.0;

const SESSION: RouteBarData = {
  title: "Beto · any%",
  gameId: "g0",
  routeSummary: {
    game_id: "g0",
    exp_run_ms: 115_000.0, exp_deaths: 3.5,
    n_estimable: 8, n_skipped: 0,
    session_started_at: NOW_S - 3600,  // 1h ago
    exp_run_diff_ms: -5_000.0,
    exp_deaths_diff: -0.5,
    practice_saved_ms: 5_000.0,
    floor_improvement_ms: 1_500.0,
  },
  nowSeconds: NOW_S,
};

describe("formatRate", () => {
  it("ms-per-hour computed from saved + elapsed seconds", () => {
    // 5000 ms saved / 1.0 hr = 5000 ms/hr → '5.0s/hr'
    expect(formatRate(5000, 3600)).toBe("5.0s/hr");
  });
  it("returns '—' for null or zero elapsed", () => {
    expect(formatRate(null, 3600)).toBe("—");
    expect(formatRate(5000, 0)).toBe("—");
  });
});

describe("renderRouteBar", () => {
  it("renders title + practice-saved with rate + duration", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderRouteBar(host, SESSION);
    expect(host.querySelector(".rb-title")!.textContent).toContain("Beto");
    const saved = host.querySelector(".rb-saved")!;
    expect(saved.textContent).toContain("Saved 5.0s");
    expect(saved.textContent).toMatch(/1:00:00|01:00:00/);  // session elapsed
  });
  it("renders Exp. Run + Exp. Deaths stat columns with colored diffs", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderRouteBar(host, SESSION);
    const stacks = host.querySelectorAll(".ss-stack");
    expect(stacks.length).toBeGreaterThanOrEqual(2);  // Exp. Run + Exp. Deaths (Floors only when non-zero)
    // -5s on exp_run → improvement → 'good'
    const goods = host.querySelectorAll(".ss-diff.good");
    expect(goods.length).toBeGreaterThanOrEqual(1);
  });
  it("renders Floors column only when floor_improvement_ms > 0", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderRouteBar(host, { ...SESSION, routeSummary: { ...SESSION.routeSummary, floor_improvement_ms: 0 } });
    expect(host.querySelector(".rb-floors")).toBeNull();
  });
  it("renders 'n of m segments estimable' when n_skipped > 0", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderRouteBar(host, { ...SESSION, routeSummary: { ...SESSION.routeSummary, n_skipped: 4 } });
    expect((host.textContent ?? "").toLowerCase()).toContain("estimable");
  });
  it("hides Practice saved when no active session", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderRouteBar(host, { ...SESSION, routeSummary: {
      ...SESSION.routeSummary,
      session_started_at: null, practice_saved_ms: null,
      exp_run_diff_ms: null, exp_deaths_diff: null, floor_improvement_ms: null,
    } });
    expect(host.querySelector(".rb-saved")).toBeNull();
  });
});
```

- [ ] **Step 2: Run — Red.** `cd frontend && npm test -- route-bar` → FAIL (module missing).

- [ ] **Step 3: Implement.** Create `frontend/src/route-bar.ts`:

```typescript
/**
 * Route bar — top section of the live practice view.
 *
 * Stable across segment switches. Renders: title (game · category), Practice
 * saved + rate + session-elapsed (live ticking via caller-supplied nowSeconds),
 * stat columns (Floors* · Exp. Run · Exp. Deaths) each as a stat-stack with
 * colored diffs vs session start. * = render only when floor improvement non-zero.
 * Pure render — no fetch. The coordinator (live-view.ts) supplies nowSeconds
 * and re-calls renderRouteBar on a setInterval tick.
 */
import { formatTime, elapsedStr } from "./format";
import { renderStatStack, type StatStack, type StatDiff } from "./stat-stack";
import type { RouteSummary } from "./types";

const MS_PER_HOUR = 3_600_000;

export interface RouteBarData {
  title: string;
  gameId: string;
  routeSummary: RouteSummary;
  nowSeconds: number;  // wall clock, supplied by caller (live ticking)
}

/** ms saved / hours elapsed → 'X.Xs/hr' (lower bound on resolution; '—' on null/zero). */
export function formatRate(savedMs: number | null, elapsedSec: number): string {
  if (savedMs == null || elapsedSec <= 0) return "—";
  const msPerHour = savedMs / (elapsedSec / 3600);
  // formatTime expects ms; reuse for consistency.
  return (formatTime(msPerHour) ?? "—") + "/hr";
}

function diffMs(deltaMs: number | null): StatDiff | null {
  // Lower expected/run → improvement → 'good'. So delta<0 means improvement.
  if (deltaMs == null) return null;
  if (deltaMs === 0) return { text: "0", sign: "neutral" };
  const sign: StatDiff["sign"] = deltaMs < 0 ? "good" : "bad";
  const text = (deltaMs < 0 ? "-" : "+") + (formatTime(Math.abs(deltaMs)) ?? "—");
  return { text, sign };
}

function diffDeaths(delta: number | null): StatDiff | null {
  if (delta == null) return null;
  if (delta === 0) return { text: "0", sign: "neutral" };
  const sign: StatDiff["sign"] = delta < 0 ? "good" : "bad";
  return { text: (delta < 0 ? "" : "+") + delta.toFixed(1), sign };
}

export function renderRouteBar(host: HTMLElement, data: RouteBarData): void {
  const rs = data.routeSummary;
  const sessionActive = rs.session_started_at != null;
  const elapsedSec = sessionActive ? Math.max(0, data.nowSeconds - rs.session_started_at!) : 0;

  const savedBlock = sessionActive
    ? `<div class="rb-saved">Saved ${formatTime(rs.practice_saved_ms) ?? "—"} ·
        ${formatRate(rs.practice_saved_ms, elapsedSec)} ·
        ${elapsedStr(rs.session_started_at!)}</div>`
    : "";

  const skippedBlock = rs.n_skipped > 0
    ? `<div class="rb-skipped dim">${rs.n_estimable} of ${rs.n_estimable + rs.n_skipped} segments estimable</div>`
    : "";

  const stacks: { html: string; key: string }[] = [];

  if ((rs.floor_improvement_ms ?? 0) > 0) {
    stacks.push({
      key: "rb-floors",
      html: stackHtml("rb-floors", {
        label: "Floors",
        value: formatTime(rs.floor_improvement_ms),
        diff: null,  // always positive when shown
      }),
    });
  }
  stacks.push({
    key: "rb-exp-run",
    html: stackHtml("rb-exp-run", {
      label: "Exp. Run",
      value: formatTime(rs.exp_run_ms),
      diff: diffMs(rs.exp_run_diff_ms),
    }),
  });
  stacks.push({
    key: "rb-exp-deaths",
    html: stackHtml("rb-exp-deaths", {
      label: "Exp. Deaths",
      value: rs.exp_deaths != null ? rs.exp_deaths.toFixed(1) : null,
      diff: diffDeaths(rs.exp_deaths_diff),
    }),
  });

  host.innerHTML = `
    <div class="rb-root">
      <div class="rb-left">
        <div class="rb-title">${escapeHtml(data.title)}</div>
        ${savedBlock}
        ${skippedBlock}
      </div>
      <div class="rb-stats">${stacks.map(s => s.html).join("")}</div>
    </div>
  `;
}

function stackHtml(slotClass: string, s: StatStack): string {
  // Render the stack into a detached div so we can grab its innerHTML — keeps
  // renderStatStack as the single source of truth for the stat markup.
  const tmp = document.createElement("div");
  tmp.className = slotClass;
  renderStatStack(tmp, s);
  return tmp.outerHTML;
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c] as string));
}
```

- [ ] **Step 4: Run — Green.** `cd frontend && npm test -- route-bar` → all pass.

- [ ] **Step 5: Commit.**

```bash
git add frontend/src/route-bar.ts frontend/src/route-bar.test.ts
git commit -m "feat(live-view): route-bar — title, Practice saved, Exp.Run/Deaths/Floors stacks"
```

---

## Task 9: `segment-summary.ts` (Red+Green+Commit)

**Files:**
- Create: `frontend/src/segment-summary.ts`
- Create: `frontend/src/segment-summary.test.ts`

- [ ] **Step 1: Write failing test.** Create `frontend/src/segment-summary.test.ts`:

```typescript
import { describe, it, expect } from "vitest";
import { renderSegmentSummary, ordinal, type SegmentSummaryData } from "./segment-summary";
import type { LiveSegmentView } from "./types";

const READY: LiveSegmentView = {
  segment_id: "s1", ready: true,
  expected_episode_ms: 21_800, practice_gain_ms: 500, death_rate: 0.62,
  floor_ms: 12_800, last_episode_ms: 16_800, last_clean_ms: 13_600,
  last_deaths: 1, last_rank: 2,
  series: [] as unknown as Record<string, never>[],
  n_successes: 6, n_deaths: 5,
  expected_episode_diff_ms: -2_100,
  practice_gain_diff_ms: -100,
  floor_diff_ms: -800,
  death_rate_diff: -0.08,
};

describe("ordinal", () => {
  it("formats English ordinals", () => {
    expect(ordinal(1)).toBe("1st");
    expect(ordinal(2)).toBe("2nd");
    expect(ordinal(3)).toBe("3rd");
    expect(ordinal(4)).toBe("4th");
    expect(ordinal(11)).toBe("11th");
    expect(ordinal(22)).toBe("22nd");
  });
});

describe("renderSegmentSummary", () => {
  it("renders segment name + 4-column stat cluster with colored diffs", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderSegmentSummary(host, { name: "L1 entrance → goal", live: READY });
    expect(host.querySelector(".sg-name")!.textContent).toContain("L1 entrance");
    const stacks = host.querySelectorAll(".ss-stack");
    expect(stacks.length).toBe(4);  // Practice + Floor + Expected + Deaths
    expect(host.querySelectorAll(".ss-diff.good").length).toBeGreaterThanOrEqual(1);
  });
  it("renders the 'last completion' headline with rank + decomposition", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderSegmentSummary(host, { name: "L1", live: READY });
    const headline = host.querySelector(".sg-headline")!;
    expect(headline.textContent).toContain("last completion");
    expect(headline.textContent).toContain("16.8s");
    expect(headline.textContent).toContain("2nd best");
    expect(headline.textContent).toContain("1 death");
    expect(headline.textContent).toContain("13.6s clean");
  });
  it("renders 'no completed runs yet' when last_episode_ms is null", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderSegmentSummary(host, { name: "L1", live: { ...READY, last_episode_ms: null, last_rank: null, last_deaths: null, last_clean_ms: null } });
    expect(host.querySelector(".sg-headline")!.textContent!.toLowerCase()).toContain("no completed");
  });
  it("hides Floor column when floor_diff_ms is null or zero", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderSegmentSummary(host, { name: "L1", live: { ...READY, floor_diff_ms: 0 } });
    expect(host.querySelector(".sg-floor")).toBeNull();
  });
  it("renders 'not enough data' when ready=false", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderSegmentSummary(host, { name: "L1", live: { ...READY, ready: false } });
    expect(host.textContent!.toLowerCase()).toContain("not enough");
  });
});
```

- [ ] **Step 2: Run — Red.** `cd frontend && npm test -- segment-summary` → FAIL.

- [ ] **Step 3: Implement.** Create `frontend/src/segment-summary.ts`:

```typescript
/**
 * Segment summary — middle section of the live practice view.
 *
 * Renders the current segment's name + a 4-column stat cluster
 * (Practice · Floor* · Expected · Deaths) with label/value/colored-diff
 * stacks, plus a "last completion" headline showing the most-recent episode
 * time, its rank among completions, and the per-completion decomposition
 * ('N deaths · X.Xs clean'). * = Floor column shown only when floor_diff_ms
 * is non-zero (a session improvement to highlight).
 */
import { formatTime } from "./format";
import { renderStatStack, type StatDiff, type StatStack } from "./stat-stack";
import type { LiveSegmentView } from "./types";

export interface SegmentSummaryData {
  name: string;
  live: LiveSegmentView;
}

/** English ordinal for the last-completion rank. */
export function ordinal(n: number): string {
  const j = n % 10, k = n % 100;
  if (k >= 11 && k <= 13) return `${n}th`;
  if (j === 1) return `${n}st`;
  if (j === 2) return `${n}nd`;
  if (j === 3) return `${n}rd`;
  return `${n}th`;
}

function diffMs(deltaMs: number | null): StatDiff | null {
  if (deltaMs == null || deltaMs === 0) return deltaMs == null ? null : { text: "0", sign: "neutral" };
  const sign: StatDiff["sign"] = deltaMs < 0 ? "good" : "bad";
  const text = (deltaMs < 0 ? "-" : "+") + (formatTime(Math.abs(deltaMs)) ?? "—");
  return { text, sign };
}

function diffRate(delta: number | null): StatDiff | null {
  if (delta == null || delta === 0) return delta == null ? null : { text: "0", sign: "neutral" };
  const sign: StatDiff["sign"] = delta < 0 ? "good" : "bad";
  return { text: (delta < 0 ? "" : "+") + (delta * 100).toFixed(0) + "%", sign };
}

function practiceStack(gainMs: number | null): StatStack {
  // Practice gain has no diff slot; value colored by sign at the value level.
  const text = gainMs == null ? null : (gainMs > 0 ? "+" : "") + (formatTime(gainMs) ?? "—");
  return { label: "Practice", value: text, diff: null };
}

function stackHtml(slotClass: string, s: StatStack): string {
  const tmp = document.createElement("div");
  tmp.className = slotClass;
  renderStatStack(tmp, s);
  return tmp.outerHTML;
}

export function renderSegmentSummary(host: HTMLElement, data: SegmentSummaryData): void {
  const v = data.live;

  if (!v.ready) {
    const needS = Math.max(0, 2 - v.n_successes);
    const needD = Math.max(0, 2 - v.n_deaths);
    const parts: string[] = [];
    if (needS) parts.push(`${needS} more clear${needS === 1 ? "" : "s"}`);
    if (needD) parts.push(`${needD} more death${needD === 1 ? "" : "s"}`);
    host.innerHTML = `
      <div class="sg-root">
        <div class="sg-name">${escapeHtml(data.name)}</div>
        <div class="sg-headline sg-empty">Not enough data yet — need ${parts.join(" and ") || "more attempts"}</div>
      </div>
    `;
    return;
  }

  const stacks: string[] = [];
  stacks.push(stackHtml("sg-practice", practiceStack(v.practice_gain_ms)));
  if ((v.floor_diff_ms ?? 0) !== 0 && v.floor_ms != null) {
    stacks.push(stackHtml("sg-floor", {
      label: "Floor",
      value: formatTime(v.floor_ms),
      diff: diffMs(v.floor_diff_ms),
    }));
  }
  stacks.push(stackHtml("sg-expected", {
    label: "Expected",
    value: formatTime(v.expected_episode_ms),
    diff: diffMs(v.expected_episode_diff_ms),
  }));
  stacks.push(stackHtml("sg-deaths", {
    label: "Deaths",
    value: (v.death_rate * 100).toFixed(0) + "%",
    diff: diffRate(v.death_rate_diff),
  }));

  const headline = v.last_episode_ms == null
    ? `<div class="sg-headline sg-empty">No completed runs yet</div>`
    : `<div class="sg-headline">
         <div class="sg-headline-label">last completion</div>
         <div class="sg-headline-value">${formatTime(v.last_episode_ms)}</div>
         <div class="sg-headline-rank">${v.last_rank != null ? ordinal(v.last_rank) + " best" : ""}</div>
         <div class="sg-headline-decomp">${v.last_deaths ?? 0} death${(v.last_deaths ?? 0) === 1 ? "" : "s"} · ${formatTime(v.last_clean_ms) ?? "—"} clean</div>
       </div>`;

  host.innerHTML = `
    <div class="sg-root">
      <div class="sg-header">
        <div class="sg-name">${escapeHtml(data.name)}</div>
        <div class="sg-stats">${stacks.join("")}</div>
      </div>
      ${headline}
    </div>
  `;
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c] as string));
}
```

- [ ] **Step 4: Run — Green.** `cd frontend && npm test -- segment-summary` → all pass.

- [ ] **Step 5: Commit.**

```bash
git add frontend/src/segment-summary.ts frontend/src/segment-summary.test.ts
git commit -m "feat(live-view): segment-summary — header + 4-col stats + last-completion headline"
```

---

## Task 10: `live-view.ts` coordinator (Red+Green+Commit)

**Files:**
- Create: `frontend/src/live-view.ts`
- Create: `frontend/src/live-view.test.ts`

- [ ] **Step 1: Write failing test.** Create `frontend/src/live-view.test.ts`:

```typescript
import { describe, it, expect, vi } from "vitest";
import { loadAndRenderLiveView, destroyLiveView } from "./live-view";

// Mock fetchJSON so tests don't hit the network.
vi.mock("./api", () => ({
  fetchJSON: vi.fn(async (url: string) => {
    if (url.includes("/live-summary")) {
      return {
        game_id: "g0", exp_run_ms: 115_000, exp_deaths: 3.5,
        n_estimable: 8, n_skipped: 0,
        session_started_at: null,
        exp_run_diff_ms: null, exp_deaths_diff: null,
        practice_saved_ms: null, floor_improvement_ms: null,
      };
    }
    return {
      segment_id: "s0", ready: true,
      expected_episode_ms: 21_800, practice_gain_ms: 500, death_rate: 0.62,
      floor_ms: 12_800, last_episode_ms: 16_800, last_clean_ms: 13_600,
      last_deaths: 1, last_rank: 2,
      series: [{episode_ms: 16800, deaths: 1, clean_ms: 13600, running_floor_ms: 12800}],
      n_successes: 6, n_deaths: 5,
      expected_episode_diff_ms: null, practice_gain_diff_ms: null,
      floor_diff_ms: null, death_rate_diff: null,
    };
  }),
}));

function setupHosts() {
  document.body.innerHTML = `
    <div id="rb"></div><div id="ss"></div><div id="gs"></div>
  `;
  return {
    routeBar: document.getElementById("rb")!,
    segmentSummary: document.getElementById("ss")!,
    graph: document.getElementById("gs")!,
  };
}

describe("loadAndRenderLiveView", () => {
  it("populates all three hosts on success", async () => {
    const hosts = setupHosts();
    await loadAndRenderLiveView({
      segmentId: "s0", gameId: "g0", segmentName: "L1",
      title: "Beto · any%", hosts,
    });
    expect(hosts.routeBar.innerHTML).toContain("Beto");
    expect(hosts.segmentSummary.innerHTML).toContain("L1");
    expect(hosts.graph.querySelector("svg")).not.toBeNull();
  });
  it("renders inline error per host on fetch failure", async () => {
    const api = await import("./api");
    (api.fetchJSON as ReturnType<typeof vi.fn>).mockImplementationOnce(() => Promise.reject(new Error("boom")));
    const hosts = setupHosts();
    await loadAndRenderLiveView({
      segmentId: "s0", gameId: "g0", segmentName: "L1",
      title: "Beto · any%", hosts,
    });
    // Whichever host owned the failed call shows the inline error; others still render.
    const combined = hosts.routeBar.innerHTML + hosts.segmentSummary.innerHTML;
    expect(combined.toLowerCase()).toContain("unavailable");
  });
});

describe("destroyLiveView", () => {
  it("clears the elapsed-tick timer", async () => {
    const hosts = setupHosts();
    await loadAndRenderLiveView({
      segmentId: "s0", gameId: "g0", segmentName: "L1",
      title: "Beto · any%", hosts,
    });
    destroyLiveView();
    // No assertion beyond 'no throw'; the next test must start clean.
  });
});
```

- [ ] **Step 2: Run — Red.** `cd frontend && npm test -- live-view` → FAIL.

- [ ] **Step 3: Implement.** Create `frontend/src/live-view.ts`:

```typescript
/**
 * Live practice view coordinator. Fetches /segments/{id}/live and
 * /games/{id}/live-summary in parallel, renders the route bar, segment summary,
 * and episode graph. Runs a 1s setInterval that re-renders only the route bar
 * with an updated nowSeconds — keeps Practice-saved-rate + session elapsed ticking
 * without re-fetching. Per-SSE-push callers re-invoke loadAndRenderLiveView,
 * which cancels the old tick and starts a fresh one.
 */
import { fetchJSON } from "./api";
import { renderRouteBar, type RouteBarData } from "./route-bar";
import { renderSegmentSummary } from "./segment-summary";
import { renderEpisodeGraph } from "./episode-graph";
import type { LiveSegmentView, RouteSummary } from "./types";

const TICK_INTERVAL_MS = 1000;

export interface LiveViewHosts {
  routeBar: HTMLElement;
  segmentSummary: HTMLElement;
  graph: HTMLElement;
}

export interface LiveViewLoadOptions {
  segmentId: string;
  gameId: string;
  segmentName: string;
  title: string;
  hosts: LiveViewHosts;
}

let _tickHandle: ReturnType<typeof setInterval> | null = null;
let _lastRouteData: RouteBarData | null = null;
let _lastHosts: LiveViewHosts | null = null;

export async function loadAndRenderLiveView(opts: LiveViewLoadOptions): Promise<void> {
  destroyLiveView();
  _lastHosts = opts.hosts;

  const [live, summary] = await Promise.all([
    fetchJSON<LiveSegmentView>(`/api/segments/${encodeURIComponent(opts.segmentId)}/live`)
      .catch((e: unknown) => { renderError(opts.hosts.segmentSummary, "segment live", e); return null; }),
    fetchJSON<RouteSummary>(`/api/games/${encodeURIComponent(opts.gameId)}/live-summary`)
      .catch((e: unknown) => { renderError(opts.hosts.routeBar, "route summary", e); return null; }),
  ]);

  if (summary) {
    _lastRouteData = {
      title: opts.title, gameId: opts.gameId,
      routeSummary: summary, nowSeconds: Date.now() / 1000,
    };
    renderRouteBar(opts.hosts.routeBar, _lastRouteData);
  }
  if (live) {
    renderSegmentSummary(opts.hosts.segmentSummary, { name: opts.segmentName, live });
    renderEpisodeGraph(opts.hosts.graph, live);
  }

  _tickHandle = setInterval(tickRouteBar, TICK_INTERVAL_MS);
}

function tickRouteBar(): void {
  if (!_lastRouteData || !_lastHosts) return;
  _lastRouteData = { ..._lastRouteData, nowSeconds: Date.now() / 1000 };
  renderRouteBar(_lastHosts.routeBar, _lastRouteData);
}

export function destroyLiveView(): void {
  if (_tickHandle != null) { clearInterval(_tickHandle); _tickHandle = null; }
  _lastRouteData = null;
  if (_lastHosts) {
    _lastHosts.routeBar.innerHTML = "";
    _lastHosts.segmentSummary.innerHTML = "";
    _lastHosts.graph.innerHTML = "";
    _lastHosts = null;
  }
}

function renderError(host: HTMLElement, what: string, err: unknown): void {
  host.innerHTML = `<div class="lv-error dim">${what} unavailable: ${String(err)}</div>`;
}
```

- [ ] **Step 4: Run — Green.** `cd frontend && npm test -- live-view` → all pass.

- [ ] **Step 5: Commit.**

```bash
git add frontend/src/live-view.ts frontend/src/live-view.test.ts
git commit -m "feat(live-view): coordinator — parallel fetch + render + 1s elapsed tick"
```

---

## Task 11: Replace the legacy practice card markup

**Files:**
- Modify: `frontend/index.html`

- [ ] **Step 1: Edit `frontend/index.html`.** Replace lines 44-71 (the entire `<div id="practice-card">` block) with:

```html
      <!-- Live practice view (hidden when not practicing) -->
      <div id="practice-card" style="display:none">
        <div id="live-route-bar" class="lv-route-bar"></div>
        <div id="live-segment-summary" class="lv-segment-summary"></div>
        <div id="live-graph-slot" class="lv-graph-slot"></div>
        <div id="improvement-view" class="lv-improvement"></div>
        <div id="savings-panel" class="savings-panel" style="display:none">
          <div class="savings-label">Time saved this session</div>
          <div class="savings-values">
            <span id="savings-total" class="savings-value"></span>
            <span class="savings-sep">·</span>
            <span id="savings-clean" class="savings-value"></span>
          </div>
        </div>
        <h3>Recent</h3>
        <ul id="recent"></ul>
        <div class="practice-footer">
          <span id="session-stats" class="dim"></span>
        </div>
        <div class="allocator-weights" id="allocator-weights">
          <div class="weight-slider" id="weight-slider"></div>
          <div class="weight-legend" id="weight-legend"></div>
        </div>
        <div id="em-suite-panel" class="em-suite-panel"></div>
      </div>
```

Note: the legacy `<div class="card">` wrapper, `#current-goal`, `#current-attempts`, and `#insight` are deleted. The new `#live-*` hosts mount above the improvement view.

- [ ] **Step 2: Build to verify markup parses.**

Run: `cd frontend && npm run build`
Expected: clean build.

- [ ] **Step 3: Commit.**

```bash
git add frontend/index.html
git commit -m "feat(practice-card): replace legacy insight card with live-view hosts"
```

---

## Task 12: Wire live view into `model.ts`, delete `renderPracticeInsight`

**Files:**
- Modify: `frontend/src/model.ts`
- Modify: `frontend/src/model-render.ts`

- [ ] **Step 1: Edit `frontend/src/model.ts`.** Update imports — remove `renderPracticeInsight` from the `model-render` import:

```typescript
import {
  renderWeightSlider,
  renderModelTable,
  renderRecentList,
  renderSessionStats,
  renderSavingsPanel,
} from "./model-render";
```

Add the live-view import below the improvement-view import:

```typescript
import { loadAndRenderLiveView, destroyLiveView } from "./live-view";
```

Replace `updatePracticeCard` (lines 72-111) with:

```typescript
export function updatePracticeCard(data: AppState): void {
  const card = document.getElementById("practice-card") as HTMLElement;
  if ((data.mode !== "practice" && data.mode !== "hyper_play") || !data.current_segment) {
    card.style.display = "none";
    destroyEmSuitePanel();
    destroyImprovementView();
    destroyLiveView();
    return;
  }
  card.style.display = "";
  updateSavingsPanel(data.session);

  // Live view (route bar + segment summary + episode graph). Fetches both
  // /segments/{id}/live and /games/{id}/live-summary in parallel.
  const cs = data.current_segment;
  const game = data.game;  // { id, description } | null per AppState
  if (game) {
    const title = `${game.description ?? game.id} · ${data.category ?? ""}`.trim();
    void loadAndRenderLiveView({
      segmentId: cs.id,
      gameId: game.id,
      segmentName: cs.description ?? cs.id,
      title,
      hosts: {
        routeBar: document.getElementById("live-route-bar")!,
        segmentSummary: document.getElementById("live-segment-summary")!,
        graph: document.getElementById("live-graph-slot")!,
      },
    });
  }

  renderRecentList(document.getElementById("recent")!, data.recent, patchAttemptInvalidated);
  renderSessionStats(data.session);

  const weightsEl = document.getElementById("allocator-weights") as HTMLElement;
  if (weightsEl) {
    weightsEl.style.display = data.mode === "hyper_play" ? "none" : "";
  }
  if (data.allocator_weights && data.mode !== "hyper_play") {
    _currentWeights = { ...data.allocator_weights };
    renderWeightSlider(data.allocator_weights, (next) => {
      _currentWeights = next;
      postAllocatorWeights(next);
    });
  }

  const improvementHost = document.getElementById("improvement-view") as HTMLElement;
  if (improvementHost) {
    void loadAndRenderImprovementView(cs.id, improvementHost);
  }

  const emSuiteHost = document.getElementById("em-suite-panel") as HTMLElement;
  if (emSuiteHost) {
    void loadAndRenderEmSuitePanel(cs.id, emSuiteHost);
  }
}
```

**IMPORTANT — verify these AppState field names before committing**:
- `data.game` — check `frontend/src/types.ts` / `AppState` for the actual game-context field name. If it's `data.game_id` + `data.game_description` (or similar flat fields), adjust accordingly. The `title` string must work either way.
- `data.category` — likewise; the category may live elsewhere (`game.category`?).
- `cs.description` — current segment's display name; mirror what `segmentName(cs)` already uses (`format.ts`).

If a field name is wrong, the test in Task 13 will surface it. Don't guess silently — read `AppState` first.

- [ ] **Step 2: Delete `renderPracticeInsight` from `frontend/src/model-render.ts`.** Remove lines 233-252 (`renderPracticeInsight` and its exports).

If any other file in `frontend/src/` still imports `renderPracticeInsight`, delete those imports. Grep with: `grep -rn "renderPracticeInsight" frontend/src/`.

- [ ] **Step 3: Typecheck.**

Run: `cd frontend && npm run typecheck`
Expected: clean.

- [ ] **Step 4: Run frontend tests.**

Run: `cd frontend && npm test`
Expected: all PASS (any tests that asserted on `#current-goal`/`#insight` need updating — fix as needed; replace with assertions against the new `#live-*` hosts).

- [ ] **Step 5: Build.**

Run: `cd frontend && npm run build`
Expected: clean build.

- [ ] **Step 6: Commit.**

```bash
git add frontend/src/model.ts frontend/src/model-render.ts python/spinlab/static/
git commit -m "feat(model): mount live view + delete legacy renderPracticeInsight card"
```

---

## Task 13: CSS — 3-section layout, aligned stat columns, colored diffs

**Files:**
- Modify: `frontend/style.css`

- [ ] **Step 1: Append styles** at the end of `frontend/style.css`:

```css
/* Live practice view — sections, stat stacks, colored diffs.
   Aligns route-bar + segment-header stat columns by visual position
   (right-aligned, identical column widths). */

.lv-route-bar, .lv-segment-summary, .lv-graph-slot, .lv-improvement {
  margin: 0 0 12px 0;
}

/* Route bar */
.rb-root {
  display: flex; justify-content: space-between; align-items: flex-start;
  gap: 16px; padding: 8px 12px;
  background: var(--bg-elev-1); border-radius: 6px;
}
.rb-left { display: flex; flex-direction: column; gap: 2px; }
.rb-title { font-weight: 600; font-size: 14px; }
.rb-saved { font-size: 12px; color: var(--text-dim); }
.rb-skipped { font-size: 11px; }
.rb-stats { display: flex; gap: 18px; }

/* Segment summary */
.sg-root { padding: 8px 12px; background: var(--bg-elev-1); border-radius: 6px; }
.sg-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.sg-name { font-weight: 600; font-size: 14px; }
.sg-stats { display: flex; gap: 18px; }
.sg-headline {
  display: grid; grid-template-columns: auto 1fr auto auto;
  gap: 6px 10px; align-items: baseline; margin-top: 8px;
}
.sg-headline-label { font-size: 11px; color: var(--text-dim); text-transform: uppercase; }
.sg-headline-value { font-size: 28px; font-weight: 600; }
.sg-headline-rank { font-size: 12px; color: var(--text-dim); }
.sg-headline-decomp { font-size: 12px; color: var(--text-dim); }
.sg-empty { color: var(--text-dim); font-size: 13px; padding: 8px 0; }

/* Stat stack (shared by route bar + segment summary) */
.ss-stack {
  display: flex; flex-direction: column; align-items: flex-end;
  min-width: 64px;
}
.ss-label { font-size: 10px; color: var(--text-dim); text-transform: uppercase; }
.ss-value { font-size: 16px; font-weight: 600; line-height: 1.2; }
.ss-diff { font-size: 11px; line-height: 1.2; }
.ss-diff.good { color: var(--green); }
.ss-diff.bad { color: var(--red); }
.ss-diff.neutral { color: var(--text-dim); }

/* Graph slot — wraps episode-graph.ts output */
.lv-graph-slot { background: var(--bg-elev-1); border-radius: 6px; padding: 8px; }

/* Live view inline errors */
.lv-error { padding: 8px; font-size: 12px; }
```

- [ ] **Step 2: Build + verify.**

Run: `cd frontend && npm run build`
Expected: clean build into `python/spinlab/static/`.

- [ ] **Step 3: Commit.**

```bash
git add frontend/style.css python/spinlab/static/
git commit -m "style(live-view): 3-section layout, aligned stat columns, colored diffs"
```

---

## Task 14: Verification — full suites + smoke

**Files:** none.

- [ ] **Step 1: Frontend full check.**

Run: `cd frontend && npm run typecheck && npm test && npm run build`
Expected: all green, clean build.

- [ ] **Step 2: Fast Python suite.** (Frontend smoke depends on the built bundle from Step 1.)

Run: `python -m pytest -m "not emulator" -q`
Expected: green, no new failures. Address any smoke test that asserted on `#current-goal` / `#insight`: replace with assertions against `#live-route-bar` / `#live-segment-summary` / `#live-graph-slot`.

- [ ] **Step 3: Pyright + ruff.**

Run: `npx pyright python/spinlab/estimators/session_snapshot.py python/spinlab/estimators/live_view.py python/spinlab/session_manager.py python/spinlab/routes/model.py`
Run: `ruff check python/spinlab/estimators/ python/spinlab/session_manager.py python/spinlab/routes/model.py`
Expected: no new errors (clean if practical; otherwise no regressions vs main baseline — pre-existing counts are tracked in [[project_test_reliability_known_issues]]).

- [ ] **Step 4: Full suite gate.**

Run: `python -m pytest`
Expected: full unfiltered suite green, zero skips (per CLAUDE.md and [[feedback_run_all_tests]] / [[feedback_run_emulator_tests]] / [[feedback_red_baseline_habit]]). If any emulator test skips or fails, surface it before declaring done. Pre-existing failures noted in [[project_test_reliability_known_issues]] don't excuse new ones.

- [ ] **Step 5: Live smoke (optional but recommended).** Launch the dashboard, do a few attempts on a gated segment in Practice mode, eyeball:
  - Route bar renders the title; Practice saved ticks live; Exp. Run / Exp. Deaths display with colored diffs after a couple of attempts shift them.
  - Segment summary header + 4-column stat cluster aligns under the route bar; "last completion · rank · decomposition" headline updates.
  - Episode-time graph (from FE1) shows + the diagonal floor + per-completion death counts.
  - Legacy `#insight` / `#current-goal` / `#current-attempts` are gone.
  - `formatRate` shows `Xs/hr` not `NaN`/`Infinity` (depends on enough wall-clock to register).

If the live smoke turns up a bug, log it as a follow-up rather than scope-creep this plan. The climbing dot (frame-by-frame liveliness) and flash-on-change are deliberately deferred to D-Live-FE3.

---

## Self-review notes

- **Spec coverage:**
  - § Two time concepts → no code (modeling spine; reducers already separate `episode_ms` / `clean_ms`).
  - § Architecture (3 sections + swappable slot) → Tasks 11 (HTML hosts) + 12 (mount) + 13 (CSS).
  - § Aligned stat columns → Task 13 (CSS `.ss-stack`/`.rb-stats`/`.sg-stats` shared widths).
  - § 1. Route bar → Task 8 (`route-bar.ts`).
  - § 2. Segment summary → Task 9 (`segment-summary.ts`).
  - § 3. Graph #1 — episode-time trend → already shipped in FE1; mounted in Task 10/12.
  - § Session overlay (vertical line on graph) → **DEFERRED to D-Live-FE3** alongside the climbing dot, since both layer animation/lines onto the graph and need extra graph signature. Diffs/colored stats are in v1 (Tasks 8/9).
  - § Liveliness (climbing dot, flash-on-change) → **DEFERRED to D-Live-FE3** (climbing dot needs an attempt-start timestamp the current `AppState.current_segment` may not carry; flash-on-change is explicitly v2 per spec).
  - § Computation Sources table → BE-2 (Tasks 1-5) populates every diff field via closed-form `current − baseline`; the FE reads, never computes.
  - § Removed (legacy `renderPracticeInsight`) → Task 12.
  - § Testing → Tasks 1, 3, 5 (BE unit + route tests), Tasks 7-10 (FE vitest), Task 14 step 2 (Playwright smoke updates).
  - § Phasing v1 = everything except flash-on-change → Floor stat IS v1 (Tasks 8/9 render it conditionally on non-zero — cheap, already exposed by BE-2).
- **Placeholder scan:** every code step has executable code or a literal command; no TODO/TBD/"appropriate" hedges. The one judgment call in Task 12 Step 1 (verifying `data.game` / `data.category` field names against AppState) is explicit and bounded with a fallback ("read AppState; don't guess").
- **Type consistency:** `SegmentBaseline` / `RouteBaseline` / `SessionSnapshot` named identically in Tasks 1/2/3/4/5; `LiveSegmentView` diff fields (`expected_episode_diff_ms`, `practice_gain_diff_ms`, `floor_diff_ms`, `death_rate_diff`) used identically in BE schema (Task 5), reducer (Task 3), and FE consumers (Tasks 9/10). `RouteSummary` extras (`exp_run_diff_ms`, `exp_deaths_diff`, `practice_saved_ms`, `session_started_at`, `floor_improvement_ms`) likewise consistent across Tasks 3/5/8/10. `renderStatStack` / `StatStack` / `StatDiff` interface stable across Tasks 7/8/9.
- **No fudged values:** every diff is `current − baseline` of a quantity that already has a principled closed form in the sampler; `formatRate` returns `"—"` for null/zero rather than fabricating; segment summary renders "Not enough data" / "No completed runs yet" when the gate fails or no completions exist — never a fake number. Floor-improvement aggregation in Task 5 uses `max(0, baseline_floor − current_floor)` because a NEW floor only ever drops (negative means data error — we floor-clip to 0 rather than show a regression that the model cannot produce); not a fudge, an invariant.
- **Reuse:** `formatTime` / `elapsedStr` from `format.ts`; `renderStatStack` shared between route-bar and segment-summary; `renderEpisodeGraph` from FE1 unchanged; `fetchJSON` from `api.ts`; the snapshot reuses `expected_episode_time_scalar` / `p_die_ema` / `_gate_passes` (no duplication of model math).
- **Atomic commits:** 12 task-level commits (one per Task 1-13 that changes code), one verification step (Task 14, no commit) — frequent commits per the plan-writing guidance.
- **Deferred-on-purpose (not skipped):** climbing dot, session-start vertical line on the graph, flash-on-change — all called out explicitly as D-Live-FE3.
- **Related memory:** [[project_practice_ui_overhaul]], [[project_model_principles]] (no-fudge stance honored), [[feedback_concrete_plans_need_concrete_reads]] (every concrete reference verified against current files), [[feedback_anchor_questions_imply_tasks]] (the "FE2 + BE-2 together" scope decision maps cleanly to Tasks 1-13).

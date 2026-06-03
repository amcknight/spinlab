# D-Live-BE: Closed-Form Live-View Payloads — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the read-only backend that feeds the redesigned live practice view — a per-segment payload and a route-level aggregate — entirely from **exact closed forms** (no Monte-Carlo, no CRN).

**Architecture:** Two pure reducers over data the sampler + DB already expose, mirroring the existing `segment_progress` pattern. The per-segment reducer reads a `SamplerState` (rebuilt from events via `replay_with_history`, as `segment_progress` does) plus the segment's episode rows (`get_segment_attempts`). The route reducer sums per-segment closed forms over a game's active segments. Two thin FastAPI routes expose them. No modeling changes; the Monte-Carlo engine is untouched (it remains the Simulator's).

**Tech Stack:** Python 3.11+, dataclasses, FastAPI, Pydantic; pytest.

**Spec:** [`docs/superpowers/specs/2026-06-02-live-practice-view-design.md`](../specs/2026-06-02-live-practice-view-design.md) — see the Computation Sources table. This plan delivers the closed-form data layer; session-diff snapshots (BE-2) and the frontend (D-Live-FE) are separate just-in-time plans.

---

## Key facts about existing code (verified)

- `spinlab.estimators.em_suite_sampler` exposes: `expected_episode_time_scalar(state) -> float | None` (closed-form mean episode time, no slide); `expected_episode_time_ms(state, fast_idx, slow_idx, *, apply_slope, reload_penalty_ms=DEFAULT_DEATH_PENALTY_MS) -> float | None` (slide=True gives the "after one practice step" episode time); `DEFAULT_FAST_IDX`, `DEFAULT_SLOW_IDX`, `DEFAULT_DEATH_PENALTY_MS`, `LOGIT_EPS`; `SamplerState.p_die_ema(idx) -> float | None`; `_gate_passes(state) -> bool`; `replay_with_history(events) -> (state, history)`.
- `db.get_segment_attempts(segment_id) -> list[AttemptRow]` — episode-shaped, chronological by close. `AttemptRow` (TypedDict): `segment_id, completed:int, time_ms:int|None, deaths:int, clean_tail_ms:int|None, created_at:str, invalidated:int`. `time_ms` = episode total (incl. deaths + 3.2s/death penalty); `clean_tail_ms` = the successful attempt's clean time.
- `db.get_segment_by_id(id) -> Segment | None` (has `.game_id`); `db.get_segment_event_rows(id)`; `db.get_active_segments(game_id) -> list[Segment]`.
- `routes/model.py` already imports `_events_from_rows`, `replay_with_history`, `HTTPException`, `Depends`, `get_db`, and defines `get_segment_progress` — copy its shape. Schemas live in `api_schemas.py` (`_BaseResponse` base).
- `routes/model.py` `get_segment_progress` builds events via `_events_from_rows(db.get_segment_event_rows(segment_id))` then `replay_with_history(events)`.

## File Structure

**New files:**
- `python/spinlab/estimators/live_view.py` — `LiveSegmentView` dataclass + `live_segment_view(state, episodes, *, reload_penalty_ms)` reducer, and `RouteSummary` dataclass + `route_summary(states)` reducer.
- `tests/unit/test_live_view.py` — reducer tests.
- `tests/unit/test_live_view_routes.py` — route tests.

**Modified files:**
- `python/spinlab/api_schemas.py` — add `LiveSegmentViewResponse`, `RouteSummaryResponse`.
- `python/spinlab/routes/model.py` — add `GET /segments/{id}/live` and `GET /games/{game_id}/live-summary`.

---

## Task 1: `live_segment_view` reducer + tests

**Files:**
- Create: `python/spinlab/estimators/live_view.py`
- Test: `tests/unit/test_live_view.py`

- [ ] **Step 1: Write the failing test.** Create `tests/unit/test_live_view.py`:

```python
"""Tests for the closed-form live-view reducers."""
from __future__ import annotations

from datetime import UTC, datetime

from spinlab.estimators.em_suite_sampler import SamplerState, process_event
from spinlab.estimators.live_view import (
    LiveSegmentView, live_segment_view,
)
from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt


def _state(success_ms: list[float], death_ms: list[float]) -> SamplerState:
    state = SamplerState()
    n = max(len(success_ms), len(death_ms))
    for i in range(n):
        if i < len(death_ms):
            state = process_event(state, EventAttempt(
                segment_id="x", session_id="s", episode_id=f"d{i}",
                outcome=AttemptOutcome.DIED, time_ms=int(death_ms[i]),
                source=AttemptSource.PRACTICE, created_at=datetime.now(UTC)))
        if i < len(success_ms):
            state = process_event(state, EventAttempt(
                segment_id="x", session_id="s", episode_id=f"c{i}",
                outcome=AttemptOutcome.SURVIVED, time_ms=int(success_ms[i]),
                source=AttemptSource.PRACTICE, created_at=datetime.now(UTC)))
    return state


def _ep(completed: int, time_ms, deaths: int, clean_tail_ms, inv: int = 0) -> dict:
    return {
        "segment_id": "x", "completed": completed, "time_ms": time_ms,
        "deaths": deaths, "clean_tail_ms": clean_tail_ms,
        "created_at": "2026-06-02T00:00:00+00:00", "invalidated": inv,
    }


class TestLiveSegmentView:
    def test_below_gate_not_ready(self):
        v = live_segment_view(SamplerState(n_successes=1, n_deaths=0, n_attempts_total=1), [])
        assert isinstance(v, LiveSegmentView)
        assert v.ready is False
        assert v.expected_episode_ms is None
        assert v.series == []

    def test_ready_payload_closed_form(self):
        state = _state(success_ms=[6000, 5000, 4500, 4200, 4000, 4000],
                       death_ms=[1500, 1500, 1500])
        episodes = [
            _ep(1, 9000, 1, 5800), _ep(0, None, 1, None),
            _ep(1, 7500, 1, 4300), _ep(1, 4200, 0, 4200),  # clean episode (no deaths)
            _ep(1, 7700, 1, 4500),  # last completion
        ]
        v = live_segment_view(state, episodes)
        assert v.ready is True
        assert v.expected_episode_ms is not None and v.expected_episode_ms > 0
        assert 0.0 <= v.death_rate <= 1.0
        # floor = min clean_tail over completed episodes = 4200
        assert v.floor_ms == 4200.0
        # last completion = last episode row that completed
        assert v.last_episode_ms == 7700.0
        assert v.last_clean_ms == 4500.0
        assert v.last_deaths == 1
        # rank of last completion's episode time among completed episodes
        # completed totals: 9000, 7500, 4200, 7700 -> sorted 4200,7500,7700,9000
        # 7700 is rank 3
        assert v.last_rank == 3
        # series carries one point per completed episode, with running floor
        completed_pts = [p for p in v.series]
        assert len(completed_pts) == 4
        assert completed_pts[-1]["running_floor_ms"] == 4200.0  # min so far by the end

    def test_practice_gain_signed_reduction(self):
        # Improving history -> a positive expected reduction from one more rep.
        state = _state(success_ms=[6000, 5800, 5400, 5000, 4400, 4000, 3800, 3600],
                       death_ms=[1600, 1500, 1400, 1300])
        v = live_segment_view(state, [_ep(1, 5000, 0, 5000), _ep(1, 4600, 0, 4600)])
        assert v.ready is True
        # practice_gain_ms is scalar(no slide) - slid(one step); may be None if
        # slopes unavailable, else a finite signed number.
        assert v.practice_gain_ms is None or isinstance(v.practice_gain_ms, float)

    def test_floor_none_when_no_completed_episodes(self):
        state = _state(success_ms=[4000, 4000], death_ms=[1500, 1500])
        v = live_segment_view(state, [_ep(0, None, 1, None), _ep(0, None, 2, None)])
        assert v.floor_ms is None
        assert v.last_episode_ms is None
        assert v.series == []

    def test_invalidated_episodes_excluded(self):
        state = _state(success_ms=[4000, 4100, 4050, 4000], death_ms=[1500, 1500])
        v = live_segment_view(state, [_ep(1, 4000, 0, 4000, inv=1), _ep(1, 4200, 0, 4200)])
        # invalidated row ignored: floor + last from the single valid episode
        assert v.floor_ms == 4200.0
        assert v.last_episode_ms == 4200.0
        assert len(v.series) == 1
```

- [ ] **Step 2: Run it — fails on import.**

Run: `python -m pytest tests/unit/test_live_view.py -q`
Expected: ImportError on `spinlab.estimators.live_view`.

- [ ] **Step 3: Implement the reducer.** Create `python/spinlab/estimators/live_view.py`:

```python
"""Closed-form live-view reducers — the data behind the live practice view.

Everything here is EXACT closed form (no Monte-Carlo): valid because the live
view uses only the additive total-run-time objective under no_reset. See the
D-Live spec's Computation Sources table. The Monte-Carlo engine stays the
Simulator's.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from spinlab.estimators.em_suite_sampler import (
    DEFAULT_DEATH_PENALTY_MS,
    DEFAULT_FAST_IDX,
    DEFAULT_SLOW_IDX,
    SamplerState,
    _gate_passes,
    expected_episode_time_ms,
    expected_episode_time_scalar,
)


@dataclass
class LiveSegmentView:
    """Closed-form per-segment payload for the live view. ms fields None below gate."""
    ready: bool
    expected_episode_ms: float | None       # closed-form mean episode time (sample 0)
    practice_gain_ms: float | None          # expected ms shaved by one more practice rep (>0 = faster)
    death_rate: float                       # p_die EMA (fast); 0.0 below gate
    floor_ms: float | None                  # best (min) clean clear over valid completed episodes
    last_episode_ms: float | None           # most recent completed episode's total time
    last_clean_ms: float | None             # that episode's clean-tail time
    last_deaths: int | None                 # that episode's death count
    last_rank: int | None                   # rank of last completion among completed episode totals (1=fastest)
    series: list[dict] = field(default_factory=list)  # per completed episode, chronological


def _valid_completed(episodes: list[dict]) -> list[dict]:
    return [e for e in episodes if e["completed"] and not e["invalidated"]
            and e["time_ms"] is not None]


def live_segment_view(
    state: SamplerState,
    episodes: list[dict],
    *,
    reload_penalty_ms: int = DEFAULT_DEATH_PENALTY_MS,
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
    # Positive = practicing once is expected to lower episode time. None if either
    # closed form is unavailable (e.g. p->1, or slopes not yet computable).
    practice_gain = (expected - slid) if (expected is not None and slid is not None) else None

    p_die = state.p_die_ema(DEFAULT_FAST_IDX)
    death_rate = float(p_die) if p_die is not None else 0.0

    valid = _valid_completed(episodes)  # chronological (input order)
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
        # rank = 1-based position of the last completion's time (ties share the
        # better rank via index of first >= ).
        last_rank = totals.index(last_episode_ms) + 1
    else:
        last_episode_ms = last_clean_ms = last_deaths = last_rank = None

    return LiveSegmentView(
        ready=True, expected_episode_ms=expected, practice_gain_ms=practice_gain,
        death_rate=death_rate, floor_ms=floor_ms, last_episode_ms=last_episode_ms,
        last_clean_ms=last_clean_ms, last_deaths=last_deaths, last_rank=last_rank,
        series=series,
    )
```

- [ ] **Step 4: Run tests — pass.**

Run: `python -m pytest tests/unit/test_live_view.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit.**

```bash
git add python/spinlab/estimators/live_view.py tests/unit/test_live_view.py
git commit -m "feat(live-view): closed-form per-segment reducer"
```

---

## Task 2: `route_summary` reducer + tests

**Files:**
- Modify: `python/spinlab/estimators/live_view.py`
- Modify: `tests/unit/test_live_view.py`

- [ ] **Step 1: Add the failing test.** Append to `tests/unit/test_live_view.py`:

```python
from spinlab.estimators.live_view import RouteSummary, route_summary


class TestRouteSummary:
    def test_sums_estimable_segments_and_counts_skips(self):
        ready = _state(success_ms=[4000, 4000, 4000, 4000], death_ms=[1500, 1500])
        ungated = SamplerState(n_successes=1, n_deaths=0, n_attempts_total=1)
        s = route_summary([ready, ready, ungated])
        assert s.exp_run_ms is not None and s.exp_run_ms > 0
        assert s.exp_deaths is not None and s.exp_deaths >= 0.0
        assert s.n_estimable == 2
        assert s.n_skipped == 1

    def test_all_skipped_yields_none(self):
        ungated = SamplerState(n_successes=0, n_deaths=0, n_attempts_total=0)
        s = route_summary([ungated, ungated])
        assert s.exp_run_ms is None
        assert s.exp_deaths is None
        assert s.n_estimable == 0
        assert s.n_skipped == 2
```

- [ ] **Step 2: Run — fails (ImportError on RouteSummary / route_summary).**

Run: `python -m pytest tests/unit/test_live_view.py::TestRouteSummary -q`
Expected: fail.

- [ ] **Step 3: Implement.** Append to `python/spinlab/estimators/live_view.py`:

```python
from spinlab.estimators.em_suite_sampler import LOGIT_EPS


@dataclass
class RouteSummary:
    """Closed-form whole-run aggregate. None when no segment is estimable."""
    exp_run_ms: float | None       # Σ expected episode time over estimable segments
    exp_deaths: float | None       # Σ p/(1-p) over estimable segments
    n_estimable: int               # segments that contributed
    n_skipped: int                 # gated-but-undefined or below-gate segments


def route_summary(states: list[SamplerState]) -> RouteSummary:
    run_ms = 0.0
    deaths = 0.0
    n_est = 0
    n_skip = 0
    for state in states:
        exp = expected_episode_time_scalar(state)
        p = state.p_die_ema(DEFAULT_FAST_IDX) if _gate_passes(state) else None
        # A segment contributes only if BOTH closed forms are defined; p->1 makes
        # the geometric mean diverge (exp is None there too), so skip honestly.
        if exp is None or p is None or p >= 1.0 - LOGIT_EPS:
            n_skip += 1
            continue
        run_ms += exp
        deaths += p / (1.0 - p)
        n_est += 1
    if n_est == 0:
        return RouteSummary(exp_run_ms=None, exp_deaths=None, n_estimable=0, n_skipped=n_skip)
    return RouteSummary(exp_run_ms=run_ms, exp_deaths=deaths, n_estimable=n_est, n_skipped=n_skip)
```

- [ ] **Step 4: Run — pass.**

Run: `python -m pytest tests/unit/test_live_view.py -q`
Expected: 7 passed.

- [ ] **Step 5: Commit.**

```bash
git add python/spinlab/estimators/live_view.py tests/unit/test_live_view.py
git commit -m "feat(live-view): closed-form route aggregate reducer"
```

---

## Task 3: Response schemas

**Files:**
- Modify: `python/spinlab/api_schemas.py`

- [ ] **Step 1: Add schemas.** Near the other per-segment response models in `python/spinlab/api_schemas.py`, append:

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


class RouteSummaryResponse(_BaseResponse):
    """Closed-form whole-run aggregate for the route bar. See route_summary()."""
    game_id: str
    exp_run_ms: float | None
    exp_deaths: float | None
    n_estimable: int
    n_skipped: int
```

- [ ] **Step 2: Confirm import.**

Run: `python -c "from spinlab.api_schemas import LiveSegmentViewResponse, RouteSummaryResponse; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit.**

```bash
git add python/spinlab/api_schemas.py
git commit -m "feat(schemas): LiveSegmentViewResponse + RouteSummaryResponse"
```

---

## Task 4: Routes + tests

**Files:**
- Modify: `python/spinlab/routes/model.py`
- Test: `tests/unit/test_live_view_routes.py`

- [ ] **Step 1: Write the failing route test.** Create `tests/unit/test_live_view_routes.py`:

```python
"""Route tests for the live-view endpoints."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from spinlab.db import Database
from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt, Segment
from spinlab.routes.model import router
from spinlab.routes._deps import get_db


def _client(tmp_path) -> tuple[TestClient, str, str]:
    db = Database(str(tmp_path / "t.db"))
    db.upsert_game("g1", "G", "any%")
    seg_id = "g1:6:entrance.0:checkpoint.1:aa:bb"
    db.upsert_segment(Segment(
        id=seg_id, game_id="g1", level_number=6,
        start_type="entrance", start_ordinal=0,
        end_type="checkpoint", end_ordinal=1, active=True))
    db.create_session("g1:s", "g1")
    for i in range(8):
        for outcome, t in ((AttemptOutcome.DIED, 1500), (AttemptOutcome.SURVIVED, 4200 - i * 20)):
            db.log_event_attempt(EventAttempt(
                segment_id=seg_id, session_id="g1:s", episode_id=f"{outcome.value}{i}",
                outcome=outcome, time_ms=t, source=AttemptSource.PRACTICE,
                created_at=datetime.now(UTC)))
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), seg_id, "g1"


class TestLiveRoutes:
    def test_segment_live_ready(self, tmp_path):
        client, seg_id, _ = _client(tmp_path)
        r = client.get(f"/api/segments/{seg_id}/live")
        assert r.status_code == 200
        b = r.json()
        assert b["segment_id"] == seg_id
        assert b["ready"] is True
        assert b["expected_episode_ms"] is not None
        assert isinstance(b["series"], list) and len(b["series"]) >= 1

    def test_segment_live_unknown_404(self, tmp_path):
        client, _, _ = _client(tmp_path)
        r = client.get("/api/segments/nope/live")
        assert r.status_code == 404

    def test_route_summary(self, tmp_path):
        client, _, game_id = _client(tmp_path)
        r = client.get(f"/api/games/{game_id}/live-summary")
        assert r.status_code == 200
        b = r.json()
        assert b["game_id"] == game_id
        assert b["n_estimable"] + b["n_skipped"] >= 1
```

- [ ] **Step 2: Run — fails (routes missing).**

Run: `python -m pytest tests/unit/test_live_view_routes.py -q`
Expected: failures (404 / route not defined).

- [ ] **Step 3: Implement the routes.** In `python/spinlab/routes/model.py`, add to the schema import block:

```python
from spinlab.api_schemas import (
    # ... existing imports ...
    LiveSegmentViewResponse,
    RouteSummaryResponse,
)
```

…and append after `get_segment_progress`:

```python
@router.get("/segments/{segment_id}/live", response_model=LiveSegmentViewResponse)
def get_segment_live(segment_id: str, db: Database = Depends(get_db)):
    """Closed-form live-view payload for one segment (episode-time trend, floor,
    expected, practice gain, deaths). Pure read; no Monte-Carlo."""
    from spinlab.estimators.em_suite_sampler import replay_with_history
    from spinlab.estimators.live_view import live_segment_view

    seg = db.get_segment_by_id(segment_id)
    if seg is None:
        logger.warning("get_segment_live: unknown segment %r", segment_id)
        raise HTTPException(status_code=404, detail=f"Segment not found: {segment_id}")

    events = _events_from_rows(db.get_segment_event_rows(segment_id))
    state, _history = replay_with_history(events)
    episodes = db.get_segment_attempts(segment_id)
    v = live_segment_view(state, episodes)
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
    }


@router.get("/games/{game_id}/live-summary", response_model=RouteSummaryResponse)
def get_route_summary(game_id: str, db: Database = Depends(get_db)):
    """Closed-form whole-run aggregate for the route bar: expected run time and
    expected deaths summed over estimable segments. Pure read; no Monte-Carlo."""
    from spinlab.estimators.em_suite_sampler import replay_with_history
    from spinlab.estimators.live_view import route_summary

    states = []
    for seg in db.get_active_segments(game_id):
        events = _events_from_rows(db.get_segment_event_rows(seg.id))
        state, _history = replay_with_history(events)
        states.append(state)
    s = route_summary(states)
    return {
        "game_id": game_id,
        "exp_run_ms": s.exp_run_ms,
        "exp_deaths": s.exp_deaths,
        "n_estimable": s.n_estimable,
        "n_skipped": s.n_skipped,
    }
```

- [ ] **Step 4: Run — pass.**

Run: `python -m pytest tests/unit/test_live_view_routes.py -q`
Expected: 3 passed.

- [ ] **Step 5: Commit.**

```bash
git add python/spinlab/routes/model.py tests/unit/test_live_view_routes.py
git commit -m "feat(routes): GET /segments/{id}/live + /games/{id}/live-summary"
```

---

## Task 5: Static analysis + full gate

**Files:** none (verification only).

- [ ] **Step 1: Static analysis.**

Run: `npx pyright python/spinlab/estimators/live_view.py python/spinlab/routes/model.py`
Expected: no new errors.

- [ ] **Step 2: Fast suite.**

Run: `python -m pytest -m "not emulator" -q`
Expected: green, count up by the new tests.

- [ ] **Step 3: Full unfiltered gate (merge rule).** REQUIRES the live dashboard stopped (binds NCI 55355 + the DB).

Run: `python -m pytest`
Expected: green.

- [ ] **Step 4: Final commit (if anything outstanding).**

```bash
git add -A
git commit -m "test(live-view): full-gate verification"
```

---

## Self-review notes

- **Spec coverage (Computation Sources):** Expected ✓ (`expected_episode_time_scalar`), Practice ✓ (closed-form delta), Deaths% ✓ (`p_die_ema`), floor ✓ (running-min `clean_tail`), last completion + rank + decomposition ✓ (episode rows), graph series ✓ (per-completed-episode with running floor), Exp. Run ✓ (Σ expected, estimable-only), Exp. Deaths ✓ (Σ p/(1−p)), honest incompleteness ✓ (`n_skipped`). **Not in this plan (by design):** session diffs / Practice-saved / rate (BE-2, needs the session snapshot); all frontend (D-Live-FE); the stepping-floor *stat* (`series.running_floor_ms` is provided so FE can draw the diagonal floor; the Floor/Floors *improvement stat* is BE-2 with the session snapshot).
- **No fudge:** every number is an exact closed form or an observed value; `None`/skip is returned wherever a value is undefined (below gate, p→1, no completed episodes) — never a fabricated default.
- **No magic numbers:** reuses `DEFAULT_DEATH_PENALTY_MS`, `DEFAULT_FAST_IDX`, `DEFAULT_SLOW_IDX`, `LOGIT_EPS` from the sampler.
- **Pattern reuse:** routes mirror `get_segment_progress` (same event-replay read path, same 404 + logger.warning shape).
- **Type consistency:** `live_segment_view(state, episodes)` and `route_summary(states)` signatures match across reducer, tests, routes; `series` dict keys (`episode_ms`, `deaths`, `clean_ms`, `running_floor_ms`) are the FE contract.
- **`AttemptRow` access:** the reducer indexes dict keys (`e["completed"]` etc.); `get_segment_attempts` returns `AttemptRow` TypedDicts (dict-compatible), and the tests pass plain dicts of the same shape.

# Run-Level Session-Improvement Graph — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a run-level "whole-session improvement" graph — projected full-run time (Exp.Run) declining over this practice session, toward a Σ-of-floors reference — rendered above the segment graph in the live practice view.

**Architecture:** A new closed-form reducer (`route_series`) replays all the game's segment events in global chronological order and emits the route Exp.Run after each in-session event. It rides on the existing `RouteSummary` / `GET /api/games/{id}/live-summary` payload alongside two new scalars (session-start baseline, Σ-floors). A pure-render frontend component (`run-graph.ts`) draws the curve + baseline line + floor line, mounted above the segment summary.

**Tech Stack:** Python (FastAPI, pytest), TypeScript (Vite, Vitest), SVG.

**Scope note:** This is Plan 1 of the iter-2 spec (`docs/superpowers/specs/2026-06-06-phase-d-run-graph-and-persistence-design.md`). Part 2 (freeze-and-persist the session snapshot so the live view survives the stop transition) is a **separate plan, written just-in-time after this lands and is smoke-tested live**. Consequence: in this plan the run graph only populates while a practice session is active (a snapshot exists); idle it shows a "not enough data yet" placeholder. That is expected until Plan 2.

---

### Task 1: `route_series` closed-form reducer

**Files:**
- Modify: `python/spinlab/estimators/live_view.py`
- Test: `tests/unit/estimators/test_live_view.py`

The reducer replays every segment's events in global chronological order, advancing one per step, and after each event whose `created_at >= session_start` appends the route Exp.Run (Σ of `expected_episode_time_scalar` over segments, skipping `None`). Returns `[]` when `session_start is None` or no in-session event yields an estimable route. Same closed forms as `route_summary` — no Monte-Carlo, no new constants.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/estimators/test_live_view.py`:

```python
from datetime import datetime

from spinlab.estimators.live_view import route_series


def test_route_series_empty_when_no_session_start():
    assert route_series([], session_start=None) == []


def test_route_series_empty_when_events_predate_session():
    # All events before session_start -> no in-session points.
    seg = [
        make_event_attempt(segment_id="s0", episode_id=f"e{i}", outcome=o,
                           time_ms=t, created_at=f"2026-01-01T00:00:0{i}")
        for i, (o, t) in enumerate(
            [("survived", 2000), ("died", 500), ("survived", 2100),
             ("died", 600), ("survived", 1900), ("died", 550)])
    ]
    start = datetime.fromisoformat("2026-01-01T00:01:00")  # after every event
    assert route_series([seg], session_start=start) == []


def test_route_series_emits_floats_for_in_session_events():
    # 3 warm-up events (pre-session) seed the EMAs so the route is estimable,
    # then 3 in-session events each yield a route Exp.Run point.
    warm = [
        make_event_attempt(segment_id="s0", episode_id=f"e{i}", outcome=o,
                           time_ms=t, created_at=f"2026-01-01T00:00:0{i}")
        for i, (o, t) in enumerate(
            [("survived", 2000), ("died", 500), ("survived", 2100)])
    ]
    in_session = [
        make_event_attempt(segment_id="s0", episode_id=f"e{i + 3}", outcome=o,
                           time_ms=t, created_at=f"2026-01-01T00:00:1{i}")
        for i, (o, t) in enumerate(
            [("died", 600), ("survived", 1900), ("died", 550)])
    ]
    start = datetime.fromisoformat("2026-01-01T00:00:10")
    series = route_series([warm + in_session], session_start=start)
    assert series, "expected at least one in-session estimable point"
    assert all(isinstance(x, float) for x in series)


def test_route_series_sums_across_segments():
    # Two identical segments -> each route point is ~2x a single segment's.
    def seg(seg_id):
        return [
            make_event_attempt(segment_id=seg_id, episode_id=f"{seg_id}_e{i}",
                               outcome=o, time_ms=t,
                               created_at=f"2026-01-01T00:00:1{i}")
            for i, (o, t) in enumerate(
                [("survived", 2000), ("died", 500), ("survived", 2100),
                 ("died", 600), ("survived", 1900), ("died", 550)])
        ]
    start = datetime.fromisoformat("2026-01-01T00:00:00")
    one = route_series([seg("s0")], session_start=start)
    two = route_series([seg("s0"), seg("s1")], session_start=start)
    assert one and two
    assert two[-1] == pytest.approx(2 * one[-1], rel=1e-6)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/estimators/test_live_view.py -k route_series -v`
Expected: FAIL with `ImportError: cannot import name 'route_series'`.

- [ ] **Step 3: Implement the reducer**

In `python/spinlab/estimators/live_view.py`, add the import near the top (the
`from __future__` line is already present; add `datetime` and `process_event`):

```python
from datetime import datetime
```

Add `process_event` to the existing `from spinlab.estimators.em_suite_sampler import (...)` block (it already imports `SamplerState`, `expected_episode_time_scalar`, etc.). Add `EventAttempt` import:

```python
from spinlab.models import EventAttempt
```

Then append this function (after `route_summary`):

```python
def route_series(
    segment_events: Sequence[Sequence[EventAttempt]],
    *,
    session_start: datetime | None,
) -> list[float]:
    """Closed-form run-level improvement curve.

    Replays every segment's events in global chronological order (by
    ``created_at``); one ``process_event`` per step. After each event whose
    ``created_at >= session_start`` appends the route Exp.Run — the sum of
    ``expected_episode_time_scalar`` over all segments, skipping segments whose
    scalar is still None (under-gated or p->1). Returns [] when there is no
    session window or no in-session event produces an estimable route.

    Exact closed form, same as route_summary; no Monte-Carlo, no new constants.
    """
    if session_start is None:
        return []
    timeline: list[tuple[datetime, int, EventAttempt]] = []
    for seg_idx, events in enumerate(segment_events):
        for ev in events:
            timeline.append((ev.created_at, seg_idx, ev))
    timeline.sort(key=lambda t: t[0])

    states = [SamplerState() for _ in segment_events]
    series: list[float] = []
    for created_at, seg_idx, ev in timeline:
        states[seg_idx] = process_event(states[seg_idx], ev)
        if created_at < session_start:
            continue
        run_ms = 0.0
        n_est = 0
        for st in states:
            exp = expected_episode_time_scalar(st)
            if exp is None:
                continue
            run_ms += exp
            n_est += 1
        if n_est > 0:
            series.append(run_ms)
    return series
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/estimators/test_live_view.py -k route_series -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/estimators/live_view.py tests/unit/estimators/test_live_view.py
git commit -m "feat(live-view): route_series closed-form run-level improvement reducer"
```

---

### Task 2: Expose the series on the route-summary endpoint

**Files:**
- Modify: `python/spinlab/api_schemas.py:531-543` (`RouteSummaryResponse`)
- Modify: `python/spinlab/routes/model.py:281-326` (`get_route_summary`)
- Test: `tests/unit/test_live_view_routes.py`

- [ ] **Step 1: Write the failing test**

Read `tests/unit/test_live_view_routes.py` first to match its fixture style (TestClient + how it seeds a game/segments/snapshot). Add a test that, with an active practice session, the `live-summary` payload carries the three new fields:

```python
def test_live_summary_includes_run_series_fields(client_with_active_session):
    # Fixture convention from this file: returns (client, game_id) with a
    # practice snapshot taken and some in-session attempts logged.
    client, game_id = client_with_active_session
    r = client.get(f"/api/games/{game_id}/live-summary")
    assert r.status_code == 200
    body = r.json()
    assert "run_series" in body and isinstance(body["run_series"], list)
    assert "baseline_exp_run_ms" in body
    assert "floor_total_ms" in body
```

If this file has no active-session fixture, mirror the snapshot setup from `tests/unit/test_session_manager_snapshot.py` to build one inline. Keep the assertion to presence + type (the reducer's numeric behavior is covered in Task 1).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_live_view_routes.py -k run_series -v`
Expected: FAIL — `KeyError`/assert on missing `run_series` (pydantic drops unknown keys today).

- [ ] **Step 3: Extend the schema**

In `python/spinlab/api_schemas.py`, add to `RouteSummaryResponse` (after `floor_improvement_ms`):

```python
    run_series: list[float] = []           # route Exp.Run after each in-session event
    baseline_exp_run_ms: float | None = None  # session-start Exp.Run (the "start" line)
    floor_total_ms: float | None = None    # sum of segment floors (theoretical best run)
```

- [ ] **Step 4: Build the series in the route + return the fields**

In `python/spinlab/routes/model.py`, replace the body of `get_route_summary`'s
segment loop and return so it collects per-segment events + floor total, and
calls `route_series`. The full replacement for lines 292-326:

```python
    snap = session.practice_session_snapshot

    states = []
    segment_events: list[list] = []
    floor_total_ms: float | None = None
    floor_improvement_ms: float | None = None
    if snap is not None:
        floor_improvement_ms = 0.0
    for seg in db.get_active_segments(game_id):
        events = events_from_rows(db.get_segment_event_rows(seg.id))
        state, _history = replay_with_history(events)
        states.append(state)
        segment_events.append(events)
        episodes = db.get_segment_attempts(seg.id)
        seg_floor = running_min_clean(episodes)
        if seg_floor is not None:
            floor_total_ms = seg_floor if floor_total_ms is None else floor_total_ms + seg_floor
        # Aggregate floor improvement vs baseline (unchanged behavior).
        if snap is not None and floor_improvement_ms is not None:
            base = snap.segments.get(seg.id)
            if base is not None and base.floor_ms is not None and seg_floor is not None:
                floor_improvement_ms += max(0.0, base.floor_ms - seg_floor)

    from datetime import datetime

    from spinlab.estimators.live_view import route_series, route_summary
    session_start_dt = datetime.fromtimestamp(snap.started_at) if snap else None
    run_series = route_series(segment_events, session_start=session_start_dt)

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
        "run_series": run_series,
        "baseline_exp_run_ms": snap.route.exp_run_ms if snap else None,
        "floor_total_ms": floor_total_ms,
    }
```

Notes: `running_min_clean` and `route_summary` are already imported at the top
of the function today (`from spinlab.estimators.live_view import route_summary`
on line 290 — extend it to also import `route_series` as shown). `running_min_clean`
is already imported in this module (used for `floor_improvement_ms`). The episodes
fetch now runs for every segment (was gated on `snap`); this is a small extra
read per segment, acceptable for the route-level floor line.

- [ ] **Step 5: Run test + full reducer suite**

Run: `python -m pytest tests/unit/test_live_view_routes.py tests/unit/estimators/test_live_view.py -v`
Expected: PASS.

- [ ] **Step 6: Regenerate the frontend types**

Run: `cd frontend && npm run gen-types`
Expected: `frontend/src/api-types.ts` now types `RouteSummaryResponse` with `run_series: number[]`, `baseline_exp_run_ms: number | null`, `floor_total_ms: number | null`. (`frontend/openapi.json` + `api-types.ts` are git-ignored; no commit needed for them.)

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/api_schemas.py python/spinlab/routes/model.py tests/unit/test_live_view_routes.py
git commit -m "feat(api): expose run_series + baseline + floor_total on live-summary"
```

---

### Task 3: `run-graph.ts` pure-render component

**Files:**
- Create: `frontend/src/run-graph.ts`
- Test: `frontend/src/run-graph.test.ts`

Mirrors `episode-graph.ts`, reusing its exported `yForTime` / `linePoints`
helpers. Draws the Exp.Run curve, a dotted session-start baseline line, a dashed
Σ-floors line, and a marker on the last point.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/run-graph.test.ts`:

```ts
import { describe, it, expect, beforeEach } from "vitest";
import { renderRunGraph } from "./run-graph";
import type { RouteSummary } from "./types";

function base(overrides: Partial<RouteSummary>): RouteSummary {
  return {
    game_id: "g", exp_run_ms: 252000, exp_deaths: 21,
    n_estimable: 3, n_skipped: 0,
    session_started_at: null, exp_run_diff_ms: null, exp_deaths_diff: null,
    practice_saved_ms: null, floor_improvement_ms: null,
    run_series: [], baseline_exp_run_ms: null, floor_total_ms: null,
    ...overrides,
  } as RouteSummary;
}

describe("renderRunGraph", () => {
  let host: HTMLElement;
  beforeEach(() => { host = document.createElement("div"); });

  it("shows a placeholder when the series is empty", () => {
    renderRunGraph(host, base({ run_series: [], baseline_exp_run_ms: 250000 }));
    expect(host.querySelector("svg")).toBeNull();
    expect(host.textContent).toMatch(/not enough data/i);
  });

  it("shows a placeholder when the baseline is missing", () => {
    renderRunGraph(host, base({ run_series: [250000, 248000], baseline_exp_run_ms: null }));
    expect(host.querySelector("svg")).toBeNull();
  });

  it("draws curve, baseline, floor and a last-point marker", () => {
    renderRunGraph(host, base({
      run_series: [258000, 255000, 252000],
      baseline_exp_run_ms: 258000,
      floor_total_ms: 231000,
    }));
    expect(host.querySelector("svg")).not.toBeNull();
    expect(host.querySelector(".rg-line")).not.toBeNull();
    expect(host.querySelector(".rg-baseline")).not.toBeNull();
    expect(host.querySelector(".rg-floor")).not.toBeNull();
    expect(host.querySelector(".rg-last")).not.toBeNull();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- run-graph`
Expected: FAIL — `Cannot find module './run-graph'`.

- [ ] **Step 3: Implement the component**

Create `frontend/src/run-graph.ts`:

```ts
/**
 * Run-level session-improvement graph — sits above the segment graph in the
 * live view. Plots the projected full-run time (Exp.Run) after each in-session
 * attempt as a curve declining from the session-start baseline toward a dashed
 * floor = sum of every segment's best clean clear (the theoretical best run).
 * Pure render over the /games/{id}/live-summary payload — no fetch, no chart dep.
 * See the iter-2 spec, Part 1.
 */
import { formatTime } from "./format";
import { yForTime, linePoints } from "./episode-graph";
import type { RouteSummary } from "./types";

// Same viewBox geometry as the segment graph so the two stack visually aligned.
const GEO = { left: 30, right: 392, top: 10, bottom: 104 } as const;
const VIEW_W = 400;
const VIEW_H = 124;

function placeholder(host: HTMLElement, msg: string): void {
  host.innerHTML = `<div class="rg-empty">${msg}</div>`;
}

export function renderRunGraph(host: HTMLElement, data: RouteSummary): void {
  host.innerHTML = "";
  const series = (data.run_series ?? []) as number[];
  const baseline = data.baseline_exp_run_ms ?? null;
  if (series.length === 0 || baseline == null) {
    placeholder(host, "Run trend: not enough data yet");
    return;
  }
  const floor = data.floor_total_ms ?? null;
  // Scale spans the floor (bottom) to the highest of baseline / series (top).
  const lo = floor ?? Math.min(...series);
  const hi = Math.max(baseline, ...series);
  const curve = linePoints(series, lo, hi, GEO);
  const baseY = yForTime(baseline, lo, hi, GEO.top, GEO.bottom);

  const parts: string[] = [];
  parts.push(`<line class="rg-baseline" x1="${GEO.left}" y1="${baseY.toFixed(1)}" x2="${GEO.right}" y2="${baseY.toFixed(1)}"/>`);
  parts.push(`<text x="${GEO.left}" y="${(baseY - 3).toFixed(1)}" class="rg-baseline-label">start ${formatTime(baseline)}</text>`);
  if (floor != null) {
    const floorY = yForTime(floor, lo, hi, GEO.top, GEO.bottom);
    parts.push(`<line class="rg-floor" x1="${GEO.left}" y1="${floorY.toFixed(1)}" x2="${GEO.right}" y2="${floorY.toFixed(1)}"/>`);
    parts.push(`<text x="${(GEO.right - 64).toFixed(1)}" y="${(floorY - 3).toFixed(1)}" class="rg-floor-label">floor ${formatTime(floor)}</text>`);
  }
  parts.push(`<polyline class="rg-line" fill="none" points="${curve}"/>`);

  const n = series.length;
  const lastX = GEO.left + (n > 1 ? GEO.right - GEO.left : 0);
  const lastY = yForTime(series[n - 1]!, lo, hi, GEO.top, GEO.bottom);
  parts.push(`<circle class="rg-last" cx="${lastX.toFixed(1)}" cy="${lastY.toFixed(1)}" r="3.5"/>`);

  host.innerHTML = `<svg class="rg-svg" viewBox="0 0 ${VIEW_W} ${VIEW_H}" preserveAspectRatio="none">${parts.join("")}</svg>`;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npm test -- run-graph`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/run-graph.ts frontend/src/run-graph.test.ts
git commit -m "feat(frontend): run-graph pure-render component"
```

---

### Task 4: Mount the run graph above the segment graph in the live view

**Files:**
- Modify: `frontend/index.html:42` (add host)
- Modify: `frontend/src/live-view.ts` (hosts + render + teardown)
- Modify: `frontend/src/model.ts:90-94` (pass the new host)
- Modify: the stylesheet defining `.eg-svg` (add `.rg-*` rules)
- Test: `frontend/src/live-view.test.ts`

- [ ] **Step 1: Add the host to index.html**

In `frontend/index.html`, insert the run-graph host between the route bar
(line 42) and the segment summary (line 43):

```html
            <div id="live-route-bar" class="lv-route-bar"></div>
            <div id="live-run-graph" class="lv-run-graph"></div>
            <div id="live-segment-summary" class="lv-segment-summary"></div>
```

- [ ] **Step 2: Write the failing live-view test**

Read `frontend/src/live-view.test.ts` first (it stubs `fetchJSON`). Add a test
that, after `loadAndRenderLiveView`, the run-graph host is rendered from the
summary payload, and `destroyLiveView` blanks it. Mirror the existing host setup
in that file, adding a `runGraph` element to the `hosts` object and asserting:

```ts
it("renders the run graph from the summary payload", async () => {
  // ...existing arrange: mock fetchJSON to return a summary with run_series...
  await loadAndRenderLiveView({ /* opts incl. hosts.runGraph */ });
  expect(hosts.runGraph.innerHTML).not.toBe("");
});
```

If the existing tests build `hosts` via a shared helper, extend that helper with
`runGraph: document.createElement("div")`. Have the mocked summary include
`run_series: [258000, 252000], baseline_exp_run_ms: 258000, floor_total_ms: 231000`
so the graph renders an `<svg>` rather than the placeholder.

- [ ] **Step 3: Run test to verify it fails**

Run: `cd frontend && npm test -- live-view`
Expected: FAIL — `hosts.runGraph` undefined / not rendered.

- [ ] **Step 4: Wire run-graph into live-view.ts**

In `frontend/src/live-view.ts`:

Add the import:

```ts
import { renderRunGraph } from "./run-graph";
```

Add `runGraph` to `LiveViewHosts`:

```ts
export interface LiveViewHosts {
  routeBar: HTMLElement;
  runGraph: HTMLElement;
  segmentSummary: HTMLElement;
  graph: HTMLElement;
}
```

In `loadAndRenderLiveView`, render the run graph inside the `if (summary)` block,
right after `renderRouteBar(...)`:

```ts
  if (summary) {
    _lastRouteData = {
      title: opts.title, gameId: opts.gameId,
      routeSummary: summary, nowSeconds: Date.now() / 1000,
    };
    renderRouteBar(opts.hosts.routeBar, _lastRouteData);
    renderRunGraph(opts.hosts.runGraph, summary);
  }
```

In `destroyLiveView`, blank the run-graph host alongside the others:

```ts
  if (_lastHosts) {
    _lastHosts.routeBar.innerHTML = "";
    _lastHosts.runGraph.innerHTML = "";
    _lastHosts.segmentSummary.innerHTML = "";
    _lastHosts.graph.innerHTML = "";
    _lastHosts = null;
  }
```

- [ ] **Step 5: Pass the host from model.ts**

In `frontend/src/model.ts`, in `updatePracticeCard`'s `loadAndRenderLiveView`
call, add the host to the `hosts` object (after `routeBar`):

```ts
      hosts: {
        routeBar: document.getElementById("live-route-bar")!,
        runGraph: document.getElementById("live-run-graph")!,
        segmentSummary: document.getElementById("live-segment-summary")!,
        graph: document.getElementById("live-graph-slot")!,
      },
```

- [ ] **Step 6: Add CSS for the run graph**

Locate the stylesheet that defines the segment-graph rules:

Run: `cd frontend && grep -rl "\.eg-line" src`

In that file, next to the `.eg-*` rules, add (mirroring them; colors reuse the
existing graph palette — `--rg-line` is the same blue as `.eg-line`):

```css
.lv-run-graph { margin: 4px 0; }
.rg-svg { width: 100%; height: 80px; display: block; }
.rg-line { stroke: var(--accent, #6cf); stroke-width: 2; }
.rg-baseline { stroke: #888; stroke-dasharray: 2 3; }
.rg-floor { stroke: #4a9; stroke-dasharray: 4 3; }
.rg-baseline-label, .rg-floor-label { fill: #888; font-size: 9px; }
.rg-last { fill: #fc6; }
.rg-empty { color: #888; font-size: 12px; padding: 6px 0; }
```

If `.eg-line` uses literal hex rather than a CSS var, match that file's
convention instead of `var(--accent)`.

- [ ] **Step 7: Run frontend tests + typecheck + build**

Run: `cd frontend && npm test && npm run typecheck && npm run build`
Expected: all pass; build writes to `python/spinlab/static/`.

- [ ] **Step 8: Commit**

```bash
git add frontend/index.html frontend/src/live-view.ts frontend/src/live-view.test.ts frontend/src/model.ts frontend/src/*.css
git commit -m "feat(frontend): mount run graph above the segment graph in the live view"
```

---

### Task 5: Smoke test + full gate

**Files:**
- Modify: the Playwright shell smoke test (find via grep below)

- [ ] **Step 1: Add a smoke assertion that the host exists on the Play page**

The run graph only populates during a live practice session (Plan 2 adds idle
persistence), so the smoke test asserts the **host is present** on the Play page,
not that a curve renders. Locate the shell smoke test:

Run: `grep -rl "sweep\|page-play\|live-route-bar" tests`

Add an assertion in the Play-page smoke that `#live-run-graph` exists in the DOM
(mirror however the existing test queries `#live-route-bar` / the practice card).
Keep it to presence only.

- [ ] **Step 2: Run the smoke test**

Run: `python -m pytest -m "not emulator" -k "shell or smoke" -v` (after `cd frontend && npm run build`)
Expected: PASS.

- [ ] **Step 3: Full gate**

Run: `python -m pytest`
Expected: all pass, 0 skipped (per project policy — emulator tests must actually run). Also run `npx pyright python/` and `ruff check python/` on the changed files; introduce no new errors.

- [ ] **Step 4: Commit**

```bash
git add tests
git commit -m "test(shell): assert run-graph host present on the Play page"
```

---

## Self-Review

**Spec coverage (Part 1 only — Part 2 is a separate just-in-time plan):**
- Run-level series reducer (closed-form, chronological replay, session-windowed, None-skip) → Task 1. ✓
- Rides on `RouteSummary` / `live-summary` with `run_series` + `baseline_exp_run_ms` + `floor_total_ms` → Task 2. ✓
- X-axis = session attempt/event order → Task 1 (timeline sort). ✓
- `run-graph.ts` mirroring `episode-graph.ts`, mounted above the segment summary (Option A) → Tasks 3-4. ✓
- Σ-floors flat reference line → Task 3 (`rg-floor`), value from `floor_total_ms`. ✓
- Tests: vitest (run-graph render), pytest (reducer closed-form + route fields), smoke (host present), full gate → Tasks 1-5. ✓

**Deferred to Plan 2 (persistence), not this plan:** snapshot freeze/persist lifecycle, AppState frozen signal, `updatePracticeCard` idle-with-frozen state, frozen elapsed. The run graph showing idle depends on Plan 2; until then it placeholders idle. This is stated in the header.

**Type consistency:** `route_series(segment_events, *, session_start)` returns `list[float]`; schema field `run_series: list[float]` → TS `number[]`; `renderRunGraph(host, data: RouteSummary)` reads `data.run_series` as `number[]`. `baseline_exp_run_ms`/`floor_total_ms` are `float | None` → `number | null`, read with `?? null`. Geometry helpers `yForTime`/`linePoints` imported from `episode-graph.ts` (already exported there). Consistent.

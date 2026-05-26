# Segment Death Histogram Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a death-time / completion-time histogram panel to the segment detail page, driven by `DeathExtras` from the active estimator.

**Architecture:** Backend extends the existing `/api/segments/{id}/history` endpoint with two additive fields (`selected_model`, plus `final_extras` per estimator). Frontend adds a self-contained module for histogram binning + rendering, wired into `segment-detail.ts` below the existing chart. Markers are drawn by a tiny inline Chart.js plugin; no new npm dependency.

**Tech Stack:** Python 3.11 + FastAPI + Pydantic (backend); TypeScript + Vite + Chart.js 4 + Vitest (frontend); happy-dom for unit tests.

**Spec:** `docs/superpowers/specs/2026-05-25-segment-death-histogram-design.md`

---

## File Map

**Backend:**
- Modify: `python/spinlab/api_schemas.py` — add `final_extras` to `EstimatorCurves`; add `selected_model` to `SegmentHistory`.
- Modify: `python/spinlab/routes/model.py` — depend on `SessionManager`, load events once per call, capture final `DeathExtras` per estimator, return `selected_model`.
- Modify: `tests/unit/test_dashboard_integration.py` — extend the existing `segment_history` test cluster with cases asserting the new fields.

**Frontend:**
- Create: `frontend/src/death-distribution.ts` — pure histogram math + render + inline Chart.js marker plugin.
- Create: `frontend/src/death-distribution.test.ts` — Vitest unit tests for the binning function and render smoke test.
- Modify: `frontend/src/segment-detail.ts` — instantiate the panel below the existing chart, clean it up in `destroySegmentDetail`.

Each file has a single responsibility: API contract in `api_schemas.py`, server-side computation in `model.py`, pure math in `death-distribution.ts`, and view wiring in `segment-detail.ts`.

---

## Task 1: Backend — extend SegmentHistory with selected_model + final_extras

**Files:**
- Modify: `python/spinlab/api_schemas.py`
- Modify: `python/spinlab/routes/model.py`
- Test: `tests/unit/test_dashboard_integration.py`

### Step 1.1 — Add the schema fields

- [ ] **Step 1.1.1: Add `final_extras` to `EstimatorCurves` and `selected_model` to `SegmentHistory`**

Edit `python/spinlab/api_schemas.py` around line 280-294.

Find:

```python
class EstimatorCurves(_BaseResponse):
    total: EstimatorSeries
    clean: EstimatorSeries


class SegmentHistory(_BaseResponse):
    segment_id: str
    description: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int
    attempts: list[SegmentAttempt]
    estimator_curves: dict[str, EstimatorCurves]
```

Replace with:

```python
class EstimatorCurves(_BaseResponse):
    total: EstimatorSeries
    clean: EstimatorSeries
    # DeathExtras from the estimator's final state (after every completed
    # attempt). None when the estimator doesn't publish death-aware extras
    # (every estimator other than death_aware_rolling today) or when the
    # segment has no completed attempts. Drives the death-histogram panel
    # on the segment detail page.
    final_extras: DeathExtras | None = None


class SegmentHistory(_BaseResponse):
    segment_id: str
    description: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int
    attempts: list[SegmentAttempt]
    estimator_curves: dict[str, EstimatorCurves]
    # Name of the currently active estimator (mirrors sched.estimator.name).
    # Frontend uses this to pick which estimator_curves entry's final_extras
    # to render.
    selected_model: str
```

- [ ] **Step 1.1.2: Verify the OpenAPI schema dumps cleanly**

Run:

```powershell
python scripts/dump_openapi.py frontend/openapi.json
```

Expected: exit 0, file written. Open it and confirm `SegmentHistory.required` includes `selected_model` and that `EstimatorCurves` lists `final_extras` as optional.

### Step 1.2 — Wire the route

- [ ] **Step 1.2.1: Update imports in `python/spinlab/routes/model.py`**

Find the top of the file (the import block under `from spinlab.scheduler import _attempts_from_rows`):

```python
from spinlab.scheduler import _attempts_from_rows
from spinlab.session_manager import SessionManager
```

Add `_events_from_rows` to the scheduler import:

```python
from spinlab.scheduler import _attempts_from_rows, _events_from_rows
from spinlab.session_manager import SessionManager
```

- [ ] **Step 1.2.2: Add `SessionManager` dependency to `segment_history` and load events once**

Find `segment_history` (line ~130):

```python
@router.get("/segments/{segment_id}/history", response_model=SegmentHistory)
def segment_history(segment_id: str, db: Database = Depends(get_db)):
    seg = db.get_segment_by_id(segment_id)
    if seg is None:
        logger.warning("segment_history: unknown segment %r", segment_id)
        raise HTTPException(status_code=404, detail=f"Segment not found: {segment_id}")

    raw_rows = db.get_segment_attempts(segment_id)
    # _attempts_from_rows already drops invalidated; filter to completed too.
    all_records = _attempts_from_rows(raw_rows)
    completed = [a for a in all_records if a.completed and a.time_ms is not None]
```

Replace with:

```python
@router.get("/segments/{segment_id}/history", response_model=SegmentHistory)
def segment_history(
    segment_id: str,
    db: Database = Depends(get_db),
    session: SessionManager = Depends(get_session),
):
    seg = db.get_segment_by_id(segment_id)
    if seg is None:
        logger.warning("segment_history: unknown segment %r", segment_id)
        raise HTTPException(status_code=404, detail=f"Segment not found: {segment_id}")

    raw_rows = db.get_segment_attempts(segment_id)
    # _attempts_from_rows already drops invalidated; filter to completed too.
    all_records = _attempts_from_rows(raw_rows)
    completed = [a for a in all_records if a.completed and a.time_ms is not None]

    # Load events once for death-aware estimators. They produce DeathExtras
    # from events, not from AttemptRecords. Estimators that ignore events
    # (rolling_mean, kalman) get this argument harmlessly.
    event_rows = db.get_segment_event_rows(segment_id)
    events = _events_from_rows(event_rows)
```

- [ ] **Step 1.2.3: Capture final extras per estimator**

Find the per-estimator loop's tail and the response dict (line ~155-198):

```python
        estimator_curves[est_name] = {
            "total": {"expected_ms": total_expected, "floor_ms": total_floor},
            "clean": {"expected_ms": clean_expected, "floor_ms": clean_floor},
        }

    return {
        "segment_id": segment_id,
        "description": seg.description,
        "level_number": seg.level_number,
        "start_type": seg.start_type,
        "start_ordinal": seg.start_ordinal,
        "end_type": seg.end_type,
        "end_ordinal": seg.end_ordinal,
        "attempts": attempts,
        "estimator_curves": estimator_curves,
    }
```

Replace with:

```python
        # Compute final extras by rebuilding state with the full event list.
        # The per-attempt curve loop above doesn't pass events (so e.g.
        # death_aware_rolling returns empty curves there — pre-existing
        # behavior, separate concern). For the final-state snapshot used by
        # the histogram panel we want extras to actually be populated.
        final_extras = None
        if completed and events:
            final_state = est.rebuild_state(completed, params=params, events=events)
            final_out = est.model_output(
                final_state, completed, params=params, events=events,
            )
            final_extras = (
                final_out.extras.to_dict() if final_out.extras is not None else None
            )

        estimator_curves[est_name] = {
            "total": {"expected_ms": total_expected, "floor_ms": total_floor},
            "clean": {"expected_ms": clean_expected, "floor_ms": clean_floor},
            "final_extras": final_extras,
        }

    sched = session.get_scheduler() if session.game_id is not None else None
    selected_model = sched.estimator.name if sched is not None else ""

    return {
        "segment_id": segment_id,
        "description": seg.description,
        "level_number": seg.level_number,
        "start_type": seg.start_type,
        "start_ordinal": seg.start_ordinal,
        "end_type": seg.end_type,
        "end_ordinal": seg.end_ordinal,
        "attempts": attempts,
        "estimator_curves": estimator_curves,
        "selected_model": selected_model,
    }
```

### Step 1.3 — Tests

- [ ] **Step 1.3.1: Add tests for the new fields**

In `tests/unit/test_dashboard_integration.py`, append two new tests to the `# -- Segment history` block (after `test_segment_history_no_completed_attempts`, around line 452):

```python
def test_segment_history_returns_selected_model(client):
    """selected_model mirrors the scheduler's active estimator name."""
    resp = client.get("/api/segments/s1/history")
    assert resp.status_code == 200
    data = resp.json()
    # The seeded client uses the default scheduler — "kalman" is the
    # default estimator, matching test_recent_attempts_ordered* assertions.
    assert data["selected_model"] == "kalman"


def test_segment_history_final_extras_present_for_death_aware(
    seeded_db, client,
):
    """Death-aware estimator returns a populated final_extras dict when
    the segment has events; legacy estimators return None."""
    from datetime import datetime

    from spinlab.models import (
        AttemptOutcome, AttemptSource, EventAttempt,
    )

    # Seed two episodes' worth of events for s1.
    # Episode A: died at 2000ms, then survived at 4500ms.
    # Episode B: survived directly at 3800ms.
    seeded_db.log_event_attempt(EventAttempt(
        segment_id="s1", episode_id="epA",
        outcome=AttemptOutcome("died"), time_ms=2000,
        session_id="sess1", capture_run_id=None,
        source=AttemptSource.PRACTICE, chosen_allocator=None,
        invalidated=False, created_at=datetime.now(),
    ))
    seeded_db.log_event_attempt(EventAttempt(
        segment_id="s1", episode_id="epA",
        outcome=AttemptOutcome("survived"), time_ms=4500,
        session_id="sess1", capture_run_id=None,
        source=AttemptSource.PRACTICE, chosen_allocator=None,
        invalidated=False, created_at=datetime.now(),
    ))
    seeded_db.log_event_attempt(EventAttempt(
        segment_id="s1", episode_id="epB",
        outcome=AttemptOutcome("survived"), time_ms=3800,
        session_id="sess1", capture_run_id=None,
        source=AttemptSource.PRACTICE, chosen_allocator=None,
        invalidated=False, created_at=datetime.now(),
    ))

    resp = client.get("/api/segments/s1/history")
    assert resp.status_code == 200
    curves = resp.json()["estimator_curves"]

    da = curves["death_aware_rolling"]["final_extras"]
    assert da is not None
    # One died event in our seeded data.
    assert len(da["death_samples"]) == 1
    assert da["death_samples"][0][0] == 2000  # (time_ms, weight)
    # Two survived events.
    assert len(da["completion_samples"]) == 2
    # p_die_per_life = 1 / (1 + 2) = 1/3 (unweighted at most-recent end).
    assert da["p_die_per_life"] is not None
    assert 0.0 < da["p_die_per_life"] < 1.0

    # Legacy estimators don't publish extras.
    assert curves["kalman"]["final_extras"] is None
    assert curves["rolling_mean"]["final_extras"] is None
```

- [ ] **Step 1.3.2: Run the dashboard-integration tests to confirm**

Run:

```powershell
python -m pytest tests/unit/test_dashboard_integration.py -k segment_history -v
```

Expected: all six `segment_history` tests pass (four pre-existing + two new).

- [ ] **Step 1.3.3: Run the full non-emulator suite**

Run:

```powershell
python -m pytest -m "not emulator"
```

Expected: green. If anything else breaks, you've drifted the contract — `final_extras` is optional with a default of `None`, and `selected_model` is a new required field on `SegmentHistory`. Pydantic's `extra="allow"` base means responses that omit `final_extras` deserialize fine; tests that explicitly assert response shape (and there are very few — only the cluster you edited touches `/segments/{id}/history`) would be the only failure surface.

### Step 1.4 — Commit

- [ ] **Step 1.4.1: Commit the backend change**

```powershell
git add python/spinlab/api_schemas.py python/spinlab/routes/model.py tests/unit/test_dashboard_integration.py
git commit -m @'
feat(api): expose selected_model + per-estimator final_extras on segment history

SegmentHistory now carries the active estimator name; EstimatorCurves
gains an optional DeathExtras payload from the estimator's final state.
Feeds the segment-detail death-histogram panel on the frontend.
'@
```

---

## Task 2: Frontend — histogram math (pure functions)

**Files:**
- Create: `frontend/src/death-distribution.ts` (math layer only — render layer follows in Task 3)
- Create: `frontend/src/death-distribution.test.ts`

### Step 2.1 — Regenerate frontend types

- [ ] **Step 2.1.1: Pull the updated OpenAPI types into the frontend**

Run:

```powershell
cd frontend; npm run gen-types
```

Expected: exit 0, `frontend/src/api-types.ts` updated. Then confirm by grepping the regenerated file:

```powershell
Select-String -Path frontend/src/api-types.ts -Pattern "final_extras|selected_model" | Select-Object -First 10
```

Expected: at least two hits — the `final_extras` optional on `EstimatorCurves` and the `selected_model` field on `SegmentHistory`.

- [ ] **Step 2.1.2: Confirm the re-export facade still compiles**

Run:

```powershell
cd frontend; npm run typecheck
```

Expected: no errors.

### Step 2.2 — Write the binning function (red)

- [ ] **Step 2.2.1: Create the test file with the binning contract**

Create `frontend/src/death-distribution.test.ts`:

```typescript
import { describe, it, expect, vi } from "vitest";
import { binSamples, BIN_COUNT } from "./death-distribution";

// vitest's happy-dom env is configured globally; vi.mock for Chart.js is
// only needed when we exercise the render function (Step 3.x).

describe("binSamples", () => {
  it("returns BIN_COUNT zero-counts on empty input", () => {
    const { bins, lo, hi } = binSamples([], []);
    expect(bins).toHaveLength(BIN_COUNT);
    for (const b of bins) {
      expect(b.deaths).toBe(0);
      expect(b.completions).toBe(0);
    }
    expect(lo).toBe(0);
    expect(hi).toBe(0);
  });

  it("places a single sample at time 0 into bin 0", () => {
    const { bins } = binSamples([[0, 1.0]], []);
    expect(bins[0]!.deaths).toBe(1);
    expect(bins[0]!.completions).toBe(0);
    for (let i = 1; i < BIN_COUNT; i++) {
      expect(bins[i]!.deaths).toBe(0);
    }
  });

  it("clamps a sample at hi into the topmost bin", () => {
    // Single sample at exactly the max — must not overflow.
    const { bins } = binSamples([[10_000, 1.0]], []);
    expect(bins[BIN_COUNT - 1]!.deaths).toBe(1);
    // Total across all bins == sample count.
    const total = bins.reduce((acc, b) => acc + b.deaths, 0);
    expect(total).toBe(1);
  });

  it("counts deaths and completions independently per bin", () => {
    // hi will be ceil(max / 1000) * 1000 = 10000 → bin width 500ms.
    const deaths: [number, number][] = [
      [100, 1.0],   // bin 0
      [200, 1.0],   // bin 0
      [5100, 1.0],  // bin 10
    ];
    const completions: [number, number][] = [
      [150, 1.0],   // bin 0
      [5100, 1.0],  // bin 10
      [9900, 1.0],  // bin 19
    ];
    const { bins } = binSamples(deaths, completions);
    expect(bins[0]!.deaths).toBe(2);
    expect(bins[0]!.completions).toBe(1);
    expect(bins[10]!.deaths).toBe(1);
    expect(bins[10]!.completions).toBe(1);
    expect(bins[19]!.deaths).toBe(0);
    expect(bins[19]!.completions).toBe(1);
  });

  it("rounds hi up to the nearest second", () => {
    const { hi } = binSamples([[3200, 1.0]], []);
    expect(hi).toBe(4000);
  });

  it("ignores sample weights for bar heights (raw counts only)", () => {
    const { bins } = binSamples([[100, 0.001], [100, 0.001]], []);
    // Two samples in bin 0 regardless of weight.
    expect(bins[0]!.deaths).toBe(2);
  });
});
```

- [ ] **Step 2.2.2: Run the test to confirm it fails**

Run:

```powershell
cd frontend; npx vitest run src/death-distribution.test.ts
```

Expected: FAIL — module `./death-distribution` not found.

### Step 2.3 — Implement `binSamples` (green)

- [ ] **Step 2.3.1: Create `frontend/src/death-distribution.ts` with the math**

Create `frontend/src/death-distribution.ts`:

```typescript
// Fixed bin count. Twenty is enough to see distribution shape in a
// segment detail card, few enough to read at a glance, and predictable
// (no Freedman-Diaconis surprises across segments).
export const BIN_COUNT = 20;

// One-second rounding for the upper edge so x-axis labels land on
// whole seconds without manual tick configuration.
const HI_ROUND_MS = 1000;

export interface Bin {
  // Left edge (inclusive) and right edge (exclusive) of this bin, in ms.
  // The topmost bin's right edge is inclusive (clamped).
  lo_ms: number;
  hi_ms: number;
  deaths: number;
  completions: number;
}

export interface BinSummary {
  bins: Bin[];
  lo: number;
  hi: number;
}

/**
 * Bucket death and completion samples into shared bins by time_ms.
 * Sample weights are ignored — bar heights are raw counts. Weighted
 * statistics (means) are surfaced via marker overlays, not bar height.
 *
 * The range always starts at 0; the upper edge is the max sample value
 * rounded up to the nearest second. Empty input returns BIN_COUNT
 * zero-counts with lo=hi=0.
 */
export function binSamples(
  deaths: [number, number][],
  completions: [number, number][],
): BinSummary {
  const empty = (): Bin[] => Array.from({ length: BIN_COUNT }, (_, i) => ({
    lo_ms: 0, hi_ms: 0, deaths: 0, completions: 0,
  }));

  if (deaths.length === 0 && completions.length === 0) {
    return { bins: empty(), lo: 0, hi: 0 };
  }

  let maxMs = 0;
  for (const [t] of deaths) if (t > maxMs) maxMs = t;
  for (const [t] of completions) if (t > maxMs) maxMs = t;
  const hi = Math.ceil(maxMs / HI_ROUND_MS) * HI_ROUND_MS;
  const lo = 0;
  const width = (hi - lo) / BIN_COUNT;

  const bins: Bin[] = Array.from({ length: BIN_COUNT }, (_, i) => ({
    lo_ms: lo + i * width,
    hi_ms: lo + (i + 1) * width,
    deaths: 0,
    completions: 0,
  }));

  const placeIdx = (t: number): number => {
    if (width === 0) return 0;
    let idx = Math.floor((t - lo) / width);
    if (idx >= BIN_COUNT) idx = BIN_COUNT - 1;
    if (idx < 0) idx = 0;
    return idx;
  };

  for (const [t] of deaths) bins[placeIdx(t)]!.deaths += 1;
  for (const [t] of completions) bins[placeIdx(t)]!.completions += 1;

  return { bins, lo, hi };
}
```

- [ ] **Step 2.3.2: Run the test to confirm it passes**

Run:

```powershell
cd frontend; npx vitest run src/death-distribution.test.ts
```

Expected: all six `binSamples` tests pass.

### Step 2.4 — Commit

- [ ] **Step 2.4.1: Commit the math layer**

```powershell
git add frontend/src/death-distribution.ts frontend/src/death-distribution.test.ts frontend/src/api-types.ts
git commit -m @'
feat(frontend): histogram binning helper for death-aware distributions

Pure binSamples() module backed by Vitest. Twenty fixed bins, range
0..ceil(max/1000)*1000. Weights deliberately ignored — bar heights are
raw counts; weighted means surface via marker overlays in the render
layer (next commit).
'@
```

---

## Task 3: Frontend — render the panel + marker plugin + integrate

**Files:**
- Modify: `frontend/src/death-distribution.ts` — add `renderDeathDistribution`, the inline marker plugin, helpers.
- Modify: `frontend/src/death-distribution.test.ts` — render smoke test.
- Modify: `frontend/src/segment-detail.ts` — instantiate the panel below the existing chart.

### Step 3.1 — Extend the test for the renderer (red)

- [ ] **Step 3.1.1: Add a render smoke test**

Edit `frontend/src/death-distribution.test.ts`. At the top, **add** the Chart.js mock (matching `segment-detail.test.ts`'s pattern):

```typescript
// Mock Chart.js — happy-dom doesn't run canvas. We just verify the
// component constructs the chart with the expected data shape and
// writes the header text we expect.
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
  BarController: class {},
  BarElement: class {},
  LinearScale: class {},
  CategoryScale: class {},
  Legend: class {},
  Tooltip: class {},
}));
```

At the bottom of the file, **append**:

```typescript
import { renderDeathDistribution } from "./death-distribution";
import type { components } from "./api-types";

type DeathExtras = components["schemas"]["DeathExtras"];

const SAMPLE_EXTRAS: DeathExtras = {
  halflife_attempts: 20,
  n_attempts_effective: 5.0,
  n_episodes_with_death_eff: 2.0,
  n_episodes_completed_eff: 4.0,
  p_die_per_attempt: 0.4,
  n_lives_died_effective: 2.0,
  n_lives_survived_effective: 4.0,
  p_die_per_life: 0.333,
  death_samples: [[2000, 1.0], [2200, 0.9]],
  completion_samples: [[4500, 1.0], [4800, 0.9], [3800, 0.7], [5100, 0.5]],
  expected_death_time_ms: 2100,
  expected_completion_time_ms: 4500,
};

describe("renderDeathDistribution", () => {
  it("renders the header stats and a chart canvas when extras are present", () => {
    const container = document.createElement("div");
    renderDeathDistribution(container, SAMPLE_EXTRAS);
    expect(container.querySelector(".death-distribution")).not.toBeNull();
    const header = container.querySelector(".death-distribution-header");
    expect(header?.textContent).toContain("0.33");  // p_die_per_life rounded
    expect(header?.textContent).toContain("0.40");  // p_die_per_attempt
    expect(header?.textContent).toContain("20");    // halflife
    expect(container.querySelector("canvas")).not.toBeNull();
  });

  it("renders an empty-state message when extras are null", () => {
    const container = document.createElement("div");
    renderDeathDistribution(container, null);
    expect(container.querySelector(".death-distribution")).not.toBeNull();
    expect(container.querySelector("canvas")).toBeNull();
    expect(container.textContent).toContain("No death data");
  });

  it("renders an empty-state message when sample arrays are both empty", () => {
    const container = document.createElement("div");
    const empty: DeathExtras = {
      ...SAMPLE_EXTRAS,
      death_samples: [],
      completion_samples: [],
      expected_death_time_ms: null,
      expected_completion_time_ms: null,
      p_die_per_attempt: null,
      p_die_per_life: null,
    };
    renderDeathDistribution(container, empty);
    expect(container.querySelector("canvas")).toBeNull();
    expect(container.textContent).toContain("No death data");
  });
});
```

- [ ] **Step 3.1.2: Run the new tests to confirm they fail**

Run:

```powershell
cd frontend; npx vitest run src/death-distribution.test.ts
```

Expected: the three new render tests fail with "renderDeathDistribution is not a function" (or an import error). The six `binSamples` tests still pass.

### Step 3.2 — Implement the renderer + marker plugin (green)

- [ ] **Step 3.2.1: Append the render layer to `frontend/src/death-distribution.ts`**

Add to the bottom of `frontend/src/death-distribution.ts`:

```typescript
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
import type { components } from "./api-types";

Chart.register(BarController, BarElement, LinearScale, CategoryScale, Legend, Tooltip);

type DeathExtras = components["schemas"]["DeathExtras"];

// Colors chosen to match the conventional red/green of failure/success
// without being saturated enough to fight the existing line-chart palette.
const DEATH_COLOR = "rgba(255, 100, 100, 0.55)";
const DEATH_LINE = "rgba(255, 100, 100, 0.95)";
const COMPLETION_COLOR = "rgba(100, 200, 100, 0.55)";
const COMPLETION_LINE = "rgba(100, 200, 100, 0.95)";

interface MarkerPluginOptions {
  death_ms: number | null;
  completion_ms: number | null;
}

// Inline Chart.js plugin: draws two vertical lines + labels on top of the
// bar chart for the weighted-mean death/completion times. The full chartjs
// plugin-annotation dep is overkill for two lines.
const deathMarkersPlugin = {
  id: "deathMarkers",
  afterDatasetsDraw(chart: Chart) {
    const opts = (chart.options.plugins as Record<string, unknown> | undefined)
      ?.deathMarkers as MarkerPluginOptions | undefined;
    if (!opts) return;
    const { ctx, chartArea, scales } = chart;
    const xScale = scales["x"];
    if (!xScale) return;
    const draw = (ms: number | null, color: string, label: string) => {
      if (ms == null) return;
      const x = xScale.getPixelForValue(ms);
      if (x < chartArea.left || x > chartArea.right) return;
      ctx.save();
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, chartArea.top);
      ctx.lineTo(x, chartArea.bottom);
      ctx.stroke();
      ctx.font = "11px sans-serif";
      ctx.fillText(label, x + 3, chartArea.top + 12);
      ctx.restore();
    };
    draw(opts.death_ms, DEATH_LINE, "μ_d");        // μ_d
    draw(opts.completion_ms, COMPLETION_LINE, "μ_c"); // μ_c
  },
};
Chart.register(deathMarkersPlugin);

let _histChart: Chart | null = null;

function buildHeader(extras: DeathExtras): HTMLElement {
  const header = document.createElement("div");
  header.className = "death-distribution-header";
  const title = document.createElement("h4");
  title.textContent = "Death distribution";
  header.appendChild(title);

  const stats = document.createElement("div");
  stats.className = "death-distribution-stats dim";
  const parts: string[] = [];
  parts.push(`halflife: ${extras.halflife_attempts} ep`);
  if (extras.p_die_per_life != null) {
    parts.push(`p(die|life): ${extras.p_die_per_life.toFixed(2)}`);
  }
  if (extras.p_die_per_attempt != null) {
    parts.push(`p(die|attempt): ${extras.p_die_per_attempt.toFixed(2)}`);
  }
  stats.textContent = parts.join("   ");
  header.appendChild(stats);
  return header;
}

function buildEmptyState(): HTMLElement {
  const msg = document.createElement("p");
  msg.className = "dim";
  msg.textContent =
    "No death data — selected estimator doesn’t publish death distributions.";
  return msg;
}

export function renderDeathDistribution(
  container: HTMLElement,
  extras: DeathExtras | null,
): void {
  destroyDeathDistribution();

  const section = document.createElement("section");
  section.className = "death-distribution";
  container.appendChild(section);

  if (extras === null) {
    const headerStub = document.createElement("div");
    headerStub.className = "death-distribution-header";
    const title = document.createElement("h4");
    title.textContent = "Death distribution";
    headerStub.appendChild(title);
    section.appendChild(headerStub);
    section.appendChild(buildEmptyState());
    return;
  }

  section.appendChild(buildHeader(extras));

  if (extras.death_samples.length === 0 && extras.completion_samples.length === 0) {
    section.appendChild(buildEmptyState());
    return;
  }

  const { bins } = binSamples(extras.death_samples, extras.completion_samples);

  const chartWrap = document.createElement("div");
  chartWrap.className = "chart-wrapper";
  const canvas = document.createElement("canvas");
  canvas.id = "death-histogram";
  chartWrap.appendChild(canvas);
  section.appendChild(chartWrap);

  // Bin centers (ms) so the bar x-position is meaningful on a linear scale.
  const labels = bins.map((b) => (b.lo_ms + b.hi_ms) / 2);
  const deathData = bins.map((b) => b.deaths);
  const completionData = bins.map((b) => b.completions);

  _histChart = new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          label: "deaths",
          data: deathData,
          backgroundColor: DEATH_COLOR,
          borderColor: DEATH_LINE,
          borderWidth: 1,
        },
        {
          label: "completions",
          data: completionData,
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
        x: {
          title: { display: true, text: "Time" },
          ticks: {
            callback: (v, idx) => formatTime(Number(labels[idx] ?? 0)),
          },
        },
        y: {
          title: { display: true, text: "Samples" },
          ticks: { precision: 0 },
        },
      },
      plugins: {
        legend: { position: "top" },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y}`,
          },
        },
        // Custom plugin sees this via chart.options.plugins.deathMarkers.
        // Cast at the boundary; the plugin reads it as MarkerPluginOptions.
        deathMarkers: {
          death_ms: extras.expected_death_time_ms,
          completion_ms: extras.expected_completion_time_ms,
        },
      } as unknown as Record<string, unknown>,
    },
  });
}

export function destroyDeathDistribution(): void {
  if (_histChart) {
    _histChart.destroy();
    _histChart = null;
  }
}
```

- [ ] **Step 3.2.2: Run the tests**

Run:

```powershell
cd frontend; npx vitest run src/death-distribution.test.ts
```

Expected: all nine tests (six `binSamples` + three `renderDeathDistribution`) pass.

### Step 3.3 — Wire into segment-detail

- [ ] **Step 3.3.1: Import and instantiate from `segment-detail.ts`**

Edit `frontend/src/segment-detail.ts`.

Add to the imports at the top:

```typescript
import { renderDeathDistribution, destroyDeathDistribution } from "./death-distribution";
```

Find the end of `renderSegmentDetail` (after the existing `_chart = new Chart(...)` block and the toggle button listeners — around line 180, just before the function closes). The very last thing the function does today is wire up `cleanBtn.addEventListener`. Append the panel render right after that, **before** the closing brace of `renderSegmentDetail`:

```typescript
  // Death-distribution panel. Reads final_extras for the active estimator.
  // Renders an empty state when the active estimator doesn't publish extras
  // or the segment has no death/completion events yet.
  const curvesForSelected = history.estimator_curves[history.selected_model];
  const extras = curvesForSelected?.final_extras ?? null;
  renderDeathDistribution(container, extras);
```

Then find `destroySegmentDetail`:

```typescript
export function destroySegmentDetail(): void {
  if (_chart) {
    _chart.destroy();
    _chart = null;
  }
  _history = null;
  _mode = "total";
}
```

Replace with:

```typescript
export function destroySegmentDetail(): void {
  if (_chart) {
    _chart.destroy();
    _chart = null;
  }
  destroyDeathDistribution();
  _history = null;
  _mode = "total";
}
```

- [ ] **Step 3.3.2: Update the existing `segment-detail.test.ts` mock to include the new history fields**

Edit `frontend/src/segment-detail.test.ts`. Find `MOCK_HISTORY` (line ~4-27). Add `selected_model` and `final_extras: null` to each estimator-curves entry:

```typescript
const MOCK_HISTORY: SegmentHistory = {
  segment_id: "s1",
  description: "Yoshi's Island 1",
  level_number: 1,
  start_type: "entrance",
  start_ordinal: 0,
  end_type: "goal",
  end_ordinal: 0,
  selected_model: "kalman",
  attempts: [
    { attempt_number: 1, time_ms: 4500, clean_tail_ms: 4500, deaths: 0, created_at: "2026-04-01T12:00:00Z" },
    { attempt_number: 2, time_ms: 3800, clean_tail_ms: 3200, deaths: 0, created_at: "2026-04-01T12:05:00Z" },
    { attempt_number: 3, time_ms: 3200, clean_tail_ms: 3200, deaths: 0, created_at: "2026-04-01T12:10:00Z" },
  ],
  estimator_curves: {
    kalman: {
      total: { expected_ms: [4500, 4150, 3700], floor_ms: [null, null, null] },
      clean: { expected_ms: [4500, 3850, 3500], floor_ms: [null, null, null] },
      final_extras: null,
    },
    rolling_mean: {
      total: { expected_ms: [4500, 4150, 3833], floor_ms: [null, null, null] },
      clean: { expected_ms: [4500, 3850, 3633], floor_ms: [null, null, null] },
      final_extras: null,
    },
  },
};
```

- [ ] **Step 3.3.3: Run the full frontend suite**

Run:

```powershell
cd frontend; npm test
```

Expected: green (all existing tests + nine new tests).

- [ ] **Step 3.3.4: Run frontend typecheck and build**

Run:

```powershell
cd frontend; npm run typecheck; npm run build
```

Expected: both exit 0. The build copies `frontend/dist` into `python/spinlab/static/` per project convention.

### Step 3.4 — Run the full pytest suite

- [ ] **Step 3.4.1: Full unfiltered run per CLAUDE.md "Merging Branches" rule**

Run:

```powershell
python -m pytest
```

Expected: green, including the frontend smoke tests (which require the build done in Step 3.3.4). If emulator tests skip, that's a CLAUDE.md violation — fix or surface before the next commit (see "feedback_run_emulator_tests" memory).

### Step 3.5 — Commit

- [ ] **Step 3.5.1: Commit the render layer + integration**

```powershell
git add frontend/src/death-distribution.ts frontend/src/death-distribution.test.ts frontend/src/segment-detail.ts frontend/src/segment-detail.test.ts
git commit -m @'
feat(frontend): segment-detail death histogram panel

Adds a Chart.js bar chart below the existing line chart on the segment
detail page. Overlays death and completion histograms with weighted-mean
vertical markers via a tiny inline plugin. Empty-state when the active
estimator doesn't publish DeathExtras or the segment has no events.
'@
```

---

## Task 4: Manual verification

**Files:** none — this task is a behavioral check.

- [ ] **Step 4.1: Start the dashboard and open a segment detail page**

Run:

```powershell
spinlab dashboard
```

Then in another shell or the browser, open `http://localhost:8000/`, navigate to the Model tab, switch the active estimator to **Death-Aware Rolling**, click into a segment that has at least one died event in its history.

Expected:

- The existing line chart at the top is unchanged.
- A "Death distribution" header appears below it with `halflife`, `p(die|life)`, `p(die|attempt)` text.
- A bar chart shows red (deaths) and green (completions) bars overlaid.
- Two vertical lines labeled `μ_d` and `μ_c` appear at the weighted-mean death and completion times respectively, in matching red/green.
- X-axis tick labels are formatted as `mm:ss.fff` (e.g. `0:02.000`).

- [ ] **Step 4.2: Verify the empty state**

In the same dashboard, switch the active estimator back to **Kalman** (or any non-death-aware estimator). Open the same segment detail page.

Expected:

- "Death distribution" header still appears.
- Instead of a chart, the panel shows: `No death data — selected estimator doesn't publish death distributions.`

- [ ] **Step 4.3: Verify hide/show stability**

Open a fresh segment detail page, click Back, click into the same segment again.

Expected:

- No duplicate panels, no console errors, no stale chart pixels.
- The `destroyDeathDistribution()` lifecycle is firing correctly.

- [ ] **Step 4.4: Report**

If anything in 4.1–4.3 deviates, stop and surface it — do not patch silently. Otherwise, the implementation is complete; close any open todos and move on.

---

## Out of Scope

- Fixing the per-attempt curve for `death_aware_rolling` in `segment_history` (it returns empty curves today because the loop doesn't pass `events=` mid-replay; this plan only fixes the **final** extras for the histogram panel).
- Live re-render when the active estimator changes while a detail page is open. The page is short-lived; users navigate back and re-enter.
- Cross-segment histogram comparison or strip-plot rendering. See the spec's "Out of Scope" section.

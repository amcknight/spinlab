# Segment Death Histogram

Per-segment visualization of death-time and completion-time distributions on the segment detail page.

## Problem

The `death_aware_rolling` estimator now computes a rich payload (`DeathExtras`) for every segment: weighted distributions of death times, completion times, point-estimates for the expected death/completion time, and two flavors of death probability. None of that is visible in the UI. The segment detail page shows only attempt totals and estimator curves over time — useful for tracking *progress*, but it answers nothing about *when* in the segment the player tends to die.

A histogram of deaths overlaid against completions, sitting alongside the existing curve, closes that gap for the active practice loop.

## Design Decisions

- **New panel on the segment detail page, below the existing chart.** Stays out of the way of the current line chart; the existing Total / Clean Tail toggle continues to do exactly what it does today.
- **Histogram of raw counts + weighted mean markers**, rather than a strip plot or weighted bars. Histograms read at a glance; per-sample weight info can't survive binning without visual gymnastics. The weighted means (which the model actually uses in its geometric formula) recover the "where is the model's mass" signal as two vertical line markers.
- **Driven by the currently selected estimator**, not always `death_aware_rolling`. The panel renders only when the active estimator publishes `DeathExtras`. For other estimators the panel shows an empty-state line. This keeps the histogram honest: it visualizes what the *scheduling-relevant* estimator believes, not a parallel side-channel.
- **Backend computes and ships `final_extras` via the existing history endpoint.** The estimator-curve loop in `segment_history` already runs every estimator over every attempt; it currently throws away `out.extras` at each step. Keep the last one and surface it. No new endpoint, no second fetch on detail-page load, no duplicate state-rebuild loop.
- **No new frontend dependencies.** Markers are drawn by a small inline Chart.js plugin (~15 lines, `afterDatasetsDraw` hook). Avoids pulling in `chartjs-plugin-annotation` for two vertical lines.
- **Pure-function histogram math.** Binning and marker placement live in a new `frontend/src/death-distribution.ts` module, imported by `segment-detail.ts`. Lets the math be unit-tested directly without a canvas.

## API

### `GET /api/segments/{segment_id}/history`

Two additive schema changes — `SegmentHistory` learns the active estimator name, `EstimatorCurves` learns the final extras payload. Everything else is unchanged.

```python
# python/spinlab/api_schemas.py

class EstimatorCurves(_BaseResponse):
    total: EstimatorSeries
    clean: EstimatorSeries
    final_extras: DeathExtras | None = None  # NEW

class SegmentHistory(_BaseResponse):
    # ...existing fields...
    selected_model: str  # NEW — name of the currently active estimator
```

- `selected_model` mirrors `sched.estimator.name` (the same value already returned by `/api/model` and `/api/state`). With it in the response, the frontend reads `history.estimator_curves[history.selected_model].final_extras` directly — no second fetch, no guessing.
- `final_extras` is the `DeathExtras` from the **last** `model_output()` call in the per-estimator replay loop — i.e. the estimator's view after seeing every completed attempt. For estimators that don't publish extras (every estimator other than `death_aware_rolling` today) the field is `null`. Also `null` in the cold-start case (no completed attempts).

### Route change

`segment_history` gains a `SessionManager = Depends(get_session)` dependency so it can read `sched.estimator.name`.

The existing per-estimator loop already builds `out = est.model_output(...)` at each step:

```python
out = est.model_output(state, completed[: j + 1], params=params)
# (curves get appended here today; out.extras is discarded)
```

Capture the last `out` produced by the loop and stash `out.extras.to_dict() if out.extras else None` into the per-estimator response under `final_extras`.

Frontend types regenerate automatically via `npm run gen-types`.

## Frontend

### Layout

A new `<section class="death-distribution">` is appended to `segment-detail.ts`'s detail view, below the existing chart wrapper.

```
┌───────────────────────────────────────────────────────────┐
│  ← Back   Segment: Level 1 entrance → goal                │
│  [ Total ] [ Clean Tail ]                                 │
│  ┌─────────────────────────────────────────────────────┐  │
│  │  (existing attempts + estimator curves line chart)  │  │
│  └─────────────────────────────────────────────────────┘  │
│                                                            │
│  Death distribution           p(die|life): 0.34            │
│  halflife: 20 ep              p(die|attempt): 0.52         │
│  ┌─────────────────────────────────────────────────────┐  │
│  │  ▓░▓▓░░  ▓▓░░░    ░░░░    ░░                        │  │
│  │     ↑μ_d          ↑μ_c                              │  │
│  │  0s    2s    4s   6s    8s    10s        time       │  │
│  └─────────────────────────────────────────────────────┘  │
│  Legend: ▓ deaths  ░ completions                          │
└───────────────────────────────────────────────────────────┘
```

- Header row carries: panel title, halflife (episodes), `p_die_per_life`, `p_die_per_attempt`. Rendered as plain text; styled via existing utility classes (`.dim`, etc.).
- A second `<canvas>` (`#death-histogram`) hosts a separate Chart.js instance with its own lifecycle. `destroySegmentDetail()` is extended to dispose this chart alongside the existing one.
- Two overlaid bar datasets at shared bins: deaths (`rgba(255, 100, 100, 0.5)`), completions (`rgba(100, 200, 100, 0.5)`). No stacking — alpha blending handles overlap visually.
- Vertical line markers via the inline plugin: `expected_death_time_ms` and `expected_completion_time_ms`, labeled `μ_d` / `μ_c`. Either marker is skipped when its source is `null`.

### Histogram math

New module: `frontend/src/death-distribution.ts`. Exports pure functions consumed by `segment-detail.ts` and unit tests.

**Binning:**

- Combined range: `lo = 0`, `hi = ceil(max(time_ms across all death + completion samples) / 1000) * 1000`. Round up to the nearest second so axis labels stay clean.
- `bin_count = 20` (fixed file-level constant with a comment). Width = `(hi - lo) / 20`.
- Each sample is assigned to `floor((time_ms - lo) / width)`, clamped to `bin_count - 1` for the topmost edge case.
- Counts are raw (sample weight is *not* used for bar heights).

**Markers:**

- A small Chart.js plugin (`afterDatasetsDraw`) reads two values from the chart's `options.plugins.deathMarkers` block, converts each `time_ms` to a pixel-x via `chart.scales.x.getPixelForValue(ms)`, strokes a 1px line floor-to-ceiling in the matching color, and renders a small label (`μ_d` / `μ_c`) at the top.
- Skipped when the value is `null`.

**Axes:**

- X: linear ms scale, but tick callbacks format via the existing `formatTime()` helper (`mm:ss.fff`). Title: `Time`.
- Y: integer counts. Title: `Samples`. No log scale.
- Tooltip on bars: `"deaths: N"` / `"completions: N"` (matches Chart.js default placement).

### Empty state

If `final_extras` for the selected estimator is `null`, OR both `death_samples` and `completion_samples` are empty arrays:

- Render the `<section>` and its header so the panel doesn't pop in and out as estimators change.
- Replace the canvas with a single `<p class="dim">No death data — selected estimator doesn't publish death distributions.</p>`.

The frontend reads `history.estimator_curves[history.selected_model].final_extras` to decide what to render. The active estimator can change via the existing estimator switcher in the Model tab; the detail page is reached *through* the Model tab, so the active estimator is implicitly stable for the duration of a detail-page session. No live re-render on estimator switch is in scope.

## Testing

### Backend

In the existing `segment_history` test file:

- One case asserting `estimator_curves["death_aware_rolling"]["final_extras"]` is a populated dict whose `death_samples` length equals the number of `died`-outcome events in the test fixture's attempts.
- One case asserting `estimator_curves["rolling_mean"]["final_extras"]` is `null` (legacy estimator, no extras).

### Frontend

New file: `frontend/src/death-distribution.test.ts`. Vitest + happy-dom (matches existing patterns):

- `binSamples()` with hand-rolled inputs:
  - Empty → all-zero bins, no NaN.
  - Single sample at `time_ms = 0` → bin 0 has count 1.
  - Sample exactly at `hi` → bin `bin_count - 1` (clamping).
  - Mixed deaths + completions → independent counts per bin.
- A render smoke test on `renderDeathDistribution()` analogous to the existing `segment-detail.test.ts` — instantiates a canvas in happy-dom, calls the renderer with sample `DeathExtras`, and asserts the chart instance exists and the panel header has the expected `p_die` strings.

No emulator tests — pure presentation over an existing computation.

## Out of Scope

- Cross-segment comparison views ("which segments do I die in fastest?"). The histogram is per-segment.
- Strip plot / per-sample weight visualization. Deferred; weighted means in the markers carry the model's view for now.
- Toggling between weighted and raw counts. Single deliberate choice (raw counts + weighted-mean markers).
- Live re-render on estimator-switch while the detail page is open. The detail page is short-lived; re-opening picks up the new selection.
- A standalone `/api/segments/{id}/events` endpoint. The existing history endpoint carries everything needed.

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

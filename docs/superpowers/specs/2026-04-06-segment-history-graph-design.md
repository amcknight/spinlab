# Segment History Graph

Per-segment chart showing attempt times and estimator curves over time.

## Problem

The dashboard shows current estimator outputs (expected time, trend, floor) as single numbers in a table. There's no way to visualize how a segment's performance has evolved over attempts, or to compare how different estimators track that data. A graph per segment fills this gap.

## Design Decisions

- **Compute-on-request, not cached.** The backend replays attempts through estimators on each fetch. Replay is pure math over small data (dozens to low hundreds of attempts × 3 estimators). No new DB tables, no cache invalidation when tuning params change.
- **Chart.js** for rendering. Popular, simple API, ~60KB gzipped, zoom plugin available for future use. Imported only in the detail view module.
- **Drill-down view** in the Model tab. Clicking a segment name replaces the tab content with the chart; back arrow returns to the table. Self-contained component that could be relocated to a tab or popup later.
- **Total / Clean Tail toggle** rather than two charts or overlaid series. Keeps the chart readable.
- **All estimators shown simultaneously** with a legend. The chart is cross-model by design — it's a comparison tool.
- **Incomplete and invalidated attempts excluded.** Only completed, non-invalidated attempts appear as data points.

## API

### `GET /api/segments/{segment_id}/history`

Returns raw attempt data and estimator curves replayed from current params.

Response:

```json
{
  "segment_id": "abc123",
  "description": "1-1 entrance.0 → checkpoint.0",
  "attempts": [
    {
      "attempt_number": 1,
      "time_ms": 4200,
      "clean_tail_ms": 4200,
      "deaths": 0,
      "created_at": "2026-04-01T12:00:00Z"
    },
    {
      "attempt_number": 2,
      "time_ms": 5100,
      "clean_tail_ms": 3800,
      "deaths": 1,
      "created_at": "2026-04-01T12:05:00Z"
    }
  ],
  "estimator_curves": {
    "kalman": {
      "total": {
        "expected_ms": [4200, 4650],
        "floor_ms": [4200, 4100]
      },
      "clean": {
        "expected_ms": [4200, 4000],
        "floor_ms": [4200, 3900]
      }
    },
    "rolling_mean": {
      "total": {
        "expected_ms": [4200, 4650],
        "floor_ms": [null, null]
      },
      "clean": {
        "expected_ms": [4200, 4000],
        "floor_ms": [null, null]
      }
    }
  }
}
```

Fields:

- `attempts` — completed, non-invalidated attempts in chronological order. `attempt_number` is a 1-based sequential index (not DB ID).
- `estimator_curves` — keyed by estimator name. Each contains `total` and `clean` sub-objects with `expected_ms` and `floor_ms` arrays. Array length equals `attempts` length. Index `i` is the estimate after processing attempt `i+1`. `null` means the estimator had no estimate at that point.

### Error cases

- Segment not found → 404
- Zero completed attempts → empty `attempts` array, empty curves per estimator

## Backend

Route added to `python/spinlab/routes/model.py`.

Replay logic:

1. Look up segment by ID (404 if missing). Read `game_id` from the segment record (needed for `get_priors`).
2. Fetch attempts via `db.get_segment_attempts(segment_id)`
3. Filter to `completed=True` and `invalidated=False`
4. Load current estimator params from DB for each registered estimator
5. For each estimator:
   - Call `get_priors(db, game_id)` for population priors
   - Call `init_state(first_attempt, priors, params)` on the first completed attempt
   - Call `model_output(state, attempts_so_far)` to record the first curve point
   - For each subsequent attempt: `process_attempt(state, attempt, attempts_so_far, params)` → `model_output(state, attempts_so_far)` → record curve point
6. Assemble response

No new DB tables. No new DB queries — `get_segment_attempts` already returns everything needed.

## Frontend

### New file: `frontend/src/segment-detail.ts`

Self-contained module exporting a render function. Takes a container element and segment ID. Fetches the history endpoint, builds the chart, wires the toggle.

### Drill-down mechanics

- Model tab tracks `currentSegmentId: string | null` (null = show table, non-null = show detail)
- Segment names in the Model table rendered as clickable links
- Click sets `currentSegmentId`, hides table, calls `renderSegmentDetail(container, segmentId)`
- Back arrow at top of detail view clears `currentSegmentId`, restores table

### Chart layout

- **Header:** back arrow + segment name
- **Toggle:** "Total" / "Clean Tail" buttons. Switches which data fields are plotted (total: `time_ms` + `estimator_curves[name].total`, clean: `clean_tail_ms` + `estimator_curves[name].clean`)
- **Chart:** Chart.js line chart, full width
  - X axis: attempt number (1, 2, 3...)
  - Y axis: time in seconds (converted from ms, formatted with `formatTime`)
  - Raw attempts: points with connecting line, semi-transparent, thicker weight
  - Estimator curves: smooth lines, distinct color per estimator
  - Legend at top: estimator names with color swatches + "Attempts" entry

### Chart.js setup

Register only required components for tree-shaking: `LineController`, `LineElement`, `PointElement`, `LinearScale`, `CategoryScale`, `Legend`, `Tooltip`.

### Component isolation

The detail view is a self-contained module. The Model tab calls `renderSegmentDetail()` and `destroySegmentDetail()`. If we later want to move the graph to a dedicated tab or popup, we call the same functions from a different trigger — no refactoring of the chart code itself.

## Dependencies

- `chart.js` added to `frontend/package.json`
- No Python dependency changes
- No DB migrations

## Testing

### Backend

- Unit test: create segment with attempts (mix of completed, incomplete, invalidated), call `/api/segments/{id}/history`, verify:
  - Only completed non-invalidated attempts in response
  - Attempt numbers are sequential starting at 1
  - Estimator curve arrays match attempt count
  - Curve values are non-null after sufficient data
- Unit test: segment with zero completed attempts returns empty arrays
- Unit test: unknown segment ID returns 404

### Frontend

- Unit test: toggle logic correctly switches between total/clean data fields
- API contract test: verify TypeScript types match the response shape from the endpoint

No emulator tests needed.

## Future work (not in this spec)

- Zoom/pan via Chart.js zoom plugin
- Gold time horizontal line on chart
- Click an attempt point to see details (deaths, timestamp)
- Chart accessible from Segments tab as well

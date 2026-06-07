# Planning-table Room & Trend columns

Date: 2026-06-07
Status: design (approved in brainstorm, pending written-spec review)

## Motivation

The planning table (the `/api/model` "Model State" table rendered by
`frontend/src/model-render.ts`) is where you decide what to grind next. Today it
shows `Segment | Expected | Floor | Plays | Best`. Two problems surfaced this
session:

1. **Best is redundant.** For a single segment, the "Best" clean time
   (`gold_ms`) and the running-min-clean "Floor" (`floor_ms`) are the same
   number. Two columns, one fact.
2. **The two signals that actually tell you where to spend practice time are
   missing: room and trend.** Room (how much is left to gain) and the practice
   trend (how fast you'd gain it) appear nowhere in the table.

### What we learned about the trend (why it is surfaced, not trusted)

An offline backtest of the alpha grid against `data/spinlab.db`
(`C:/tmp/alpha_backtest_spike.py`, re-runnable) found that the trend slide does
**not** improve one-step-ahead prediction at our current data depth (median 14.5
attempts/segment; no segment past 43). No `(fast, slow)` pair beat a flat EMA.
The slow alphas (`0.0, 0.01, 0.02, 0.03, 0.05`) are statistically
indistinguishable from each other and from the all-time mean; `0.5`/`1.0` are
volatile. This is an **identifiability** limit, not proof the trend is useless —
a real trend only becomes visible once a single segment is ground past ~40
attempts. So: keep the trend, surface it, but treat it as a *watch-it /
build-intuition* signal, never a ranking driver yet. Re-fire the spike at the
40-attempt trigger.

### The objective the columns serve

"What to practice next" = **what reduces expected total run time the fastest per
wall-clock second of practice.** Because the route is additive
(`total = sum of segment expected episode times`), a segment's practice gain
propagates 1:1 to total time. One practice rep costs ~one episode run of
wall-clock = the segment's `Expected` time. So the value of practicing a segment
is `gain / Expected` = seconds-of-run saved per second-of-practice. That ratio
is `Trend%` below. It is the real allocation objective, but it rides on the
unvalidated slope, so for now **Room% is the robust, trend-free ranking key** and
Trend% is the objective we grow into.

## Vocabulary (the locked definitions)

| Term | Definition | Source |
|---|---|---|
| Floor | achievable-best clean time (Best collapsed into it) | `model_output().total.floor_ms` |
| Expected | expected episode time incl. deaths/reloads = wall-clock cost of one rep | `model_output().total.expected_ms` |
| Room | `Expected - Floor` (absolute, the default thing shown) | derived (frontend) |
| Room% | `(Expected - Floor) / Floor` | derived (frontend) |
| Practice | practice gain = `expected_scalar - slid` (sec saved per rep) | NEW in payload |
| Trend% | `gain / Expected` = value per wall-clock second | derived (frontend) |

`gain` is the existing closed-form practice delta from `live_view.py`:
`expected_episode_time_scalar(state) - expected_episode_time_ms(state,
DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX, apply_slope=True)`. Positive gain = practicing
is predicted to help (faster). It is `None` when the slope is ungated.

Both percentages are numerically safe: `Floor > 0` always (a clean time) and
`Expected` is bounded, so neither Room% nor Trend% blows up. (The rejected
`gain / Room` alternative would have blown up as Room -> 0; we are not using it.)

## Table layout

Columns: `Segment | Floor | Expected | Room | Practice`.

- **Floor** — `formatTime(floor_ms)`, gold-tinted (matches the existing
  `--gold` Floor styling).
- **Expected** — `formatTime(expected_ms)`.
- **Room** — white absolute `formatTime(Expected - Floor)` followed by a grey
  Room% (e.g. `7.7s  45%`). Inline treatment (chosen over stacked / two-column
  in the brainstorm; two-column is a later move only if the row gains space).
- **Practice** — signed, colored gain: green `down-arrow 0.6s` when gain > 0
  (practicing helps), red `up-arrow 0.3s` when gain < 0 (trend says practicing
  hurts -- the known upward-noise pathology), neutral grey near zero; followed
  by a grey Trend% (`gain / Expected`).
- **Best is removed.**

**Default sort: Room% descending** (most headroom on top). Trend% is shown but
does not sort by default.

### None / thin-data handling

When `expected_ms` or `floor_ms` is `None` (segment below the prediction gate),
Room / Room% / Practice / Trend% render blank/dim, exactly as the current table
already dims missing estimates. Practice/Trend% also blank when `gain` is `None`
(slope ungated even if expected is present).

## Backend change (one wiring)

`Expected` and `Floor` already ride in the `/api/model` per-segment payload via
`model_outputs[name].total.{expected_ms, floor_ms}`. **The gain does not.** Add
`practice_gain_ms` to the per-segment model payload, computed by the same formula
`live_view.py` uses, so there is one definition of the practice delta, not two.

Mechanism (final shape left to the plan): either lift the gain computation into a
small shared helper called by both `live_segment_view` and the `/model` route, or
extend `model_output()` to populate a gain field on `ModelOutput.total`. Either
way: same inputs (`state`, default alpha pair, `reload_penalty_ms`), `None` when
ungated. This touches `python/spinlab/api_schemas.py` (the `ModelData` / segment
schema), which regenerates the frontend types on the next `npm run build`
(`gen-types`).

## Frontend change

`frontend/src/model-render.ts` `renderModelTable`: update headers, drop the
`gold_ms` cell, add the Room and Practice cells with the abs+grey-% treatment and
the signed/colored Practice gain, and sort segments by Room% descending. The
Room / Room% / Trend% arithmetic and the sort comparator live in
`frontend/src/model-logic.ts` (pure, unit-tested) so the renderer stays a thin
view. Update the empty-state `colspan` to match the new column count.

## What ranks, and the trust story (carried in the UI)

- Room% ranks now (robust, trend-free).
- Trend% is the value-per-wall-clock objective, shown but provisional because it
  rides the unvalidated slope. It is deliberately *not* the default sort.
- The `Plays` (n_completed) count is the trust signal for the provisional
  columns -- see open question below.

## Out of scope (explicitly deferred)

- The sparkline and any richer trend viz -> the click-into segment **detail/lab
  view**, a later spec. The detail view is the "perpetual lab" where
  experimental signals live at full size until they earn promotion into the row.
- Live-card changes (Floor-delta-on-improvement, demote verdict to chip, number
  headline) -> separate spec.
- Merging the separate "Practice Next" engine panel into this table.
- Any allocator rewiring (the current allocators are stale / not hooked to the
  new sampler) and the pooled learning-curve prior -> separate modeling work.

## Open question for review

- **Keep the `Plays` (n_completed) column?** It was not in the locked mockups
  (which showed only Floor/Expected/Room/Practice) but exists today and is the
  natural confidence cue for the provisional Practice/Trend% numbers.
  Recommendation: keep it, placed at the row end as a quiet trust column.

## Testing

- **Backend:** `/api/model` payload includes `practice_gain_ms`; it equals the
  `live_view` gain for the same state; `None` when the slope is ungated.
- **Frontend (`model-logic.test.ts`):** Room = Expected - Floor; Room% and
  Trend% arithmetic; Room%-descending sort; None propagation.
- **Frontend (`model-render.test.ts`):** Best cell gone; Room cell renders
  abs + grey %; Practice cell renders signed/colored gain + grey Trend%; correct
  column count / headers.
- Full gate before merge: `python -m pytest` + `cd frontend && npm test` +
  `npm run typecheck` + `npm run build`.

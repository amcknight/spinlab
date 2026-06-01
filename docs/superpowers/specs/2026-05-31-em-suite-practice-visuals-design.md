# EMA-Suite Practice Visuals — Design

**Date:** 2026-05-31
**Status:** Brainstorming captured, awaiting review
**Scope:** Surface the per-segment EMA-suite sampler's internals live during a Practice (or Hyperplay) session so Andrew can visually confirm the algorithm is doing what it should. Builds on [`2026-05-30-em-suite-sampler-design.md`](2026-05-30-em-suite-sampler-design.md). No new modeling in Phase 1; Phase 2 introduces a bootstrap-with-slide sampler.

## Motivation

The EMA-suite matrix view today lives only on the Model-tab segment-detail drill-in, loads once on page open, and surfaces only one number per `(α_fast, α_slow)` cell: the closed-form expected episode time. That's enough to verify the math is wired correctly but not enough to *feel* the algorithm reacting attempt-by-attempt during a live practice session.

Andrew wants to sit in front of the dashboard while practicing and ponder three things, each updating per attempt:

1. The three underlying parameters (`p_die`, `T_s`, `T_d`) at each of the 10 α decay rates, as time series.
2. The three slide values (`slope_log_success`, `slope_log_death`, `slope_logit_p`) at each `(fast, slow)` α pair, as heatmaps — to see *why* the predicted-episode-time matrix moves.
3. The actual distribution of predicted episode times, as a histogram from many bootstrap draws — to see bimodal structure (e.g. the 4s-trick + 20s-late example) that the single-number matrix collapses.

(1) and (2) only require exposing state the sampler already computes. (3) requires the bootstrap-with-slide sampler that came out of brainstorming.

## Principles

Carried forward:

1. **No silent fallbacks.** Insufficient-data cells show "—", never a fudge value.
2. **Practice and Hyperplay share the surface.** `updatePracticeCard` at `frontend/src/model.ts:107` already renders for both modes; both get these visuals for free.
3. **No new per-attempt push infrastructure.** The existing SSE app-state push fires `updatePracticeCard` per attempt. Practice-card visuals fetch on that callback. 1s freshness ceiling is acceptable; UI snappiness overhaul is out of scope here.
4. **Phase 1 = no modeling risk.** Only Phase 2 changes the sampler.

## Phase 1 — expose what's already computed

### Backend

Extend the existing `/api/segments/{id}/em-suite-matrix` endpoint payload (defined in `python/spinlab/routes/model.py:248`) with two new top-level fields:

- **`param_history`**: a dict with three keys — `p_die`, `log_success_time`, `log_death_time` — each mapping to a 2D array of shape `[n_alphas][n_snapshots]`. Each snapshot is the EMA value at that α after the kth attempt. Cells where the α has no data yet (denominator == 0) are `null`. `n_snapshots = n_attempts_total + 1` (snapshot 0 = empty state, snapshot k = after the kth event). Times are stored as `log(time_ms)`; the frontend exponentiates for display. `p_die` is stored as the raw EMA value (in [0, 1]).

  Implementation: extend `EmSuiteSamplerEstimator.rebuild_state` (or add a sibling function `rebuild_with_history`) to collect the snapshot list as it replays. The current rebuild discards intermediate states.

- **`slope_matrices`**: a dict with three keys — `slope_log_success`, `slope_log_death`, `slope_logit_p` — each mapping to a 10×10 upper-triangular array of slope values (`E_fast − E_slow` per quantity, in the same space the closed-form uses internally). Cells where the prediction gate fails or either EMA is undefined are `null`.

  Implementation: thin wrapper around `trend_signal_slopes` in `python/spinlab/estimators/em_suite_sampler.py:230`. Already computed inside `expected_episode_time_ms`; just lift the tuple into a returned grid instead of consuming it locally.

API contract is added to `python/spinlab/api_schemas.py:EmSuiteMatrixResponse`. Existing fields (`alpha_grid`, `baseline`, `matrix`, counters) stay untouched.

### Frontend

Two new render functions, both following the existing `renderEmSuiteMatrix` pattern (clear and redraw a host element):

- **`renderParamHistory(host, data)`** in `frontend/src/em-suite-params.ts`. Three stacked Chart.js line charts, one per quantity:
  - Top: `p_die` (y-axis in [0, 1]).
  - Middle: `T_s` in ms (y-axis log scale; backend ships log values, frontend exponentiates).
  - Bottom: `T_d` in ms (same as T_s).

  Each chart has 10 line datasets, colored on a continuous gradient from fast (α=1.0, hot color) to slow (α=0.0, cool color). x-axis = attempt index. Legend collapsible. Reuse the chart-instance teardown pattern from `renderColdHistogram` to avoid leaks across re-fetches.

- **`renderSlopeMatrices(host, data)`** in `frontend/src/em-suite-slopes.ts`. Three stacked 10×10 upper-triangular grids, one per quantity. Cell color is signed:
  - Strong green: large negative (improving — getting faster / less likely to die).
  - Strong red: large positive (regressing).
  - Pale at zero.

  Use a single shared color scale per quantity, normalized to that quantity's max-abs over the visible matrix so weak signals stay visible when one cell dominates. Cell text shows the raw slope to ~2 decimals.

### Wire-up

`renderPracticeInsight` (in `frontend/src/model.ts`, called from `updatePracticeCard` at line 105–117) gains a fourth render call after the existing insight content:

```typescript
await loadAndRenderEmSuitePanel(currentSegment.segment_id, emSuitePanelHost);
```

`loadAndRenderEmSuitePanel` fetches the (extended) matrix endpoint once and renders all three panels: existing matrix + new param-history + new slope-matrices. Since `updatePracticeCard` fires on every SSE app-state push, this re-fetches per attempt at no new infrastructure cost.

Hyperplay gets all of this for free via the existing `mode === "practice" || mode === "hyper_play"` gate.

### Out of scope for Phase 1

- Sampler perf — `rebuild_state` still runs from scratch per fetch. Out of scope; Andrew explicitly deferred speed work until correctness is locked.
- New SSE channels or push protocols.
- Any change to the segment-detail (Model tab) drill-in. It picks up the extended endpoint payload automatically, but new renderers are not added there in Phase 1.

## Phase 2 — bootstrap-with-slide sampler + histogram

### Modeling

The shared generator agreed during brainstorming, parameterized by `(p, m_d, m_s)`:

```
while True:
    if Bernoulli(p) == died:
        d ~ weighted_empirical(death_pool, α_fast)
        episode_time += d * m_d + reload
    else:
        s ~ weighted_empirical(success_pool, α_fast)
        episode_time += s * m_s
        return episode_time
```

- `sample(0)` → `(p_fast, 1.0, 1.0)`. Pure empirical at current skill.
- `sample(1)` → `(logistic(logit(p_fast) + slope_logit_p), exp(slope_log_death), exp(slope_log_success))`. Three-axis slide applied multiplicatively at the moment of each draw. Empirical shape per quantity preserved; only the location of each draw is shifted.
- `sample(k)` → same with `k · slope` per axis.

State addition per segment: a ring buffer of recent `(time_ms, outcome)` tuples sized to the slowest α's effective window (~5 halflives at the slowest meaningful α — a few hundred entries max).

### Backend

- New endpoint `/api/segments/{id}/em-suite-histogram?fast=<idx>&slow=<idx>&which=<0|1>&n=<draws>` returning binned counts.
- Sampler implementation in `python/spinlab/estimators/em_suite_sampler.py`. Pure Python first; vectorize with numpy when it shows up as a hot path.

### Frontend

- `renderEpisodeHistogram(host, data)` — reuse the cold-distribution histogram pattern from `frontend/src/death-distribution.ts`. No PDF overlay (the point of the empirical view is that we don't assume a parametric shape).
- Controls: pair of dropdowns for `fast_idx` / `slow_idx`, toggle for sample(0) vs sample(1).
- Wired into the same practice-card panel.

### Modeling note

Choice of log-space vs linear-space EMA for the `T_s` / `T_d` quantities is a deliberate v0 call: stay log-space (multiplicative slide on each draw). Reason: positivity is free, lognormal ≈ normal at tight σ (Andrew's "should end up quite tight" intuition), and the slope coefficients we already compute live in log space. Revisit if data shows otherwise. This is a modeling decision, not a perf one.

## Open questions

None blocking. Phase 2 details (exact ring buffer size, draw count default, fast/slow defaults) can be picked at implementation time and adjusted from observation.

## Out of scope (entire spec)

- Outer allocator §1–§5 (still deferred per the parent spec).
- UI snappiness / sub-1s update mechanism overhaul.
- Vectorization / numpy hot path. Numbered as a future optimization in Phase 2 only.
- Per-attempt push protocol changes beyond the existing SSE app-state cadence.

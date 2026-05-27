# Segment Hazard Plot

Per-segment survival-style hazard visualization on the segment-detail page, sharing a panel with the existing death histogram via a Histogram/Hazard toggle.

## Problem

The just-landed death-distribution histogram (on `feat/segment-death-histogram`, unmerged) shows *when* in a segment the player tends to die — useful, but it understates risk in late bins. Three deaths in the 8s bin and 50 in the 2s bin tells you the 2s bin is where most deaths happen — but a player who reaches the 8s bin almost-completed has *also* faced high per-moment risk all along; the histogram doesn't show that. A Kaplan-Meier-style hazard rate (`deaths_in_bin / at_risk_entering_bin`) closes that gap: it shows risk *conditional on having reached this moment*, which is the quantity the player actually wants to know when deciding where to focus.

This is branch 3 of the death-distribution arc. Branch 1 (per-attempt `is_hot` column) and branch 2 (`bootstrap_resample` estimator) are already on `main`. Branch 3 is a visualization, not a model — it doesn't feed the scheduler.

## Design Decisions

- **Panel-shared toggle, not a separate panel.** Histogram and Hazard live in the same `<section>` on the segment-detail page, switched by [Histogram] [Hazard] buttons in the panel header. Saves vertical space and forces a deliberate view choice. Shared X-axis (same time bins) means the chart layout doesn't reflow on toggle.

- **Cold-only filter at the attempt level.** The data pool for both views is `event_attempts` rows where `is_hot = false`, scoped to this segment. Hot attempts (carried-over state from a prior segment completion in a reference run) are a different population — including them would conflate cold-load risk with hot-handoff risk. Filtering at the attempt row (not the episode aggregate) keeps cold respawns from mixed episodes in the pool.

- **Weighted, not raw.** Hazard uses the same halflife-decay weighting as `death_aware_rolling`, reading the *active* tuned halflife (not a hardcoded default) so the user's one halflife knob drives both views. Same `5 × halflife` truncation window. Both numerator (`deaths_w`) and denominator (`at_risk_w`) are weighted sums; the ratio is meaningful as long as the denominator > 0. Recent shifts in player skill move the chart; deep history fades.

- **Adaptive bin count: `min(20, max(5, ceil(sqrt(n))))`.** `n` is the count of cold attempts within the `5 × halflife` truncation window (not lifetime — events past the window contribute weight < 3% and aren't being binned). Sparse data (n=5) → 5 wide bars; rich data (n≥400) → 20 narrow bars. Standard square-root rule. Re-bins only at thresholds (n=26, 37, 50, …), so accumulating attempts doesn't make the chart twitchy. Weighting affects bar *values*; the truncation horizon and raw event count within that horizon affect *bin layout*.

- **Opacity = effective at-risk fraction.** Hazard bar opacity is `at_risk_w_at_bin / at_risk_w_at_bin_0`. Bin 0 is always fully opaque (all weighted cold attempts are at-risk at t=0); tail bins fade as the pool exits (died or completed). When weighting shrinks the recent pool, opacity tracks that — so a faded tail bar means "thin recent data here," not "few attempts ever."

- **`null` hazard for `at_risk_w == 0` bins.** No at-risk pool → no defined hazard. Frontend treats `null` as a blank bin (no bar drawn), distinct from `hazard = 0` (zero deaths but some at-risk = bar of height 0, full opacity).

- **Computed in the route handler, not in an estimator.** Hazard is a per-segment visualization, not a model output. Coupling it to an estimator would force `death_aware_rolling` to take on the cold-only-filter concern it doesn't otherwise have. `routes/model.py::segment_history` already loads the segment's events for the estimator-curve loop; it computes `hazard_curve` from the same event list.

- **Histogram view inherits the cold filter and the adaptive binning.** Both views share the data pool and X-axis. This changes the histogram branch's behavior (currently no cold filter, fixed 20 bins) — the implementation plan folds those changes into the histogram branch before merge.

## Precondition

This work stacks on `feat/segment-death-histogram` (8 commits, unmerged as of 2026-05-27). The implementation plan must:

1. Land the cold-filter + adaptive-binning changes on the histogram branch first (changes the existing spec at `docs/superpowers/specs/2026-05-25-segment-death-histogram-design.md` — note the deviation).
2. Merge the histogram branch to `main`.
3. Build branch 3 (hazard) on top.

## API

### Schema additions

```python
# python/spinlab/api_schemas.py

class HazardBin(_BaseResponse):
    lo_ms: float
    hi_ms: float
    hazard: float | None        # null when at_risk_w == 0
    at_risk_w: float            # weighted at-risk count entering this bin

class HazardCurve(_BaseResponse):
    bins: list[HazardBin]
    halflife: int               # echoed for label / debugging

class SegmentHistory(_BaseResponse):
    # ...existing fields (incl. selected_model, attempts, estimator_curves)...
    hazard_curve: HazardCurve | None  # NEW — null when n_cold_attempts == 0
```

### Route change

`segment_history` already loads `events = db.get_segment_event_rows(segment_id)` for the estimator-curve loop. Add:

```python
cold_events = [ev for ev in events if not ev.is_hot]
halflife = active_halflife_for(session, "death_aware_rolling")  # tuned value, not default
hazard_curve = compute_hazard_curve(cold_events, halflife=halflife) \
    if cold_events else None
```

The exact wiring for `active_halflife_for(...)` — i.e. how the route reaches the user's current tuned halflife — is deferred to the implementation plan; the existing param-resolution path for the estimator-curve loop in `segment_history` is the right starting point.

`compute_hazard_curve` lives in a new module `python/spinlab/hazard.py` (pure function, no DB dependency). Implementation outline:

```python
def compute_hazard_curve(
    cold_events: list[EventAttempt], halflife: int,
) -> HazardCurve:
    """Caller is responsible for the cold filter (is_hot=false). This
    function does not re-filter — passing any hot events skews the curve."""
    # 1. Truncate to the last 5 * halflife events; let truncated = that list.
    # 2. Compute per-event decay weights over truncated (matches
    #    _episode_helpers._compute_weights: most-recent weight 1.0,
    #    weight 0.5 at one halflife back).
    # 3. Adaptive bin count: min(20, max(5, ceil(sqrt(len(truncated))))).
    # 4. Compute lo=0, hi=ceil(max(time_ms in truncated) / 1000) * 1000.
    # 5. For each bin i in [0, bin_count):
    #      deaths_w  = sum(w for ev, w in pairs if ev.outcome == DIED  and ev.time_ms in [lo_i, hi_i))
    #      at_risk_w = sum(w for ev, w in pairs if ev.time_ms >= lo_i)
    #      hazard    = deaths_w / at_risk_w if at_risk_w > 0 else None
    # 6. Return HazardCurve(bins=..., halflife=halflife).
```

Frontend types regenerate via `npm run gen-types`.

### File-level constants (no magic numbers, per CLAUDE.md)

In `python/spinlab/hazard.py`:

```python
# Maximum bin count. 20 matches the histogram's screen-width comfort cap;
# above this, bars are too thin to read at typical viewport widths.
MAX_BINS = 20

# Minimum bin count. Below 5, the chart degenerates into a quantile summary
# and loses its shape-as-distribution affordance.
MIN_BINS = 5

# Truncation horizon as multiples of halflife. Same value as
# _episode_helpers' EFFECTIVE_WINDOW_HALFLIVES — at 5x halflife, an event's
# weight is 2^-5 ≈ 3%, below the noise floor of the binning.
EFFECTIVE_WINDOW_HALFLIVES = 5

# X-axis upper-edge rounding. One-second rounding gives clean axis labels
# without manual tick configuration. Matches the histogram's HI_ROUND_MS.
HI_ROUND_MS = 1000
```

## Frontend

### Layout

The detail-page panel that branch 2 (histogram) added has its header reworked:

```
┌────────────────────────────────────────────────────────────────┐
│  Cold distribution   [Histogram] [Hazard]   p(die|attempt) 0.34│
│                                              p(die|life)    0.52│
│  ┌──────────────────────────────────────────────────────────┐  │
│  │   <one of two chart variants>                            │  │
│  │   X: time (ms),  shared bins                             │  │
│  │   Y: counts | hazard (0–1)                               │  │
│  └──────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────┘
```

- Panel title `Cold distribution` (renamed from `Death distribution`).
- Toggle buttons `Histogram` (default), `Hazard`. Toggle state is in-memory only; resets to Histogram on each fresh detail-page open.
- p_die stats stay in the header (segment-wide, not per-bin).

### Histogram view

Unchanged from the histogram-branch behavior, except:
- Data source = `history.hazard_curve.bins` (deaths and completions reconstructed via the existing `final_extras.death_samples` / `completion_samples`, **filtered to cold attempts only**).
- Bin count = adaptive (from `hazard_curve.bins.length`).
- μ_d / μ_c markers stay, computed over cold samples only.

### Hazard view

- One bar per `HazardBin`. Color `#fff176` (yellow, matching the mockups).
- Bar height = `bin.hazard` (or skipped/zero-height when `bin.hazard === null`).
- Bar opacity = `bin.at_risk_w / bins[0].at_risk_w` clamped to `[0, 1]`.
- Y axis: linear `[0, 1]`, title "Hazard rate".
- Tooltip per bin: `hazard: 0.34 · at_risk: 27.4 (effective)`.
- No μ markers in this view.

### Module structure

- `frontend/src/hazard-render.ts` (new) — pure renderer; takes a `HazardCurve` and a canvas, returns a Chart.js instance.
- `frontend/src/segment-detail.ts` — extended to wire the toggle, swap datasets, and destroy/recreate the chart on view change. Existing `destroySegmentDetail()` handles cleanup.

## Testing

### Backend

New `tests/test_hazard.py` (pure-function module, no DB):

- `compute_hazard_curve([])` → caller (route) substitutes `None`; the function itself is not called on an empty list.
- Bin-count rule, against the post-truncation count: 5 attempts → 5 bins; 25 → 5 bins; 26 → 6 bins; 400 → 20 bins; 10_000 → 20 bins.
- Cold filter is the caller's responsibility — function trusts its input. Test that the function doesn't re-filter (a hot event in input would skew the result), documenting the contract.
- Hazard math:
  - One attempt died at t=2000ms (cold) → bin containing 2000ms has `hazard = 1.0`, `at_risk_w = 1.0`; later bins have `at_risk_w = 0` and `hazard = None`.
  - Two attempts: one died at t=2000, one survived to t=8000 → bin@2s: `hazard = 0.5, at_risk_w = 2.0`; bins between 2s and 8s: `hazard = 0.0, at_risk_w = 1.0`; bins beyond 8s: `at_risk_w = 0`.
  - Three attempts, weighted (most recent = 1.0, 2 halflives back = 0.25, 4 halflives back = 0.0625): verify `deaths_w` and `at_risk_w` match the hand-computed sums.
- Truncation: 200 attempts with `halflife=20` → only most recent `5*20 = 100` events reach the binner. Verify bin sums reflect 100, and bin count = `min(20, ceil(sqrt(100))) = 10`.

Backend integration (extend `tests/test_segment_history.py` if present, or new file):

- Segment with mixed hot/cold attempts → `hazard_curve.bins` populated; weighted sums respect the cold filter.
- Segment with only hot attempts → `hazard_curve = None`.
- Segment with no attempts → `hazard_curve = None`.

### Frontend

`frontend/src/hazard-render.test.ts` (new), Vitest + happy-dom:

- `renderHazard()` with a hand-rolled `HazardCurve` → chart instance exists; dataset length = `bins.length`; bar opacities match `at_risk_w / bins[0].at_risk_w`.
- Bin with `hazard = null` → not drawn (or drawn at height 0 with opacity 0; whichever the implementation picks, asserted).
- `hazard_curve = null` → Hazard tab is disabled (greyed) with "no cold data" tooltip; toggle has no effect.

Extend `frontend/src/segment-detail.test.ts`:
- Clicking Hazard tab swaps the chart's Y-axis title and dataset.
- Clicking back to Histogram restores the original.
- Fresh `renderSegmentDetail` call resets to Histogram view regardless of prior state.

No emulator tests — pure presentation over computed data.

## Out of Scope

Deferred to the death-distribution arc backlog:

- **Hot view toggle.** No way to view a segment's hot attempts in this UI. Future work adds a [Cold] [Hot] sub-filter once hot data accumulates (currently rare; see `project_death_distribution_arc.md`).
- **Cross-segment hazard view.** Per-segment only.
- **Bootstrap filter consistency.** `bootstrap_resample` filters at the episode level (drops any episode containing a hot attempt). Hazard filters at the attempt level. The bootstrap behavior may be a bug — separate question, separate fix.
- **Episode-aggregate refactor.** The "do we need `SegmentAttempt` at all, or just `EventAttempt` rows?" question Andrew raised is a deeper data-model refactor, not branch 3.
- **Histogram bar weighting.** Histogram stays raw counts; only hazard's denominator is weighted. (Open: revisit if the divergence between raw histogram and weighted hazard turns out to be confusing in practice.)
- **Tab persistence across page reloads.** Histogram is always the default on fresh open.
- **Confidence bands or error bars on hazard.** Opacity-as-confidence is the only signal. Binomial s.e. or KM confidence intervals can come later if needed.

# Per-Segment EMA-Suite Sampler — Design

**Date:** 2026-05-30
**Status:** Brainstorming captured, awaiting review
**Scope:** v0 — per-segment sampler + live matrix view + offline replay. Outer §1–§5 of [`docs/practice-allocator-spec.md`](../../practice-allocator-spec.md) explicitly deferred.

## Motivation

Andrew is moving away from the v07 segment model (uncertainty wrangling too heavy) and toward a simpler stance: the fundamental data point is an **attempt** (one death or one success on a segment); learning happens entirely through recency weighting on those attempts; no fitted skill trajectory.

The existing `death_aware_rolling` estimator (`python/spinlab/estimators/death_aware_rolling.py`, spec [`2026-05-24-death-aware-rolling-design.md`](2026-05-24-death-aware-rolling-design.md)) is the closest match in the codebase. It already tracks per-segment `p_die_per_life`, `death_time`, and `completion_time` under a single exponential decay halflife, and uses the geometric formula `E[episode_time] = (p/(1−p)) × (E[death_time] + reload) + E[completion_time]`.

This spec extends that estimator along three axes for v0:

1. **Suite of EMAs** — maintain 10 parallel decay rates instead of one halflife; let the operator pick (or median across) `(α_fast, α_slow)` pairs at decision time.
2. **Two-EMA trend signal** — replace the `_weighted_half_split_slope` heuristic with `E_fast − E_slow`, computed in log-time / logit-p space.
3. **Live matrix view + replay** — surface the (α_fast, α_slow) prediction grid in the dashboard, updating after every attempt; the same code runs against historical data for offline validation.

The original [`docs/practice-allocator-spec.md`](../../practice-allocator-spec.md) describes the outer Monte Carlo allocator (§1 simulation, §3 objective, §4 value-of-practice, §5 decision). v0 builds only §0 — the per-segment sampler — and the live view. §1–§5 are deferred and remain valid future scope.

## Principles

Carried forward from the death-aware-rolling spec and our brainstorming:

1. **No silent fallbacks.** Insufficient data returns `None` / "—" in the UI; never a fudge value.
2. **No fitted skill trajectory.** Learning is recency weighting over observed attempts. The decay parameter(s) are the only smoothing knob(s).
3. **Memorylessness.** Exponential decay only — `EMA(n) = α·X_n + (1−α)·EMA(n−1)` — to keep updates O(1).
4. **Honest separation of success and death.** Three sub-distributions per segment; episode time is *derived*, not modeled directly.
5. **Per-attempt updates.** Model state changes on every death AND every success, not only on episode completion.

## Terminology

There is a real naming conflict between this design and the existing DB schema. Spec uses the brainstorming terminology; code may continue to use its existing names. Mapping table:

| Concept | Spec name | Code name |
|---|---|---|
| Atomic outcome (one death OR one success) | **attempt** | `EventAttempt` / "event" |
| Sequence of attempts ending in success | **episode** | "episode" (and DB `attempts` row) |
| Per-segment sampler instance | **sampler** | extension of `DeathAwareRollingEstimator` |

The DB `attempts` table stores episode-level rows. The DB `events` table (queried via `db.get_segment_event_rows`) stores per-attempt rows. We use spec terminology in prose and code-level names in implementation-touching sections.

## Concept overview

For each segment, the sampler maintains three sub-distributions:

- **`p_die`** — probability the next attempt on this segment ends in death. Bernoulli on per-attempt outcomes.
- **`success_time`** — distribution of gameplay time for successful attempts. Positive, right-skewed.
- **`death_time`** — distribution of gameplay time for fatal attempts. Positive, often spike-shaped (death usually happens at a specific obstacle).

Each sub-distribution is summarized by a suite of EMAs at 10 decay rates. From the suite, the operator picks a `(α_fast, α_slow)` pair; this defines:

- **Current estimate**: `E_fast` per quantity (where you are right now).
- **Trend**: `E_fast − E_slow` per quantity, in log-space for times and logit-space for p_die (where things are heading).

The sampler exposes:

- **`sample(0)`** — a draw from the current empirical (current state).
- **`sample(1)`** — `sample(0)` translated by `slope` in log/logit space (current state + one attempt of forward drift).
- **`sample_episode(0)` / `sample_episode(1)`** — simulate forward: keep drawing attempts (each gated by `p_die`) until success, sum gameplay times + 3200 ms × deaths.

The live UI shows a per-segment 10×10 matrix (upper triangle, ~45 cells) where each cell is the expected (mean) episode-time for a `(α_fast, α_slow)` pair. The matrix updates after every attempt the player completes.

## Data model

**Attempt record** (matches existing `EventAttempt`):

```
attempt:
  segment_id
  attempt_index    # ordinal per segment
  outcome          # "died" or "survived"
  time_ms          # gameplay-only, NOT including reload
  timestamp_ms     # for ordering + bookkeeping
```

Episodes are derived by `_group_into_episodes(events)` from existing code; v0 does not need to persist episode rows independently.

**Per-segment sampler state** (held in memory, recomputable from attempt log on startup):

```
sampler_state(segment_id):
  log_success_time_emas:    array[10] of float | None    # log-ms
  log_death_time_emas:      array[10] of float | None    # log-ms
  p_die_emas:               array[10] of float | None    # in [0, 1]
  n_attempts_total:         int                          # for nil-gates
  n_successes:              int
  n_deaths:                 int
```

Storage budget per segment: 30 floats + 3 ints. For ~100 segments per game this is on the order of tens of kilobytes total, depending on float width — negligible either way.

**Persistence policy:** the raw event log (`(segment_id, attempt_index, outcome, time_ms, timestamp_ms)`) is the source of truth. EMAs reconstruct on startup by replaying events through the update rule. No EMA state in the DB. Replay cost is O(n_attempts) per segment; for a season's worth of data this is sub-second per game.

## EMA suite

**Decay rates (locked):**

```
α ∈ {0.0, 0.01, 0.02, 0.03, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0}
```

- α = 0.0 (never update) and α = 1.0 (only most recent counts) are sanity-check endpoints — should look obviously degenerate on the matrix, validating mid-range pairs visually.
- Intermediate values span roughly half-orders of magnitude in effective memory length.

**Update rule** — applied to the matching parallel EMAs after every attempt. `p_die` updates on every attempt (regardless of outcome); the time EMAs update only on attempts of their matching outcome (success-time EMAs on successes only, death-time EMAs on deaths only).

For time EMAs, when an attempt of the matching outcome is observed:

```
log_x = log(attempt.time_ms)
log_E_α ← α · log_x + (1 − α) · log_E_α    for each α in the suite
```

For p_die EMAs, on every attempt:

```
outcome_bit = 1 if attempt.outcome == "died" else 0
p_E_α ← α · outcome_bit + (1 − α) · p_E_α    for each α in the suite
```

Update cost per attempt: 10 multiply-adds for `p_die` plus 10 for the matching time quantity = 20 total per segment per attempt. Negligible.

**Initialization and gating (nil-until-2):**

- Each EMA starts as `None`. On the *first* attempt of its matching type, the EMA is seeded directly to the observed value (`log(time_ms)` for time EMAs, `outcome_bit` for p_die). Subsequent attempts apply the normal update rule above.
- **Prediction gate** (separate from EMA initialization): a segment's prediction matrix shows "—" until all three of the following hold:
  - `n_successes ≥ 2`
  - `n_deaths ≥ 2`
  - `n_attempts_total ≥ 2`
- The gate exists because a single observation gives no trend signal; the second observation is what makes the slope between `α_fast` and `α_slow` meaningful. The EMAs themselves are well-defined for `n ≥ 1` and persist across the gate so the moment the gate clears, predictions are immediately available without re-warmup.

**Trend signal** for a selected `(α_fast, α_slow)` pair (α_fast > α_slow):

```
slope_log_success = log_E_α_fast_success − log_E_α_slow_success
slope_log_death   = log_E_α_fast_death   − log_E_α_slow_death
slope_logit_p_die = logit(p_E_α_fast)    − logit(p_E_α_slow)
```

With `logit(p)` evaluated on `clamp(p, ε, 1−ε)` where `ε = 1e−6` — defensive against numerical edges from same-outcome streaks. The clamp is only for the logit math; the underlying EMA proportion is unclamped.

## Sampler interface

```python
class SegmentSampler:
    def sample(self, k: int = 0) -> tuple[Outcome, time_ms]:
        """Return an (outcome, time_ms) for a single attempt.

        k=0: at current state.
        k=1: shift one trend-step forward.
        k=k: shift k trend-steps forward.

        Concretely:
          - outcome ← Bernoulli with p = logistic(logit(p_E_fast) + k · slope_logit_p_die)
          - time_ms ← exp(log_E_α_fast_<outcome> + k · slope_log_<outcome>)
        """

    def sample_episode(self, k: int = 0) -> EpisodeOutcome:
        """Sim-forward: call sample() repeatedly until outcome=success,
        sum the times, add DEFAULT_DEATH_PENALTY_MS × deaths_in_episode.
        Cap at 100 attempts (return 'insufficient' if not converged)."""
```

**v0 time semantics (point estimates).** `sample()`'s `time_ms` is the *point estimate* derived from the EMA — deterministic given the outcome and the chosen α pair. The only stochasticity in `sample_episode()` is the Bernoulli outcome at each step (which determines how many deaths happen before success). Each death attempt contributes `exp(log_E_α_fast_death)`; the final success contributes `exp(log_E_α_fast_success)`. This is sufficient to populate matrix cells with median (or mean) simulated episode-time and matches what the EMAs actually carry.

**Why no random time within an outcome in v0:** the EMA alone is a point estimate, not a distribution. Returning a draw from a distribution would require either storing raw observation samples per segment (per the existing `death_aware_rolling` pattern) or fitting a parametric form (lognormal, etc.). Both are real future options; v0 deliberately doesn't commit to either. The matrix-cell display only needs a representative number per cell, which point estimates supply directly.

When a richer time distribution lands (sample arrays or parametric fit), `sample()` swaps to a draw from that distribution; nothing else in the architecture changes.

**Reload penalty:** uses `DEFAULT_DEATH_PENALTY_MS = 3200` (from `condition_registry.py`). Per-condition override already exists in the codebase; we honor it where applicable. Raw `attempt.time_ms` is gameplay-only, matching existing convention `episode_total = sum(time_ms) + penalty · deaths`.

## Live matrix view

**Per-segment card** showing a 10×10 upper-triangular grid where row index = α_fast, column index = α_slow, and cell value = mean (expected) episode-time at that `(α_fast, α_slow)` pair.

**Computation per cell** (closed-form, exact under v0 point-estimate semantics):

```
p              = logistic(logit(p_E_α_fast) + slope_logit_p_die)
success_time   = exp(log_E_α_fast_success + slope_log_success)
death_time     = exp(log_E_α_fast_death   + slope_log_death)
mean_episode   = success_time + (p / (1 − p)) · (death_time + reload_penalty_ms)
```

The matrix displays sample(1) — the one-step-forward prediction. The baseline row above the matrix uses the same formula with all slopes set to zero (sample(0)).

**Why mean and not median in v0:** under v0 point-estimate semantics, only `n_deaths` is stochastic; the per-attempt times are constants. Median episode-time then collapses to `success_time` whenever `p_die < 0.5` (because median of geometric(p) at integer support = 0), hiding the death contribution entirely. Mean integrates the death cost proportionally and reads as "expected wall-clock," which is the right framing for the live view. When sample times become stochastic (future), we can revisit median display.

**Implementation flexibility:** the sim-forward signature in `sample_episode()` still works — it just collapses to the closed form numerically. The implementation may use either approach; closed form is one line and exact, sim-forward keeps the code path ready for stochastic times. Pick at build time.

**Defaults:**

- Cell display unit: seconds with one decimal (e.g., `25.6s`).
- Cells with insufficient data: "—" (per the prediction gate above).
- **Sample(0) baseline reference**: a separate single row above the matrix shows the flat-prediction (no-slope) value for each α, so trend-corrected matrix cells can be visually compared against the no-trend baseline. Same display unit and gating.
- Updates on every attempt. Recompute after each EMA tick.

**No percentile bands in v0** — single mean number per cell.

**Per-quantity sub-views (bonus surface, optional):** three smaller grids showing the trend on each underlying quantity (`p_die`, `success_time`, `death_time`) — useful for diagnosing when the episode trend is noisy but one underlying quantity is moving clearly.

**Placement:** inline on the existing per-segment view. Live-watching is the use case, not buried in a tab.

## Offline replay mode

Same code, different data source. Replay walks the historical event log for a segment, ticks the EMAs in order, and at each step records the prediction matrix. Outputs:

1. **Per-segment one-step-ahead MAE-log per (α_fast, α_slow) pair.** Heatmap of which pairs predict best.
2. **Slope-vs-flat baseline.** For each pair, compare slope-augmented prediction (`E_fast + slope`) to flat prediction (`E_fast` alone). If no pair beats flat consistently, the trend mechanism doesn't earn its complexity — switch v0 to flat-only and revisit later.
3. **By-attempt-count breakdown.** Prediction quality as a function of attempt history depth. Calibrates the warmup story.
4. **Suite stability.** Are top-K pairs within ±10% of each other on their predictions? If yes, the median-across-pairs strategy works as a v1 default.
5. **Sample(0) calibration** (bonus). Coverage of predicted 50/80/95% intervals — does the empirical resample actually represent the right distribution shape?

**Data:** Beto's existing event log (covered early-to-mid segments well, late-run segments sparse). Tests 1–3 are the gating criteria for whether to ship the live view; tests 4–5 are nice-to-haves.

## Build sequence

1. **Sampler module + suite update logic.** New file `python/spinlab/estimators/em_suite_sampler.py` extending the death-aware-rolling pattern. Reuses `EventAttempt`, `_compute_weights` where applicable. Pure-Python, no JAX.
2. **Replay/offline test harness.** Script under `scripts/` that loads Beto's events and produces the five test plots above.
3. **Live matrix view.** Frontend component on the per-segment view. New API endpoint serving the matrix for a given segment.

Decision gate between (2) and (3): if the replay tests show the trend mechanism doesn't beat flat prediction, ship the matrix as `E_fast`-only (no trend column) and revisit the slope design.

## Scope

**In v0:**

- Sampler with 10-α suite × 3 sub-distributions per segment.
- Cold-only attempt distribution (no hot vs cold partitioning).
- Sim-forward episode prediction.
- Live matrix UI per segment.
- Replay/offline test mode.
- Reload penalty from existing config constant.

**Explicitly out (deferred to later versions):**

| Deferred | Why | Where it goes |
|---|---|---|
| Hot vs cold partitioning of distributions | Brainstorm-decided to cold-only for v0; hot/cold sharing is a complexity step | Future spec; doubles to 6 sub-distributions per segment |
| Location-aware death modeling | "Death-location × death-time combo" model deferred to v1+ if v0 visualization motivates it | Spec extension; would replace single `death_time` with conditional structure |
| Outer Monte Carlo allocator (§1–§5) | Reset policy, target time, allocator decision rule all out of v0 scope | Original `docs/practice-allocator-spec.md` covers the intent |
| Stochastic per-attempt time draws | v0 uses point-estimate times from EMAs; sample() is deterministic given outcome | Add raw observation arrays per segment OR fit a parametric form when needed |
| Fitting α grid to data | v0 uses fixed grid + manual selection; pooled fit deferred | One-step-ahead grid search across pooled segments |
| Per-segment vs player-wide α selection | Manual for now; median-across-pairs as a v1 default once Andrew has gut intuitions | UI exposes selectors |
| Within-episode learning during simulation | Sim draws all sub-attempts from same fixed state; second-order effect | Stochastic update during simulation, if it ever bites |
| p_die EMA in pure logit-space update | Maintained in [0,1] for sampling; only converted to logit for trend signal | Could swap; no functional difference for v0 |

## Open questions resolved during brainstorming (for the record)

- **Why exponential decay and not Ebbinghaus / hyperbolic / step?** Memorylessness gives O(1) updates and clean math. Other shapes are more memory-realistic but cost simplicity. Revisit if predictions are systematically biased.
- **Why attempts as the clock, not wall-clock?** Andrew streams daily; cross-session decay is small enough to ignore for v0. Wall-clock is a future option if data shows meaningful between-session regression.
- **Why encapsulated episode model and now decomposed?** Andrew wanted per-attempt-update behavior ("the model changes on death"). Decomposed Layer-1 modeling preserves that; episode-time becomes a derived quantity by sim.
- **Why mean in cells (vs median)?** Originally discussed as median (sim-forward), but under v0's point-estimate time semantics the median of `episode_time` collapses to `success_time` whenever `p_die < 0.5` (median of geometric integer = 0), which would hide the death contribution. Mean integrates death cost proportionally, is closed-form exact, and reads as "expected wall-clock." Median becomes the right call again once sample times are stochastic; we can revisit then.
- **Why nil-until-2 for all EMAs?** No global prior; honest about uncertainty. UI shows "—" until enough data; users won't be surprised.
- **Why Laplace smoothing dropped for p_die?** Same reason — no synthetic data. Edges handled at sim time via attempt cap.

## Open questions for v1+ (deferred, but worth flagging)

- **Per-segment α selection.** If Beto-data tests show meaningful segment-level variation, do we fit per-segment or pool? Pooled is simpler; per-segment is more honest.
- **Hot vs cold sharing.** Six sub-distributions per segment (3 quantities × 2 hot/cold). Are they fit independently or shrunk toward each other? Shrinkage is principled but adds a knob.
- **Decomposed allocator scoring.** When the outer model lands, does it score practice value on episode-time only, or on p_die + success_time + death_time decomposed? Decomposed is more informative but harder to communicate.
- **Long-break wall-clock decay.** If Andrew's data ever shows skill regression after extended breaks, attempts-only clock becomes wrong and we need a hybrid.

## Implementation notes (non-binding hints for the plan)

- Likely subclass or alongside `DeathAwareRollingEstimator`, sharing event-loading and weight machinery.
- The matrix endpoint is a new GET (per segment), payload ≈ 45 floats + metadata. Cheap.
- The frontend matrix view is a small grid component; reuse existing per-segment card styling.
- The replay script is a one-shot python script, not a permanent system component — it queries the DB directly.
- All numeric operations on times: log-space where possible to avoid negative-prediction edge cases.

---

## Cross-references

- Outer model intent: [`docs/practice-allocator-spec.md`](../../practice-allocator-spec.md) (Andrew's original spec; §1–§5 still valid future scope).
- Predecessor estimator: [`docs/superpowers/specs/2026-05-24-death-aware-rolling-design.md`](2026-05-24-death-aware-rolling-design.md).
- Model principles: [`docs/superpowers/specs/2026-04-01-model-output-v2-design.md`](2026-04-01-model-output-v2-design.md).

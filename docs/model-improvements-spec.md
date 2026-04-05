# Estimator Model Improvements — Design Spec

Produced from 2026-04-01 design review.

## Principles

1. **No silent fallbacks.** If a model can't compute a value, return `None`. Never substitute another model's answer or a fake 0.0.
2. **No unexplained constants.** Every number is either data-derived, a named parameter with statistical meaning, or explicitly marked as a placeholder awaiting data-driven priors.
3. **Show honest data.** Wild values from few data points are preferable to hidden problems. The user needs to see what's actually happening to build intuition.
4. **Bayesian aspiration.** Track uncertainty. Build toward proper priors. Population priors (hierarchical) are a goal, not a v1 requirement.
5. **Clean tail vs total time are both needed.** Different stats require different signals — don't conflate them.

## Domain Context (SMW Speedrun Practice)

- **Burn-in period:** Early attempts may be minutes slower than eventual times. Priors must be wide — improvement rate could be 60,000 ms/att or 100 ms/att.
- **Drift is generally downward** and decelerates (exponential or hyperbolic decay toward a floor). The Kalman's constant-drift assumption is a known simplification.
- **Deaths add massive noise** to total time. Clean tail (time from last death to segment end) is the execution skill signal. Total time is what matters for runs.
- **Segments vary wildly** in length (3s to 60s+), difficulty, and death probability.

---

## ModelOutput v2

```python
@dataclass
class ModelOutput:
    expected_time_ms: float | None      # E[total_time] next attempt
    clean_expected_ms: float | None     # E[clean_tail] next attempt
    ms_per_attempt: float | None        # improvement rate (positive = improving)
    floor_estimate_ms: float | None     # E[total_time | infinite practice]
    clean_floor_estimate_ms: float | None
    uncertainty_ms: float | None        # std dev of expected_time estimate
    trend_uncertainty_ms: float | None  # std dev of ms_per_attempt estimate
```

### Changes from v1

- **All fields nullable.** `None` = "model cannot compute this with available data." Not used to hide ugly values — only for genuinely incalculable fields.
- **`uncertainty_ms` added.** Std dev on the expected time. Essential for future VoI allocator (exploration vs exploitation). Kalman has this (`sqrt(P_mm) * 1000`). Others should compute it or return `None`.
- **`trend_uncertainty_ms` added.** Std dev on `ms_per_attempt`. Kalman has this (`sqrt(P_dd) * 1000`). Tells the allocator whether the improvement signal is real.

### What's exactly correct vs approximated

| Field | Exactly correct for | Approximated by |
|-------|-------------------|-----------------|
| `expected_time_ms` | Kalman (Bayesian posterior mean) | Rolling (sample mean), Exp Decay (curve eval) |
| `ms_per_attempt` | Exp Decay (curve derivative) | Kalman (linear drift), Rolling (half-split diff) |
| `floor_estimate_ms` | Exp Decay (fitted asymptote) | Kalman (gold — not a real floor), Rolling (min observed) |
| `uncertainty_ms` | Kalman (posterior variance) | Others: None for now |
| `trend_uncertainty_ms` | Kalman (P_dd) | Others: None for now |

### Open: are floors worth keeping?

Floors are the least reliable output. Kalman's floor is just gold (a fact, not an estimate). Rolling's is min observed (also a fact). Only Exp Decay computes a real estimated floor. May drop from ModelOutput and make it Exp Decay-specific. Decision deferred — keep for now, revisit with more data.

### Open: expected time after N attempts

A VoI allocator ideally wants `E[time | N more attempts]`, not just `E[time | 1 more attempt]`. For now, `ms_per_attempt` for the next attempt is sufficient — the allocator can handle multi-step projection later (monte carlo forward simulation once uncertainties exist). The models don't need to compute this themselves.

---

## Per-Model Improvements

### Kalman Filter

**Current state:** Most principled model. Linear state-space model with `[mu, d]` state and full covariance tracking. Textbook predict/update cycle.

**Issues to fix:**

1. **Floor estimate:** Currently `gold_ms` (best observed), not a model-derived estimate. Should be: project `mu + d*N` forward until improvement per attempt drops below a threshold relative to `P_dd` uncertainty. Or: use the clean tail filter's projected asymptote.

2. **Measurement noise (R) adaptation:** The `0.7/0.3` exponential blend in `_reestimate_R` is ad hoc. Replace with proper adaptive Kalman (innovation-based R estimation using a window of recent innovations). SMW-contentful: R should be estimated separately for clean tails (tight) vs total times (loose, because deaths).

3. **Clean vs dirty:** Run two parallel Kalman filters on each segment:
   - Total time filter: tracks `mu_total, d_total` — what matters for runs
   - Clean tail filter: tracks `mu_clean, d_clean` — execution skill signal
   - Same attempt sequence, different observations. Failed attempts (no time) update neither.
   - `expected_time_ms` from total filter, `clean_expected_ms` from clean filter, `ms_per_attempt` from total filter (what allocator needs).

4. **Default constants review:**
   - `DEFAULT_D = -0.5` — Too aggressive. Assumes 500ms/att improvement before seeing data. Should be closer to 0 (agnostic) or small negative. With wide priors, the filter will learn quickly.
   - `DEFAULT_R = 25.0` — Reasonable (~5s std dev) for SMW segments. Should be estimated from data after a few attempts.
   - `DEFAULT_P_D0 = 1.0` — Uncertainty on drift. **Needs to be much wider** given burn-in. `P_D0 = 100.0` or higher — allowing improvement rates from 0 to 10s/att.
   - `Q_MM = 0.1` — Process noise on mu. Controls responsiveness. Placeholder.
   - `Q_DD = 0.01` — Process noise on d. Controls how fast drift estimate adapts. Placeholder.
   - `Q_MD = 0.0` — Uncorrelated. Fine.
   - `R_FLOOR = 1.0` — Defensive floor on R. Fine.
   - **Goal:** Data-driven priors from population (hierarchical). For now, widen `P_D0` and make `DEFAULT_D` less opinionated.

5. **Population priors (`get_population_priors`):** Interface is right (average mature segments to bootstrap new ones). Keep as-is, improve when we have enough data to make it meaningful.

6. **New outputs:** `uncertainty_ms = sqrt(P_mm) * 1000`, `trend_uncertainty_ms = sqrt(P_dd) * 1000`. These already exist in the state — just expose them.

### Rolling Stats (Model A)

**Current state:** Simplest model. Mean of all times, first-half vs second-half trend, min observed for floor.

**Improvements:**

1. **Two explicit windows.** Recent window and broad window, each producing a mean. Everything else derives from these two numbers. Window sizes should be named and eventually configurable/tunable.

2. **Trend with < 4 attempts.** Remove the `>= 4` gate. With 2 attempts, trend = `(time_1 - time_2)`. With 3 attempts, compare first 1 vs last 2 (or first 1 vs last 1). It may be noisy — that's honest. Return `None` only if there are 0 or 1 completed attempts.

3. **`expected_time_ms`:** Mean of recent window (total times). This is fine.

4. **`clean_expected_ms`:** Mean of recent window (clean tails). Also fine.

5. **Floor:** `min(total_times)` — this is a fact (gold), not an estimate. Honest about what it is. Fine for the empirical model.

6. **`uncertainty_ms`:** Could compute `std(recent_window) / sqrt(n_recent)` as a standard error. Better than None. Or leave as None — this model doesn't claim statistical rigor.

### Exp Decay (Model B)

**Current state:** Fits `time(n) = A * exp(-rate * n) + asymptote` via scipy curve_fit. Two fits (total times, clean tails).

**Improvements:**

1. **`ms_per_attempt` from total time curve, not clean tail.** The allocator needs "how much does my total time improve?" Compute as `f(n) - f(n+1)` (discrete difference) from the total time fit. This is the right conceptual quantity — "how much faster will I be after one more attempt."

2. **No fallback for < 3 points.** Return `None` for curve-derived fields (`ms_per_attempt`, `floor_estimate_ms`, `clean_floor_estimate_ms`) when `n < MIN_POINTS_FOR_FIT`. Don't substitute means. The dashboard should show a "needs data" indicator.

3. **Two separate fits are fine.** Total time fit for `expected_time_ms` and `ms_per_attempt`. Clean tail fit for `clean_expected_ms` and `clean_floor_estimate_ms`. Performance is not a concern.

4. **Asymptote accuracy:** Known to be unreliable with few points. This is fine — the user wants to see the estimate and judge it themselves. Priors (e.g., "floor is probably between 40-90% of current best") will help eventually but aren't needed now.

5. **`uncertainty_ms`:** Could derive from fit covariance matrix (`pcov` from curve_fit). Worth doing — it would give this model a real uncertainty estimate. Deferred but noted.

---

## Dashboard Changes

- Model table shows `None` as "--" or "..." instead of "0.0"
- Trend arrow logic: `None` trend = no arrow (gray dash)
- Consider showing uncertainty inline (e.g., "10.2s +/- 1.3s") when available

## Bug Fixes (from this testing session)

Tracked separately from model improvements:

1. **`_expected_columns` for attempts was wrong** — missing `id`, `goal_matched`, `rating`. Caused attempts table to be dropped and recreated on every restart. **Fixed 2026-04-01.**
2. **Overlay character regression** — `>` changed to Unicode arrow which Lua can't render. **Fixed 2026-04-01.**
3. **Greedy allocator stuck on first segment** — symptom of model_state save failure (stale composite PK). **Fixed 2026-04-01** via stale table detection.

---

## Implementation Order

Phase 1 (mechanical, no model changes):
1. Make ModelOutput fields `float | None`
2. Update dashboard to handle None values
3. Fix Exp Decay to return None when < 3 points instead of falling back
4. Fix Rolling Stats trend to work with 2-3 attempts

Phase 2 (Kalman improvements):
5. Add `uncertainty_ms` and `trend_uncertainty_ms` to ModelOutput (Kalman computes, others return None)
6. Widen Kalman priors (P_D0, DEFAULT_D)
7. Dual Kalman filter (clean + total)

Phase 3 (principled R and floor):
8. Principled R estimation (innovation-based)
9. Model-derived floor for Kalman (projected convergence)
10. Exp Decay `ms_per_attempt` from total time curve (discrete difference)

Phase 4 (future):
11. Data-driven population priors
12. Exp Decay uncertainty from pcov
13. VoI-aware allocator using uncertainty fields

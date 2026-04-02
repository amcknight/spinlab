# ModelOutput V2 — Clean Foundation

Design spec produced from 2026-04-01 review session. Supersedes `docs/model-improvements-spec.md` for ModelOutput structure and per-model output behavior. Does NOT cover uncertainty fields, dual Kalman filter implementation, or Kalman constant tuning — those are future work.

## Principles

1. **No silent fallbacks.** If a model can't compute a value, return `None`. Never substitute another model's answer, duplicate a total-time value into a clean-tail field, or fake a 0.0.
2. **No unexplained constants.** Every number is data-derived, a named parameter with statistical meaning, or explicitly marked as a placeholder.
3. **Show honest data.** Wild values from few data points are preferable to hidden problems. `None` means "cannot compute," not "ugly number."
4. **Predict forward.** `expected_ms` answers "what will my next attempt look like?" — always predict at index `n` (the next unobserved attempt), not `n-1` (the last observed one).

## Core Design: Two-Sided Estimate

### Problem

ModelOutput V1 has five flat fields mixing total-time and clean-tail semantics. Field names are asymmetric (`expected_time_ms` vs `clean_expected_ms`). Models that can't compute clean-tail values silently copy total-time values, violating Principle 1.

### Solution

Split ModelOutput into two parallel `Estimate` structs — one for total time, one for clean tail. Each has the same three fields. Clean/total is a structural distinction, not a naming convention.

```python
@dataclass
class Estimate:
    """One coherent set of predictions for a single time series."""
    expected_ms: float | None    # E[this quantity | next attempt]
    ms_per_attempt: float | None # decrease in expected_ms per attempt (positive = improving)
    floor_ms: float | None       # estimated best achievable

@dataclass
class ModelOutput:
    """What every estimator produces — predictions for total time and clean tail."""
    total: Estimate   # total segment time — what matters for runs
    clean: Estimate   # clean tail — execution skill signal
```

### Field Definitions

**`expected_ms`** — Point prediction for the next attempt. Kalman: `(mu + d) * 1000`. Exp Decay: `f(n)` where `n = len(completed)`. Rolling Mean: `mean(recent_times)`.

**`ms_per_attempt`** — How much `expected_ms` decreases after one more attempt. Positive means improving. Kalman: `-d * 1000` (constant linear drift). Exp Decay: `f(n) - f(n+1)` (discrete difference, decelerating). Rolling Mean: half-split trend (crude average). These are different quantities from different models answering the same question — the allocator uses them for relative ordering, which works.

**`floor_ms`** — Estimated best achievable time with infinite practice. Only meaningful when the model has a floor concept. Exp Decay: fitted asymptote. Rolling Mean: `min(observed)` (a fact, not a model estimate, but honest about what it is). Kalman: `None` (gold is a fact about history, not a model-derived floor estimate).

### What `None` Means

`None` = "this model cannot compute this field with available data." Not used to hide ugly values — only for genuinely incalculable fields. Dashboard renders `None` as "--". Allocator treats `None` as "skip this segment for this criterion."

## Model Renames

| Old | New (registry name) | New (class) | New (filename) | Display Name |
|-----|---------------------|-------------|----------------|--------------|
| `model_a` | `rolling_mean` | `RollingMeanEstimator` | `rolling_mean.py` | Rolling Mean |
| `model_b` | `exp_decay` | `ExpDecayEstimator` | `exp_decay.py` | Exp. Decay |
| `kalman` | `kalman` (unchanged) | `KalmanEstimator` (unchanged) | `kalman.py` (unchanged) | Kalman Filter |

Renames apply to: filenames, class names, registry keys, display names, test files, imports, dashboard references, and any JS that references model names.

## Per-Model Output Behavior

### Kalman Filter (single filter on total times)

```
total:
  expected_ms:    (mu + d) * 1000     # predict forward one step
  ms_per_attempt: -d * 1000           # linear drift rate
  floor_ms:       None                # no floor model

clean:
  expected_ms:    None                # no clean filter yet
  ms_per_attempt: None
  floor_ms:       None
```

Current Kalman runs one filter on total times. Until a dual Kalman filter is implemented (separate spec), the clean side is entirely `None`. This is honest — the single filter has no information about clean tails.

The off-by-one fix: V1 returned `state.mu * 1000` which is the posterior after the last update. V2 returns `(state.mu + state.d) * 1000` — one predict step forward, answering "what will my next attempt be?"

### Exp Decay (two separate fits)

```
total (from total time fit):
  expected_ms:    A_total * exp(-r_total * n) + asymptote_total
  ms_per_attempt: f_total(n) - f_total(n+1)     # discrete difference
  floor_ms:       asymptote_total

clean (from clean tail fit):
  expected_ms:    A_clean * exp(-r_clean * n) + asymptote_clean
  ms_per_attempt: f_clean(n) - f_clean(n+1)
  floor_ms:       asymptote_clean
```

Where `n = len(completed)` (the next unobserved index).

**< 3 completed attempts:** Both sides return `Estimate(None, None, None)`. No silent fallback to means or mins.

**`ms_per_attempt` change from V1:** V1 computed this from the clean tail curve derivative. V2 computes `total.ms_per_attempt` from the total fit and `clean.ms_per_attempt` from the clean fit. Each side uses its own curve.

**`ms_per_attempt` formula change:** V1 used the continuous derivative `A * r * exp(-r * n)`. V2 uses the discrete difference `f(n) - f(n+1)` which directly answers "how many ms faster will the next attempt be?" Same idea, slightly more honest at small `n`.

### Rolling Mean (recomputes from attempt history)

```
total:
  expected_ms:    mean(total_times)          # mean of all completed attempts
  ms_per_attempt: half-split trend on total times
  floor_ms:       min(total_times)

clean:
  expected_ms:    mean(clean_tails)
  ms_per_attempt: half-split trend on clean tails
  floor_ms:       min(clean_tails)
```

**Trend gate fix:** V1 returned `0.0` for < 4 attempts. V2:
- 0-1 completed: `ms_per_attempt = None`
- 2 completed: `time_1 - time_2` (positive if improving)
- 3+ completed: first-half mean minus second-half mean, divided by half-length

**Clean tail fallback:** If no `clean_tail_ms` values exist for any attempts, `clean` side returns `Estimate(None, None, None)`. V1 silently copied total times into clean tails.

## Consumer Changes

### Allocator (`greedy.py`)

Currently reads `output.ms_per_attempt`. Changes to `output.total.ms_per_attempt`. Handles `None` by skipping that segment (if all segments are None, falls back to round-robin or whatever the current no-data behavior is).

### Dashboard

- Model table shows both total and clean columns
- `None` renders as "--" (not "0.0", not blank)
- Trend arrows: `None` trend = gray dash, no arrow

### Practice Session (`practice.py`)

Currently reads `output.expected_time_ms`. Changes to `output.total.expected_ms`.

### Serialization (`to_dict` / `from_dict`)

Nested structure:
```json
{
  "total": {"expected_ms": 12000.0, "ms_per_attempt": 150.0, "floor_ms": 9500.0},
  "clean": {"expected_ms": 8000.0, "ms_per_attempt": 80.0, "floor_ms": 6200.0}
}
```

`from_dict` must handle V1 format for backward compatibility with existing DB rows (flat keys → map to `total` side, `clean` side gets `None`).

### DB `model_state` table

The `output_json` column stores serialized ModelOutput. Existing rows have V1 format. Migration strategy: `from_dict` handles both formats. No schema migration needed.

## What's NOT In This Spec

- **Uncertainty fields** (`uncertainty_ms`, `trend_uncertainty_ms`) — Phase 2, added as new optional fields on `Estimate`
- **Dual Kalman filter** — separate spec; when implemented, Kalman populates both sides
- **Kalman constant tuning** (widening `P_D0`, adjusting `DEFAULT_D`) — independent concern
- **Population priors** — future phase
- **VoI allocator** — future phase, will consume uncertainty fields
- **Innovation-based R estimation** — Kalman internal improvement, doesn't affect ModelOutput shape

## Implementation Order

1. Create `Estimate` dataclass, restructure `ModelOutput` with `total` / `clean`
2. Rename Model A → `rolling_mean`, Model B → `exp_decay` (files, classes, registry, tests)
3. Fix Kalman `model_output`: predict forward (`mu + d`), clean = all None, floor = None
4. Fix Exp Decay: evaluate at `n` not `n-1`, `ms_per_attempt` as discrete difference from respective fits, None for < 3 points
5. Fix Rolling Mean: trend with 2+ attempts, clean side from clean data only (None if no clean data)
6. Update consumers: allocator, dashboard, practice session, serialization
7. Update all tests
8. Dashboard: render None as "--", show both total/clean columns

# Multi-Model Estimator Upgrade — Design Spec

## Context

SpinLab currently has one estimator (Kalman filter). This spec adds Models A (rolling stats) and B (exponential decay) in v1, with Model C (death-aware generative) planned for v2. The reference spec is in `reference/speedrun_model_spec.md` and `reference/speedrun_models.py`.

This spec was produced from a brainstorming session. It covers architectural prerequisites, model designs, and what we explicitly deferred.

---

## Decisions Made

### Death-Awareness Is Cross-Cutting (Not Just Model C)

A dirty completion (with deaths) on a CP-to-CP segment contains an embedded clean completion: the time from the last death to the segment finish. This "clean tail" is a direct observation of execution skill without death noise.

**Every model can use clean tails:**
- Model A: rolling stats on clean tails for better floor estimates
- Model B: fit exponential on clean tails so floor `c` reflects true execution floor
- Kalman: could track clean tail separately for better skill signal

**Data to capture per attempt:**
- `time_ms` — total time including deaths (what we track now)
- `deaths` — count (record it, might be useful later, but clean_tail is the richer signal)
- `clean_tail_ms` — time from last death to segment finish (= time_ms when 0 deaths)

The Lua script already detects deaths with timestamps. On segment completion, `clean_tail_ms = completion_timestamp - last_death_timestamp` (or `= time_ms` if 0 deaths).

**Overlay:** Show clean tail time, death count, and total time on the Lua overlay after completion. Total time is an estimate since it skips death animation (could eventually use config or learn from reference runs).

### ModelOutput Contract

What every model produces:

```python
@dataclass
class ModelOutput:
    expected_time_ms: float     # E[time] total, including deaths, for next attempt
    clean_expected_ms: float    # E[clean_tail] for next attempt
    ms_per_attempt: float       # ms improvement per attempt (positive = improving)
    floor_estimate_ms: float    # model's predicted asymptotic best clean time
```

Notes:
- `ms_per_attempt` is the model's native output. The allocator computes `ms_per_min = ms_per_attempt * attempts_per_minute` itself from attempt timestamps.
- `floor_estimate_ms` is NOT gold_ms. Gold is best observed time (fact). Floor is predicted asymptotic best (estimate). Floor <= gold eventually.
- `headroom_ms` dropped from contract — trivially derived as `expected - floor`, UI can compute.
- `confidence` dropped from v1. Exploration/exploitation handled by allocator policy using n_completed, not model-reported confidence. Revisit if we design VoI/multi-armed-bandit allocator later.
- `expected_after_1min_ms` dropped — redundant with `expected - ms_per_attempt * apm`.

### Allocator Input

The allocator wants: "what should I practice for the next N minutes to improve fastest?"

That's `ms_per_min = ms_per_attempt * attempts_per_minute`.

The current `marginal_return = -d/mu` is a dimensionless ratio that doesn't account for attempts_per_minute. It accidentally partially works because longer segments have fewer attempts/min, but it's not the right formula. Replace with `ms_per_min`.

`attempts_per_minute` is a shared utility computed from attempt timestamps, independent of any model.

### One Active Model for Allocator

v1: User manually selects which model feeds the allocator (like current estimator selector). All models compute on every attempt in the background. Dashboard shows all models' outputs. Allocator reads from the active one.

No auto-selection rules (too hardcoded). No ensemble weighting (too complex for v1).

### Online vs Batch

- **Kalman:** Truly stateful/online. State updated incrementally per attempt.
- **Model A:** Stateless. Recomputes from all attempts each time. Fast (medians on small arrays).
- **Model B:** Stateless. Recomputes from all attempts each time. Fast (curve_fit on <100 points is ~1ms).

All models receive the full attempt history on each new attempt. Uniform interface:

```python
def process_attempt(
    self,
    state: EstimatorState,
    new_attempt: AttemptRecord,
    all_attempts: list[AttemptRecord],
) -> EstimatorState:
```

Kalman uses `new_attempt` and ignores `all_attempts`. A and B use `all_attempts` and ignore `new_attempt`/`state`. Each model uses what it needs.

### Multi-Model Storage

`model_state` table PK changes from `segment_id` to `(segment_id, estimator)`. On each attempt, scheduler runs ALL registered models and saves each one's state + ModelOutput. Cost is trivial.

### No Migrations

All current data is minimal and can be remade. DB schema changes are destructive (drop and recreate). No migration code needed.

---

## Prerequisite Architecture Changes

In dependency order:

### Prereq 1: AttemptRecord + Deaths + Clean Tail

New shared dataclass for attempt data flowing through the system:

```python
@dataclass
class AttemptRecord:
    time_ms: int | None       # total time including deaths; None if incomplete
    completed: bool
    deaths: int               # 0 if not tracked
    clean_tail_ms: int | None # time from last death to finish; None if incomplete
    created_at: str           # ISO timestamp
```

DB changes:
- Add `deaths INTEGER DEFAULT 0` to attempts table
- Add `clean_tail_ms INTEGER` to attempts table
- `get_segment_attempts()` returns full records including new fields

Lua changes:
- Count deaths per attempt, track last death timestamp
- On completion event, include `deaths` and `clean_tail_ms` in the TCP message
- Overlay shows: total time, death count, clean tail time

### Prereq 2: Widen Estimator ABC

```python
class Estimator(ABC):
    def process_attempt(
        self,
        state: EstimatorState,
        new_attempt: AttemptRecord,
        all_attempts: list[AttemptRecord],
    ) -> EstimatorState: ...

    def rebuild_state(self, attempts: list[AttemptRecord]) -> EstimatorState: ...

    def model_output(self, state: EstimatorState, all_attempts: list[AttemptRecord]) -> ModelOutput: ...
```

Kalman adapts: reads `new_attempt.time_ms / 1000 if completed else None`, ignores deaths/clean_tail for now. Its `model_output()` translates mu/d into the ModelOutput contract.

### Prereq 3: Multi-Model model_state

Change `model_state` table:
```sql
CREATE TABLE model_state (
    segment_id TEXT NOT NULL REFERENCES segments(id),
    estimator TEXT NOT NULL,
    state_json TEXT NOT NULL,
    output_json TEXT NOT NULL,   -- serialized ModelOutput
    updated_at TEXT NOT NULL,
    PRIMARY KEY (segment_id, estimator)
);
```

`marginal_return` column replaced by `output_json` containing the full ModelOutput.

### Prereq 4: Scheduler Runs All Models

`Scheduler.process_attempt()` loops over all registered estimators, updates each one's state, saves each one's ModelOutput. The "active" estimator selection only affects which ModelOutput the allocator reads.

### Prereq 5: SegmentWithModel + Dashboard + Allocator

`SegmentWithModel` changes:

```python
@dataclass
class SegmentWithModel:
    # ... segment metadata fields unchanged ...
    model_outputs: dict[str, ModelOutput]  # {"kalman": ..., "A": ..., "B": ...}
    selected_model: str                     # which one feeds allocator
    n_completed: int
    n_attempts: int
    gold_ms: int | None
```

Dashboard API returns all models' outputs. Frontend renders them side by side. Allocator uses `model_outputs[selected_model].ms_per_attempt * apm`.

---

## Model Designs (v1)

### Model A: Rolling Statistics (Model-Free)

**Philosophy:** No assumptions about curve shape. Always works.

**Input:** All attempts (uses clean_tail_ms when available, falls back to time_ms).

**Algorithm:**
- Recent window (last 20% of completions, min 3): compute median of clean tails
- Broad window (last 50%, min 5): compute median of clean tails
- Trend = (recent_median - broad_median) / attempt_gap between window centers
- `ms_per_attempt = -trend` (positive = improving)
- `expected_time_ms = recent_median` (of total times)
- `clean_expected_ms = recent_median` (of clean tails)
- `floor_estimate_ms = min(clean_tails)` (crude but honest)

**State:** Minimal bookkeeping (n_completed, n_attempts, gold). Recomputes from all_attempts each time.

**When it's useful:** Always. Sanity check for other models. Best with few data points. Adapts to strat changes naturally (rolling window forgets).

### Model B: Exponential Decay

**Philosophy:** Times follow `a * exp(-b * n) + c` toward a floor.

**Input:** All attempts. Fits on clean_tail_ms for floor estimation.

**Algorithm:**
- Fit `clean_tail(n) = a * exp(-b * n) + c` via scipy.optimize.curve_fit
- `c` is the floor estimate (clean execution asymptote, not contaminated by deaths)
- `ms_per_attempt` from derivative: `a * b * exp(-b * n)` at current n
- `expected_time_ms` from recent total times (not the curve, which is clean-only)
- `clean_expected_ms` from the fitted curve at current n
- `floor_estimate_ms = max(c, best_clean_tail * 0.60)` (clamped by hard prior)

**Parameters stored:** a, b, c, sigma (fit residual std), plus bookkeeping.

**When it's useful:** 10+ attempts, monotone improvement (no recent strat change).

**Floor prior:** No floor below 60% of best observed clean tail. Prevents absurd extrapolation.

### Kalman: Adapt to New Interface

No algorithm changes. Just implement `model_output()`:
- `expected_time_ms = mu * 1000`
- `clean_expected_ms` = same as expected for now (Kalman doesn't distinguish; could improve later)
- `ms_per_attempt = -d * 1000` (drift in ms)
- `floor_estimate_ms = gold * 1000` (best observed; Kalman has no principled floor)

---

## v2: Model C (Death-Aware Generative)

Deferred. Will decompose time into clean execution + death penalties. Uses the death data we're already accumulating in v1. Design in `reference/speedrun_model_spec.md` is mostly right but needs the clean_tail insight integrated.

Key v2 additions:
- ImprovementBreakdown: "X ms/min from execution, Y ms/min from fewer deaths"
- Debiased death cost estimation (regress on deaths AND attempt number)
- Clean run analysis diagnostics

## v2+: Meta-Learning / Priors

Deferred. The data needed (fitted parameters per segment across many segments) accumulates naturally from v1. When enough segments have 30+ attempts, we can derive population priors for Model B's learning rate, typical floor ratios, etc.

Backtesting = retroactively scoring model predictions against what actually happened. Useful for tuning Model A's window sizes empirically. Requires stored attempt history (which we have).

## Dropped

- **Ensemble layer** (disagreement metrics, model voting) — v3+
- **Strat change detection** (CUSUM on residuals) — maybe v2+, plan for it but don't build
- **Session awareness** (warmup/fatigue) — dropped entirely
- **Diagnostic flags system** — dropped as a formal system
- **Confidence in ModelOutput** — dropped; allocator handles exploration via n_completed
- **expected_after_1min_ms** — dropped; redundant
- **headroom_ms in contract** — dropped; trivially derived

---

## Open Questions (for future sessions)

1. Should Kalman track clean_tail separately for a "clean mu" signal?
2. What should the Lua overlay look like for the new death/clean-tail display?
3. Death animation time estimation — config value vs learned from reference runs?
4. VoI / multi-armed bandit allocator design (uses uncertainty, would want CI on ms_per_min)

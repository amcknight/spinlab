# Practice Simulation Engine — Design Spec

**Status:** drafted 2026-06-01. Spec #3 of the model-purge / sampler-core / practice-allocator arc (see `project_model_purge_arc`).

**Companion docs:**
- Sampler (§0) built in: [`2026-05-30-em-suite-sampler-design.md`](2026-05-30-em-suite-sampler-design.md) and [`2026-06-01-sampler-real-and-scalar.md`](../plans/2026-06-01-sampler-real-and-scalar.md).
- Underlying allocator vision: [`docs/practice-allocator-spec.md`](../../practice-allocator-spec.md) (§1–§5 from-first-principles statement).

## 1. Purpose

Build the **Practice Simulation Engine**: a self-contained library that consumes per-segment `SamplerState` from the EMA-Suite sampler (`§0`) and produces:

- **Vectorized rollouts** of full runs (`§1`+`§2` from the allocator spec): the matrix `T[N, K]` of per-segment times for N imagined runs across K gated segments.
- **Reset-policy reductions** over the matrix → per-rollout `(finished, abort_at, wall_ms)` masks.
- **Objective reductions** → scalar quantities (expected total time, `q(T*)`, `p_pb_this_session`, etc.).
- **Per-segment value attribution** — the `§4` ranking primitive.

Plus a **read-only dashboard panel** that surfaces all of the above for live inspection.

The engine is positioned as **shared infrastructure** for several downstream consumers (practice allocator, run-time advisor, dashboard stats) — none of which are built in this spec.

## 2. Out of scope

Each of these is a deliberate non-goal of this spec — each is a candidate for a downstream spec once the engine ships and we've eyeballed its behavior on real data.

- **A new practice allocator.** The engine exposes `per_segment_values`; the consumer that turns it into `pick_next` is a separate spec.
- **A run-time advisor** ("should I quit this run?"). Different objective, different trigger, different UI.
- **A target-time / session subsystem.** `p_pb_this_session` requires `session_remaining_ms`; for v0 the dashboard panel takes it as a user input. No persistence.
- **Multi-segment value attribution.** "Practice A and B together" is mathematically supported by the matrix (swap two columns) but absent from the ranking API.
- **Storing engine state in the DB.** The matrix is in-memory only and rebuilds from `SamplerState` rows on engine init.
- **Incremental column updates** ("don't redraw, mutate the column in place"). N=20k full-column rebuilds are fast enough.
- **Threshold-source variety beyond user-entered splits.** v0 takes a single user-entered per-segment cumulative split table. For dev/testing while Andrew is AFK, a default derived from gold-per-segment is acceptable.

## 3. Architecture

```
┌────────────────────────────────────────────────────────┐
│  Per-segment SamplerState (from em_suite_sampler)      │  ← already shipped
└──────┬─────────────────────────────────────────────────┘
       ▼
┌────────────────────────────────────────────────────────┐
│  RolloutMatrix    T[N, K]                              │
│  • column-keyed cache invalidation                     │
│  • always-full-length draws                            │
│  • deterministic per-column seeding                    │
└──────┬─────────────────────────────────────────────────┘
       ▼
┌────────────────────────────────────────────────────────┐
│  ResetPolicy (pure fn) :                               │
│    (T, threshold_cum_ms, **kwargs) → ResetMasks        │
└──────┬─────────────────────────────────────────────────┘
       ▼
┌────────────────────────────────────────────────────────┐
│  Objective (pure fn) :                                 │
│    (T, masks, ctx) → float | None                      │
└──────┬─────────────────────────────────────────────────┘
       ▼
┌────────────────────────────────────────────────────────┐
│  PracticeEngine  (the consumer-facing API)             │
│  • evaluate(policy, threshold_kwargs, obj, ctx) → ...  │
│  • per_segment_values(policy, …, obj, ctx) → dict      │
│  • total_time_distribution(policy, …) → ndarray        │
│  • column_summary(seg_id) → dict                       │
└────────────────────────────────────────────────────────┘
```

The engine is **owned by the `Scheduler`** (`scheduler.engine`, lazy property). Game switch → new Scheduler → new engine.

## 4. Data structures

```python
@dataclass
class RolloutMatrix:
    T: np.ndarray            # shape (N, K), dtype float64, ms; k=0 draws
    seg_ids: list[str]       # column k corresponds to segment seg_ids[k]
    N: int
    rng_seed: int            # global engine seed; per-(rollout, segment) seeds derived
    dirty: set[str]          # seg_ids whose columns need a rebuild
    cost_ms: np.ndarray      # shape (K,), per-segment E[sample(0)] cached at build time

@dataclass
class ResetMasks:
    finished: np.ndarray     # shape (N,), bool
    abort_at: np.ndarray     # shape (N,), int; -1 if finished, otherwise segment index of abort
    wall_ms: np.ndarray      # shape (N,), float64; cumulative ms through (and including)
                             # the aborting segment, or full sum if finished

@dataclass
class PerSegmentValue:
    seg_id: str
    value: float              # baseline_obj − swap_obj (raw, signed; UI colors by direction)
    value_per_second: float   # value / cost_ms[i]; None if cost_ms[i] == 0
    e_sample_0_ms: float
    e_sample_1_ms: float
```

**RNG seeding** is per-segment: column `k` uses `np.random.default_rng(rng_seed + k)`, then draws N rows in sequence. This gives reproducibility — the same engine seed produces the same matrix — but does not pair individual draws between baseline and counterfactual evaluations. The per-segment value computation just redraws the column with `k_param=1` and takes the diff against the baseline mean; both sides carry Monte Carlo noise of `~σ/√N`, and the diff carries roughly `√2·σ/√N`. See risk #1 in §13.

## 5. Reset policies

All reset policies are pure functions of `T` and a few kwargs. No DB, no state.

```python
class ResetPolicy(Protocol):
    name: str
    def __call__(self, T: np.ndarray, **kwargs) -> ResetMasks: ...
```

**v0 implementations:**

```python
def no_reset(T):
    N, K = T.shape
    return ResetMasks(
        finished=np.ones(N, dtype=bool),
        abort_at=np.full(N, -1, dtype=np.int32),
        wall_ms=T.sum(axis=1),
    )

def target_paced(T, threshold_cum_ms, slack=0.0):
    """Abort the first time cumulative time exceeds threshold_cum_ms[k] * (1+slack)."""
    if threshold_cum_ms is None:
        return no_reset(T)
    N, K = T.shape
    cum = T.cumsum(axis=1)
    threshold = threshold_cum_ms * (1.0 + slack)
    over = cum > threshold[None, :]
    any_over = over.any(axis=1)
    abort_at = np.where(any_over, over.argmax(axis=1), -1).astype(np.int32)
    finished = ~any_over
    # wall_ms = cum through (and including) the abort segment, or full if finished
    safe_abort = np.where(any_over, abort_at, K - 1)
    wall_ms = cum[np.arange(N), safe_abort]
    return ResetMasks(finished, abort_at, wall_ms)
```

`best_recent_N` and `user_per_segment` are not implemented in v0 — they're future additions with the same signature. Adding them is a one-function change.

## 6. Threshold sources

v0 ships **one** source: user-entered per-segment cumulative split times.

```python
def thresholds_from_user(seg_ids: list[str], cum_splits_ms: dict[str, int]) -> np.ndarray:
    """User-entered cumulative split thresholds, one per segment."""
    return np.array([cum_splits_ms[s] for s in seg_ids], dtype=np.float64)
```

For dev/testing convenience: the dashboard panel exposes a "fill from gold" button that pre-populates the input table with each segment's cumulative gold time. Andrew can then edit. This is **not** a separate threshold source — it's a UI seed.

Future sources (PB-of-full-runs, WR-anchored, best-recent-N, etc.) are downstream additions.

## 7. Objectives

All objectives are pure functions; return `None` when their gate fails (e.g., zero finished rollouts). Never return a silent fallback. Per CLAUDE.md.

```python
class Objective(Protocol):
    name: str
    def __call__(self, T: np.ndarray, masks: ResetMasks, ctx: dict) -> float | None: ...
```

**v0 slate:**

```python
def expected_wall_clock_per_attempt(T, masks, ctx):
    return float(masks.wall_ms.mean())

def expected_total_finished_time(T, masks, ctx):
    if not masks.finished.any():
        return None
    return float(masks.wall_ms[masks.finished].mean())

def q(T, masks, ctx):
    """Fraction finished under ctx['target_ms']."""
    target = ctx["target_ms"]
    return float((masks.finished & (masks.wall_ms <= target)).mean())

def quantile(T, masks, ctx):
    p = ctx["p"]
    finished_times = masks.wall_ms[masks.finished]
    if len(finished_times) == 0:
        return None
    return float(np.quantile(finished_times, p))

def p_pb_this_session(T, masks, ctx):
    """1 − (1 − q)^(H/τ̄)."""
    q_val = q(T, masks, ctx)
    tau_bar = expected_wall_clock_per_attempt(T, masks, ctx)
    if tau_bar is None or tau_bar <= 0:
        return None
    H = ctx["session_remaining_ms"]
    attempts_remaining = H / tau_bar
    return float(1.0 - (1.0 - q_val) ** attempts_remaining)
```

**Sign convention:** the engine does *not* declare objective direction. `per_segment_values` returns the raw `baseline − swap` diff. The UI colors values green/red based on a lookup table keyed by objective name (`expected_wall_clock_per_attempt` → green if positive; `q` → green if negative; etc.). Keeps the math simple; UX layer owns the directionality.

## 8. The `PracticeEngine` API

```python
class PracticeEngine:
    matrix: RolloutMatrix

    def __init__(self, sampler_states: dict[str, SamplerState], N: int, rng_seed: int):
        ...

    def invalidate(self, seg_id: str) -> None:
        """Mark a segment's column dirty. Next .ensure_fresh() rebuilds it."""

    def ensure_fresh(self) -> None:
        """Rebuild all dirty columns. No-op if nothing dirty."""

    def evaluate(
        self,
        policy: ResetPolicy,
        threshold_kwargs: dict,
        objective: Objective,
        ctx: dict,
    ) -> dict:
        """Single objective evaluation. Returns {'value': float|None, 'masks_summary': {...}}."""

    def per_segment_values(
        self,
        policy: ResetPolicy,
        threshold_kwargs: dict,
        objective: Objective,
        ctx: dict,
    ) -> dict[str, PerSegmentValue]:
        """For each gated segment, baseline_obj − swap_i_to_k=1_obj."""

    def total_time_distribution(
        self,
        policy: ResetPolicy,
        threshold_kwargs: dict,
    ) -> dict:
        """Histogram payload of masks.wall_ms (over finished rollouts)."""

    def column_summary(self, seg_id: str) -> dict:
        """Per-segment stats: mean, std, p10, p50, p90, n, e_sample_0, e_sample_1."""
```

### Per-segment value attribution (the `§4` method)

```python
def per_segment_values(self, policy, threshold_kwargs, objective, ctx):
    self.ensure_fresh()

    baseline_masks = policy(self.matrix.T, **threshold_kwargs)
    baseline_obj = objective(self.matrix.T, baseline_masks, ctx)
    if baseline_obj is None:
        return {}

    results: dict[str, PerSegmentValue] = {}
    for i, seg_id in enumerate(self.matrix.seg_ids):
        swap_col = self._draw_column(seg_id, k_param=1)
        T_swap = self.matrix.T.copy()
        T_swap[:, i] = swap_col

        swap_masks = policy(T_swap, **threshold_kwargs)
        swap_obj = objective(T_swap, swap_masks, ctx)
        if swap_obj is None:
            continue

        value = baseline_obj - swap_obj  # raw signed diff; UI handles color
        cost = self.matrix.cost_ms[i]
        value_per_second = value / cost if cost > 0 else None

        results[seg_id] = PerSegmentValue(
            seg_id=seg_id,
            value=value,
            value_per_second=value_per_second,
            e_sample_0_ms=cost,
            e_sample_1_ms=float(swap_col.mean()),
        )
    return results
```

**Variance:** `baseline_obj` and `swap_obj` each carry Monte Carlo noise of `~σ_total/√N`; the diff carries roughly `√2·σ_total/√N`. At N=20k and σ_total in the range of a few seconds, that's tens of ms of std-error on each per-segment value. Sufficient to rank segments whose true Δ is meaningfully above that floor; users wanting tighter measurements crank N. Common-random-numbers variance reduction is a defensible future addition (see §13 risk #1).

## 9. Scheduler integration

- New property `Scheduler.engine: PracticeEngine` — lazy; first read builds it from `self._load_all_sampler_states()`.
- New method `Scheduler._load_all_sampler_states() -> dict[str, SamplerState]` — reads `db.load_all_model_states_for_game(game_id)`, deserializes each `state_json` via `SamplerState.from_dict`, filters to gated segments. Cheap; runs once per engine build.
- `Scheduler.update_state_after_episode(seg_id)` calls `self.engine.invalidate(seg_id)` at the bottom (after the state rewrite). Subsequent engine calls automatically refresh.
- Game switch destroys the old `Scheduler` (already true today); new one builds a fresh engine on next access.

**Ungated segments** are excluded from the matrix's `K`. When a segment crosses the gate threshold (≥2 successes and ≥2 deaths) after a new attempt, `ensure_fresh()` detects the new gated segment via the current `SamplerState` set and grows the matrix by one column. Concretely: `Scheduler.update_state_after_episode` always calls `engine.invalidate(seg_id)`; on the next `ensure_fresh()`, the engine reloads the SamplerState dict, diffs `seg_ids` against its current `seg_ids`, and rebuilds the matrix from scratch if K changed (cheap at N=20k: ~1s). Otherwise it rebuilds only dirty columns. Matrix dimension is dynamic.

## 10. Performance budget

- **N = 20,000** by default. YAML-configurable via `practice_engine.rollouts`.
- **Storage**: `N × K × 8 bytes`. For K=50: 8 MB. Trivial.
- **Column rebuild**: N calls to `sample_episode`. Assumed ~1µs per call → 20ms per column. To be profiled in the implementation plan's first task.
- **Per-segment-value full pass**: K column-swap rebuilds + K reductions. For K=50, N=20k: ~1 second total. Acceptable as a live cost.
- **Reset-policy + objective reduction over T**: vectorized numpy. Microseconds.

If profiling reveals `sample_episode` is materially slower than 1µs/call, the default N can drop; the architecture is unchanged.

## 11. Dashboard panel

Read-only diagnostic surface. Renders well on desktop (mobile is acceptable but not optimized).

### Backend routes

```
GET  /api/practice-engine/state
  → {
      "gated_segments": [
        {seg_id, description, level, e_sample_0_ms, e_sample_1_ms,
         pool_sizes: {success: int, death: int}},
        ...
      ],
      "ungated_segments": [{seg_id, reason: str}, ...],
      "matrix_built_at": iso8601 | null,
      "N": int,
    }

POST /api/practice-engine/evaluate
  Body: {
    "policy": "no_reset" | "target_paced",
    "policy_kwargs": {
      "cum_splits_ms": {seg_id: int} | null,   # only for target_paced
      "slack": float,                            # only for target_paced
    },
    "objective": "expected_wall_clock_per_attempt"
               | "expected_total_finished_time"
               | "q"
               | "quantile"
               | "p_pb_this_session",
    "objective_ctx": {
      "target_ms": int | null,
      "p": float | null,
      "session_remaining_ms": int | null,
    },
  }
  → {
      "objective_value": float | null,
      "per_segment_values": [
        {seg_id, value, value_per_second, e_sample_0_ms, e_sample_1_ms},
        ...
      ],
      "total_time_summary": {
        "bins": [...], "counts": [...],
        "mean": float, "median": float, "p10": float, "p90": float,
        "finished_pct": float,
        "aborted_by_segment": {seg_id: int},
      },
    }
```

### Frontend panel

A new dashboard tab or a section under the existing Model tab. UX details are deferred to implementation; the required content is:

- **Controls**: policy dropdown, threshold inputs (per-segment editable cumulative-split table with a "fill from gold" button for `target_paced`), slack slider, objective dropdown, per-objective ctx inputs (target_ms, session_remaining_ms, p), recompute button.
- **Display**: headline objective value; total-time histogram (chart.js); reset breakdown (`"73% finished, 27% aborted at: 0:12% 1:7% 2:5% 3:3%"`); per-segment value table with sortable columns `Segment | E[sample(0)] | E[sample(1)] | Δ (one-step) | Value | Value/sec`. Cells colored green/red by sign according to objective direction (green = "practice helps this objective"; red = "practice hurts").

## 12. Testing

**Unit tests:**

- Per reset policy: deterministic `T` → expected `ResetMasks`. Cover never-aborts, always-aborts-at-0, mixed.
- Per objective: deterministic `(T, masks, ctx)` → expected scalar; also each None-gate case.
- Threshold helpers: cum-splits-from-user matches manual computation.
- `per_segment_values` correctness: with a controlled fake sampler whose `k=1` shifts a single segment's column by a known constant `δ` and other segments are unchanged, `value[that_segment]` should equal `δ × (per-objective sensitivity)` to within `O(1/√N)` error. Other segments' values fluctuate within Monte Carlo noise of zero.

**Engine integration tests:**

- Build from a synthetic `dict[seg_id, SamplerState]`; column means match analytic expectations.
- `invalidate(seg_id)` adds to `dirty`; `ensure_fresh()` rebuilds and clears it.
- `Scheduler.update_state_after_episode(seg_id)` propagates to `engine.matrix.dirty.contains(seg_id)`.
- Game switch: new Scheduler → fresh engine, no leakage.
- Mid-session gating: a previously-ungated segment whose new attempt pushes it past the gate appears as a new column on the next `ensure_fresh()`.

**Dashboard API tests:**

- `GET /api/practice-engine/state` for empty / all-ungated / all-gated DBs.
- `POST /api/practice-engine/evaluate` round-trips for each (policy, objective) combination; objective `None` surfaces as `null` JSON.

## 13. Risks / open items

1. **Per-segment value Monte Carlo noise.** Each per-segment value carries `~√2·σ_total/√N` std-error from independent re-draws of the swap column. At N=20k that's tens of ms — fine for ranking segments whose true Δ is meaningfully above that floor, marginal for splitting hairs. Mitigations available if it bites: (a) crank N via config (linear cost), (b) add common-random-numbers variance reduction in a follow-up (thread N pre-drawn uniforms into `sample_episode`; structural change to the sampler API). Pick (a) until measurements force (b).

2. **`sample_episode` performance** — assumed ~1µs/call. If 10× slower, column rebuild grows from ~20ms to ~200ms at N=20k. Mitigation: profile in the implementation plan's first task; drop default N if needed. Architecture is unchanged.

3. **Scheduler.engine cold-start cost** — building the matrix on first access loads K SamplerStates + draws K columns. For K=50, N=20k: ~1s. Acceptable; the dashboard panel's first call wears it.

4. **`session_remaining_ms` has no producer today** — for `p_pb_this_session` objective, ctx requires this. v0: dashboard panel takes it as a user input. Future: a session-tracking subsystem produces it for live consumers.

5. **Mid-session segment gating** — handled by the dirty-set mechanism but adds a code path. Tested explicitly.

6. **N=20k correctness with pool-size=300** — every `sample_episode` draws independently from the ring buffer with replacement. ~67 expected uses per ring entry across the column. Adequate for distributional representativeness; tighter N or stratified sampling is a future optimization.

7. **Dashboard panel UX polish** — v0 ships functional but lightly styled. Iteration happens once Andrew is at the desktop and sees it.

## 14. Summary

This spec builds an engine, not an allocator. The engine produces vectorized rollouts (always full length), evaluates pluggable reset policies and objectives over them, and computes per-segment improvement values by re-drawing one column at a time. Downstream consumers — practice allocator, run-time advisor, dashboard stats — are explicit non-goals here; they consume the engine in their own specs.

The architecture is conservatively scoped so it can grow:
- More reset policies (best-recent-N, PB-anchored, WR-anchored) are one-function additions.
- More objectives (median-quantile-anchored, percentile-over-target, etc.) are one-function additions.
- The matrix sizes scale to N=100k+ with the same data structure; the only constraint is `sample_episode` throughput.
- A future smart-incremental-update mechanism (don't redraw whole columns when only one ring entry shifted) is compatible with this design.

The first deliverable beyond unit tests is a dashboard panel that lets Andrew play with policies and objectives on his real data — the "before we commit to a live allocator, let's see what the math actually does" step.

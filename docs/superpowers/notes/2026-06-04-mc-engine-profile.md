---
date: 2026-06-04
artifact: scripts/profile_mc_engine.py
git_head: d7c324a
related_scan: docs/superpowers/scans/2026-06-04-improve-mc-engine.md
---

# MC rollout engine profile — what's actually slow

Followup to the 2026-06-04 /improve scan focused on MC rollout engine
efficiency. The scan's six lenses + two critique agents converged on
engine-layer wins (one-shot evaluate API, cumsum dedup, K column copies).
The convergence-hunter and verifier both said: profile first, then pick
a plan. This is the profile.

## Setup

`scripts/profile_mc_engine.py` builds K=20 synthetic gated SamplerStates
(60 events each, ~2/3 success / ~1/3 death) and drives one full /evaluate
route workload — `engine.evaluate` + `engine.per_segment_values` +
`engine.total_time_distribution` with `target_paced` policy and
`expected_total_finished_time` objective. cProfile around the workload.

```
N=10000 rollouts, K=20 columns
initial ensure_fresh()        930 ms (full matrix build, off the route path)
one /evaluate workload       2560 ms  ← the reproducible 3-second lag
```

## Where the time goes

| function | calls | tottime | cumtime | share |
|---|---|---|---|---|
| `per_segment_values` (engine.py:107) | 1 | 0.002 | **2.554** | **99.8%** |
| `draw_column` (rollout_matrix.py:107) | 20 | 0.000 | 2.531 | 98.9% |
| `_draw_column_impl` (rollout_matrix.py:125) | 20 | 0.053 | 2.531 | 98.9% |
| `sample_episode` (em_suite_sampler.py:409) | 200_000 | 0.344 | 2.478 | 96.8% |
| `draw_from_pool` (em_suite_sampler.py:90) | **254_590** | **0.670** | 1.133 | 26.2% leaf, 44.3% cum |
| `random.choices` (stdlib) | 254_590 | 0.345 | 0.448 | 13.5% leaf |
| `trend_signal_slopes` (em_suite_sampler.py:316) | **200_000** | 0.232 | **0.722** | **28.2% cum** |
| `_logit` (em_suite_sampler.py:298) | 600_000 | 0.206 | 0.333 | 13.0% cum |
| `p_die_ema` | 600_000 | 0.093 | 0.126 | |
| `target_paced` (reset_policies.py:22) | 23 | 0.003 | **0.018** | **0.7%** |
| `numpy.cumsum` | 23 | 0.009 | 0.009 | 0.4% |
| `T.copy()` | 20 | 0.003 | 0.003 | 0.1% |
| `evaluate` + `total_time_distribution` baseline work | — | — | **<0.010** | <0.4% |

## What the scan got wrong

The six lenses + skeptic + convergence-hunter fixated on three things
that the profile shows are **noise**:

1. **"K full `T.copy()` in `per_segment_values`"** — 3 ms total. The
   "10×80KB copy" framing in the architect lens was right about shape
   but wrong about cost; NumPy memcpy of a 10000×20 float64 array is
   ~150 µs.

2. **"`target_paced.cumsum` recomputed K+1 times"** — 9 ms total for 23
   cumsums. Cumsum reuse across `evaluate` + `total_time_distribution` +
   `per_segment_values` would save single-digit milliseconds.

3. **"Three engine methods recompute the same baseline masks"** —
   `evaluate` + `total_time_distribution` baseline cost is well under
   10 ms. F1 ("one-shot `evaluate_full` API") would absorb less than 1%
   of the route's wall clock.

The scan's F4 (typed engine surface + structured logging) is still worth
doing for legibility, but the perf framing for it was wrong.

## What's actually slow

**99.8% of the route's time is inside `per_segment_values` doing the
K=20 swap-column redraws at `k_param=1`.** Two hoisting wins on the
per-draw primitive collapse most of that:

### W1 — Hoist `trend_signal_slopes` out of the per-draw loop

`em_suite_sampler.py:316 trend_signal_slopes(state, fast_idx, slow_idx)`
is called **once per `sample_episode` invocation when `k != 0`** (line
450). It depends only on `(state, fast_idx, slow_idx)`, which are
**constant across all N=10000 draws** of a single (seg_id, k_param=1)
column.

- Per swap column today: 10000 calls × ~3.6 µs each = 36 ms of pure
  trend_signal_slopes work
- × 20 columns = 720 ms cumulative
- This pulls 600_000 `_logit` calls + 600_000 `p_die_ema` /
  `log_success_time_ema` / `log_death_time_ema` calls along with it

**Fix:** compute slopes (and the derived `p, slide_success, slide_death`
triple at line 449-460) ONCE per `_draw_column_impl` call when k≠0, then
pass them into `sample_episode` (or refactor `sample_episode` into a
"setup + per-draw" pair). Estimated saving: ~22% of the route's wall
clock, no semantic change.

### W2 — Precompute `draw_from_pool` weights per (pool, alpha)

`em_suite_sampler.py:107` rebuilds the recency-weighted list
`[alpha * (1-alpha)**(n-1-j) for j in range(n)]` on **every** call.
That's 254_590 invocations producing the same ~300-element list per
column. List allocation + comprehension dominates `random.choices`
itself (670 ms leaf vs 345 ms inside `choices`).

**Fix:** precompute weights once per column (per pool, per alpha) — they
are static for the duration of `_draw_column_impl`. Cache on the
SamplerState's pool/alpha pair or on a per-column scratchpad. Estimated
saving: ~250–400 ms / 2560 ms = **~10–15%**.

W1+W2 together should land the route under ~1500 ms with zero algorithm
change — pure call-graph cleanup.

### W3 — Vectorize the reject-until-survive loop (the F2 big swing)

After W1+W2, the residual ~1.5 s is the inner geometric-trial loop
in `sample_episode` (the `for _ in range(MAX_ATTEMPTS_PER_EPISODE)`).
NumPy can batch the Bernoulli draws over the N=10000 rollouts of a
column: vectorized `Generator.geometric(p) + cumsum + index into the
death/success pools` replaces the per-draw Python loop entirely.

This is the F2 swing — big, requires preserving the per-column
reproducibility scheme, requires equivalent gate/None handling. **Only
worth it after W1+W2 land** — those plausibly cover the
"is the dashboard snappy?" threshold on their own.

## What about `random.Random` → `numpy.random.Generator`?

The profiler shows `_random.Random.random` at 41 ms total — not a
bottleneck. The cost is in `draw_from_pool`'s list-allocation and
`trend_signal_slopes`'s EMA arithmetic, not in `rng.random()`. Switching
RNG families wouldn't help by itself; vectorization helps because it
batches Python overhead, not because NumPy's RNG is faster per call.

## What about `scheduler.sampler_states()` rebuilding from DB?

The verifier's B4 listed this as a suspect. **Not visible in this
profile** because the harness skips the scheduler entirely — the
`PracticeEngine` is constructed directly from in-memory states. In a
real /evaluate hit on production, `engine` is already built (lazy
property cached) and `ensure_fresh()` runs only over the dirty set, so
`sampler_states()` doesn't run on the request path. It runs on engine
construction (lazy first access) and that's measured here at 930 ms for
20 columns — **off** the request path. Not the lag culprit.

## Recommendation

Land W1 + W2 as a single small-medium plan. Re-run this profile after.
If the residual is still uncomfortable, escalate to W3 (F2). Drop F1 as
not-worth-the-design-cost given the actual perf contribution.

The scan's other picks (cleanup batch: M9a delete `thresholds_from_gold_default`,
M9c delete `column_summary`, N4 justify `MAX_ATTEMPTS_PER_EPISODE=100`,
M8a fix tautology test, M8d switch to `pytest.raises`, N9 strengthen
invalidation test) stand on their own and are unaffected by this profile.

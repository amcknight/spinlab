# Model Purge & Sampler-Centric Core — Design (Spec #1)

**Date:** 2026-06-01
**Status:** Brainstorming captured, awaiting review
**Scope:** Backend only. Consolidate the multi-estimator system down to the single EMA-Suite sampler, make it an *actual sampler* (real episode-time draws, not a closed-form mean), and rip out the multi-model scaffolding. **No UI/aesthetic work** (that is Spec #2) and **no outer value-of-practice allocator** (that is Spec #3).

This is the first of a three-spec arc agreed during brainstorming:

- **Spec #1 (this doc):** model purge + sampler-centric core.
- **Spec #2:** UI overhaul — single-screen layout, unified aesthetic, humanized graphs, decide the fate of the allocator bar / cold histogram / Segments tab.
- **Spec #3 (future):** the §1–§5 Monte Carlo value-of-practice allocator from [`docs/practice-allocator-spec.md`](../../practice-allocator-spec.md), which consumes the sampler's `sample(0)`/`sample(1)` draws.

## Motivation

SpinLab accumulated six practice models (`kalman`, `death_aware_rolling`, `rolling_mean`, `bootstrap_resample`, `exp_decay`, `em_suite_sampler`). The scheduler runs **all** of them on every episode and persists a `ModelOutput` per estimator; only the "active" one feeds the allocator. None proved itself good enough to keep, and the multi-model machinery (registry, factory, estimator-switch, per-estimator tuning, a UI selector) is dead weight — the "old failed experiments" Andrew wants cleared.

The decision is to commit to **one** model, the **EMA-Suite Sampler**, and delete the rest. This is deliberately the risky, irreversible move, so it goes first and on its own, behind a git safety net, before any UI work depends on it.

Crucially, the EMA-Suite sampler **does not currently sample**. The shipped v0 (per [`2026-05-30-em-suite-sampler-design.md`](2026-05-30-em-suite-sampler-design.md)) computes only a closed-form *mean* episode time and a 10×10 matrix of those means. The `sample()`/`sample_episode()` interface that design described was never built. Consolidating onto "the sampler" while it cannot sample would ship the same half-built state in new clothes — so Spec #1 also **makes it a real sampler** (the "B-real" decision below).

## Principles

Carried forward from the model-output and EMA-suite specs:

1. **No silent fallbacks / no fudge values.** Insufficient data returns `None` / "—", never a synthetic number.
2. **No magic numbers.** Every constant (α grid, ring-buffer sizes, default α-pair, reload penalty) is a named, documented module-level value.
3. **The object is a sample.** The sampler's product is a *draw* of an episode time. Means and matrices are *derived diagnostics*, not the product.
4. **Recency weighting is the only learning mechanism.** No fitted skill trajectory; the α decay rates are the only smoothing knobs. (Preserve the normalized EMA — see below.)
5. **Delete the indirection, keep the seam.** Remove scaffolding that only existed to support a model zoo; keep the one boundary (per-segment sampler state, replayable from the event log) that future work plugs into.

## The object: what the sampler produces

The outer model (and the allocator, eventually) only ever sees **per-segment episode times** — `sample(0)` is one random episode time at current skill, `sample(1)` is one after a notional further attempt of practice. This matches [`docs/practice-allocator-spec.md`](../../practice-allocator-spec.md) §0–§1 ("Everything else — deaths, recency-weighting — stays inside. The outer model only ever sees times").

The layering, made explicit:

```
PRODUCT (what leaves the wall):
  sample_episode(k) -> episode_time_ms          # a DRAW (e.g. 13.654 dead + 19.667 win = 33.321s)

INTERNAL (stays inside the sampler):
  loop: draw (outcome, time) until first win, sliding each draw by k·slope
  pools: recency-weighted success-time and death-time ring buffers
  machinery: process_event -> EMAs -> slopes  (give p, and the per-step slide)

DERIVED DIAGNOSTICS (for the eye, not the allocator):
  expected_episode_time_ms()  -> closed-form mean of the geometric process
  build_matrix()              -> 45 of those means, one per (alpha_fast, alpha_slow) pair
```

The EMAs and slopes are **machinery**: their entire job is to produce the *slide* (`exp(slope)` on times, logit-shift on p) that turns a current-skill draw `sample(0)` into a one-attempt-ahead draw `sample(1)`. We compute these "non-samples" only to be able to slide a sample forward.

## B-real: make the sampler sample

`sample_episode(k)` is built as the **bootstrap-with-slide** generator (the Phase-2 generator from [`2026-05-31-em-suite-practice-visuals-design.md`](2026-05-31-em-suite-practice-visuals-design.md)):

```
episode_time = 0
while True:
    if Bernoulli(p_k):                       # died
        d ~ weighted_empirical(death_pool, alpha_fast)
        episode_time += d * slide_death_k + reload_penalty_ms
    else:                                     # survived -> episode ends
        s ~ weighted_empirical(success_pool, alpha_fast)
        episode_time += s * slide_success_k
        return episode_time
```

Where for a chosen `(alpha_fast, alpha_slow)` pair and step `k`:

- `p_k        = logistic(logit(p_E_fast) + k · slope_logit_p)`
- `slide_death_k   = exp(k · slope_log_death)`
- `slide_success_k = exp(k · slope_log_success)`
- `k = 0` → no slide (pure current-skill draw); `k = 1` → one-attempt-ahead draw.

The slopes (`E_fast − E_slow` per quantity) already exist in `trend_signal_slopes`. The only new state is the two draw pools.

**Convergence guard:** cap the inner loop (e.g. 100 attempts) and return an "insufficient / non-converged" sentinel rather than a fudge value, consistent with the no-silent-fallback principle. Cap is a named constant.

### Draw pools — two ring buffers, not one

A **ring buffer** is a fixed-size buffer that overwrites its oldest entry when full ("keep the last N"). We keep **two**, per segment:

- `success_time_pool` — recent successful-attempt times.
- `death_time_pool` — recent fatal-attempt times.

`p_die` is **not** a pool — it stays the EMA rate (the chosen α's `p_die_ema`).

**Why p_die has no pool (the asymmetry is principled):** p_die is Bernoulli, and a Bernoulli is fully described by its mean — the mean is a *sufficient statistic*, there is no "shape" beyond p. So drawing a recent 0/1 outcome from a pool would be *identical* to `Bernoulli(p_EMA)`; a pool buys nothing. The time quantities are the opposite — continuous, right-skewed, sometimes **bimodal** (the 4s-trick vs 20s-late case) — where the mean discards exactly the structure we care about, so we must draw from the empirical pool to preserve shape. Hence: pool the things whose *shape* matters (times); keep a rate for the thing whose *mean is everything* (p_die). One deferred subtlety: v0 uses p as a point estimate in the `Bernoulli(p_k)` draw, ignoring uncertainty in p itself (few attempts → p is fuzzy); a Beta posterior on p is a clean future refinement, not v0.

**Why two pools, not one combined buffer:** a single combined buffer starves the rarer outcome. A brutal segment (e.g. 80 attempts to clear, mostly deaths) would crowd successes down to a handful in a 200-slot shared buffer, making success draws noisy. Two independent buffers decouple each pool's adequacy from the death rate — the segment keeps a healthy window of recent successes *and* deaths regardless of `p_die`.

**Sizing:** each pool is a named constant, sized to cover the slowest meaningful decay's window (≈ a few hundred). Start at `POOL_SIZE = 300` per pool; tunable as a constant, not a structural change. The slowest *recency-weighted* rate (α=0.01) has ~100-attempt memory, so a few hundred comfortably covers several effective windows. Note the ring buffer bounds only the **draw pools** (sampling shapes); the EMA/uniform-mean accumulators see *all* replayed events, so the α=0 uniform mean is genuinely all-time, not buffer-bounded.

**In-pool draw weighting:** the `weighted_empirical(pool, alpha)` draw is recency-weighted within the pool, using the **same normalized weighting** as the EMAs (weights divided by their observed mass), so small pools weight honestly. The ring buffer bounds staleness; the α-weighting within it shapes the draw.

### Normalized EMA — keep it, and fix α=0's *semantics* (not the grid)

The normalized `(Sum, Denom)` EMA built last session is correct and stays untouched for α>0. It is what makes small-N weighting honest (it divides by the actually-observed weight mass instead of letting the first observation dominate — see the `em_suite_sampler.py` module docstring for the 96%-at-N=5 motivation).

**α=0 means zero decay, not updatelessness.** The grid keeps all 10 entries; what changes is α=0's *implementation*. The spirit of α=0 is **uniform weighting** — every observation counted equally, forever — i.e. the unbiased all-time mean `E = (Σxᵢ)/n`. The normalized form already *converges* to exactly this as α→0 (the weights `α(1−α)^k` flatten toward uniform and `S/D → (Σxᵢ)/n`); the only reason the current code reads `—` at α=0 is the degenerate `0/0` exactly at the limit, an implementation edge — not the concept.

So α=0 is **not** a dead cell and must not be dropped. It is the **zero-decay / no-recency-bias anchor**: "what does my whole history say with every attempt weighted equally." Implementation: the α=0 slot uses an unbiased `(running Σ, running count)` accumulator (`E = Σ/count`); all α>0 slots use the normalized EMA. One small special-case in the update.

```
ALPHA_GRID = (0.0, 0.01, 0.02, 0.03, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0)    # 10 rates
#             ^uniform / zero decay (all-time equal-weight mean)        ^goldfish (last only)
```

α=0 will sit numerically *close* to α=0.01 in practice (both are near-uniform over ~100+ attempts); its value is conceptual — the equal-weight baseline the decaying rows are read against. The matrix stays 10×10 upper-triangular; downstream code reads `len(ALPHA_GRID)`, so no hard-coded counts. (This corrects an earlier draft of this spec that wrongly proposed dropping α=0 by reading it as "never updates.")

### The scalar the allocator/table needs

The allocator (greedy) and the segment table need **one** number per segment (the renamed *Episode Time*). It is the **mean of N `sample_episode(0)` draws** (N a named constant), or equivalently the closed-form `expected_episode_time_ms` as a cheap shortcut — both are valid; the closed form is exact under point semantics and avoids draw variance for a display/ranking number. Implementation picks one; the spec's requirement is just "a single, honest expected episode time, or `None` when the prediction gate fails."

This requires a **default `(alpha_fast, alpha_slow)` pair** — the one decay choice used for the headline scalar until the operator (Spec #2) or the matrix says otherwise. This is a real modeling default, named and documented, **not** a magic pair. Proposed: `DEFAULT_ALPHA_PAIR = (fast=0.2, slow=0.05)` (≈ 5-attempt current skill vs ~20-attempt baseline — a moderate, responsive-but-not-twitchy trend). Flagged for Andrew's review; easy to change as a constant.

## What gets deleted

**Safety net (first commit):** branch off `main` and cut an annotated tag `pre-model-purge`. Every deleted estimator stays fully recoverable in history. No file copies.

**Delete — estimators:**
- `python/spinlab/estimators/kalman.py`
- `python/spinlab/estimators/death_aware_rolling.py`
- `python/spinlab/estimators/rolling_mean.py`
- `python/spinlab/estimators/bootstrap_resample.py`
- `python/spinlab/estimators/exp_decay.py`

**Delete — multi-model machinery:**
- The registry/factory indirection: `@register_estimator`, `get_estimator`, `list_estimators`, `_register_all` in `estimators/__init__.py`.
- `Scheduler.switch_estimator()`, the `allocator_config["estimator"]` DB key, `_sync_config_from_db`'s estimator branch, and the "run ALL estimators" loop in `update_state_after_episode` / `rebuild_all_states`.
- Per-estimator tunable params: `declared_params`/`ParamDef` plumbing, `estimator_params:*` config keys, `_load_estimator_params`, and the tuning API routes + UI selector (frontend selector removal is mechanical; the UI's broader redesign is Spec #2).
- `ModelOutput` / `Estimate` as the model's **output port** (the point-estimate container EMA-Suite already routes around by returning all-`None`). The per-segment scalar is delivered through the sampler instead.

**Delete — the `Estimator` ABC ceremony** that only one model no longer needs: `init_state`, `get_priors`, the bare-state-from-death-first branching in `_process_attempt_for_estimator`, the deserialize-by-name `EstimatorState` registry.

## What stays / changes

- **`em_suite_sampler.py`** becomes the core: `SamplerState` + module functions (`process_event`, `trend_signal_slopes`, `expected_episode_time_ms`, `build_matrix`, `replay_with_history`, `build_slope_matrices`) **plus** new `sample_episode(state, k, ...)` and the two draw pools on `SamplerState`.
- **Per-segment state persistence stays.** `SamplerState` still serializes to the `model_state` table so reads don't replay the whole event log every time. The pools are reconstructable from the event log on rebuild (same as the EMAs), so they need not necessarily be persisted — implementation may persist them or rebuild; either is acceptable as long as a read is fast.
- **Scheduler shrinks** to: on episode close, replay the segment's events → `SamplerState` → persist. `pick_next` asks the sampler for the per-segment scalar. No estimator loop, no `ModelOutput` round-trip.
- **Allocators: logic frozen, input swapped.** Greedy/random/round-robin/least-played/mix keep their selection *logic* unchanged. Only their **input** changes: from `SegmentWithModel.model_outputs[name].total.ms_per_attempt` to the sampler's scalar field on the segment. This is a field swap, not a redesign — "freeze the allocators" holds. Allocators survive because prediction of best-improvement is imperfect and worsening-trend segments still need greedy/softmax exploration rather than "never play it again" (Andrew's point); the principled §4 replacement is Spec #3.

## Replay seeding bug fix

Today, Replay and Fast Replay capture death/checkpoint events from movie playback and write them as segment time data, which then feeds the EMAs. Fast Replay especially pollutes the distributions with wall-clock-collapsed frame deltas. **Fix: Replay and Fast Replay stop seeding model data entirely.** Replay-sourced events are excluded from the sampler's ingestion (filter on `AttemptSource.REPLAY` at the point events are loaded for the sampler, or stop writing model-bound rows for replay sessions — implementation picks the cleaner seam).

The "re-seed a deleted run from a replay" idea Andrew floated is **explicitly deferred** ("for now it should just not seed data").

## Cold-distribution decoupling

`compute_cold_distribution()` (`cold_distribution.py`) builds the histogram and hazard **directly from events**, independent of any estimator. Its only tie to a doomed model is that `routes/model.py` currently borrows `death_aware_rolling`'s `halflife` to recency-weight the histogram.

**Fix:** decouple. For v0, the cold histogram is **equal-weighted** (drop the recency-halflife knob entirely) — a halflife is the same kind of knob as an α, and inventing a standalone one would be a redundant second decay parameter. The histogram becomes the raw empirical "here is all my cold data" diagnostic, which is arguably the more honest view for a diagnostic. Whether the cold histogram/hazard survives into the new UI at all is a **Spec #2** decision; Spec #1 only ensures the deletion does not silently kill the data path.

## Migration / data

**Zero schema migrations.** This spec deletes *code*, not table shapes. The `model_state` and `allocator_config` table schemas are untouched; `output_json` simply goes unused (dropping a SQLite column is a table-rebuild for no benefit — leave it). So no `migrations/NNNN_*.sql` file is added.

**Two data classes, treated oppositely:**
- **Event data (`attempts` table) is source of truth and is never touched.** Beto's collection and all recorded events survive intact. Wiping the database wholesale would be wrong — it is the only non-regenerable data.
- **Derived data (`model_state` rows, stale `allocator_config` keys, optionally `segment_fits`) is fully recomputable from the event log** and is wiped + rebuilt. This is a one-shot runtime cleanup, not a migration:
  - `DELETE FROM model_state` (clears the 5 deleted models' orphan rows *and* em_suite's old-format rows — its `SamplerState` gains the two pools, so old serialized state is stale anyway).
  - Delete `allocator_config` keys `estimator` and `estimator_params:*`.
  - Rebuild em_suite `model_state` from events (the existing replay path), lazily on next read or eagerly via a `rebuild_all_states`-style pass.

This makes "start over" safe: only the derived cache resets; the precious event data is inviolate.

## Testing

- Full `pytest` (unit + emulator + frontend) green as the **baseline before** any deletion and **again before** declaring done — per project policy. A red baseline is stop-and-ask.
- Tests referencing the deleted estimators (Kalman priors, death-aware rolling, bootstrap, etc.) are removed or rewritten against the sampler. Removal of a test that only documented a deleted model's behavior is expected, not a regression.
- New tests for `sample_episode`: convergence under high `p_die`, the two-pool separation (death-heavy segment still draws successes), slide direction (improving trend lowers sampled times), the non-converged sentinel, and `None` below the prediction gate.
- Replay-seeding fix gets a regression test: a replay/fast-replay session contributes **zero** new sampler data.
- Cold-distribution: a test that the histogram is estimator-independent and equal-weighted.

## Out of scope (Spec #1)

| Out | Where it goes |
|---|---|
| All UI layout, tabs, aesthetic, graph humanization, allocator-bar/cold-histogram/Segments-tab fate | Spec #2 |
| The §4 outer Monte Carlo value-of-practice allocator | Spec #3 |
| Per-segment vs player-wide α selection (manual default for now) | Spec #3 / future |
| Hot vs cold partitioning of the draw pools (cold-only for v0) | Future spec |
| Re-seeding a deleted run from a replay | Future ("for now, just don't seed") |
| Sampler performance / rebuild-from-scratch cost | Deferred until correctness locked |

## Open questions for review

- **`DEFAULT_ALPHA_PAIR = (0.2, 0.05)`** — does this default decay choice match Andrew's intuition for the headline *Episode Time* number, or should the scalar use the no-slope `sample(0)` baseline at a single α instead of a fast/slow pair?
- **Scalar source** — mean-of-draws vs closed-form `expected_episode_time_ms` for the table/allocator number. Closed-form is exact and variance-free for a *display* number; draws are the truer object. Lean closed-form for the scalar, draws for everything user-facing later. Confirm.
- **`POOL_SIZE = 300`** per pool — accept as the starting constant, knowing it is a one-line bump?
- **α=0 = zero-decay uniform mean** (resolved in review) — restored to the grid with unbiased-accumulator semantics, *not* dropped. Confirm the implementation special-case (α=0 slot uses running Σ/count) reads right.

## Cross-references

- Outer model intent: [`docs/practice-allocator-spec.md`](../../practice-allocator-spec.md) (§1–§5, Spec #3).
- Sampler v0 (means only): [`2026-05-30-em-suite-sampler-design.md`](2026-05-30-em-suite-sampler-design.md).
- Bootstrap-with-slide generator + practice visuals: [`2026-05-31-em-suite-practice-visuals-design.md`](2026-05-31-em-suite-practice-visuals-design.md).
- Model principles (no magic numbers, no silent fallbacks, nullable outputs): `CLAUDE.md` § Modeling & Numerics.

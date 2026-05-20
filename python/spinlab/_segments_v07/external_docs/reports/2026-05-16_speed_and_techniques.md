# V07 fitting — speed and techniques

Short report on the JAX rewrite: measured numbers, the techniques that
mattered, and pitfalls worth knowing about. Captured 2026-05-16 from the
session that produced [learning_model_v07_jax.py](../learning_model_v07_jax.py),
[fit_jax.py](../fit_jax.py), and the test suite.

## Headline numbers (N=500, single segment, CPU)

| Operation | Time |
|---|---|
| One `log_posterior` evaluation, numpy | 42 ms |
| One `log_posterior` evaluation, JAX (JIT) | **0.12 ms** |
| Cold MAP fit, Nelder-Mead + numpy | **~70 s** |
| Cold MAP fit, L-BFGS-B + JAX (from `warm_init_from_data`) | ~100 ms after JIT warmup |
| Streaming refit, warm-started, bucketed | **p50 8 ms, p99 35 ms** |
| Laplace posterior (Hessian + invert + 2000 samples) | ~600 ms |

**Speedup vs the numpy reference: ~700× on cold MAP, effectively
~5000× on streaming.** Per-call log_posterior is 342× faster on its own.

### "How long to process 500 data points?"

Three useful interpretations:

- **One-shot fit on the full 500 attempts** (typical "post-session" use):
  ~100 ms after JIT, ~300 ms from a cold process.
- **Streaming all 500 attempts live, one at a time, each triggering a refit**
  (the live-viz workload): ~7 s end-to-end, with each individual refit at
  median 8 ms (99% under 35 ms).
- **Cold process startup including JIT pre-warm for 8 bucket sizes**: ~3 s
  one-time cost.

## Techniques that bought the speedup

1. **JAX autodiff replacing Nelder-Mead.** L-BFGS-B converges in ~100
   iterations at d=10, vs ~1300 for Nelder-Mead. With JAX gradients each
   iteration is also ~10× cheaper than NM's likelihood-only eval.

2. **JIT compilation with shape bucketing.** JAX `jax.jit` compiles a fresh
   kernel per unique input shape. In a streaming loop where N grows by 1
   each step, every iteration would trigger a recompile and dominate
   per-call latency. Fix: pad inputs to the next multiple of 64 and pass
   `valid_n` as a mask; the JIT cache then has ~8 entries total for
   N=64..512 instead of 450 entries for N=51..500.

3. **scipy's L-BFGS-B for the outer loop, JAX for value-and-grad.** Pure
   `jaxopt.LBFGS` overshoots the safe region on the first step from a
   prior-mean init. scipy's battle-tested line search backtracks gracefully.
   The hybrid is cheap glue.

4. **Soft penalty for -inf `log_posterior` regions.** `jnp.where(isfinite, -lp,
   1e10)` lets the optimizer see a finite cliff at degenerate boundaries
   (e.g. `bpt > min(survived)`). Without this, NaN gradients blow up the
   solver. Important: the penalty branch has zero gradient, so the cliff
   is one-sided — the line search must approach from inside the feasible
   region (which scipy's does).

5. **Warm-start heuristic from data.** `warm_init_from_data` reads empirical
   statistics (bpt ≈ min(survived) − 500ms, sf from late-attempt slop CV,
   alpha from death rate) and produces a theta within ~15% of MAP on synth.
   Halflives stay at prior medians (the windowed empirical stats don't
   contain curvature info). This is what lets the cold fit converge in
   ~10 iterations instead of 100+.

6. **JIT pre-warming.** A 3-second cost at process start that eliminates
   200 ms spikes mid-session. Critical for live-viz UX.

7. **`jax.config.update("jax_enable_x64", True)`.** JAX defaults to float32.
   Without float64, `log_posterior` drifts ~1e-3 vs numpy and L-BFGS-B
   can't tell convergence from noise. Always enable for stat models.

## Pitfalls worth knowing

- **`jnp.where(cond, A, B)` with B = `-inf` produces -inf forward but zero
  gradient.** Fine for the outer safe-wrap; not what you want inside a
  loss surface where you need gradients to point you out of bad regions.

- **The naive `jaxopt.LBFGS` initial step is `−grad`, magnitude ≈ ‖grad‖.**
  At d=10 with gradient norm ~2600, that's a step of 2600 in log-space —
  catastrophically large. Use scipy's L-BFGS-B (smarter initial step) or
  pass an explicit `linesearch_init`.

- **JIT cache size grows unboundedly with unique input shapes.** Always
  pad to buckets in streaming workloads. Even a few hundred unique shapes
  costs gigabytes of compiled binaries in the cache.

- **`int(np.exp(np.log(x)))` rounds down when `x` is exactly an integer.**
  exp(log(28)) is 27.9999…; `int()` floors to 27. Use `round()` for
  attempt counts.

- **Laplace covariance from `jax.hessian` works only when the Hessian is
  positive-definite.** In the regime-1 ridge case (no learning, halflife
  unidentified — see [pgm_tricks.md](pgm_tricks.md)) it goes non-PD even
  with the joint prior in place. NUTS fallback is on the roadmap.

## What's next on the speed/scaling path

The remaining bottleneck for production scale (thousands of segments)
isn't speed per se — it's **the fitting topology**: each segment is
independent today. The natural next move is to share information across
segments via a hyperprior.

### Smallest principled hyperprior: pool halflife across segments

If you can only add ONE hyperprior, **`halflife_sf`** is the right pick
(arguments by priority):

1. **Best-motivated.** It's the latent that suffers most from the
   identifiability ridge: when a segment shows little learning, single-segment
   data carries no info about how fast learning would happen. The joint prior
   collapses halflife to a default in that case; pooling across segments
   replaces that default with a learned population value.
2. **Most generalizable.** "How fast does this player learn?" is a player
   property as much as a segment property — likely to be similar across
   segments of similar genre/difficulty.
3. **Lowest risk of false sharing.** Pooling bpt across segments would be
   wrong (different segments have different theoretical best times); pooling
   asymptotes is partially right (same player, similar style). Halflife
   pools most cleanly.

Once you've done one, adding the other two halflives (`halflife_ssp`,
`halflife_alpha`) is incremental — same machinery, no new design.

### Effort estimate for one hyperprior via empirical Bayes

This is the lighter of two paths (full hierarchical NUTS is the heavier).

| Step | Lift |
|---|---|
| Per-segment fit with overridable prior on halflife_sf | Refactor `log_prior` to accept an overridden μ/σ for halflife_sf. ~30 lines. |
| Multi-segment driver: fit S segments → estimate (μ̂_pop, σ̂_pop) → refit | New module. ~100 lines plus orchestration. |
| Test harness with multi-segment synth (already easy — generate with varying truth) | New synth function + ~50 lines of validation. |
| Validation: does pooled halflife beat unpooled on regime-1 segments? | New test, ~50 lines. |

**Total: ~1-2 sessions of work** to land the smallest meaningful HYPER step.
This is "empirical Bayes" — point-estimate the hypers, then refit segments
conditionally. Standard production pattern; scales linearly with segment count.

The full Bayesian alternative (NUTS over the joint hyperprior + per-segment
posteriors) is ~3-4 sessions and crosses the line into needing NUTS infrastructure.
The empirical Bayes path can be upgraded to full Bayesian later without changing
the per-segment fit code.

### Update (same day): EB pool on halflife_sf landed

[fit_eb_pool.py](../fit_eb_pool.py) implements the loop above. See
[CHANGELOG.md](../CHANGELOG.md) for details. Measured numbers on a 5-segment
demo (4 informative truths at halflife=50, 1 regime-1 no-learning segment):

| Workload | Time (post-JIT-warmup) | Per-segment |
|---|---|---|
| Independent fits (no pooling) | 413 ms | 83 ms |
| EB pooled fits (5 iter loop, warm-started) | 1049 ms | 210 ms |
| Warm-started per-fit cost inside EB | — | 26 ms |

The EB loop is **~2.5× the cost of independent fits** at this scale. At
100+ segments the multiplier stays in the 3-5× range because the EB
iteration count is roughly constant in S (5-8 iters), not growing.

**Headline behavior recovery** — what the pool actually does:

| Metric | Independent | Pooled | Truth (pop) |
|---|---|---|---|
| Regime-1 segment halflife_sf | 29 attempts | **45 attempts** | 50 attempts (pop mean) |
| Distance from pop target | 15.8 | **0.0** | — |

The regime-1 segment is data-silent on halflife; the pool replaces the
broad default prior (median 28) with the population estimate (~45 from
the other 4 segments), shrinking the segment's MAP to where it should be.

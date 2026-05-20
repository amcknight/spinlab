# Learning curves — current plan (2026-05-16)

Companion to `learning_curves.md`. Sessions through 2026-05-15 prototyped
the 10-latent (Δ, τ) model from the spec and exposed two structural
problems. This doc is the regroup: streamlined named plans + the
pitfalls that need to stay avoided.

## TL;DR

The (Δ, τ) curve formalism is mathematically correct but produces a
model that's hard to reason about and bias-prone at single-segment scale.
Next move is to **reparameterize** to use observable quantities and
**tighten priors** using cross-segment domain knowledge. Hyperpriors and
real-data MCMC stay deferred but the new priors are pre-structured to
become hyperpriors later.

## Plans

### Plan V07 — reparameterize + tighten + knob console

The implementation work for the next session.

1. **Reparameterize.** Drop Δ. Use (θ_1, θ_∞, τ) per learning param:

   `log θ(n) = log θ_∞ + (log θ_1 − log θ_∞) · exp(−(n−1) / τ)`

   where (n is 1-indexed, matching how data is recorded):
   - **θ_1** = parameter on the *first attempt* (the n=1 you actually
     observe). The inspector slider for θ_1 means "what does my first
     try look like."
   - **θ_∞** = the asymptote.
   - **τ** = attempts to close ~63% of the remaining gap *after the
     first attempt*.

   We use the subscript `1` (not `0`) deliberately, so the symbol
   matches the smallest n you can observe. Original spec form
   `exp(-n/τ)` is mathematically equivalent up to a trivial reindex
   of τ, but its θ_0 is a fictitious pre-attempt-1 extrapolation that
   confuses interpretation.

   Implementation: replace `exp(-n/τ)` with `exp(-(n-1)/τ)` in
   `learning_model.py` and the synth generator; replace `Δ` storage
   in the theta vector with `log θ_1`.

2. **Tighten priors** using "between-segment σ" intuition (factor ~1.6×,
   not ~4×):

   | Latent | New prior | Was |
   |---|---|---|
   | log bpt | Normal(log 25s, 0.5) | σ=1.5 |
   | log sf_0 | Normal(log 0.2, 0.5) | Δ_sf ~ N(0, 2) |
   | log sf_∞ | Normal(log 0.05, 0.5) | σ=1.5 |
   | log ssp_0 | Normal(log 0.4, 0.5) | — |
   | log ssp_∞ | Normal(log 0.2, 0.5) | σ=1.0 |
   | log α_0 | Normal(log 1.0, 0.5) | Δ_α ~ N(0, 2) |
   | log α_∞ | Normal(log 0.3, 0.5) | σ=1.5 |
   | log τ_sf, log τ_ssp | Normal(log 40, 0.5) | σ=1.5 |
   | log τ_α | Normal(log 20, 0.5) | σ=1.5 |

3. **Knob console.** Each latent has a mode: `learned` or `constant`. The
   user can pin any latent at a value and refit. Lets you isolate
   problems by hand: pin everything to known good values, then unpin one
   knob at a time and watch what falls over. No latent-count reduction
   — pure debugging affordance.

4. **No ties yet.** Don't merge τ_sf with τ_ssp; don't tie ssp to sf.
   Domain intuition isn't ready to commit. Reconsider after real-data
   fits.

5. **Inspector updates.**
   - Sliders in natural units (sf_0 = 0.2, not log)
   - Pin-checkbox per slider (wires into the knob console)
   - Drop the Δ display permanently

**Recovery experiment to validate v0.7** (fit on existing
`test_synth_learning.tsv`):

- Prior 95% on M_clear(n=0) < 1 hour (currently 10²⁵ s)
- Asymptote MAP (θ_∞ values and bpt) within 30% of truth at N=500
- Cold-start MAP under 30 s at N=500 (currently ~100 s)
- No multi-seed stuck-at-init failures

**What V07 explicitly does NOT fix.** The (θ_1, τ) ridge is the same
ridge as (Δ, τ) — just renamed. MAP bias and overconfident posterior
bands at single-segment scale persist. Fixing those needs Plan HYPER or
more data.

### Plan DATA — recording schema + parallel grinding

Independent of V07. Run in parallel.

```
data/<game>/<segment>/<run-id>.tsv
columns: attempt_n  outcome  time_ms  notes(optional)

data/<game>/metadata.json
fields:
    game_short_name
    segment_short_name
    segment_length_s_est
    bpt_known_s          # null if no TAS reference exists
    difficulty_rating    # optional ordinal
    session_notes
```

Record session boundaries via a comment column or metadata so we can
group warm-ups later without re-grinding.

### Plan HYPER — cross-segment hyperpriors (v2)

Promotes V07's priors to learned hyperpriors per
`segment_model_hyperpriors.md`. Required when ≥3 real segments exist.
Includes non-centered parameterization at every level (Neal's funnel
fix).

### Plan NUTS — PyMC + NUTS sampler

Replaces MAP + Laplace/MH with real HMC sampling. Best done after HYPER
so the non-centered reparameterization helps both. Confirms whether
single-segment MAP bias is the ridge or a sampling artifact.

### Plan JAX — gradient-based MAP via JAX

Replaces Nelder-Mead. Sub-second streaming refit; robust at high d. Best
done after V07 so the reparameterization is baked in first.

**Target scale.** This becomes a prerequisite once HYPER lands, because
per-segment-plus-shared-hyperprior fits cross d≈30 quickly and NM falls
over. Long-term scale: ~24 games × ~60 segments × 10 latents per segment
= 14k+ params (plus hyperpriors), where gradient-based methods are
mandatory.

**GPU compilation (follow-on).** Once `log_posterior` is rewritten in
`jax.numpy`, JIT-compiling to GPU is essentially free (`jax.jit` + GPU
backend). The big win is **batched per-segment fits** — fit hundreds of
segments in parallel as one vectorized GPU kernel via `jax.vmap`. This
is the natural scaling story for the production sub-PGM decomposition
pattern (see `pgm_tricks.md`). Worth keeping in mind when designing the
single-segment JAX rewrite — make `log_posterior(theta, data, hazard)`
trivially vmappable from the start.

## Constraints (won't change)

- **bpt is a learned latent.** Required for WR discovery on games with
  no TAS reference — that's the killer app.
- **All three learning curves stay** (sf, ssp, α). Asymptote prediction
  and improvement trajectory is why the model exists.
- **Debug-ability beats expressiveness.** A model the user can't
  troubleshoot from first principles is worse than a simpler one they
  can.

## Active pitfalls (do not re-walk into these)

- **Laplace approximation is broken on this curve.** Hessian at MAP is
  non-PD due to the (θ_1, τ) ridge. Use MH or NUTS for posterior
  summaries.
- **The ridge survives reparameterization.** (θ_1, τ) trade off the
  same way (Δ, τ) did. Renaming helps interpretability, not
  identifiability. Real fix is pooling (Plan HYPER) or much more data.
- **Single-segment posterior bands are not honest CIs.** They're sharp
  but biased — the bias from MAP carries through. Don't promise
  calibrated uncertainty until HYPER or NUTS lands.
- **Cold-start Nelder-Mead at d≥10 is fragile.** ~30% of cold starts
  on synth got stuck at the initial point. Always warm-start from a
  coarser fit, or move to gradient MAP (Plan JAX).
- **Showing the prior as a single line is misleading.** It's a
  distribution. Always show prior-predictive bands or samples. The
  inspector does this; keep it that way.
- **MAP `|Δ| ≤ 0.5` is not a Phase 1 pass criterion.** Posterior median
  is what the spec meant. Finite-N Binomial noise alone is enough to
  push MAP outside ±0.5 on truly-static synth.
- **θ_1 means "first observed attempt," not "Big Bang."** v0.7 uses
  the `exp(−(n−1)/τ)` shift so θ_1 = θ(1) exactly. Do not slip back
  to the unshifted `exp(−n/τ)` form (which would make θ_0 a
  fictitious pre-attempt-1 latent). Symbol is **θ_1**, never θ_0,
  precisely to prevent that drift.

## Decision point

Say **"go with Plan V07"** to start the reparameterization + prior
tightening + knob console build.
Say **"also Plan DATA"** to lock the recording schema in parallel.
Or pick another plan by name.

## Existing artifacts (kept for reuse)

`learning_model.py`, `phase1_check.py`, `generate_synthetic_learning.py`,
`phase2_recovery.py`, `prior_inspect.py`, `pgm_inspect.py`,
`pooling_experiment.py`, `pgm_inspect.html`, `pooling_experiment.png`,
`test_synth_learning.tsv` (+ `.truth.json`).

Plan V07 will produce `learning_model_v07.py` alongside the existing
`learning_model.py`, not replacing it — so we keep the option to
compare.

# Learning Curves Extension

Companion to [segment_model.md](segment_model.md). The base model fits
each segment with time-invariant params. This extension lets the
skill-dependent params evolve over attempts within a segment.

## What learns, what doesn't

| Param | Learns? | Why |
|---|---|---|
| `bpt` | **no** | Route floor, not skill-driven. Discrete jumps on route discovery are a separate model (change-points), out of scope. |
| `slop_frac` | **yes** | Mean execution overshoot above `bpt`. Improves with reps. |
| `slop_spread` | **yes** | CV of execution noise. Improves with reps. *Caveat: this may not actually decay in real data — see "open questions" below.* |
| `α` | **yes** | Death rate per attempt (cumulative hazard at s=1). Drops fast with familiarity. |
| Beta-shape (`μ_k`, `κ_k`, `w_k`) | **no, v1 simplification** | Assumed static for v1. **This is a simplification, not a structural claim** — practice can reshape death patterns (peak disappearance, migration, narrowing). Real data will tell us if this matters; if so, the cheapest extension is a `w_k(n)` curve. |

## Functional form (V07 parameterization)

For each learning param θ over attempt number n (1-indexed):

```
log θ(n) = log θ_∞ + (log θ_1 − log θ_∞) · 2^(−(n−1)/halflife)
```

Three latents per learning param:

| Latent | Meaning |
|---|---|
| `θ_1` | Value on the **first observed attempt** (n=1). The "what does my first try look like" anchor. |
| `θ_∞` | Asymptote — the floor θ approaches with infinite practice. |
| `halflife` | Attempts to close 50% of the remaining gap *after the first attempt*. |

At `n=1`: curve evaluates to `log θ_1` exactly.
At `n=1+halflife`: half the log-gap to `θ_∞` is closed.
As `n→∞`: curve → `log θ_∞`.

**Three independent triples** — one per learning param (sf, ssp, α).
No sharing across params; they measure different aspects of mastery.

### Total latents

- **haz1** (default): 10 latents = `bpt` + 3 × (θ_∞, θ_1, halflife).
- **beta2** (opt-in): 15 latents = same 10 + 5 static beta-shape
  (`μ_1`, `μ_2`, `κ_1`, `κ_2`, `η_1` for softmax weight).

### Why `θ_1` instead of `Δ`

Earlier parameterization used `Δ = log θ_1 − log θ_∞` (the
"amplitude"). Mathematically equivalent, but `θ_1` is directly
observable (the value on your first attempt) while `Δ` is abstract.
The renaming was for interpretability — see resume notes in
[reports/2026-05-16_learning_curves_findings.md](reports/2026-05-16_learning_curves_findings.md).

The `(θ_1, halflife)` pair is still ridge-correlated when learning is
weak — the rename helps interpretation, not identifiability. The
joint prior below (and hyperprior pooling, see
[segment_model_hyperpriors.md](segment_model_hyperpriors.md)) is what
fixes identifiability.

## Per-attempt likelihood

The likelihood machinery in `model.py` is **unchanged in form** — it
receives an attempt-indexed θ value per attempt instead of fixed ones.
At attempt n:

- Survived at T: `L_survived(T; n) = clean_dist(T; θ(n)) · surv_prob(1; θ(n))`
- Died at τ: same quadrature integrand, with `slop_frac(n)`,
  `slop_spread(n)`, `α(n)` substituted

No per-attempt cost increase from learning curves *per se* — the
died-attempt quadrature was already per-attempt in the static model.
Extra cost is from the larger latent dimension at MAP+Hessian.

## Priors

Tightened from the original spec (σ=0.5 ~ between-segment factor 1.6×).
Pre-structured to promote to hyperpriors per
[segment_model_hyperpriors.md](segment_model_hyperpriors.md).

| Latent | Prior | Notes |
|---|---|---|
| `log bpt` | `Normal(log 25s, 0.5)` | (static) |
| `log sf_∞` | `Normal(log 0.05, 0.5)` | asymptote |
| `log ssp_∞` | `Normal(log 0.2, 0.5)` | asymptote |
| `log α_∞` | `Normal(log 0.3, 0.5)` | asymptote |
| `log sf_1` | `Normal(log 0.2, 0.5)` | first-attempt anchor |
| `log ssp_1` | `Normal(log 0.4, 0.5)` | first-attempt anchor |
| `log α_1` | `Normal(log 1.0, 0.5)` | first-attempt anchor |
| `log halflife_*` | `Normal(log 28, σ(A))` | sf, ssp; α uses median 14 |

`σ(A)` is a joint prior on (halflife, amplitude) — see next section.

## Joint prior on (halflife, A)

When the learning amplitude `A = log(θ_1) − log(θ_∞)` is near zero,
the data carries no information about how fast a curve *would* decay
— so `halflife` is non-identifiable in that regime. Independent
priors leave MAP wandering along the ridge.

The fix: make `halflife`'s prior σ tighten as `|A|` shrinks.

```
σ_halflife(A) = σ_min + (σ_max − σ_min) · tanh(|A|)
```

At `A=0`: σ collapses to σ_min, prior dominates posterior, MAP
returns the prior median confidently.
At `|A|` large: σ expands to σ_max, data dominates.

This is a *meta-modeling choice*, not a structural claim. We're not
saying A and halflife are causally linked — we're encoding "report a
sensible default when the question is meaningless." See
[pgm_tricks.md](pgm_tricks.md) "Joint priors" entry.

## Identifiability summary

Per-quantity at low N:

| Quantity | ID at low N | Why |
|---|---|---|
| `M_clear` at current n | Tight | Driven by recent data |
| Local improvement rate | Tight | Curvature of recent data |
| `θ_∞` (asymptote) | Loose | (θ_1, halflife, θ_∞) trade-off; tightens as curve flattens |

No permanent ID problem: as N exceeds a few `halflife`s, the curve
flattens and `θ_∞` becomes directly observed. Early-N wide bars on
`θ_∞` are honest uncertainty, not a flaw.

## Inference notes

- **MAP + Laplace** via JAX (scipy L-BFGS-B outer loop, JAX
  value-and-grad inner ops). p50 8 ms streaming refit at N=500.
- **NUTS fallback** planned for ridge cases where Laplace's Hessian
  goes non-PD. See [reports/2026-05-17_handoff_status.md](reports/2026-05-17_handoff_status.md).

## Open questions

- **Does `slop_spread` actually learn?** The model assumes it
  decreases over attempts. If execution noise is more a fixed personal
  trait than a learnable thing, `halflife_ssp` will be unidentified
  on real data. Diagnostic: posterior on `A_ssp = log(ssp_1) −
  log(ssp_∞)` near zero across segments → ssp doesn't learn →
  demote to constant (drop 2 latents) or random walk (different
  model). Won't know until real data.
- **Static beta-shape over attempts.** As called out above, this is a
  v1 simplification. The empirical answer to whether it's adequate
  comes from posterior predictive checks on real data.

## Plan-forward

PPC infrastructure, then NUTS, then halflife hyperprior pool expansion.
See [reports/2026-05-17_handoff_status.md](reports/2026-05-17_handoff_status.md)
for the agreed sequence.

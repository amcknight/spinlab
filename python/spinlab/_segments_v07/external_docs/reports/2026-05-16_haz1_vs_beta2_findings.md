# haz1 vs beta2 — JAX comparison findings

Session: 2026-05-16. Closed the spec-vs-impl gap from `beta_mixture_decisions.md`:
beta2 is now implemented in JAX alongside haz1, both fit through the
same L-BFGS-B + Laplace pipeline. This doc reports the 2x2 comparison
(haz1-truth vs beta2-truth synth × haz1-fit vs beta2-fit) at N=500.

## TL;DR

| Question | Answer |
|---|---|
| Does beta2 work in JAX? | Yes. JIT-compatible after replacing `betainc` with a fine-grid cum-trapz lookup (`betainc` lacks gradients w.r.t. shape params in JAX). |
| Speed penalty? | Cold MAP 1–5× slower; Laplace 3–7× slower; streaming 4× slower. Still all sub-100ms p99 streaming. |
| Identification penalty? | 20% Laplace PD failure on haz1-truth at N=500 (vs 0% for haz1). Shape params collapse to broad/uniform when there's no shape to find. |
| NLL signal at right truth? | Beta2 wins beta2-truth by **~25 nats** (5/5 seeds). Strong, reliable. |
| Occam tax? | Haz1 wins haz1-truth by **~7–14 nats** (5/5 seeds). Modest. |
| M_clear robustness? | Within 1s across all 4 cells. Confirms beta_mixture_decisions claim. |
| Real-data behavior (N=22)? | Both Laplace **non-PD** — ridge bites hard at low N regardless of model. |

## Implementation gotcha worth remembering

`jax.scipy.special.betainc` has no gradient w.r.t. `a` and `b` (the shape
parameters). For any model where you need to backprop through a
regularized incomplete beta, you cannot just call it. The workaround
used here:

```python
# Precompute g(s) on a fine grid of s in [0, 1] using jbeta.pdf
# (which IS differentiable). Build G(s) by cumulative trapezoid.
# Evaluate at arbitrary s via jnp.interp.
```

Cost: 257 grid points + cumsum, amortized over all attempts. Cheap.

A custom-VJP for `betainc` w.r.t. `a, b` is possible (the derivatives
exist, just not in JAX). Not needed at this fidelity.

## Speed details (single-seed, N=500)

| Cell                       | Cold MAP | Laplace | Streaming p50 | PD |
|----------------------------|----------|---------|---------------|-----|
| beta2-truth / haz1-fit     |   153 ms |  608 ms |   38 ms        | Y |
| beta2-truth / beta2-fit    |   418 ms | 2126 ms |   23 ms        | Y |
| haz1-truth / haz1-fit      |   119 ms |    3 ms |    6 ms        | Y |
| haz1-truth / beta2-fit     |   121 ms |   19 ms |   26 ms        | Y |

Beta2's cold MAP varies wildly (60–700 ms across seeds) because the
shape params are sometimes well-constrained and sometimes ridge-bound,
which determines how long L-BFGS spends in low-gradient regions.

## Identification (5-seed averages)

### Beta2-truth, N=500

| Cell | NLL | Δ NLL (vs best) | Notes |
|---|---|---|---|
| beta2 fit | 4365 ± 35 | 0 | Wins all 5 seeds |
| haz1 fit  | 4391 ± 37 | +26 ± 3 | Misspec cost ~25 nats |

Beta2 shape recovery on its own truth (seed 0): μ=(0.358, 0.704),
κ=(16.8, 13.7), w=(0.565, 0.435). Truth: μ=(0.35, 0.70), κ=(15, 10),
w=(0.6, 0.4). Within ~5% on means, ~30% on concentrations and weights.

### Haz1-truth, N=500

| Cell | NLL | Δ NLL (vs best) | Notes |
|---|---|---|---|
| haz1 fit  | 4364 ± 27 | 0 | Wins all 5 seeds |
| beta2 fit | 4373 ± 27 | +9 ± 3 | Occam tax ~7–14 nats |

**Beta2 fails Laplace PD on 1/5 seeds** (vs 0/5 for haz1). When there's
no shape structure to find, beta2's μ, κ, w wander along a ridge and
the Hessian goes non-PD. Practical implication: beta2 needs a NUTS
fallback (or stronger shape priors) for production use on unknown-truth
data.

### Shape collapse on no-shape data

When fit on haz1-truth (no real shape structure), beta2's MAP shape
typically lands at κ ≈ 4 — well above the boundary-spike regime
(κ < 2) but markedly broader than its prior median (15). This is the
"shape collapses to uniform-ish" behavior — beta2 is honestly admitting
"I don't see any peaks."

## Real-data spot check (N=22, `test_data.tsv`)

Both models fit to convergence but **both Laplace go non-PD**. Expected:
at N=22 the (Δ, τ) ridge dominates regardless of hazard family — single
segments at this scale don't have enough deaths to anchor halflife
posterior covariance.

Beta2 extracts a shape (μ=(0.418, 0.795), κ=(12.4, 15.3),
w=(0.633, 0.367)) but these numbers are not credible at N=22 —
prior-dominated for the shape latents.

## What this 2x2 does NOT measure

1. **Within-segment drift.** Both synths have stationary shape parameters
   across all attempts. Real grinding has strat changes, skill
   normalization, peak shifts. Haz1 handles this gracefully because
   drift just rides the α-curve; beta2's static μ/κ would misattribute
   drift to noise on α or to overall fit error. See
   `memory/haz1_vs_beta2.md` and `memory/moving_peaks.md`.
2. **K=3+ truth.** `beta_mixture_decisions.md` measured K=3-truth
   degradation for K=2 on static data — that's still relevant. Not re-run
   here.
3. **Posterior calibration.** MAP + Laplace is a point estimate. For
   honest posterior bands under the ridge case, NUTS is the next step.
4. **GPU / vmap scaling.** Single-segment fits only.

## Recommendation

Reading the numbers: **beta2 is worth running when you have data, but
haz1 should remain the default for unknown-truth single-segment fits.**
The ~25-nat win on beta2-truth means beta2 is genuinely capturing real
structure when present. The 20% PD failure rate on haz1-truth and the
~5× slower Laplace mean it's not safe to make beta2 the unconditional
default.

Three practical paths:

1. **Both available, haz1 default.** User (or downstream service) picks
   beta2 when they want the shape detail. Cheapest path; matches
   current state after this session.
2. **Auto-promote.** Fit haz1 first (fast), then beta2; if beta2's NLL
   beats haz1's by some threshold (e.g. 5 nats) the shape is real, use
   beta2; else fall back. ~2× cost.
3. **Always beta2 + NUTS fallback.** Honest posteriors regardless. Most
   expensive but cleanest. Requires NUTS infra (CHANGELOG item #2).

Path 1 keeps the JAX path simple and is consistent with the current
production-handoff scope. Paths 2 and 3 are upgrades when the production
project asks for shape data.

## Files added/changed this session

- `learning_model_v07_beta2_jax.py` — 15-param beta2 model (NEW)
- `fit_jax_beta2.py` — beta2 fit pipeline mirroring `fit_jax.py` (NEW)
- `tmp/compare_haz1_vs_beta2.py` — 2x2 comparison runner (NEW)
- `generate_synthetic_learning_v07.py` — added `hazard_truth='haz1'`
  mode (truncated-exponential s_death) alongside existing 'beta2'
- `external_docs/haz1_vs_beta2_findings.md` — this doc (NEW)

## Caveats for future readers

- **α parameterization choice.** beta2 here uses the "smooth cumulative
  hazard" form where `S(s) = exp(-α · G(s))`, matching haz1's α meaning
  exactly. Different from the spec's "scaled CDF" form
  (`F(s) = α · G(s)`, α = P(die)). Decision documented at top of
  `learning_model_v07_beta2_jax.py`. The spec form may still be
  preferable for downstream consumers — open question.
- **Label switching.** K=2 has 2! posterior modes. Warm-init breaks the
  symmetry; MAP picks one side consistently. Posterior summaries from
  Laplace samples conflate the modes — interpret with care.
- **No PPC or evidence comparison.** This is MAP + Laplace only. The
  numbers are consistent with what the data is asking for but don't
  prove the model is well-specified in any global sense.

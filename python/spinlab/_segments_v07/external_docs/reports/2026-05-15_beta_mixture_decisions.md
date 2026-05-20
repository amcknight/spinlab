# Beta Mixture Hazard — Exploration & Decisions

Summary of what was explored, what was decided, and where everything lives, as of the locked-in state at the end of the 2026-05-15 session.

## Decision summary

**`BetaMixtureHazard(K=2)`** is the standard segment-level death model. No `haz_floor`. No K-sweep with model averaging in normal use.

`beta_compare.py` is retained as a diagnostic tool for end-of-session model exploration, not the standard fit path.

## The model (locked in)

A K=2 beta mixture for death density `f(s)` on `s ∈ [0, 1]` (fractional position within attempt):

```
f(s)    = α · (w_1 · Beta(s | a_1, b_1) + w_2 · Beta(s | a_2, b_2))
F(s)    = α · (w_1 · I_s(a_1, b_1) + w_2 · I_s(a_2, b_2))
S(s)    = 1 - F(s)
h(s)    = f(s) / S(s)
cum(s)  = -log S(s)
```

where each component uses the `(mean, concentration)` parameterization:
- `a_k = μ_k · κ_k`,  `b_k = (1 - μ_k) · κ_k`
- `μ_k ∈ (0, 1)` — where component k clusters deaths
- `κ_k > 0` — how peaked it is

Total **9 parameters** per segment: 3 scalars (`bpt`, `slop_frac`, `slop_spread` — shared base model) + 6 hazard params (`α, μ_1, μ_2, κ_1, κ_2, η_1`).

### Priors (in unconstrained log/logit space)

| Param | Prior | 68% CI on natural scale |
|---|---|---|
| α (P die per attempt) | logit-Normal(0, 1.0) | (0.27, 0.73) |
| μ_k (component mean) | logit-Normal(0, 1.5) | (0.18, 0.82) |
| κ_k (concentration) | log-Normal(median=15, σ=1.0) | (5.5, 41) |
| η_1 (softmax logit for w_1) | Normal(0, 1.5) | weight ratios e^±1.5 |

### Key prior choices and why

- **κ prior median=15, σ=1.0**: tight enough to put P(κ<2)=2.2% (boundary-spike regime where Beta diverges at s=0 or s=1 is essentially excluded), broad enough to allow narrow peaks (κ→100) and uniform-ish (κ≈3) shapes.
- **No `log(K!)` correction on Laplace evidence**: tried it, broke on sparse data. See [log(K!) correction](#logk-correction-misadventure) below.

## What was explored

### Side-by-side beta vs piecewise

Built `BetaMixtureHazard` as a `HazardModel` subclass alongside the existing `PiecewiseConstantHazard`. Both plug into the unchanged MAP+Laplace inference pipeline.

On the K=3-truth synthetic data (`test_synth.tsv`, 150 attempts):
- piecewise K=3: log_ev = -1567.37
- beta K=3:      log_ev = -1539.49

**Beta beats piecewise by 28 nats** — overwhelming evidence that the smooth-density family fits better than uniform-bin step functions.

### K sweep with Laplace evidence

`beta_compare.py` fits K=1..5 betas, computes Laplace log-evidence per K, blends predictions via softmax weights. On the K=3-truth synth:
- K=3 won with **91.7% weight**, K_eff = 3.04
- The blend across K is a defensible final estimate when the right K is uncertain.

### log(K!) correction misadventure

Initial implementation added `+log(K!)` to Laplace evidence for K-component mixtures, on the theory that K! equivalent permutation-modes exist.

**Empirical breakage**: with only 1 data point and K=1..5, K=5 won (w=0.728) because `log(5!) = 4.79` overwhelmed the raw evidence differences. K=1 should have won decisively on 1 data point.

**Root cause**: components collapse to identical params with sparse data (e.g., K=3 fit gave μ=[0.59, 0.59, 0.59], κ=[14, 14, 14] — all three on top of each other). The K! permutation modes coincide rather than being distinct.

**Fix**: dropped the correction. `log_evidence_correction()` returns 0. Worst case: under-credits genuinely well-separated mixtures by at most log(K!) nats (≤5 for K≤5). Small relative to typical evidence differences at N≥100.

### κ prior tightening

Initial prior `log-Normal(median=10, σ=1.5)` allowed **6% of prior samples to have κ < 1**, the boundary-spike regime (Beta density diverges at s=0 or s=1).

Verified via 0-data prior predictive (`pic/beta_zero_data.png`): hazard samples were spiking to >250.

**Fix**: `log-Normal(median=15, σ=1.0)`. P(κ<1) → 0.3%, P(κ<2) → 2.2%. Spike regime effectively excluded.

### haz_floor experiment (REVERTED)

Tested adding a constant background hazard: `h_total(s) = haz_floor + h_beta(s)`. Idea: 1 extra parameter to absorb broad/uniform mass, reducing K needed for sharp peaks.

**Empirical result** on K=3-truth synth:
- K=3+floor LOSES to K=3 by 1.5 nats
- K=2+floor LOSES to K=2 by 0.7 nats

The Occam penalty (extra parameter) exceeded the likelihood gain. **User's intuition was correct**: K=3 with one κ≈2 component already subsumes K=2+floor's capacity.

**Reverted**. K=3 with one collapsed-to-uniform component handles "background + 2 peaks" natively.

### Why K=2, not K=1 or K=3

- **K=1** produces NaN `M_clear` on multimodal data (boundary divergence when forced to fit multi-peak truth with one component). Structurally too restrictive.
- **K=2** degrades gracefully on multi-modal data: combines extra peaks into wider bumps, headline numbers (M_clear, α) stay within 0.5s of K=3 truth. Sweet spot.
- **K=3** best on K=3-truth data but ~3× slower fits with diminishing returns for the practice-ROM use case.

### M_clear is robust to model choice

Computed `M_clear` (expected wall-clock to first clear, including all deaths + 3.5s respawns) for K=1..5 betas + piecewise K=3 on the K=3-truth synth:

```
beta K=2 → 56.61s
beta K=3 → 56.59s ← truth
beta K=4 → 56.44s
beta K=5 → 55.82s
piecewise K=3 → 57.46s
```

**All within 1.5s**. The headline summary doesn't depend on model choice — what differs across models is *where* deaths cluster, not how many or how costly they are in aggregate.

## Per-fit timing (cold-start MAP + Laplace Hessian on N=150)

| Model | d | MAP | Hessian | Total |
|---|---|---|---|---|
| piecewise K=3 | 6 | 7s | 1s | 8s |
| beta K=1 | 3 | 11s | 2s | 13s |
| **beta K=2** | **6** | **27s** | **6s** | **33s** |
| beta K=3 | 9 | 86s | 11s | 97s |
| beta K=4 | 12 | 134s | 20s | 154s |
| beta K=5 | 15 | 164s | 33s | 197s |

Warm-started per-attempt streaming should be ~1-2s for K=2 cold, sub-second once warm. Both manageable.

For prod, expected next-level optimizations:
- Closed-form `E[s|died] = Σ w_k μ_k` (eliminate quadrature in `mean_s_given_died`, fix the NaN bug for free)
- L-BFGS-B with finite-difference gradients (5-10× faster MAP)
- JAX/Numba JIT (another 5-20× if pursued)
- Cached MAPs between sessions
- Possibly the streaming-EM from `online_density_spec.md` for microsecond per-event updates

## Files

### Core (production path)
- `hazard.py` — `HazardModel` interface, `PiecewiseConstantHazard`, `BetaMixtureHazard`.
- `model.py` — likelihood, M_clear, etc. Model-agnostic.
- `inference.py` — MAP via Nelder-Mead, Laplace covariance.
- `config.py` — config constants for piecewise priors / inference.
- `run.py` — streaming demo. Default `--model beta --K 2`.

### Diagnostics / one-shot tools
- `beta_compare.py` — end-of-data K-sweep with Laplace evidence. Use rarely.
- `generate_synthetic.py` — produce synthetic test data from known ground truth.
- `preview_synthetic.py` — visualize a candidate ground truth before generating.
- `bin_search.py` — piecewise-specific bin-edge picker comparison.
- `evidence_scan.py` — piecewise evidence vs K.
- `binning.py` — piecewise bin pickers.

### Data
- `test_data.tsv` — real data (22 attempts).
- `test_synth.tsv` — current synthetic (K=3 truth: μ=[0.15, 0.55, 0.80], κ=[45, 540, 72], w=[0.4, 0.2, 0.4], α=0.65), 150 attempts.

### Plots (`pic/`)
- `beta_compare_synth.png` — headline: K=1..5 + piecewise on K=3-truth synth (K=3 wins, K=2 graceful).
- `preview_synth.png` — current synthetic ground truth decomposition.
- `beta_one_point_K3.png` — K=3 fit on 1 data point (illustrates degenerate "collapsed components" mode).
- `beta_one_point_sweep.png` — K=1..5 on 1 data point (K=1 wins as expected after K! correction fix).
- `beta_zero_data.png` — prior predictive (0 data) showing prior is well-behaved.
- `kappa_prior_sanity.png` — Beta shape gallery for various μ, κ; demonstrates spike-regime exclusion.
- `diagnostics_beta_k2.png` — run.py output for beta K=2 on real data.
- `diagnostics.png` — older piecewise diagnostic.
- `bin_search.png`, `evidence_scan.png` — piecewise-side diagnostics.

### External docs (`external_docs/`)
- `segment_model.md` — base speedrun segment model.
- `segment_model_hyperpriors.md` — future hierarchical extension.
- `online_density_spec.md` — streaming-EM spec we did not implement (yet).
- `beta_mixture_decisions.md` — this document.

## Open items / deferred work

1. **Closed-form `mean_s_given_died`**: replace numerical Simpson quadrature with `Σ w_k μ_k`. Fixes the K=1 NaN bug at any K, removes a chunk of unnecessary work.
2. **Streaming-EM (`online_density.py`)**: implement the spec for microsecond per-event live density updates. Useful for live in-attempt UI, complementary to MAP+Laplace.
3. **Compute optimization**: L-BFGS-B with gradients, then JAX/Numba JIT. ~10-50× speedup pre-hierarchy.
4. **Hyperprior layer**: cross-segment pooling per `segment_model_hyperpriors.md`. K=2 fixed structure makes this straightforward (components have consistent identity across segments).
5. **Practice-ROM dashboard**: segment ranking by α/M_clear/improvement-trend. Single concrete user-facing artifact.

## What "good enough" means here

We're committing to:
- A model that gives **headline numbers** (M_clear, α, bpt) accurate within ~1s on multi-modal data even when modes get combined.
- A model that captures the **early-vs-late distinction** when it exists in the data, which is the most-common real-world pattern.
- A model that is **fast enough** for end-of-session fits today and that has clear paths to live-update speeds (streaming-EM, JIT) when needed.

Things we accept losing:
- The third-and-beyond mode location on segments with K=3+ structure (acceptable — practice the two biggest, the third is rounding error).
- The Bayesian model averaging in normal use (kept as diagnostic).
- The (incorrect) +log(K!) correction (was always wrong on sparse data, no actual loss).

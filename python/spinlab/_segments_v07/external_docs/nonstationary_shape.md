# Non-stationary shape (proposal)

**Status:** V2-SCOPE — PARKED. Design proposal, 2026-05-17, scope-reframed
post-handoff-discussion. v1 ships static shape under haz1 only; this is
where the moving-peaks extension lives for v2 iteration inside SpinLab.
See `/V1_ESSENCE.md` for the scope split. Companion to
[segment_model.md](segment_model.md) and [learning_curves.md](learning_curves.md);
addresses the open concern logged as `moving_peaks` in session memory.

Captures the design for letting the beta-mixture hazard *shape* drift across
attempts within a segment, without inflating parameter count or breaking
identification with V07's existing alpha-learning curve.

## Problem

The current beta2 model ([learning_model_v07_beta2_jax.py](../learning_model_v07_beta2_jax.py))
treats the 5 shape params (`logit_mu_1`, `logit_mu_2`, `log_kappa_1`,
`log_kappa_2`, `eta_1`) as *static across attempts*. Only the V07 base
params (`alpha`, `sf`, `ssp`) evolve over n via the learning curve.

Empirically, the *where* of deaths moves over a run:

| Phenomenon | Frequency | Signature in shape-space |
|---|---|---|
| (1) thinky → muscle memory | most common | slow eta drift (decision-point peak shrinks, execution peak holds); mu's mostly stable |
| (2) section strat change | medium | one peak's eta drops, another's rises; localized weight swap |
| (3) complete strat change | rare | big simultaneous jump in mu's and eta's |

A static shape forces all three to be absorbed (badly) by alpha-learning
or by data noise. We want shape to drift.

## Why alpha stays clean

In the smooth-cumulative-hazard formulation (the one beta2 already uses):

```
f(s|n) = alpha(n) · g(s) · exp(-alpha(n) · G(s))
F(1|n) = 1 - exp(-alpha(n))         since G(1) = ∫g = 1
```

`g(s)` is a proper PDF — beta components integrate to 1, mixture weights
sum to 1. Therefore the *total death probability* per attempt depends only
on `alpha(n)`. Shape redistribution cannot soak up rate changes. Alpha
owns "how often"; g owns "where within".

This is the property the scaled-CDF form deliberately gave up and the
reason beta2 chose smooth-cum-hazard ([learning_model_v07_beta2_jax.py:27-35](../learning_model_v07_beta2_jax.py)).
We preserve it here.

Consequence: phenomenon (1)'s *rate* drop must live in V07's alpha
learning curve. Only the *where* of (1) can leak into shape drift.

## Proposal

### Latent state per attempt

K_max = 3 overfitted mixture. The state per attempt is

```
x_t = (logit_mu_1, logit_mu_2, logit_mu_3, eta_1, eta_2)  ∈ ℝ^5
```

(3 mus + 2 free etas, with eta_3 := 0 as the softmax reference.)

A spare third peak lets phenomenon (2) swap into a fresh location rather
than relabel an existing peak. A sparse prior on softmax weights keeps
unused components at w ≈ 0.

**Static dims (NOT in x_t):**
- `log_kappa_k` per component: frozen with its existing prior. The three
  phenomena don't really need width changes — a peak getting "sharper"
  is well-approximated by eta moving + neighboring peaks dimming.
  Re-evaluate if PPC shows residual kurtosis the static model can't reach.

### Dynamics

Random walk with heavy-tailed step noise:

```
x_{t+1} - x_t  ~  StudentT(ν = 4, scale = σ_drift_d)   for each dim d
```

- Gaussian RW is the simpler default; Student-t with low ν is "cheap
  insurance" for phenomenon (3) — rare large jumps without inflating
  typical step size.
- Diagonal noise. No cross-coupling between dims.
- One `σ_drift_d` per dim, but tied across dim *types*: σ_drift_mu shared
  by the 3 mu dims, σ_drift_eta shared by the 2 eta dims. → **2 noise
  hyperparams**.

### Manual break flag (optional, escape hatch)

For phenomenon (3) when the user knows they switched strats: mark
attempt t = t*. At t*, replace `σ_drift_d` with `σ_jump >> σ_drift_d`
for one step. Model otherwise unchanged.

- Single extra hyperparam: `σ_jump`.
- Heavy-tailed RW already handles (3) without the flag; the flag just
  makes known breaks cheaper to fit.

### Initial condition

`x_0` gets the same priors the static beta2 currently uses:

| Dim | Prior |
|---|---|
| logit_mu_k | N(0, 1.5), reparameterized to enforce mu_1 < mu_2 < mu_3 (see Identification below) |
| eta_k | N(0, 1.5) |

The order constraint replaces the warm-init label-symmetry hack the
static beta2 uses ([learning_model_v07_beta2_jax.py:37-42](../learning_model_v07_beta2_jax.py)) — with drift, warm-init isn't
sufficient because labels can swap mid-trajectory.

## How each phenomenon is captured

| Phenomenon | Mechanism |
|---|---|
| (1) thinky → muscle, rate | V07 alpha learning curve (unchanged) |
| (1) thinky → muscle, where | slow drift in eta, possibly slow drift in mu |
| (2) section strat | drift in eta — one peak dims, another rises (the "spare" K=3 component is what rises if it's a new location) |
| (3) complete strat | Student-t tail step OR manual `σ_jump` break |

## Param budget

| New hyperparams | Count |
|---|---|
| σ_drift_mu | 1 |
| σ_drift_eta | 1 |
| σ_jump (optional) | 1 |
| **Total new** | **2–3** |

Latent state is 5×T per segment (5 dims × T attempts), but those are
*inferred*, not free — the prior pays for them via dynamics, not via
hyperpriors. Same trick as Kalman/state-space models in general.

Hyperpriors on the σ's: HalfNormal(scale ~ 0.1) for `σ_drift_*`,
HalfNormal(scale ~ 1.0) for `σ_jump`. Weakly informative; data is
expected to identify them.

## Inference concerns

The blocker. Adding 5×T latent dims per segment breaks the current
Laplace approximation as a viable approach: high-dim Gaussian
approximation around a MAP of a state-space model is notoriously bad
(curvature dominated by the dynamics prior, not the data).

Options, roughly in order of effort:

1. **EKF / UKF.** Dynamics are linear-Gaussian (RW); the emission
   (death-time likelihood given shape) is non-linear in the state.
   Standard non-linear state-space territory. EKF gives O(T·d³) per
   likelihood eval — fast. Outer optimization over static params +
   hyperparams via gradient descent. Loses the Student-t tail (would
   need a Gaussian-sum filter or particle filter); acceptable if heavy
   tails are only used at flagged break points (where the filter
   handles them via inflated Q for one step).
2. **Structured VI.** Mean-field over (latent trajectory) × (static
   params) × (hyperparams), or a structured posterior using a Gaussian
   chain over the trajectory. Scales, gives credible intervals. More
   implementation work than EKF.
3. **NUTS over the joint.** Works as a reference, too slow for
   production — static-only beta2 NUTS already runs 23–453s per
   segment; adding 5×T latent state per segment would be much worse.

Default recommendation: **EKF for fitting, NUTS as occasional
validation**, mirroring the current Laplace/NUTS split. Heavy-tailed RW
implemented via flagged jumps only (so the EKF assumptions hold).

## Identification within shape

Two residual concerns the static beta2 also has but the drift makes
worse:

1. **Label-switching.** K=2 has 2! modes; K=3 has 3! modes. With drift,
   modes are per-t — labels can swap mid-trajectory. Mitigation: enforce
   `mu_1 < mu_2 < mu_3` as an order constraint on the state at every t.
   Cheap to implement (reparameterize as cumulative sum of positive
   deltas in logit space).
2. **Overcomplete K=3.** When fewer than 3 peaks are needed, the model
   has redundancy. Mitigation: sparse prior on softmax weights — either
   Dirichlet(α<1) on the simplex (operates on weights directly) or a
   shrinkage prior on the etas (Laplace / horseshoe). The two induce
   different priors on the weight simplex — pick by what's tractable in
   the chosen inference scheme.

## Open questions

- Does V07's alpha-learning already eat phenomenon (1) entirely? Worth
  measuring before committing to shape drift: fit haz1 with vs without
  alpha-learning, compare residual variability in PPC mid_third stat.
  If alpha eats most of (1), drift is justified only by (2)/(3) — much
  smaller benefit.
- Is K_max = 3 enough? K_max = 4 grows the latent state from 5 to 7
  dims (4 mus + 3 free etas). Not a dealbreaker but worth checking
  against the rare "three-peak-section" case.
- Per-segment vs pooled `σ_drift`? Most likely pool across segments
  (drift behavior is a player/run property, not segment-specific). Fits
  the existing empirical-Bayes hyperprior pattern in
  [segment_model_hyperpriors.md](segment_model_hyperpriors.md).
- How are flagged breaks supplied? Manual annotation on attempt t, or
  via a "strat tag" field in SpinLab's schema? Coordinate with the
  SpinLab handoff before assuming an interface.

## Out of scope for this doc

- Multi-segment pooling of drift hyperparams (covered by the hyperprior
  doc once shape drift is committed).
- Birth-death of components (BNP / IBP / dDP machinery). The
  overfitted-mixture trick is the cheap substitute; revisit only if
  K_max-fixed proves restrictive.
- Real-time inference (streaming Kalman update per new attempt). Out of
  V1 scope; would be a natural follow-on once batch EKF works.

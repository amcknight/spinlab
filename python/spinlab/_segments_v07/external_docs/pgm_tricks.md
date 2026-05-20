# PGM tricks & gotchas

Short glossary of patterns that come up when building probabilistic graphical
models. Captured from the segment-model work; meant to live as a checklist
when designing or debugging future PGMs. Each entry: name, when it bites,
what to do.

## Identifiability spectrum

Three flavors, in increasing severity:

- **Identifiable.** Data uniquely pins the parameter down in the
  infinite-data limit. Standard case.
- **Weakly identifiable.** Technically identifiable, but finite-noisy data
  leaves a wide elongated posterior. MAP is reproducible only loosely
  across seeds. Manifests as **ridges** in posterior geometry (see below).
- **Non-identifiable.** Data carries zero information about the parameter
  in some region of the other params' space. Likelihood is flat along the
  parameter's axis. Posterior = prior in that region.

Common cause: parameters that *only matter when another parameter is in
a particular range*. e.g. in our learning curves, `halflife` is
non-identifiable when `A = log(θ_1) - log(θ_∞) = 0` (no learning to
characterize).

## Ridges

A 1-D (or higher) direction in parameter space along which the likelihood
is nearly flat. Typically appears when two parameters trade off: doubling
one and halving the other gives the same likelihood.

**Symptoms:**
- MAP wanders across seeds at fixed truth.
- Asymptotic CIs are wrong (Hessian non-positive-definite at MAP).
- Posterior is a long thin sausage rather than a ball.

**Detection:** compute log_posterior on a 2-D slice through MAP for the
suspected pair. Plot as a heatmap; look for an elongated high-density
region. `tmp/ridge_demo.py` is an example.

**What fixes it (in order of complexity):**
1. Reparameterize to put the ridge in a more benign direction (sometimes
   possible, often not — algebraic equivalence preserves ridges).
2. Tighten the prior on one of the two params.
3. **Joint prior** (next entry).
4. More data of the right kind.
5. **Partial pooling** across many groups (hierarchical model).

## Joint priors

A.k.a. **dependent priors**: instead of factorizing the prior as
`P(A, B) = P(A) · P(B)`, model the joint directly so B's prior depends
on A.

**When to use:** when B is meaningless or non-identifiable for some
values of A, and you'd rather report a confident default than a wandering
estimate.

**Example (this project, `learning_model_v07.py`):** when `A=0` (no
learning), `halflife` is non-identifiable from data. We make
`σ_halflife(A) = σ_min + (σ_max - σ_min) · tanh(|A|)`. At `A=0`, σ shrinks
toward σ_min — prior dominates posterior; MAP returns prior median
confidently. At `|A|` large, σ expands toward σ_max — data dominates.

**The tanh smoothing matters.** A discontinuous switch (σ = σ_min if
A < ε, σ_max otherwise) breaks gradient-based optimizers and creates
posterior cliffs. tanh is smooth, monotonic, and asymptotically saturates.

**This is a meta-modeling choice, not structural.** You're not claiming
A and B are causally linked. You're encoding "report a default when the
question is meaningless." Worth a code comment to that effect.

**Compare to spike-and-slab:** `P(A) = π · δ(0) + (1-π) · Normal(...)`
is the cleanest treatment of "A might be exactly zero," but the point
mass breaks NM/L-BFGS and needs careful MCMC. Joint prior is the smooth
alternative.

## Smooth-once isn't smooth-twice — non-differentiable Hessian bombs

**Symptom.** Laplace bands are wrong (or fail PD) on cases where the MAP
fit looks fine. L-BFGS converges, the gradient is small, but the
Hessian at MAP has a large *negative* eigenvalue. Finite-difference 2nd
derivatives along the bad eigenvector are positive and large. Autodiff
disagrees with finite differences.

**Diagnosis.** The negative log-posterior contains a once-differentiable
but not-twice-differentiable function near the MAP. Common culprits:

- `|x|` (kink at 0; 2nd derivative formally a Dirac there)
- `max(0, x)`, ReLU, hinge
- `min(x_lower, x, x_upper)` (clamp)
- `jnp.where(cond, ...)` with a sharp `cond` boundary
- `jnp.sqrt(x)` near x=0
- Any piecewise definition glued at a single point

JAX (and most autodiff frameworks) produce *some* value for the second
derivative through these — often 0 or a one-sided limit — and it's not
what the cusp actually contributes (a finite spike or a diverging
limit). Forward-mode autodiff gives the same wrong answer because the
underlying function isn't C².

This bites doubly when the MAP *lands on the cusp*. The optimizer is
attracted there because the prior pulls A toward 0 and the likelihood
provides no signal in the no-learning case. So the cusp isn't an
"edge case you might hit" — it's the *most common case* in some regimes.

**This project (real bug, found 2026-05-17 — see `memory/nuts-perf` for
the bench that surfaced it):** `halflife_sigma(A) = σ_min + (σ_max − σ_min)
· tanh(|A|)`. The `tanh(|A|)` is once-differentiable in A (tanh smooths
the kink in the *value* of σ), but the *second* derivative of σ w.r.t.
A is wrong at A=0. The MAP of well-spec haz1/N=100 data landed at
A_ssp = 4.6e-9 (essentially exactly the cusp), JAX autodiff returned
d²/dA² = -147 there, Laplace flagged the Hessian non-PD, and the
"fallback to NUTS" code path looked like it was kicking in for a genuine
ridge when actually it was a pure autodiff bug.

**Fix (always available, always principled): the smooth-abs trick.**

```python
def smooth_abs(x, eps=1e-2):
    return jnp.sqrt(x * x + eps * eps) - eps   # smooth_abs(0) = 0 exactly
```

`smooth_abs` is twice (in fact infinitely) differentiable, equals 0 at 0,
and approaches `|x|` to within `eps` for `|x| >> eps`. Its second
derivative at 0 is `1/eps` — large but finite, which is the correct
representative for the cusp's contribution to local curvature. Then
`tanh(smooth_abs(A))` is *twice* differentiable everywhere and autodiff
Hessians work.

Same trick handles other culprits:

- `max(0, x)` → `0.5 · (x + smooth_abs(x))`  (smooth ReLU)
- `min(a, x)` → `a − smooth_abs(a − x)`      (smooth clamp lower)
- `sqrt(x)` near x=0 → `sqrt(x + eps²)`      (smooth root)
- `jnp.where(c, f, g)` with sharp c → multiply by a sigmoid window

**General lesson.** "Once-differentiable" is a low bar. Laplace, NUTS
mass adaptation, and any inference that wants second-order information
needs the model to be C² along every direction the sampler/optimizer can
take. When designing priors and likelihoods, ask: does this function
have a continuous *second* derivative everywhere on the support, not
just a continuous first? If not, smooth it before you ship.

**Testing trick.** For any new prior or likelihood term, compute the
Hessian at MAP via both autodiff and central finite differences along
the smallest-eigvec direction. If they disagree by more than a few
percent, you have a non-C² spot. The diagnosis script in
`tmp/` for the segment-model bug is a reusable template.

## Hierarchical models / partial pooling / shrinkage

When you have many groups (segments, users, units), let each group's
params draw from a shared population:

```
For each group g:
    θ_g  ~  Normal(μ_pop, σ_pop)
μ_pop, σ_pop  ~  hyperpriors
```

**Effect (shrinkage):** each group's posterior on θ_g pulls toward μ_pop;
pull strength depends on how informative the group's own data is. Silent
groups shrink fully to μ_pop; data-rich groups barely move.

**When this is the answer:** when single-group identifiability is poor
(weak or non-identifiable) but you have many groups and at least some are
informative. The informative groups pin down (μ_pop, σ_pop) and that
inferred population prior fills in for the silent groups.

**What it doesn't do:** fix a structurally ill-posed single-group
likelihood. If literally one segment is non-identifiable on halflife AND
you have only one segment, you need a joint prior, not HYPER.

## Neal's funnel

The posterior geometry of centered hierarchical models pathologizes when
σ_pop is small (groups cluster tightly around μ_pop). The posterior in
(θ_g, σ_pop) space pinches into a funnel: the cross-section in θ_g
narrows as σ_pop → 0. HMC/NUTS samplers get stuck near the tip, taking
tiny steps and never exploring.

**Fix: non-centered reparameterization.** Replace
`θ_g ~ Normal(μ_pop, σ_pop)` with

```
z_g ~ Normal(0, 1)
θ_g = μ_pop + σ_pop · z_g     (deterministic)
```

The latent is now `z_g`, which is unit-variance regardless of σ_pop. The
funnel geometry vanishes; samplers explore freely.

Standard Stan/PyMC pattern. Mandatory for hierarchical MCMC at any
non-trivial dimension.

## Empirical Bayes & cached hyperparameters (scaling pattern)

Full hierarchical inference at the scale of thousands of groups is rarely
done jointly. Standard pattern:

1. Fit hyperparameters (μ_pop, σ_pop, ...) once using all data.
2. **Freeze them as point estimates.**
3. Per-group fits become independent and fully parallel — each is a
   small fit (10ish latents) conditional on frozen hypers.
4. Refit hypers periodically as new data accumulates.

This makes the PGM **decompose** into one shared hyperparameter block plus
N independent per-group blocks. Linear scaling. Production friendly.

**Tradeoff:** point-estimated hypers don't propagate hyperparameter
uncertainty into the per-group posteriors. For small N this matters; for
large N it's negligible.

### EB σ collapse — iterate the mean, fix the scale

**Symptom.** Iterative EB (re-fit MAPs → re-estimate (μ_pop, σ_pop) from
MAPs → loop) appears to converge, but σ_pop pegs at the floor regardless
of true population spread. Per-group posterior bands look impressively
tight ("X% SD reduction!"). Closer look: the algorithm is collapsing the
prior, not learning the population.

**Why it happens.** As σ_pop shrinks, each group's MAP is pulled tighter
toward μ_pop. So `std(MAPs)` (the next iter's σ_pop estimate) shrinks
mechanically. Feedback loop: σ_pop ↓ → MAPs cluster ↓ → std(MAPs) ↓ →
σ_pop ↓. Floored at `sigma_floor` to avoid going to 0.

**Why the natural method-of-moments fix doesn't save you.** The standard
unbiased estimator
`σ²_pop = max(0, var(MAPs) - mean(σ²_within))`
returns 0 (negative variance, clipped) when within-segment Laplace SD is
comparable to the cross-MAP spread — i.e., when per-segment data can't
distinguish population spread from segment noise. This is *correct*
behavior on weakly-identified populations, but combined with iteration it
just collapses faster.

**Fix: one-step scale, iterated location.**

```python
# Stage 1: estimate σ_pop ONCE from broad-prior MAPs.
unpooled_maps = [fit(seg, default_prior) for seg in segments]
sigma_pop = std(unpooled_maps[:, idx])      # raw — slightly overestimates
                                            # because it includes within-noise

# Stage 2: fix σ_pop, iterate μ_pop only.
for it in range(max_iter):
    maps = [fit(seg, pool=(mu_pop, sigma_pop)) for seg in segments]
    mu_pop = mean(maps[:, idx])
```

σ_pop from broad-prior MAPs slightly overestimates the true σ_pop
(includes within-noise), but the overestimate gives wider, more *honest*
bands rather than overconfident ones. The μ_pop iteration is well-behaved
because σ_pop is fixed.

**This project (real bug, found+fixed 2026-05-17 — see
`memory/eb-sigma-collapse`):** The original `fit_eb_pool` iterated both
parameters; σ collapsed to floor=0.05 within 3 iters and the reported
"68-86% SD reduction" was a partial artifact. After one-step σ + iterated
μ: SD reduction is 29-57% (honest), and the pool mean correctly drifts
toward truth instead of getting pinned near the prior median.

**General lesson.** When the population is weakly identified relative to
within-group noise (very common — it's the whole reason you're pooling),
iterating both scale and location is unstable. Iterate the cheap thing
(location), fix the expensive one (scale). Test with verbose output the
first time you implement EB — silent collapse is the failure mode.

## Other gotchas

### Optimizer stuck-at-init ≠ model bug

If MAP returns ≈ the initial point, check whether the *gradient at init*
is zero on the axes that didn't move. Common cause: initialization at a
point where some params are unidentifiable (e.g. flat-curve init makes
timescale params have zero gradient). Fix the init, not the model.

### MAP hides ridges

A single MAP point gives no indication of posterior elongation. Always
either:
- compute Hessian and inspect its condition number / eigenvalues, OR
- run multiple seeds and look at the spread of MAPs, OR
- compute log_posterior on a 2-D slice through MAP.

If asymptotic Hessian is non-PD, you have a ridge (or a saddle).

### Independent priors aren't always right

The default factorization `P(θ) = ∏ P(θ_i)` is convenient but isn't a
neutral choice — it's a strong claim that all the params are a priori
unrelated. Sometimes (often, in PGMs with weak identification) a joint
prior encodes your actual beliefs better.

### Reparameterizations preserve ridges

If `(A, B)` are ridged, then any smooth invertible reparameterization
`(A', B') = f(A, B)` is ridged in the new coords (Jacobian moves the
geometry but doesn't flatten it). Renaming doesn't fix identifiability.
Only adding information does — via data, prior, or pooling.

### "Sub-PGM decomposition" = empirical Bayes

When the joint PGM is too big to fit, factor it into blocks: a small
shared "hyperparameter" block plus many independent per-group blocks. Fit
the hyperparameter block once and freeze it. This is the standard pattern
for production hierarchical models at scale; not a hack.

## Cross-references in this codebase

- Joint prior implementation: `learning_model_v07.py` `halflife_sigma()`
  and the comment block above it.
- Ridge demonstrations: `tmp/ridge_demo.py` (strong learning) and
  `tmp/ridge_demo_no_learning.py` (regime-1 no-learning).
- Plan-level commitments: `reports/2026-05-16_learning_curves_findings.md`
  Plans HYPER (partial pooling), NUTS (sampler), JAX (autodiff).

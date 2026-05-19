# Pre-handoff status — 2026-05-17

> **SUPERSEDED 2026-05-17 (later same day) by [`/V1_ESSENCE.md`](../../V1_ESSENCE.md).**
> The reframe parked beta2 + moving-peaks + route-change handling for v2
> iteration inside SpinLab, leaving v1 as haz1 with monotone learning curves
> and the EB pool. The priority tables below predate that reframe — read
> `/V1_ESSENCE.md` for the authoritative v1 scope. This doc is kept as
> historical record of the in-session decision trail.

A regroup doc. Captures where the model lives now, what's locked in, what
still needs answering before SpinLab can consume the fits, and what we
can productively do in parallel while real data is being captured.

This supersedes nothing — it's an overlay. The main spec docs
([`../segment_model.md`](../segment_model.md),
[`../learning_curves.md`](../learning_curves.md),
[`../segment_model_hyperpriors.md`](../segment_model_hyperpriors.md))
stay authoritative for the *model*. This doc is authoritative for the
*handoff*. Earlier session reports (`2026-05-15_beta_mixture_decisions.md`,
`2026-05-16_learning_curves_findings.md`) are sibling snapshots.

## Where we are

### What works end-to-end

- **V07 learning model** in numpy + JAX, 10 latents per segment for
  haz1, 15 for beta2.
- **JAX speedups**: ~700× over numpy cold MAP. p50 8 ms streaming
  refit, p99 35 ms — well inside live-UI budget.
- **EB pool on `halflife_sf`** working across multiple segments; shrinks
  regime-1 (no-learning) segments to the population mean.
- **2-model comparison**: haz1 vs beta2 measured. Beta2 wins ~25 nats
  on shape-rich truth (5/5 seeds), pays 7–14 nat Occam tax on
  no-shape truth, fails Laplace PD on 20% of no-shape seeds at N=500.
- **Joint prior on (halflife, A)** handles the no-learning ridge cleanly.
- **Synth generators**: single-segment, multi-segment (varied),
  beta2-truth and haz1-truth modes.
- **Knob console / inspector** (`pgm_inspect_v07.py`) for interactive
  exploration.
- **Test suite**: 24 tests passing.

### What's decided / locked

| Decision | State |
|---|---|
| Model parameterization (V07: θ_∞, θ_1, halflife) | locked |
| Joint (halflife, A) prior with tanh-σ | locked |
| Pure-JAX fit path with bucketed/padded streaming | locked |
| Empirical-Bayes (not full Bayesian) for hyperpriors at scale | locked |
| haz1 default, beta2 as alternative | locked this session |
| Cold-starts-only model + schema scope | locked this session |
| Small-power-only model scope | locked this session |
| Defer warm-start and route-changes | locked this session |
| **Data schema v2** (id, game, segment, outcome, time_ms, created_at) | locked, handed to SpinLab |
| Export order: `ORDER BY created_at, id` | locked this session |

### What's NOT yet decided

| Open question | Owner |
|---|---|
| α parameterization for beta2 (smooth-cum-hazard vs scaled-CDF spec form) | us — only matters once beta2 ships |
| Should `slop_spread` learn at all, or be constant/random-walk? | data — won't know until real fits |
| Hyperprior expansion: which params get pooled beyond `halflife_sf`? | us, post-data |
| Live-UI stat list — what numbers does SpinLab actually show? | user + SpinLab |
| API contract between `segments` and SpinLab | us + SpinLab |

### Known limitations

Empirically-measured caveats that affect API design and band reporting
are collected in [`../known_limitations.md`](../known_limitations.md).
Headline items (all measured this session, 2026-05-17):

- log_halflife_ssp 90% intervals under-cover (broad 57%, pool 29%).
  Bands too narrow; report MAP only or widen.
- ssp is the weakest-identified learning latent — wider per-segment SDs
  are honest, not defective.
- beta2 Laplace PD fails on ~20% of no-shape data. haz1 default, beta2
  opt-in with NUTS fallback.
- PPC catches abrupt shape change reliably; mostly blind to slow weight
  drift. Don't promise PPC monitors moving-peaks.
- `find_map` can converge to `-inf` posterior on edge-case segments;
  callers must check `info['converged']` (now stricter post-2026-05-17).

## What needs to happen before SpinLab ports

Three categories, in priority order.

### Critical path (without these, no port)

1. **API contract**. SpinLab needs to know what to call and what comes
   back. Concretely: a JSON schema for the fit-result payload, with the
   set of numbers we promise to compute + their semantics + their
   refresh cadence. This is the thing SpinLab integrates against; the
   model implementation is an internal detail. **Open.**

2. **Validation harness**. Frozen TSV inputs + expected output JSONs
   so SpinLab (or anyone) can verify their port matches the reference
   numerics. Math-only is enough for v1 — log_prior, log_likelihood,
   log_posterior at frozen θ vectors across 3–4 representative
   datasets. **Open** — scoped to "Full" in the earlier conversation
   but not built.

3. **Stable model commitment**. Are we shipping haz1 only, beta2 only,
   or both? Affects the API surface (beta2 exposes shape params; haz1
   doesn't). My read: **haz1 default + beta2 opt-in** is right given
   beta2's 20% PD failure rate at N=500 on no-shape data. **Tentative**;
   could re-litigate.

### Strongly recommended (improves handoff quality)

4. **Posterior Predictive Check (PPC) diagnostic**. A "is the model
   actually fitting the data, or just pretending?" signal. Without
   this, SpinLab can't tell when a fit is misleading them. Surfaces as
   a single number ("Bayes p-value on clean-time skewness", say) with a
   sensible threshold. **Open**, ~1 session.

5. **NUTS fallback for ridge cases**. When Laplace fails PD (20% of
   no-shape beta2 fits, ~100% at N≤22), bands are unreliable.
   BlackJAX or NumPyro for posterior sampling in those cases. **Open**,
   ~1–2 sessions.

6. **Derived-stats spec**. Enumerate what closed-form / cheap-compute
   stats fall out of MAP + Laplace. E.g. "attempts until X% chance of
   WR" is one. SpinLab UI shopping list. **Open**, dialog with user.

### Nice-to-have (defer, mention only)

7. **Hyperprior expansion** to other latents (currently only
   `halflife_sf` is pooled). Pays off only once we have multiple real
   segments to pool across.

8. **GPU + `jax.vmap`** batched fits. Real win, but requires a port
   from scipy L-BFGS-B to a pure-JAX optimizer first. Bench data this
   session: would unlock ~5–10× on batched workloads (CPU vmap alone)
   and another ~5× on top with GPU. Not blocking handoff.

9. **Moving-peaks / non-stationary shape**. Open modeling concern (see
   `memory/moving_peaks.md`); no clean approach proposed yet.

10. **Closed-form `mean_s_given_died`**. Replaces a Simpson quadrature
    with `Σ wₖ μₖ`. Minor speed win; fixes a NaN bug for K=1. From
    `beta_mixture_decisions.md` deferred list.

## Parallel work available while waiting on real data

SpinLab's data capture pipeline is on their critical path; nothing we
can do here moves their progress. Work we can productively do in
parallel:

| Item | Effort | Unlocks |
|---|---|---|
| **API contract draft** | 1 session | Critical path — SpinLab can integrate against shape, not implementation |
| **Validation harness (math-only)** | 1 session | Critical path — porters can verify |
| **PPC diagnostic** | 1 session | Recommended — fit-honesty checks |
| **NUTS fallback** | 1–2 sessions | Recommended — honest bands at low N |
| **Derived-stats enumeration** | 0.5 session, mostly dialog | Recommended — UI inventory |
| **Hyperprior expansion (more EB pools)** | 1–2 sessions | Nice — needs real multi-segment data to validate |
| **Fisher info / a priori ID budget** | 2 sessions | Nice — pre-fit precision claims |
| **GPU/`vmap`/`jaxopt` port** | 2–3 sessions | Nice — batch-fit speed |

## Recommendation

If I had to pick one item to do next while real data is being captured,
it would be **the API contract**. Reasons:

- It's the only item that *directly* blocks SpinLab's UI work. They
  can build capture without us, but they can't build the live display
  without knowing what we'll send them.
- It forces a productive conversation that surfaces several other open
  questions for free (which stats to show → derived-stats spec; what
  precision to promise → NUTS fallback need; whether to expose beta2
  shape params → model commitment).
- It's session-sized: a doc and a sample JSON, not a code project.
- The artifact gates the validation harness (the harness's "expected
  output" is shaped by the API contract).

Second pick, if you want something hands-on rather than design: **PPC
diagnostic**. Concrete code, ships a real diagnostic, doesn't require
real data to develop (synth misspec works fine for testing). Lands
something SpinLab will appreciate once data arrives.

Third pick: **validation harness**, but I'd wait until API contract
exists since the harness shape is contract-driven.

## What this doc does NOT cover

- **Plan revisions you wanted to do separately.** You said after the
  beta2 work you'd want to discuss/modify the open-question priorities
  in the docs. Not done yet. This doc reports current state, doesn't
  re-prioritize.
- **The α parameterization decision** (beta2 smooth-cum-hazard vs spec
  scaled-CDF form). Documented inline in the model file; not surfaced
  for re-decision here.
- **Long-term scaling plans** (24 games × 60 segments × thousands of
  fits). Captured in `speed_and_techniques.md`; not re-summarized here.

---

## 2026-05-17 update — decisions and plan forward

### Decisions made this session

| Decision | Status |
|---|---|
| Game-level + global-level hyperprior layers | **Skip for v1.** Add when ≥2 games are real and `difficulty_game` becomes useful. Single-game pooling collapses these anyway. |
| `difficulty_game` latent + SMW Central rating observation | **Skip for v1.** Falls out of skipping game-level. |
| Pool `bpt` across segments | **No, ever.** Different segments have legitimately different theoretical times. Pooling would actively hurt. |
| Pool asymptotes (`sf_inf`, `ssp_inf`, `alpha_inf`) | **Skip for v1.** Usually well-identified at moderate N; pooling marginal. Revisit if real-data segments show low-N identification problems. |
| Pool `halflife_ssp` and `halflife_alpha` | **Yes** — symmetric with existing `halflife_sf` pool. Same machinery. Biggest identification win after the existing pool. |
| Add NUTS to the calculation set | **Yes.** For ridge fallback + Laplace validation + reusable for hierarchical work. NumPyro or BlackJAX wrapping our JAX `log_posterior`. |
| Add PPC diagnostic | **Yes.** Tension barometer for misspec. Works on synth now, becomes the moving-peaks empirical answer when real data arrives. |
| Spec line "death locations are route-structural, not skill" | **Wrong, edit when next touching `learning_curves.md`.** Real story includes skill, micro-routing, muscle memory. Static-shape is a v1 simplification, not a structural claim. |

### Plan forward — sequence

1. **PPC infrastructure** (~1 session). Posterior-sample-driven forward
   simulation; discrepancy statistics (skewness, kurtosis, mode-count
   in `s_death`); Bayes p-values. Validates the tension story on synth:
   - haz1 on beta2-truth → high tension (catches misspec)
   - beta2 on haz1-truth → low tension (no false alarm)
   - own-model on own-truth → low tension (baselines)
   - real data → empirical verdict, including the moving-peaks question

2. **NUTS via NumPyro** (~1–2 sessions). Wrap JAX `log_posterior`.
   Bench timings (per-segment, per-N) vs Laplace. Cross-check bands
   agree on PD cases. Trigger NUTS automatically when Laplace fails
   PD. Reusable for hierarchical work.

3. **Hyperprior expansion to all halflives** (~1 session). Pool
   `halflife_ssp` and `halflife_alpha` symmetric with existing
   `halflife_sf`. Extend `tests/test_eb_pool.py` with parallel tests
   on multi-segment synth (run against both haz1 and beta2 to verify
   no model-specific surprises).

4. **API contract draft** (after the above). With the model committed
   and diagnostics in place, draft the JSON shape SpinLab consumes.
   Pulls in derived-stats discussion at that point.

Total: ~3.5 sessions of work before API contract. PPC and NUTS each
ship reusable artifacts independent of the hyperprior milestone.

### Moving peaks — explicit deferral

The shape-over-attempts gap (peak disappearance, migration, narrowing
with practice) is a known limitation and the spec line about
"route-structural" is misleading. **The plan above does not address
it directly.** Rationale:

- The cheapest extension (per-component-weight learning curve `w_1(n)`)
  adds 3 latents but requires evidence it's needed before we pay for it.
- PPC in step 1 is the empirical diagnostic. If PPC catches shape
  tension over time on real data, we extend; if not, static-shape v1
  is justified.
- Designing the extension before PPC is premature.

If PPC on real data confirms the moving-peaks effect, the natural v2
move is `w_1(n)` first (most common phenomenon: peak disappears with
mastery), then `μ_k(n)` if needed (rarer: strat migration).

### Resume cue

When picking up next session, the first task is **PPC infrastructure**.
Probably the right entry points:
- New file: `ppc.py` — posterior-sample-driven forward sim + discrepancy stats
- New script: `tmp/ppc_synth_check.py` — runs the 2×2 (haz1/beta2 × haz1-truth/beta2-truth) and prints Bayes p-values
- New test: `tests/test_ppc.py` — pins the well-spec and misspec cases

Read order if cold:
1. This doc, the "2026-05-17 update" section
2. [2026-05-16_haz1_vs_beta2_findings.md](2026-05-16_haz1_vs_beta2_findings.md) for the model-comparison context
3. The session memory in `.claude/projects/c--Users-thedo-git-segments/memory/` for behavior calibration

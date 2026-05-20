# V1 — what to integrate

The single source of truth for what this repo ships to SpinLab in v1, what's
parked for v2, and what to do (and not do) with the numbers. If you're a
porter wiring this into SpinLab, read this front-to-back once and refer back
to the deep specs only when something here points you there.

This doc supersedes:
- `external_docs/reports/2026-05-17_handoff_status.md` (early reframe, less
  decided)
- `external_docs/known_limitations.md` (merged here)
- scattered priority lists in `CHANGELOG.md` and the older session reports

## The model in 200 words

A speedrun segment is a fixed slice of a run (e.g., "World 1-2 Castle"). A
player makes many attempts. Each attempt either survives (clears the
segment) or dies. We model:

- **`bpt`** — best possible time for the segment.
- **`slop_frac`** (= `sf`) — mean execution overhead, as a fraction of `bpt`.
- **`slop_spread`** (= `ssp`) — coefficient of variation of execution noise.
- **`alpha`** — death rate per attempt (so P(die) = 1 - exp(-alpha)).

The three execution latents (`sf`, `ssp`, `alpha`) evolve over attempts via a
**learning curve**: `theta(n) = theta_inf + (theta_1 - theta_inf) ·
2^(-(n-1)/halflife)`. Each one has its own (`theta_1`, `theta_inf`,
`halflife`), so 10 latents per segment in v1.

If an attempt dies, the death lands at some fraction `s ∈ [0, 1]` through
the segment. **v1 uses a single constant hazard rate (`haz1`)** — the death
location is just an exponential conditional on `alpha`. v2 will introduce a
beta-mixture for multi-peaked death locations; the code is in the repo
already (`learning_model_v07_beta2_jax.py`) but not in v1's API.

When fitting many segments together, we **pool the three halflives** across
segments via empirical Bayes (`fit_eb_pool.py`). Other latents stay
per-segment.

That's it. Deep formal spec: [external_docs/segment_model.md](external_docs/segment_model.md).
Learning curve details: [external_docs/learning_curves.md](external_docs/learning_curves.md).
Pool details: [external_docs/segment_model_hyperpriors.md](external_docs/segment_model_hyperpriors.md).

## What ships in v1

- **haz1 model** (10 latents per segment).
- **Per-segment MAP fit** via JAX-jitted L-BFGS-B (`fit_jax.find_map`).
- **Laplace posterior bands** at MAP (`fit_jax.laplace_posterior`).
- **NUTS fallback** for segments where Laplace is non-PD (`fit_nuts.posterior_samples`).
- **EB pool on the three halflives** across segments (`fit_eb_pool.fit_eb_pool`).
- **PPC diagnostics** (`ppc.run_ppc`) — whole-run discrepancy stats only.
- **Synth generators** for porting validation (`generate_synthetic_learning_v07`).

That's the entire integration surface. Everything else in the repo is
either internal machinery, v2-parked, or a historical artifact.

## What's parked for v2 (still in the repo, do not integrate)

- `learning_model_v07_beta2_jax.py` + `fit_jax_beta2.py` — beta-K=2 mixture
  hazard. Captures multi-peaked death-location data. Banner: "v2-scope —
  parked." Don't expose in the API.
- `external_docs/nonstationary_shape.md` — design for moving-peaks
  (within-segment hazard shape drift over attempts). v2 only.
- `ppc.V2_WITHIN_RUN_STATS` — tertile-binned mid_third detectors. Defined
  for v2 use; not part of `ppc.DEFAULT_STATS`.
- `tmp/within_run_stat_sensitivity.py` — validation infrastructure for v2
  drift detectors.
- Peak-shift synth builders (`weight_drift_shape`, `strat_swap_shape`,
  `strat_change_shape` in `generate_synthetic_learning_v07.py`) — v2
  validation truth. v1 generators ignore them by default.

These exist so the v2 iteration inside SpinLab has working starting points,
not because v1 needs them. The v1 model is **deliberately** static-shape.
Real-world non-stationarity (route changes, strat shifts) is absorbed into
residual noise in v1; if real data shows the residuals are systematic, v2
picks them up.

## Public API surface

For the JSON wire format that the SpinLab boundary expects, see
[`external_docs/api_contract.md`](external_docs/api_contract.md) and
the live serializer in `api.py` (re-exported as
`sv.{fit_segment, refit_segment, fit_pool}`). The Python surface below
is the lower-level math API that the serializer composes; callers who
just want the JSON payload should use the three contract helpers.

```python
import segments_v07 as sv

# v1 JSON-payload surface (the SpinLab handoff)
result = sv.fit_segment(attempts, segment_id="w1-2-castle")
result = sv.refit_segment(attempts, prev_result=result)
result = sv.fit_pool([{"segment_id": ..., "attempts": [...]}, ...])

# Lower-level math API (composed by the helpers above)

# 1. Single-segment fit (haz1, the v1 model)
t_ms, is_died = sv.data_to_arrays(rows)   # rows: list of ('survived'|'died', ms)
theta_map, info = sv.find_map(t_ms, is_died)

# 2. Posterior bands — Laplace if PD, NUTS fallback otherwise
import fit_nuts
out = fit_nuts.posterior_samples(t_ms, is_died, theta_map, model='haz1')
samples = out['samples']           # shape (n_samples, 10), in log-space theta
band_source = out['source']        # 'laplace' or 'nuts'

# 3. Multi-segment pooled fit
import fit_eb_pool
segments = [{'data': rows_for_seg_i} for i in range(S)]
pool, maps, history = fit_eb_pool.fit_eb_pool(segments)
# Each `maps[i]` is the segment's pooled MAP. `pool` has the learned
# population (mean, sigma) for each of the three halflives.

# 4. PPC honesty check — call after fitting, before showing bands
import ppc
res = ppc.run_ppc(samples, t_ms, is_died, model='haz1')
# Each res[stat]['p_two_sided'] near 0.5 means the model can reproduce the
# data; near 0 or 1 means tension. The four DEFAULT_STATS:
#   - died_rate
#   - died_tau_skew
#   - died_tau_kurt
#   - died_s_mid_third
```

**Theta layout (length 10).** Always in log-space; convert with `np.exp` for
display. Indices in `learning_model_v07_jax.IDX_*`.

| idx | name              | meaning |
|---:|---|---|
| 0 | `log_bpt`          | log of best-possible-time, ms |
| 1 | `log_sf_inf`       | log slop_frac asymptote |
| 2 | `log_ssp_inf`      | log slop_spread asymptote |
| 3 | `log_alpha_inf`    | log hazard rate asymptote |
| 4 | `log_sf_1`         | log slop_frac at first attempt |
| 5 | `log_ssp_1`        | log slop_spread at first attempt |
| 6 | `log_alpha_1`      | log hazard rate at first attempt |
| 7 | `log_hl_sf`        | log halflife (attempts) for sf curve |
| 8 | `log_hl_ssp`       | log halflife (attempts) for ssp curve |
| 9 | `log_hl_alpha`     | log halflife (attempts) for alpha curve |

**Sentinel: `info['converged']`.** Always check before chaining the MAP into
NUTS or anything that requires a feasible theta. The flag is now AND'd with
a finite-`log_posterior` check internally, so a `True` value means both
scipy succeeded *and* the model evaluates finitely at the result.

## Caveats — what NOT to claim

Each caveat ships with v1. None are blockers; all need handling in the UI.

### 1. `log_hl_ssp` bands are not honest 90% intervals

**What.** Both the broad-prior 90% credible interval and the EB-pool 90%
interval on `log_hl_ssp` cover the true value less than 90% of the time on
realistic synth. Measured: broad 57–70%, pool 29%.

**Why.** The joint prior on `(halflife, A_amplitude)` deliberately collapses
halflife's σ when the learning amplitude `|A|` is small (a stabilization
trick for no-learning segments). When ssp is also weakly identified by data,
the posterior is prior-dominated and narrow. The pool further tightens
intervals faster than its mean drifts toward truth — pool helps *point
estimates*, hurts *intervals*.

**Don't.** Show pool-Laplace 90% bands on `log_hl_ssp` as "90% intervals" in
the live UI. They will systematically miss reality.

**Do.** One of:
- Suppress the band on this latent; show MAP only.
- Inflate the interval post-hoc by a calibration factor (re-run
  `tmp/calibration_probes.py --probe 5` to estimate).
- Use the broader (single-segment) interval as a conservative fallback.

This caveat does **not** apply to `log_hl_sf` or `log_hl_alpha`. Their
priors happen to align with realistic truths and they show ~90–100%
coverage in the same probe.

### 2. `ssp` is the weakest-identified learning latent

**What.** Per-segment posterior SDs are roughly: sf 0.15, ssp 0.28, alpha
0.37 (broad prior, N=300). ssp is structurally hardest to pin down because
it manifests in higher moments of the survived-time distribution.

**Why this is fine.** Wide ssp bands are honest. Downstream `M_clear` and
streaming-p50 predictions depend more on mean_slop (sf-driven) than spread,
so the wide ssp bands don't degrade headline-stat precision much.

**Do.** When a SpinLab user asks "how reliable is ssp here?", answer:
wider than sf/alpha per segment by design; pool reduces it some but not
to sf's tightness. If real data ever needs tighter ssp inference, the move
is more attempts per segment, not more model.

### 3. `find_map` can plateau on the `-inf` penalty cliff

**What.** L-BFGS-B converges by climbing log_posterior; we wrap `-inf`
regions with a `1e10` penalty cliff so the line search can detect them.
Occasionally the optimizer settles *on* the cliff and scipy reports
`success=True`, but the actual log_posterior at the returned theta is still
`-inf`. NUTS init would crash on that theta with "Cannot find valid initial
parameters."

**Fix (already in v1).** `info['converged']` is now `scipy_success AND
raw_lp_finite`. The `info` dict has both flags for diagnostics.

**Do.** Check `info['converged']` before passing the MAP into NUTS or any
band-derivation step. On `False`, the segment is data-prior incompatible —
flag as un-fittable in the API rather than producing garbage bands.

### 4. Beta2 fails Laplace PD on ~20% of no-shape data

**Not v1's problem** — beta2 is parked. Listed here only so you know why
the v1 API does not expose model choice: forcing haz1 sidesteps this.

### 5. PPC catches abrupt model misspec, not slow drift

**What.** The four `DEFAULT_STATS` reliably catch coarse model failures
(e.g., haz1 fit on beta2-truth data flags at p≈0.01). They are insensitive
to within-run shape drift. v2 detectors exist in `ppc.V2_WITHIN_RUN_STATS`
but aren't reliable on slow drift either (see
`memory/ppc_drift_blind_spot.md`).

**Don't.** Tell the user "PPC monitors for shape non-stationarity." It
monitors for static-shape model fit honesty only.

**Do.** Use PPC as a "did the fit converge to something sensible?" gate.
On any stat with `p_two_sided < 0.01`, the fit is structurally mis-matched
to the data — flag the segment, don't ship its bands as authoritative.

## What the porter ports

Three things, in order of priority. The first two are mandatory; the third
is recommended.

1. **The math.** `log_prior`, `log_likelihood`, `log_posterior` from
   `learning_model_v07.py` (numpy reference) or
   `learning_model_v07_jax.py` (jax). Validate to ~1e-5 against this
   repo's numpy reference at a frozen theta on the validation TSVs (see
   `tests/test_jax_vs_numpy.py` for the pattern).
2. **The fit.** L-BFGS-B with bounded `log_bpt` (upper bound from
   `min(survived) - 500ms`), warm-started from
   `warm_init_from_data(...)`. Laplace from the Hessian at MAP. Match
   `info['converged']` semantics.
3. **The PPC.** Forward simulator and the four discrepancy stats. The
   only "is this fit honest?" gate the consumer has.

Things you do **not** need to port for v1:
- beta2 model (parked).
- The full NumPyro NUTS — this repo's `fit_nuts.py` is reusable as a
  black box for fallback, or you can re-implement against your stack.
- The pool — only needed once SpinLab fits ≥5 segments concurrently.
  Single-segment fits use the module-default prior.
- Anything in `tmp/` (calibration probes, sensitivity matrices). These
  are for ongoing model validation in this repo; SpinLab doesn't need
  them.

## What we never solved (and that's OK for v1)

- **Route changes / strat shifts that make slopes go *up* mid-segment.**
  v1 learning curves are monotone (decay toward asymptote). Real-world
  route changes show as residual noise. If SpinLab data eventually shows
  systematic mis-fit around player-flagged strat changes, the v2 move
  is either auto-detection of regime change or absorbing the variance
  into a wider posterior — not manual labels. Out of v1 scope.
- **Moving peaks** (within-segment hazard shape drift). Design proposal
  in `external_docs/nonstationary_shape.md`. v2 only.
- **Game-level / global hyperpriors.** Single-game scope in v1 collapses
  the game level; revisit when ≥2 games are real (see
  `external_docs/segment_model_hyperpriors.md`).

These are *deliberate* v1 deferrals, not missing features. Surface them as
"future work" in SpinLab's docs, not as bugs.

## Where the deep specs live

Read these only when something above sends you there.

- [`external_docs/segment_model.md`](external_docs/segment_model.md) —
  static-skill base model. Read first if you want to understand the math
  from scratch.
- [`external_docs/learning_curves.md`](external_docs/learning_curves.md) —
  V07 learning curve parameterization. Read when porting the curve form.
- [`external_docs/segment_model_hyperpriors.md`](external_docs/segment_model_hyperpriors.md)
  — the empirical-Bayes pool. Read when wiring multi-segment fits.
- [`external_docs/pgm_tricks.md`](external_docs/pgm_tricks.md) —
  vocabulary for the design choices (joint priors, ridges, σ collapse,
  smooth-once-not-smooth-twice). Read when debugging fit pathologies.
- [`external_docs/nonstationary_shape.md`](external_docs/nonstationary_shape.md)
  — v2-scope moving-peaks proposal. Read only when starting v2 work.
- [`external_docs/api_contract.md`](external_docs/api_contract.md) — v1
  JSON wire format between this repo and SpinLab. Read when wiring the
  serializer or implementing the porter side.

The older session reports in `external_docs/reports/` are historical record
of how we got here; they're not part of the v1 contract.

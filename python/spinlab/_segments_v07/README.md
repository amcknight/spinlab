# segments — V07 speedrun segment model

Probabilistic model for per-segment speedrun grinding: given a sequence of
attempts (`survived` / `died`, times in ms), infer the underlying skill
parameters and how they evolve as the player learns the segment. Designed
for handoff to a production Python service feeding a TypeScript live viz.

> **If you're porting v1 into SpinLab, read [`V1_ESSENCE.md`](V1_ESSENCE.md)
> first.** That doc is the integration contract: what ships, what's parked,
> what bands to trust, what to suppress. This README is the in-repo
> developer view — file map, perf, dev workflow.

## Quick start

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt

# Sanity-check the install (81 tests, ~105s; the NUTS suite is the slow ~70s of that)
.venv/Scripts/python -m pytest tests/

# Render the inspector (numpy reference; opens HTML in tmp/)
.venv/Scripts/python pgm_inspect_v07.py --out tmp/v07.html

# Fit on synth (JAX path, fast)
.venv/Scripts/python -c "import segments_v07 as sv; \
  from generate_synthetic_learning_v07 import TRUTH_DEFAULTS, generate; \
  d = generate(500, TRUTH_DEFAULTS, seed=0); \
  t, dd = sv.data_to_arrays(d); \
  th, info = sv.find_map(t, dd); \
  print('MAP iter', info['iter'], 'nll', round(info['nll'],1))"
```

## File map

### Production V07 (the keep-pile)

| File | What |
|---|---|
| `segments_v07.py` | **Public API.** `import segments_v07 as sv` and use. |
| `learning_model_v07_jax.py` | **JAX impl of haz1 (10 latents).** Differentiable, JIT-compilable. |
| `learning_model_v07_beta2_jax.py` | **JAX impl of beta2 (15 latents).** K=2 beta-mixture hazard. **v2-scope — parked**; not in v1 API. |
| `learning_model_v07.py` | **Numpy reference impl.** Slower; used for cross-validation and offline tools. Tests pin JAX ≡ numpy to ~1e-5. |
| `fit_core.py` | **Generic fit machinery** parametrized over the model module: JIT'd value/grad/Hessian, MAP, Laplace. Adding a new hazard is a thin shim against this. |
| `fit_jax.py` | haz1 fit shim: `warm_init_from_data` + thin wrappers binding `learning_model_v07_jax` into `fit_core`. |
| `fit_jax_beta2.py` | beta2 fit shim: same shape, binds beta2 model. **v2-scope — parked.** |
| `fit_nuts.py` | **NUTS via NumPyro** wrapping our JAX `log_posterior`. `posterior_samples()` is the auto-fallback: Laplace where PD, NUTS otherwise. |
| `fit_eb_pool.py` | **Empirical-Bayes pool on all three halflives.** One-step σ + iterated μ (avoids the iterative-σ collapse pathology — see `external_docs/pgm_tricks.md`). `Pool` namedtuple (6 fields) + `fit_eb_pool()` + `fit_independent()` baseline. |
| `ppc.py` | **Posterior predictive checks.** Vectorized forward simulator + discrepancy stats (skew/kurt/mid-third of died-attempt taus) + Bayes p-value. Catches haz1-on-beta2-truth misspec at p≈0.01. |
| `generate_synthetic_learning_v07.py` | Synth generator. `generate(...)` for single-segment, `generate_multi_segment*()` for population-varying truth. Supports `hazard_truth='haz1'` or `'beta2'`. |
| `phase2_recovery_v07.py` | Recovery experiment runner (numpy fit). The "does V07 hit pass criteria" gate. |
| `pgm_inspect_v07.py` | Interactive PGM inspector + knob console; renders HTML + mermaid sidecar. |
| `tests/` | pytest suite: 80 tests covering curve identities, JAX≡numpy, joint prior + autodiff Hessian regression, fit pipeline, PPC, NUTS, EB pool, v1 API shape, and the validation harness. |

### Shared infrastructure (numpy-era, still used)

| File | What |
|---|---|
| `model.py` | Static (no learning) segment model: clean_dist, log_lik_survived/died, m_clear. Composed by V07 numpy code. |
| `hazard.py` | Hazard model interface and impls (haz1 piecewise, beta mixture). Used by `beta_compare.py` and the inspector. |
| `inference.py` | numpy MAP/Laplace helpers. Used by inspector and phase2 recovery. |
| `binning.py` | Bin-edge pickers (uniform, quantile, kde_valleys, k-means). Used by `beta_compare.py`. |
| `beta_compare.py` | Offline tool: beta-mixture K sweep on a single dataset with Bayes-evidence model averaging. **v2-scope — parked.** Default fixtures `test_data.tsv` / `test_synth.tsv` at the repo root exist only to support this script. |
| `config.py` | Cross-cutting constants (RESPAWN_MS, QUAD_POINTS, etc.). |

### Data layer

| File | What |
|---|---|
| `data/schema.sql` | Minimal sqlite table (`attempts`). |
| `data/db.py` | `init`, `export` CLI. Insert via your own pipeline. |

### v1 handoff surface (the porter walks in here)

| File | What |
|---|---|
| `V1_ESSENCE.md` | Integration contract: what ships, what's parked, what bands to trust. **Read first.** |
| `external_docs/api_contract.md` | JSON wire-format spec for the SpinLab boundary. |
| `api.py` | Live serializer: `fit_segment` / `refit_segment` / `fit_pool` that emit the contract payload. |
| `validation/` | Frozen TSV inputs + expected JSON payloads + `regenerate.py`. Porters validate their reimplementation against these. |

### Docs

| File | What |
|---|---|
| `external_docs/segment_model.md`, `learning_curves.md` | Model spec. Authoritative for the math. |
| `external_docs/segment_model_hyperpriors.md` | Hyperprior layer spec (the EB pool's role). |
| `external_docs/pgm_tricks.md` | Glossary of PGM patterns + gotchas: identifiability, ridges, joint priors, partial pooling, Neal funnel, EB σ collapse, smooth-once-isn't-smooth-twice Hessian bombs. |
| `external_docs/nonstationary_shape.md` | Moving-peaks design proposal. **v2-scope — parked.** |
| `external_docs/reports/` | Snapshots: session findings, comparison results, handoff status. |
| `CHANGELOG.md` | Dated session-by-session notes. |

### Experiments / scratch

| Dir | What |
|---|---|
| `tmp/` | Throwaway scripts: benches (`bench_full_streaming.py`, `bench_nuts.py`), comparisons (`compare_haz1_vs_beta2.py`), demos (`ridge_demo.py`, `demo_eb_pool.py`), diagnostics (`diagnose_hessian_cusp.py`, `calibration_probes.py`), budget probes (`eb_pool_budget.py`), PPC drivers (`ppc_synth_check.py`). Treat as scratch — anything important gets promoted to `tests/` or `external_docs/`. |

## Public API surface

`import segments_v07 as sv` exposes:

```python
# Indices into the length-10 theta vector (haz1)
sv.IDX_LOG_BPT, sv.IDX_LOG_SF_INF, sv.IDX_LOG_SF_1, sv.IDX_LOG_HALFLIFE_SF, ...

# Pure-math eval (JAX, haz1)
sv.learning_curve_at_n(theta, n)
sv.log_prior(theta)               # accepts 6 pool kwargs (sf/ssp/alpha × mean/sigma)
sv.log_likelihood(theta, time_ms, is_died)
sv.log_posterior(theta, time_ms, is_died)
sv.halflife_sigma(A)
sv.initial_theta()

# Data wrangling
sv.data_to_arrays(attempts)        # list of (outcome, time_ms) -> jax arrays
sv.bucket_size(n)
sv.pad_to(time_ms, is_died, n_max)

# Fit (haz1; for beta2 use fit_jax_beta2 directly)
sv.warm_init_from_data(time_ms, is_died) -> theta_init
sv.prewarm_buckets()                                  # one-time JIT compile
sv.find_map(time_ms, is_died, theta_init=...,
            halflife_sf_pop_mean=..., halflife_sf_pop_sigma=...,
            halflife_ssp_pop_mean=..., halflife_ssp_pop_sigma=...,
            halflife_alpha_pop_mean=..., halflife_alpha_pop_sigma=...)
                                                       -> (theta_map, info)
sv.laplace_posterior(theta_map, time_ms, is_died, ...) -> (cov, ok)
sv.sample_laplace(theta_map, cov, n_samples=2000)      -> samples

# Multi-segment empirical-Bayes pool (all three halflives)
sv.fit_independent(segments)               -> (maps, infos)
sv.fit_eb_pool(segments, max_iter=10)      -> (pool, maps, history)
# pool = sv.Pool(log_halflife_sf_mean, log_halflife_sf_sigma,
#                log_halflife_ssp_mean, log_halflife_ssp_sigma,
#                log_halflife_alpha_mean, log_halflife_alpha_sigma)
# pool.as_kwargs()  ->  6-kwarg dict ready to splat into find_map

# Numpy reference (offline tools, cross-validation)
sv.numpy_reference.log_prior, .log_posterior, .sample_prior, ...
```

For beta2: `import fit_jax_beta2 as f2; import learning_model_v07_beta2_jax as lm2` —
parallel API to the haz1 surface, 15-param theta. For NUTS / honest bands on
ridge cases: `import fit_nuts; fit_nuts.posterior_samples(...)` chooses
Laplace-or-NUTS automatically.

## Performance

On CPU JAX (after `prewarm_buckets()`), single-segment at N=500:

| Op | haz1 | beta2 |
|---|---|---|
| Cold MAP fit | ~10–300 ms | ~100–500 ms |
| Streaming refit (`fit_jax.find_map` alone, warm-started) | p50 8ms, p99 35ms | ~30 ms p50 |
| Streaming refit (`api.refit_segment` full payload, default live cadence) | **p50 ~15 ms, p99 ~45 ms** | n/a |
| Laplace posterior (Hessian + invert + sample 2000) | ~600 ms | ~2 s |
| NUTS posterior (500 warmup + 1000 samples × 2 chains, MAP-init) | ~160 s | ~450 s |

Reference: numpy + Nelder-Mead takes ~70s for haz1 N=500 MAP. JAX is ~700× faster.
NUTS is for fallback / honest bands when Laplace fails PD, not the streaming path.

GPU JAX (`pip install jax[cuda12]`) is the natural next step for batched
multi-segment fits via `jax.vmap`.

## Known limitations

See [`V1_ESSENCE.md`](V1_ESSENCE.md) "Caveats — what NOT to claim" for the
authoritative v1 list (log_hl_ssp bands, ssp ID weakness, find_map plateau,
PPC scope). Items here are repo-internal developer notes that don't affect
the v1 integration contract:

- **EB pool is point-estimated (EB-lite), not full Bayes.** One-step σ +
  iterated μ — see `external_docs/pgm_tricks.md` "EB σ collapse". For full
  hyperparameter-uncertainty propagation, wrap `log_posterior` in a NumPyro
  plate over segments.
- **CPU-only.** GPU bake-in via `jax[cuda12]` is a follow-on.
- **Label switching on beta2 (v2-parked).** The K=2 mixture posterior has
  2!=2 modes. Warm-init breaks symmetry; canonicalize via
  `tmp/compare_haz1_vs_beta2.py:canonicalize_beta2_shape` if needed during
  v2 work.

## Development

```bash
# Run tests
.venv/Scripts/python -m pytest tests/ -v

# Run benches
.venv/Scripts/python tmp/bench_full_streaming.py
.venv/Scripts/python tmp/bench_nuts.py             # ~15 min, NUTS-heavy

# Identification-budget probe (EB pool gains)
.venv/Scripts/python tmp/eb_pool_budget.py

# Calibration probes (Hessian autodiff vs FD, PPC p-value uniformity, coverage)
.venv/Scripts/python tmp/calibration_probes.py --probe fast
.venv/Scripts/python tmp/calibration_probes.py --probe all

# 2x2 PPC misspec detection on haz1/beta2 truth
.venv/Scripts/python tmp/ppc_synth_check.py

# Re-render inspector after a model change
.venv/Scripts/python pgm_inspect_v07.py --fit-n 500 --seed 1 --out tmp/v07.html
```

When adding code: tests first, doc second. Prior choices and architectural
notes go in `external_docs/` so the next person doesn't have to reverse-engineer
them. Gotcha patterns (non-obvious failure modes like the |A| Hessian bug or
the EB σ collapse) go in `pgm_tricks.md`.

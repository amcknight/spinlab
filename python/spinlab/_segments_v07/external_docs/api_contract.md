# v1 API contract — what `segments` ships to SpinLab

The wire format between the `segments` model and SpinLab. SpinLab calls a
small set of Python entry points and gets back JSON-shaped dicts described
here. This is the integration surface; everything else in the repo is
implementation detail.

If you're a porter, this doc + [`V1_ESSENCE.md`](../V1_ESSENCE.md) is what
you implement. The validation harness (frozen TSV → expected JSON payload)
locks the numerics to this contract.

**Versioning.** Every payload carries `"schema": "segments-v1"` at the top
level. Breaking changes bump to `segments-v2`. Additive fields don't bump;
porters MUST ignore unknown fields rather than error on them.

## Entry points

Three calls. Each returns a dict that follows one of the payload schemas
below. They share a common envelope.

```python
from segments_v07 import fit_segment, refit_segment, fit_pool

# 1. Cold fit (first call for a segment, or after a long gap)
result = fit_segment(attempts, segment_id="w1-2-castle")

# 2. Streaming refit (one new attempt arrives; warm-start from prev MAP)
result = refit_segment(attempts, segment_id="w1-2-castle",
                       prev_result=last_result)

# 3. Pool fit (EB across many segments — typically a daily background job)
pool_result = fit_pool([
    {"segment_id": "w1-2-castle",  "attempts": [...]},
    {"segment_id": "w1-2-cannon",  "attempts": [...]},
    ...
])
```

All three call into the existing `fit_jax` / `fit_eb_pool` / `fit_nuts` /
`ppc` modules and serialize the result. The Python signatures are stable;
internals are free to change.

`attempts` is the schema from
[`reference/proposed_data_2`](reference/proposed_data_2/schema.sql): a
list of `{"outcome": "survived"|"died", "time_ms": int, ...}` ordered by
attempt number.

## Top-level envelope

Every payload has these fields:

```json
{
  "schema": "segments-v1",
  "kind": "segment_fit" | "pool_fit",
  "segment_id": "w1-2-castle",
  "n_attempts": 234,
  "model": "haz1",
  "wall_time_s": 0.043,
  "status": { ... },
  "result": { ... },
  "caveats": [ ... ]
}
```

| field | type | meaning |
|---|---|---|
| `schema` | string | wire-format version. Always `"segments-v1"` in v1. |
| `kind` | string | which result shape. `"segment_fit"` or `"pool_fit"`. |
| `segment_id` | string \| null | caller-supplied id; passed through. `null` on pool envelopes. |
| `n_attempts` | int | number of attempt rows the fit saw. |
| `model` | string | always `"haz1"` in v1. `"beta2"` is parked. |
| `wall_time_s` | float | total wall time spent in the call. For SLO tracking. |
| `status` | object | see "Status block" below. |
| `result` | object | the schema-specific body. See "Segment fit result" and "Pool fit result" below. |
| `caveats` | array of string | machine-readable caveat keys. See "Caveats" below. |

## Status block

Every payload carries a status block. The UI decides whether to render the
fit, widen bands, or surface a warning based on these flags.

```json
"status": {
  "converged": true,
  "band_source": "laplace",
  "laplace_pd": true,
  "ppc_tension": false,
  "fittable": true
}
```

| field | type | meaning |
|---|---|---|
| `converged` | bool | `info['converged']` from `find_map`. AND of scipy `success` and finite log-posterior. **If `false`, do not render bands or derived stats — flag the segment as un-fittable.** |
| `band_source` | string | `"laplace"` (PD-safe Hessian), `"nuts"` (Laplace fell back), or `"none"` (no bands; `converged` is false or sample budget was 0). |
| `laplace_pd` | bool | did the Hessian invert cleanly? `false` ⇒ NUTS was used. Informational; the active band source is `band_source`. |
| `ppc_tension` | bool | any PPC default stat had `p_two_sided < 0.01`. If true, treat MAP as a point estimate only; the bands aren't honest. |
| `fittable` | bool | shorthand: `converged && !ppc_tension`. UI should suppress this segment from leaderboards / comparison views when false, but MAP can still be shown with a warning. |

## Segment fit result

Body returned by `fit_segment` / `refit_segment`. The headline live-UI
numbers live in `derived`; everything else is supporting detail.

```json
"result": {
  "map":     { "log_theta": [...10 floats...], "natural": { ... } },
  "bands":   { ...per-latent log-space intervals... },
  "derived": { ... },
  "ppc":     { ... }
}
```

`map.log_theta` is the warm-start seed for the next `refit_segment`:
pass the previous payload back via `prev_result=` and the helper
extracts it. There is no separate `warm_start_seed` field — they would
be identical in v1.

### `result.map`

The 10-latent MAP estimate, in both log-space (canonical, what the math
operates on) and natural-space (display).

```json
"map": {
  "log_theta": [
    9.901, -2.302, -1.609, -1.204,
    -1.609, -1.204, -0.916,
    2.708,  2.708,  2.708
  ],
  "natural": {
    "bpt_ms":      20000.0,
    "sf_inf":      0.10,    "sf_1":     0.20,
    "ssp_inf":     0.20,    "ssp_1":    0.30,
    "alpha_inf":   0.30,    "alpha_1":  0.40,
    "halflife_sf": 15.0,    "halflife_ssp": 15.0, "halflife_alpha": 15.0
  }
}
```

**`log_theta` is the source of truth.** The 10 entries follow the V07
layout (indices in `segments_v07.IDX_LOG_*`). `natural` is `exp(log_theta)`
with the right field names for the UI. If `natural` and `log_theta`
disagree by more than 1e-6, that's a serialization bug — trust
`log_theta`.

### `result.bands`

Posterior intervals in log-space. Sourced from the Laplace covariance
(default) or NUTS samples (fallback). One entry per V07 latent, keyed by
the same names as the `IDX_LOG_*` constants. Natural-space bands are
`exp(p5)` and `exp(p95)` — left to the consumer to avoid a duplicate
serialization surface that can drift.

```json
"bands": {
  "log_bpt":          {"p5": 9.85, "p50": 9.90, "p95": 9.95},
  "log_sf_inf":       {"p5": -2.45, "p50": -2.30, "p95": -2.15},
  "...":              { ... },
  "log_hl_ssp":       null,
  "log_hl_alpha":     {"p5": 2.5, "p50": 2.7, "p95": 2.9}
}
```

**`null` means "band suppressed; show MAP only."** This is the
machine-readable signal for the V1_ESSENCE caveat #1 (`log_hl_ssp` bands
under-cover). The serializer emits `null` for `log_hl_ssp` whenever the
fit ran under a pool prior; under broad prior it emits the band with a
`ssp_band_inflated` caveat (band reported as widened by the calibration
factor). The UI should not show a "90% interval" label on a `null`
band.

### `result.derived`

Display stats — the numbers the SpinLab live UI actually shows.
Closed-form from `(map, bands)`.

```json
"derived": {
  "M_clear": {
    "median_ms": 31250.0,
    "p5_ms": 28900.0,
    "p95_ms": 35100.0
  },
  "death_rate_next": 0.21
}
```

| field | meaning |
|---|---|
| `M_clear.median_ms` | Posterior median of expected total wall-clock time to clear, **at the current attempt n** (`n = n_attempts + 1`). The headline number on the segment card. |
| `M_clear.{p5,p95}_ms` | 5/95 percentile band on `M_clear`, integrated over the joint posterior on `(sf, ssp, alpha)` at the current `n`. |
| `death_rate_next` | `1 - exp(-alpha(n_next))`. Probability that the *next* attempt dies. Shown as a percentage in the segment header. |

Curve-shaped outputs (M_clear over an n-grid, survival probability over
s) are intentionally not in v1 — SpinLab evaluates them client-side from
`map.log_theta` via `segments_v07.learning_curve_at_n`. If the UI later
needs them server-side, they're additive (non-breaking).

Adding a new derived stat is non-breaking (porters ignore unknown fields).
Removing or renaming one is breaking and bumps the schema.

### `result.ppc`

Posterior Predictive Check honesty signal. The four `DEFAULT_STATS` with
their Bayes p-values. UI uses `ppc_tension` (status block) as the
machine-readable signal; this object is for diagnostics surfaces.

```json
"ppc": {
  "died_rate":         {"obs": 0.32, "p_two_sided": 0.43},
  "died_tau_skew":     {"obs": 1.21, "p_two_sided": 0.51},
  "died_tau_kurt":     {"obs": 3.40, "p_two_sided": 0.39},
  "died_s_mid_third":  {"obs": 0.18, "p_two_sided": 0.44}
}
```

p-values near 0.5 mean "the model can reproduce the observed value." Near
0 or 1 means tension. The `ppc_tension` flag in `status` fires when any
of these has `p_two_sided < 0.01`. The full `sims` arrays from `run_ppc`
are NOT in the payload — too large; SpinLab can re-run `ppc.run_ppc`
itself if it wants histograms.

## Pool fit result

Body returned by `fit_pool`. Different shape — the result is a population
fit plus per-segment fits that share the pool prior.

```json
"result": {
  "pool": {
    "halflife_sf":    {"mean": 2.7,  "sigma": 0.6},
    "halflife_ssp":   {"mean": 2.8,  "sigma": 0.5},
    "halflife_alpha": {"mean": 2.9,  "sigma": 0.7},
    "n_segments_used": 8
  },
  "segments": [
    { "segment_id": "...", "status": {...}, "result": {... segment_fit body ...} },
    ...
  ]
}
```

`pool.{halflife_*}.mean` and `.sigma` are in log-space (consistent with
`log_theta`). Each entry in `segments[]` is a full segment-fit envelope
(minus the outer wrapper — just `status` + `result`). SpinLab can render
them independently or roll up via `pool` for cross-segment displays.

## Caveats

`caveats` is an array of stable string keys. The UI maps each key to a
user-visible message; the keys themselves are stable across schema
versions (additive only — keys are never removed).

| key | meaning |
|---|---|
| `unconverged` | `find_map` returned `info['converged']=false`. Do not show bands or derived stats. |
| `nuts_fallback` | Laplace was non-PD; bands came from NUTS. Bands are still honest; this is informational so the UI can show a small badge if desired. |
| `ssp_band_suppressed` | `log_hl_ssp` band is `null` (pool fit) — see V1_ESSENCE caveat #1. Show MAP only on that latent. |
| `ssp_band_inflated` | `log_hl_ssp` band shown but widened post-hoc by the calibration factor (broad-prior fit). |
| `ppc_tension_died_rate` | PPC `died_rate` p-value < 0.01. |
| `ppc_tension_died_tau_skew` | PPC skewness p-value < 0.01. |
| `ppc_tension_died_tau_kurt` | PPC kurtosis p-value < 0.01. |
| `ppc_tension_died_s_mid_third` | PPC mid-third p-value < 0.01 — usual signal for hazard-shape misspec (i.e. moving-peaks territory). |
| `low_n` | `n_attempts < 22`. Bands tend to be Hessian-ridge-y; expect `nuts_fallback`. |

The `ppc_tension_*` keys are specific so that the UI can call out *which*
discrepancy fired. The aggregate `status.ppc_tension` bool is the
cheap-to-check rollup.

## Refresh cadences

Which entry point SpinLab uses when. Cribbed from
`memory/product_role_metrics.md`.

| Cadence | Call | Budget | Notes |
|---|---|---|---|
| Live (per attempt) | `refit_segment` with `prev_result=last` | p50 ~15 ms, p99 ~45 ms (CPU, N up to 500, post-prewarm) | Skips NUTS. On non-PD Laplace, returns Laplace bands with `laplace_pd: false` + `low_n` — UI widens heuristically. |
| Periodic (every K attempts, K≈50) | `refit_segment` with `nuts_on_nonpd=True` | p50 ~50 ms; NUTS path 1–10 s | Refreshes honest bands; `band_source` flips to `"nuts"` when needed. |
| Post-session | `fit_segment` + `run_ppc` from scratch | hundreds of ms | Cold fit with full PPC sample budget; offline audits. |
| Background (daily) | `fit_pool` over all the player's segments | minutes | Pool update; per-segment MAPs shrink toward population. |

Numbers above assume `sv.prewarm_buckets()` was called at process start
(~10 s, one-time). Without prewarm, first hit at each bucket size
recompiles (~200–400 ms each).

Cold `refit_segment` (no `prev_result`) is the same path as `fit_segment`.

## What's NOT in the payload

- **Hessian / covariance matrix** — 10×10, not needed for the UI. Callers
  who want joint uncertainty call `fit_jax.laplace_posterior` directly.
- **NUTS samples / PPC sims** — bandwidth-hostile. The summary stats in
  `bands` and `ppc` are what the UI needs; re-run `ppc.run_ppc` for sims.
- **The pool's `history`** — internal EB iteration trace.
- **`beta2` shape params** — parked. The `model: "haz1"` discriminant
  tells the porter not to expect them.

## Worked examples

### Cold fit, well-identified

```json
{
  "schema": "segments-v1",
  "kind": "segment_fit",
  "segment_id": "w1-2-castle",
  "n_attempts": 234,
  "model": "haz1",
  "wall_time_s": 0.31,
  "status": {
    "converged": true, "band_source": "laplace", "laplace_pd": true,
    "ppc_tension": false, "fittable": true
  },
  "result": {
    "map":  { "log_theta": [9.901, -2.302, ...], "natural": { ... } },
    "bands": { "log_bpt": {"p5": 9.85, "p50": 9.90, "p95": 9.95}, "...": {} },
    "derived": {
      "M_clear":         { "median_ms": 31250, "p5_ms": 28900, "p95_ms": 35100 },
      "death_rate_next": 0.21
    },
    "ppc": { "died_rate": {"obs": 0.32, "p_two_sided": 0.43}, "...": {} }
  },
  "caveats": []
}
```

### Streaming refit, low N triggers NUTS

```json
{
  "schema": "segments-v1",
  "kind": "segment_fit",
  "segment_id": "w8-bowser",
  "n_attempts": 18,
  "model": "haz1",
  "wall_time_s": 1.7,
  "status": {
    "converged": true, "band_source": "nuts", "laplace_pd": false,
    "ppc_tension": false, "fittable": true
  },
  "result": {
    "map":  { ... },
    "bands": { ... },
    "derived": {
      "M_clear":         { "median_ms": 198400, "p5_ms": 145000, "p95_ms": 312000 },
      "death_rate_next": 0.39
    },
    "ppc": { ... }
  },
  "caveats": ["low_n", "nuts_fallback"]
}
```

### PPC tension — model misfit flagged

```json
{
  "schema": "segments-v1",
  "kind": "segment_fit",
  "segment_id": "w7-2-fortress",
  "n_attempts": 312,
  "model": "haz1",
  "wall_time_s": 0.42,
  "status": {
    "converged": true, "band_source": "laplace", "laplace_pd": true,
    "ppc_tension": true, "fittable": false
  },
  "result": {
    "map":  { ... },
    "bands": { ... },
    "derived": { ... },
    "ppc": {
      "died_rate":        {"obs": 0.42, "p_two_sided": 0.40},
      "died_tau_skew":    {"obs": 2.10, "p_two_sided": 0.18},
      "died_tau_kurt":    {"obs": 8.40, "p_two_sided": 0.34},
      "died_s_mid_third": {"obs": 0.42, "p_two_sided": 0.003}
    }
  },
  "caveats": ["ppc_tension_died_s_mid_third"]
}
```

UI render: show MAP and derived stats with an "unmodeled shape detected"
badge. Bands suppressed for trust reasons (the model can't generate
data with this stat distribution, so its uncertainty bands are
extrapolating from a misspecified posterior).

### Pool fit, 3 segments

```json
{
  "schema": "segments-v1",
  "kind": "pool_fit",
  "segment_id": null,
  "n_attempts": 712,
  "model": "haz1",
  "wall_time_s": 14.2,
  "status": {
    "converged": true, "band_source": "laplace", "laplace_pd": true,
    "ppc_tension": false, "fittable": true
  },
  "result": {
    "pool": {
      "halflife_sf":    {"mean": 2.71, "sigma": 0.58},
      "halflife_ssp":   {"mean": 2.83, "sigma": 0.42},
      "halflife_alpha": {"mean": 2.90, "sigma": 0.71},
      "n_segments_used": 3
    },
    "segments": [
      { "status": { ... }, "result": { ..., "bands": { "...": {}, "log_hl_ssp": null, "...": {} } } },
      { ... },
      { ... }
    ]
  },
  "caveats": ["ssp_band_suppressed"]
}
```

The top-level `caveats` aggregates per-segment caveats (deduped). The
top-level `status` is the AND/OR rollup: `converged = all converged`,
`fittable = all fittable`, `ppc_tension = any ppc_tension`,
`band_source` = `"nuts"` if any segment fell back else `"laplace"`.

## Stability promise

Within `"schema": "segments-v1"`:

- Field names, types, and semantics frozen.
- Adding fields (new caveats keys, new `derived` stats) is allowed.
- Removing or renaming fields is NOT allowed — that bumps to v2.
- Numerical drift: bit-for-bit reproducibility is NOT guaranteed across
  patch versions of `segments`. Within a fixed patch version the same
  inputs produce the same outputs cross-process if JAX JIT compilation
  order is held constant — see `validation/regenerate.py` for the
  established import order. The validation harness pins JSON values to
  ~5 sig figs (`map`/`derived`) and ~4 sig figs (`bands`/`ppc`). See
  [`tests/test_validation_harness.py`](../tests/test_validation_harness.py)
  for the tolerance budget.

The porter's job is to reproduce these numerics within the harness
tolerance. Anything tighter is non-binding.

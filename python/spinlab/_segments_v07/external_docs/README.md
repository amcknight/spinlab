# external_docs

Specs and reference for the speedrun segment model. Implementation
lives in the parent directory; this is the design surface.

## Reading order (cold start)

1. **[segment_model.md](segment_model.md)** — base model. The
   generative story, the likelihood, the primitives, the Lognormal
   prior choices. No learning, no hierarchy — the conceptual
   foundation.
2. **[learning_curves.md](learning_curves.md)** — V07 extension:
   `slop_frac`, `slop_spread`, `α` evolve over attempts with
   `(θ_1, θ_∞, halflife)` per param. The model that's actually
   implemented today.
3. **[segment_model_hyperpriors.md](segment_model_hyperpriors.md)** —
   what's pooled across segments and what isn't. v1 scope is the
   three halflives via empirical Bayes. Game-level + difficulty
   explicitly deferred.
4. **[nonstationary_shape.md](nonstationary_shape.md)** — *proposal,
   not v1*: how to let the beta-mixture hazard shape drift across
   attempts (moving peaks). State-space + Student-t RW; addresses the
   `moving_peaks` open concern.
5. **[pgm_tricks.md](pgm_tricks.md)** — glossary of PGM patterns
   (identifiability, ridges, joint priors, partial pooling, Neal
   funnel, empirical Bayes). Reference when reading the model docs.

## What's where

```
external_docs/
├── README.md                          (this file)
├── api_contract.md                    v1 wire format for SpinLab (segment_fit / pool_fit JSON)
├── segment_model.md                   base model (fixed skill)
├── learning_curves.md                 V07 extension (what's implemented)
├── segment_model_hyperpriors.md       hierarchical pooling (v1 = halflives only)
├── nonstationary_shape.md             PROPOSAL: drifting beta-mixture peaks
├── pgm_tricks.md                      glossary
├── reports/                           dated snapshots from prior sessions
└── reference/                         external (SpinLab schema)
```

For the integration contract specifically, read
[`../V1_ESSENCE.md`](../V1_ESSENCE.md) (Python API surface) and
[`api_contract.md`](api_contract.md) (JSON wire format) together.

## Reports vs main docs

**Main docs** describe what the model *is* — the design intent that
the implementation should match.

**Reports** are time-stamped snapshots: comparisons, benchmarks,
status updates, exploration notes. They capture what we *learned* in
specific sessions. Useful for tracing why a decision was made; not
authoritative on current model design.

Current reports:

| Report | What |
|---|---|
| [reports/2026-05-15_beta_mixture_decisions.md](reports/2026-05-15_beta_mixture_decisions.md) | Beta-mixture hazard exploration; K=2 chosen, `haz_floor` rejected |
| [reports/2026-05-16_learning_curves_findings.md](reports/2026-05-16_learning_curves_findings.md) | Plan V07 (reparameterize, tighten priors, knob console); plans HYPER / NUTS / JAX named |
| [reports/2026-05-16_speed_and_techniques.md](reports/2026-05-16_speed_and_techniques.md) | JAX rewrite findings (~700× cold-fit speedup); first hyperprior recommendation |
| [reports/2026-05-16_haz1_vs_beta2_findings.md](reports/2026-05-16_haz1_vs_beta2_findings.md) | 2×2 comparison (haz1/beta2 × haz1-truth/beta2-truth); NLL gaps, PD failure rates, M_clear robustness |
| [reports/2026-05-17_handoff_status.md](reports/2026-05-17_handoff_status.md) | Pre-handoff state + decisions + agreed plan-forward sequence |
| [reports/online_density_spec.md](reports/online_density_spec.md) | Streaming-EM alternative for live density updates; explored, never implemented |

## What's NOT in this folder

- **Implementation code** — parent directory. See `segments_v07.py` for
  the public API; `README.md` at repo root for the file map.
- **CHANGELOG** — repo root (`CHANGELOG.md`).
- **Session memory / behavior calibration** —
  `.claude/projects/c--Users-thedo-git-segments/memory/`. Per-user
  context, not project documentation.
- **Tests** — `tests/` in parent directory.

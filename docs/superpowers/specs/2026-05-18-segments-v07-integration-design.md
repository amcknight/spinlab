# V07 Segments Model Integration — Phased Plan

**Date:** 2026-05-18
**Status:** Brainstorm complete; OQ1 resolved 2026-05-18 (see "Resolution" below); spec drafted for review.
**Branch:** TBD (likely `feature/segments-v07-integration` or similar).

## Resolution log

- **2026-05-18 — OQ1 (`s_at_death` source) closed as a non-question.**
  Walking the prototype confirmed the v1 (and v2) JAX/numpy model consumes
  only `(outcome, time_ms)` rows — see `segments_experiment/api.py`'s
  `_attempts_to_arrays`, `learning_model_v07_jax.data_to_arrays`, and the
  PPC `stat_died_s_middle_third` (which derives `s_proxy = tau /
  quantile_99(tau)`, i.e. time-fraction). The phrase "death lands at fraction
  `s ∈ [0, 1]` through the segment" in `V1_ESSENCE.md` refers to *normalized
  time-into-attempt*, not measured spatial progress. No new SMW WRAM
  address is needed; the Phase 0 schema drops the `s_at_death` column;
  the capture pipeline does not need a new poke read. The Mismatch-1
  refactor (episode → per-event attempts) still stands — just simpler.

## Goal

Integrate the `segments_experiment/` prototype — a Bayesian probabilistic
segment model with full posterior bands, PPC honesty checks, and an EB
pool across segments — into SpinLab. The prototype is a substantially
more honest model than the current Kalman/ExpDecay/RollingMean trio, but
its data unit and output shape don't fit SpinLab's existing pipeline.
This spec lays out the path from "the prototype lives in a sibling dir
and we've never run it on real SpinLab data" to "we've decided how it
drives the live UX."

## Background — What the prototype actually is

A V07 hazard-1 model in numpy + JAX. 10 latents per segment:
`(bpt, sf_inf, ssp_inf, alpha_inf, sf_1, ssp_1, alpha_1, hl_sf, hl_ssp, hl_alpha)`.
Each of the three execution latents (`sf` = slop fraction, `ssp` = slop
spread, `alpha` = death rate) evolves over attempts via a decaying
learning curve. JAX-jitted L-BFGS-B MAP fit; Laplace covariance with
NUTS fallback; PPC discrepancy stats; EB pool on the three halflives
across segments. Locked v1 JSON contract
([`segments_experiment/external_docs/api_contract.md`](../../../segments_experiment/external_docs/api_contract.md))
and a frozen validation harness (TSV → expected JSON, 80 tests).

Performance: p50 ~15ms streaming refit per attempt on CPU JAX. Cold MAP
fit ~10–300 ms. NUTS fallback 1–10 s when Laplace fails PD. JAX prewarm
~10 s at process start.

The integration contract is [`segments_experiment/V1_ESSENCE.md`](../../../segments_experiment/V1_ESSENCE.md);
read that for what to trust and what to suppress in the v1 model output.

## Background — Why this is a hard integration

Two structural mismatches between the prototype and current SpinLab.

### Mismatch 1 — The attempt unit

The prototype's "attempt" is one discrete try at the segment, ending in
**either**:

- `died` with a `time_ms` spent dying, **or**
- `survived` with the `time_ms` to clear.

(The "fraction `s ∈ [0,1]` through the segment" phrasing in
`V1_ESSENCE.md` is *normalized time-into-attempt*, derived inside the
model from `time_ms` — not a measured spatial position. The data the
prototype consumes is just `(outcome, time_ms)`.)

SpinLab's current `Attempt` record is an *episode*: a sequence of zero
or more deaths followed by either a death-out or a completion, with
aggregate `time_ms` (total including deaths), `clean_tail_ms` (last
death to finish), and `deaths` (count). Multiple model-attempts collapse
into one SpinLab attempt.

This is not a serialization difference. The current SpinLab data model
literally does not contain per-death timing — the information was never
captured.

### Mismatch 2 — The output shape

`ModelOutput { total: Estimate, clean: Estimate }` where `Estimate` is
`{expected_ms, ms_per_attempt, floor_ms}`. That's a smoother's output —
a current best guess and a per-attempt drift. The prototype produces a
10-D MAP, per-latent posterior bands (Laplace or NUTS), four PPC stats,
and derived stats with bands (`M_clear.{p5,median,p95}_ms`,
`death_rate_next`). The value of the prototype *is* the bands and the
PPC. Shoehorning it into the current `Estimate` shape throws away
exactly what we built it for.

## Direction

**Phased: don't commit to a B-vs-C UI shape before we've seen real
fits.** Both paths need Phases 0 and 1. Phase 2 generates the evidence
that lets us pick a UI shape in Phase 3 without speculating.

Decided up front in brainstorming (2026-05-18):

- Refactor SpinLab's data model to **two-event-ending attempts** (one
  `died`-or-`survived` event per row), not just capture-pipeline
  augmentation. The current episode-shaped data is itself an aggregation
  artifact.
- **Drop existing attempt data.** Andrew confirmed it's expendable; not
  worth the lossy-backfill mess. Existing reference runs / segments /
  state stay; the attempts table starts empty under the new schema.
- **Existing estimators don't have to die.** They can run as
  "smoothers" alongside the prototype's "generative model" view.
  Decision deferred to Phase 3.

## The decision framework (this is the central thing)

We **cannot** responsibly choose Path B (parallel insights surface) vs.
Path C (full overhaul) today. Whatever we pick is shaped by what the
prototype's fits actually look like on real SpinLab data — and we have
none. The path the spec proposes earns that decision before forcing
it.

The Phase 2 questions whose answers select B vs C:

1. **Does the model fit?** PPC tension on real segments. If a meaningful
   fraction (>~20%) PPC-fail, the band story is moot until we
   understand why.
2. **Are bands informative *to the player*?** Does seeing M_clear 31s ±
   4s vs ± 0.4s change practice behavior? If no, the rich output isn't
   worth a dedicated UI surface (Path A as a 4th estimator might
   suffice).
3. **Does the learning curve identify?** On segments already at
   asymptote, half the model collapses. Pool helps; need to see it.

Decision rubric (locked):

| Phase 2 evidence | Phase 3 path |
|---|---|
| Bands tight + PPC clean + curves identifiable on most segments | **C-ish:** rebuild the model tab around segments-v1; legacy estimators tucked away as a "smoothers" view. |
| Bands often wide / PPC sometimes fires / curves often flat | **B-ish:** new insights tab for the rich stuff; keep Kalman as the live driver because it's robust. |
| `M_clear.median_ms` materially better than Kalman's `expected_ms` for allocator purposes | **+ A:** pipe the prototype in as a fourth estimator regardless of UI choice above. |

## Scope

### Phase 0 — Data model refactor (event-level attempts)

**In:**

1. New schema. `attempts` table redefined:
   - `outcome` enum: `survived | died`
   - `time_ms` int (time spent on this single attempt)
   - `episode_id` text (groups consecutive attempts from the same
     player run; UI grouping only, model ignores)
   - `segment_id`, `created_at`, `source`, `chosen_allocator`,
     `invalidated` carry over.

   (No `s_at_death` column. The v1 model derives death-time-fraction
   from `time_ms` internally; see Resolution log.)
2. Migration: a new `python/spinlab/db/migrations/NNNN_*.sql` that
   drops the old `attempts` table and creates the new one. (Old data
   confirmed expendable.) Existing `model_states` rows deleted (table
   schema unchanged): they hold per-estimator numerical state that was
   integrated from the old episode-shaped attempt stream; the legacy
   adapter rebuilds them from the new event-level attempts on next
   `rebuild_all_states`.
3. Capture pipeline changes:
   - Death detection (already happens for the `deaths` counter) emits
     one `attempts` row with `outcome=died` and `time_ms = elapsed since
     last spawn/respawn`.
   - Segment completion emits one `attempts` row with
     `outcome=survived` and `time_ms = elapsed since last spawn/respawn`.
   - This means `time_ms` semantics change: it is no longer the
     episode total — it is per-event elapsed time, summed by the
     legacy adapter for the existing estimators.
   - `episode_id`: a UUID minted at episode start; all attempts under
     it share it.
4. Adapter for legacy estimators. Kalman/ExpDecay/RollingMean keep
   their current math; they receive an `EpisodeRecord` view derived from
   v2 attempts (aggregating `time_ms` across the episode's deaths +
   completion). Existing `AttemptRecord` shape goes away.
5. Tests:
   - DB migration test (drop old, new schema present).
   - Capture-pipeline integration test: a multi-death episode emits N+1
     attempt rows with the correct outcomes and per-event `time_ms`
     values.
   - Legacy estimator test: same numerical output as today when fed an
     episode-equivalent attempt sequence.

**Out (deferred):**

- Backfill of legacy data. Confirmed expendable.
- Any model integration. Phase 0 is data-shape only.

### Phase 1 — Silent fit pipeline

**In:**

1. Vendor `segments_experiment/` into the repo as
   `python/spinlab/segments_model/` (or sibling, see open question 2).
   Pinned `requirements.txt` additions for JAX + NumPyro + scipy.
2. JAX prewarm hook in `spinlab dashboard` startup
   (~10s, one-time per process).
3. `segment_fits` table. Stores the v1 JSON payload returned by
   `segments_v07.fit_segment` / `refit_segment` per (segment_id,
   fit_kind, created_at). Schema:
   ```
   (id, segment_id, kind text, n_attempts, fitted_at, payload_json,
    band_source, ppc_tension, fittable)
   ```
4. Wiring: after every persisted `attempts` row, kick off a
   `refit_segment` for that segment using the previous payload as warm
   start. Off the request path — fire-and-forget on a worker thread or
   simply inline (15ms is acceptable).
5. Pool job: `fit_pool` over all the game's segments where
   `n_attempts ≥ POOL_MIN_PER_SEGMENT` (5 from V1_ESSENCE). Triggered
   manually (`spinlab fit-pool`) in Phase 1; daily cron in Phase 2 if
   the manual run looks useful.
6. **No UI changes.** **No allocator changes.** Allocator continues
   reading the existing `ModelOutput`s from Kalman/ExpDecay.
7. Tests:
   - Vendor-and-prewarm smoke test (JAX boots, prewarm completes).
   - End-to-end: insert a synthetic event-level attempts sequence,
     verify a `segment_fits` row appears with valid v1 payload shape
     (use the prototype's validation harness as the schema check).
   - Pool job test (5+ segments → pool row written).

**Out:**

- UI surfaces (Phase 3).
- Allocator integration (Phase 3).
- GPU JAX (`jax[cuda12]`) — CPU is enough for v1 and prevents the
  install footprint from doubling.

### Phase 2 — Look at it

**In:**

1. CLI: `spinlab fit show <segment_id> [--latest|--history]` dumps the
   v1 JSON payload, pretty-printed.
2. Renderer: a single self-contained HTML page generator that takes a
   payload and produces a learning-curve plot + band plot + PPC summary.
   Static HTML (no FastAPI integration yet). Vendor-render via the
   prototype's existing `pgm_inspect_v07.py` patterns if reusable;
   otherwise a thin matplotlib/plotly wrapper.
3. Andrew picks 5–10 representative segments (mix of high-N / low-N /
   well-learned / actively-learning) and inspects.
4. Answers the three Phase 2 questions; writes findings into
   `docs/superpowers/specs/YYYY-MM-DD-segments-v07-phase2-findings.md`.

**Out:** integration into the live dashboard UI. Phase 2 is a static
audit, not a feature.

### Phase 3 — UI decision, informed

Spec'd separately after Phase 2 findings. The decision rubric above
selects which sub-spec we write (B-ish, C-ish, +A, or some combination).

### Explicitly NOT in v1 (any phase)

- **beta2** model (parked in the prototype itself).
- **Backfill** of legacy attempt data.
- **GPU JAX**.
- **Game-level / global hyperprior**. Single-game scope per V1_ESSENCE.
- **Within-segment shape drift / moving peaks**. v2 of the *model*,
  not just of the integration.
- **Per-attempt rendering** in the dashboard. Phase 1 keeps the dash
  unchanged.

## Architecture sketch

```
        capture pipeline
              │
              ▼
        attempts table (event-level: outcome, time_ms, episode_id)
         │            │
         ▼            ▼
  legacy adapter   segments_v07.refit_segment
  (episode roll-up)        │
         │                 ▼
         ▼            segment_fits table (v1 JSON payload)
  Kalman / ExpDecay         │
  / RollingMean             ▼
         │            CLI / static HTML renderer (Phase 2)
         ▼
  ModelOutput (current shape)
         │
         ▼
  allocator + live dashboard (unchanged through Phase 2)
```

After Phase 3 the right edge of this diagram rewires. Until then both
trees coexist.

## Open questions

1. ~~Where does `s_at_death` come from?~~ **Resolved 2026-05-18** —
   not a question; the prototype consumes only `(outcome, time_ms)` and
   derives the time-fraction `s` internally. See Resolution log.
2. **Vendor location for `segments_experiment/`.** Options:
   - `python/spinlab/segments_model/` (in-package vendor).
   - `python/spinlab/_segments_v07/` (underscore-prefixed, signals
     "vendored, treat as opaque").
   - Sibling package in a top-level `vendor/` directory, imported via
     the editable install.
   - Convert to a real git submodule pointing at a separate repo.
   Recommend in-package vendor with the underscore prefix; the
   prototype is small enough (~3k lines) that submodule overhead beats
   the simplicity payoff. Resolves at Phase 1.
3. **Pool job orchestration.** Inline trigger after N new attempts?
   Cron? APScheduler? Resolves at Phase 1; cron is the cheapest if
   `spinlab dashboard` is the long-running process.
4. **`episode_id` lifetime.** Minted on segment entry? On reset?
   On level load? Affects UI grouping more than the model. Resolves
   at Phase 0.
5. **What counts as "elapsed since last spawn/respawn" in mixed
   episodes?** A practical follow-up exposed by the OQ1 resolution:
   when an episode has N deaths + 1 clear, the legacy estimators
   currently sum on the *episode total*. The per-event `time_ms`
   semantics need a clear definition of "respawn" — does it include
   the death animation and fade-in, or only post-respawn frames? The
   prototype uses `tau = time_ms - RESPAWN_MS` internally, so SpinLab's
   `time_ms` should be the post-respawn-to-event-end window with
   `RESPAWN_MS` (or equivalent) consistent with what the prototype's
   `config.RESPAWN_MS` expects. Resolves at Phase 0 design step
   (probably "include the respawn animation, match prototype's
   `RESPAWN_MS` constant").

## Cost / time estimate (very rough)

- Phase 0: ~2–4 sessions (schema migration is small; capture pipeline
  per-event emission + legacy estimator adapter is the bulk; reduced
  from the original 3–5 estimate now that the spatial-`s` work is
  dropped).
- Phase 1: ~2–4 sessions (vendor + wire + pool job + tests; the
  prototype itself is already tested).
- Phase 2: ~1–2 sessions (CLI + renderer + inspection).
- Phase 3: spec'd separately after Phase 2.

Total to the decision point: ~5–10 sessions.

## Risks

- **The legacy adapter breaks the existing estimators in non-obvious
  ways.** Mitigation: golden-output test that re-fits the current
  estimators against a known episode and pins their output to today's
  values.
- **`time_ms` semantics mismatch with the prototype's `RESPAWN_MS`
  convention** (e.g., we include the death-animation frames and the
  model expects post-respawn only, or vice versa). Mitigation: pin
  what SpinLab captures and reconcile with `segments_v07.config.RESPAWN_MS`
  on the Phase 1 boundary (the `_attempts_to_arrays` adapter is where
  the subtraction happens, so an offset bug there will show as a
  systematic shift in `bpt` MAPs). Surface via a unit test that fits a
  known-clear-time synth and checks `bpt_ms` lands within tolerance.
- **JAX install footprint** breaks the dev/sandbox bootstrap.
  Mitigation: pin compatible versions and add to
  `scripts/bootstrap-sandbox.sh`; verify on a clean clone before
  declaring Phase 1 done.
- **PPC universally fires on real data** (e.g., the haz1 model is
  systematically misspec'd for SMW). This would force a Phase 2
  re-spec rather than a Phase 3 UI decision — but better to learn that
  with silent fits than after a UI rewrite. The phasing already absorbs
  this risk.

## Resume cue

OQ1 is closed (see Resolution log). Next gating decision is **open
question 5** — defining the exact `time_ms` window per event (death,
clear) and reconciling with the prototype's `config.RESPAWN_MS`. That's
the last design-level call before the Phase 0 implementation plan can
be written. The Phase 0 plan itself is the next concrete artifact:
schema migration + capture-pipeline emission + legacy-estimator
adapter + golden-output regression test. After that, Phase 1 is the
vendor-and-wire pass.

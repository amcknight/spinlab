---
date: 2026-06-04
focus: "mc rollout engine efficiency"
git_head: 0ab1ddc (baseline) → d7c324a (after M7c) → on branch improve/mc-engine-profile-and-silent-fallback
lenses_run: [architect, control-inversion, test-skeptic, dead-code, types, observability]
critiques_run: [skeptic, convergence]
raw_findings: ~55
merged_findings: 10 clusters (M1-M10), 5 convergent fixes (F1-F5)
picked: 2 (1 inline + 1 profile investigation)
status: full_scan
related_profile: docs/superpowers/notes/2026-06-04-mc-engine-profile.md
---

Branch: `improve/mc-engine-profile-and-silent-fallback`. Baseline 1149 fast
passing (full pre-pick) + 14 deselected emulator + 1 pre-existing v07
RuntimeWarning. Post-edit identical.

## Top wins

### must-fix
- **M7c — silent `or 0.0` fallback** — `routes/practice_engine.py:89` was coercing `expected_episode_time_scalar(state) or 0.0` into a fabricated 0 ms when the closed-form scalar returns None (`p_die >= 1 - LOGIT_EPS`; geometric mean diverges). Violates `objectives.py:5` "no silent fallback" docstring and `project_model_principles`. Empirically samplable segments in the p_die≈1 corner showed 0ms on the dashboard. — size: **trivial** — SHIPPED (commit d7c324a).

### high-leverage
- **Profile-first investigation (PICKED)** — Conducted via `scripts/profile_mc_engine.py`. K=20 N=10000 synthetic workload reproduces the 2.56s lag. **Result: 99.8% of time is in `per_segment_values` doing K=20 swap-column redraws.** Inside that: `trend_signal_slopes` called per-draw (200_000 ×) when it's constant per (state, k_param); `draw_from_pool` weights list rebuilt every call (254_590 ×) when constant per (pool, alpha). Full writeup at `docs/superpowers/notes/2026-06-04-mc-engine-profile.md`. — size: **medium** (the investigation itself was trivial; the next-plan is medium).
- **W1 — hoist `trend_signal_slopes` out of the per-draw loop** — `em_suite_sampler.py:316 trend_signal_slopes` is invariant over the N=10000 draws of a single column but called inside `sample_episode` (line 450). Compute once in `_draw_column_impl` per (seg_id, k_param), pass into sample_episode. **~22% wall-clock reduction projected.** — size: **medium** — DEFERRED to a writing-plans next session.
- **W2 — precompute `draw_from_pool` weights per (pool, alpha)** — `em_suite_sampler.py:107` rebuilds `[alpha * (1-alpha)**(n-1-j) for j in range(n)]` 254_590 times per route hit. Constant per column. Cache or compute once per `_draw_column_impl`. **~10-15% wall-clock reduction projected.** — size: **medium** — DEFERRED.
- W1+W2 ship together; F1 (one-shot `evaluate_full` engine API) dropped because the profile shows baseline mask + cumsum recomputation is <0.5% of wall clock.

### convergent win
- **F4 — typed PracticeEngine surface + structured logging** — Still worth doing for legibility/observability, but **NOT a perf fix** (the scan's framing was wrong about this; profile confirms). Absorbs M5 (engine.matrix public), M6 (bare-dict returns/ctx, loose Callable aliases), M7a/b (no logging), parts of M9 (`matrix_built_at` placeholder becomes real). — size: **medium** — DEFERRED.

### nearby cleanup (verified real, low value — do when in the area)
- **M9a** delete `python/spinlab/practice_engine/threshold_sources.py:26 thresholds_from_gold_default` — verified zero production consumers.
- **M9c** delete `python/spinlab/practice_engine/engine.py:86-105 column_summary` — verified zero production consumers; lens framing as "reserved for future UI" debunked by verifier.
- **N4** `python/spinlab/estimators/em_suite_sampler.py:80 MAX_ATTEMPTS_PER_EPISODE = 100` — named + commented but doesn't justify *why 100* (CLAUDE.md "no magic numbers" rule).
- **M8a** `tests/unit/test_practice_engine_routes.py:107` `finished_pct == approx(100.0)` with `no_reset` is a tautology — `no_reset` returns `finished=ones(N)` unconditionally.
- **M8d** `tests/unit/practice_engine/test_threshold_sources.py:18-25, 36-43` manual `try/except KeyError` → `pytest.raises(KeyError)`.
- **N9** strengthen `tests/unit/practice_engine/test_scheduler_integration.py:70-84` invalidation test — current asserts the dirty flag changed but NOT that the redraw actually produces different values for the invalidated column and identical values for others.
- **M3** lazy engine holds stale `sampler_states` snapshot — verifier confirmed real but practical risk low (scheduler accesses engine per-request, lazy first-access materializes from fresh DB). One-line guard if it ever bites.

## Picked this session
- **M7c** silent `or 0.0` fallback → inline → commit d7c324a on branch `improve/mc-engine-profile-and-silent-fallback`.
- **Profile-first investigation** → `scripts/profile_mc_engine.py` + `docs/superpowers/notes/2026-06-04-mc-engine-profile.md` — committed on the same branch.

## Dropped during critique / verify

- **F1 (one-shot `evaluate_full` API)** — DROPPED. Convergence-hunter proposed it as the biggest perf win absorbing M1+M9+M10. Skeptic K1 correctly noted swap-column cumsum makes mask-reuse impossible for `per_segment_values`. Profile then showed baseline mask + cumsum across `evaluate` + `total_time_distribution` is <0.5% of wall clock anyway. The architectural cleanup remains a legibility win but the perf framing was wrong.
- **M1 "K full T.copy() per swap" framing** — DEBUNKED by profile. 3 ms total for all 20 copies. Not a bottleneck.
- **M1d "K+1 cumsums in target_paced"** — DEBUNKED by profile. 9 ms total. Not a bottleneck.
- **M2 "switch `random.Random` → `np.random.Generator`"** — DEBUNKED by profile. `_random.Random.random` is 41 ms total; the cost is in `draw_from_pool`'s list allocation and `trend_signal_slopes`'s EMA arithmetic, not in RNG-family choice.
- **K2 — M3 framing** — partially upheld by verifier: stale-snapshot architectural mismatch is real, practical risk low. Demoted to nearby cleanup with a one-line guard pattern.
- **K3 — M5 "engine.matrix public" stylistic** — kept by verifier; folded into F4 (deferred) where it's part of a real typed-surface refactor, not a stand-alone item.
- **K4 — M8d "try/except not pytest.raises is style noise"** — skeptic kept this as low-priority; kept in cleanup list (no functional impact, but a one-line idiom upgrade).
- **K5 — M9c "column_summary reserved-for-future-UI"** — debunked by verifier; confirmed dead.

## New finds from VERIFY pass

- **N4 verified** — MAX_ATTEMPTS_PER_EPISODE=100 named with what-comment but not *why-100*. Bumped into nearby cleanup.
- **B4 (profile reality check)** — verifier's pre-profile estimate (worst-case 20M rng calls, realistic 3-5M, ~0.3-1.0s pure RNG + 2-3x overhead = 1-3s) matched the actual measurement very closely. The "profile first" gate paid off.
- **B1 (M7c upgrade)** — verifier flagged this as critical-by-principle even though the impact is purely cosmetic in the /state endpoint. Shipped this session.
- **B2** — no integration test for /evaluate per_segment_values producing materially different results under different policies. Bump in cleanup pile if revisiting tests.

## Patterns observed by lenses

**Lens 1 (Architect):** Saw engine.matrix encapsulation leak, three-method-no-composition pattern, threshold_sources premature pluggability. All legibility-real but perf-irrelevant per profile.

**Lens 2 (Control-inversion):** Saw the same engine-method redundancy as Lens 1. Convergence-hunter built F1 atop it. Both proven non-bottlenecks.

**Lens 3 (Test-skeptic):** Caught the M8a tautology and the hand-coded T-array unit tests (objectives/reset_policies tests test NumPy more than they test the engine). Real cleanup, low effort. Also flagged the absence of a perf smoke benchmark — `scripts/profile_mc_engine.py` is the embryonic version; can graduate into a pytest-benchmark assertion under `tests/perf/` if regressions become a recurring problem.

**Lens 4 (Dead-code):** Found `thresholds_from_gold_default` (real dead code) and `column_summary` (dead despite "reserved" framing). Both cleanup-tier.

**Lens 5 (Types):** Big TypedDict cluster — engine returns bare dicts in four methods, ctx is bare dict across all 5 objectives, threshold_kwargs ad-hoc. Real type debt, folded into F4 (deferred).

**Lens 6 (Observability):** Engine internals are a black box on a known-slow path. `ensure_fresh`, `_rebuild_full`, `invalidate` all silent. M7c (silent `or 0.0` fallback) was real and shipped. Per-phase timing on the route would help future diagnosis even after W1+W2 land.

## Pivot insight

The convergence-hunter and the verifier's B4 both pre-anticipated this:
**"profile first or we're guessing whether the 3s is in sampling, in
cumsum, in JSON serialization, or in scheduler/DB."** The profile
unambiguously says: it's `trend_signal_slopes` recomputed per-draw and
`draw_from_pool` weights rebuilt per-draw, both inside `sample_episode`.
The lens-level "engine fanout" framing was geometrically right (K
swap columns is the work multiplier) but missed the actual leaf
inefficiencies that produce the per-draw cost.

Three of the five convergent fixes (F1, F2's perf framing, parts of F4)
get partially or wholly debunked by the profile. The honest read of
this scan: **the lenses produced solid architectural/type/observability
findings (M5/M6/M7/M9 cluster), but the perf root-cause was below the
abstraction level any of them looked at.** The W1+W2 fix lives at line
107-108 of em_suite_sampler.py (draw_from_pool weights list) and line
450 of em_suite_sampler.py (sample_episode trend_signal_slopes call) —
the kind of leaf-level perf bug a profiler finds in 30 seconds and a
swarm of architectural lenses takes hours to circle around.

## Full merged list (collapsed)

See M1-M10 in the critique reports below.

## Critique reports (skeptic + convergence + verify)

Skeptic, convergence-hunter, and verifier reports remain in the
conversation transcript. The merged + ranked digest above represents
the orchestrator's final synthesis post-profile.

# Model Purge (Plan 2) — Execution Resume Note

**Read this first on resume, then continue executing [`2026-06-01-model-purge.md`](2026-06-01-model-purge.md) from Task 6.**

## Where things stand (as of 2026-06-01)

- **Branch:** `model-purge` (off `main`). **HEAD:** `f7d09da`.
- **Plan 1** (sampler-real & scalar) is **MERGED to `main`**. Tag **`pre-model-purge`** marks the pre-deletion commit (safety net for T8's deletions).
- **Execution method:** superpowers:subagent-driven-development — fresh subagent per task, then a spec-compliance review AND a code-quality review per task (use **opus** reviewers for the risky T8 deletion). Run the FULL `python -m pytest` only at T10 / before finishing; use `pytest -m "not emulator" -q` as the per-task gate.
- **Test count:** `pytest -m "not emulator" -q` → **1308 passed, 14 deselected**. Green at every commit.

## Tasks done (T1–T5)

| Task | Commits | Notes |
|---|---|---|
| T1 default→em_suite | `9c27116`, `a16288f`, `d0364fc` | Flipped scheduler + allocator + factories defaults. **Fixed a latent Plan-1 bug:** `SamplerState.to_dict/from_dict` weren't round-tripping inherited `n_completed`/`n_attempts` → scheduler always routed em_suite through `init_state` (blank) + table "Runs" read 0. Now serialized (regression test added). |
| T2 frontend cleanup | `e42a153` | Removed estimator selector + tuning panel from model.ts/model-render.ts/model-api.ts/app.ts/index.html + frontend tests. |
| T3 replay no-seed | `da8ddcc`, `04b9056`, `943a54a` | Recorder stamps `source=REPLAY` (via `recorder.set_source()` at arm-time in `_enter_recording`); `scheduler._events_from_rows` filters REPLAY (affects sampler + matrix + cold views — **intended**); `_maybe_refit_segment` also filters REPLAY. `clear()` resets source. |
| T4 scheduler collapse | `849945b`, `4c21444`, `832a85d` | Single-sampler `update_state_after_episode`/`rebuild_all_states`; deleted `_process_attempt_for_estimator`, `switch_estimator`, `_load_estimator_params`, estimator branch of `_sync_config_from_db`. 15 multi-estimator tests deleted, 2 retargeted (opus-verified all justified). Added `rebuild_all_states` regression test (closed a coverage hole). Added try/except guard to `update_state_after_episode`. |
| T5 routes collapse | `f7d09da` | **NEEDS REVIEW (resume step 1).** Removed `POST /api/estimator` + `GET/POST /api/estimator-params`; simplified `/api/model` (dropped `estimators` list); collapsed `segment_history` to single em_suite entry (empty per-attempt series — segment-detail redesign is Spec #2). Deleted 3 endpoint tests, retargeted 2, added 3 "endpoint-gone → 404" tests. |

## Remaining work (resume here)

0. **Review T5** (`git diff 832a85d f7d09da`) — spec + code-quality. Verify: only the 3 handlers + their imports removed; `/api/model` keeps `estimator`/`segments`/`allocator_weights`; `segment_history` single-model shape correct; cold-distribution block + `get_em_suite_matrix` UNTOUCHED; the 3 deleted tests were endpoint-gone only; the 2 retargets faithful.
1. **T6 cold-distribution decouple** — equal-weight; remove `from spinlab.estimators.death_aware_rolling import _resolve_halflife` and the `estimator_params:death_aware_rolling` lookup from `routes/model.py`. **MUST land before T8** (T8 deletes death_aware_rolling).
2. **T7 schema trim + regen types** — delete `EstimatorInfo`, `TuningData`, `ParamDef`, `EstimatorSwitchRequest/Response`, `EstimatorParamsRequest` from `api_schemas.py`; drop `ModelData.estimators`; `cd frontend && npm run gen-types && npm run typecheck && npm run build`.
3. **T8 delete the 5 estimators + registry** (HIGHEST RISK — opus reviewers). Gate on the no-references grep first. Also delete/retarget the still-kalman-coupled tests: **`tests/unit/test_estimator_parity_phase0.py`** (iterates `kalman/exp_decay/rolling_mean`), **`tests/fixtures/segments_v07/capture_golden.py:115`** (same tuple), **`tests/unit/test_model_output.py`** (saves/reads kalman state), plus each estimator's own unit test file. In `estimators/__init__.py` remove the registry (`_ESTIMATOR_REGISTRY`/`register_estimator`/`get_estimator`/`list_estimators`/`_register_all`/`load_mature_states`); in `em_suite_sampler.py` remove the `@register_estimator` decorator + `EstimatorState.register_state(...)` line. Keep `Estimator`/`EstimatorState` ABCs (the Spec #3 seam). **Also remove `EmSuiteSamplerEstimator.init_state`'s transient `n_completed=1` band-aid** now that T4 deleted the bare-state routing (verify it's truly unused first) — and `process_attempt` if unused.
4. **T9 derived-data reset** — add `Database.purge_stale_model_state(keep="em_suite_sampler")` (DELETE non-em_suite model_state rows + `estimator`/`estimator_params:%` config keys; event data untouched) + a test + call it once at dashboard startup.
5. **T10 verify** — `ruff check python/`, `npx pyright python/spinlab/`, frontend typecheck/build/test, **FULL `python -m pytest`** (skips count as failures), straggler grep (`kalman|death_aware|switch_estimator|estimator-select|tuning-panel|declared_params`), update `docs/ARCHITECTURE.md` if it describes the multi-estimator system.
6. **Final opus review** of the whole Plan-2 diff, then **superpowers:finishing-a-development-branch** (the user merged Plan 1 locally via fast-forward; offer the same options — likely merge to `main` locally; local `main` is ~261 ahead of `origin`, user pushes on own cadence).

## Key context / decisions (don't relitigate — see the spec + memory)

- Spec: [`../specs/2026-06-01-model-purge-sampler-core-design.md`](../specs/2026-06-01-model-purge-sampler-core-design.md). Memory: `project_model_purge_arc`.
- **em_suite gate:** expected time is `None` until ≥2 successes AND ≥2 deaths (honest, per the no-fudge principle). Seed gate-passing data in tests via `process_attempt(seg, completed=True, deaths=1)` ×2 (each = 1 died + 1 survived event).
- **Allocators survive, frozen** — `model_outputs` dict kept (one entry); greedy reads em_suite's scalar. Don't refactor the allocator input (deferred).
- **`Estimator`/`EstimatorState` ABCs stay** (Spec #3 plugs in here); only the registry indirection dies.
- **Zero migrations** — event data (`attempts`) is source-of-truth, never wiped; only derived `model_state`/config cache resets.
- **`clean`/`extras` are intentionally unmodeled in em_suite** (Plan 1) — `model_output` returns `clean`=all-None, `extras`=None. Spec #2 surfaces them.

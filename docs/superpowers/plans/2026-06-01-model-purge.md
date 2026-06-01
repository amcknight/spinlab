# Model Purge — Implementation Plan (Plan 2 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the 5 dead estimators and the multi-model scaffolding, leaving the EMA-Suite sampler (made real in Plan 1) as the sole model, with the allocator zoo frozen and functional, replay/fast-replay no longer seeding model data, and the cold-distribution decoupled — all with zero schema migrations.

**Architecture:** A destructive collapse across the model layer, ordered so the suite stays green at every commit. The frontend stops *calling* the estimator-switch/tuning endpoints (Task 2) before the backend removes them (Tasks 5, 7); every reference to the 5 estimators is removed from scheduler/routes/cold-distribution (Tasks 4–6) before their files are deleted (Task 8). Event data (`attempts`) is never touched; only derived `model_state` rows + stale config keys are reset (Task 9).

**Tech Stack:** Python 3.11+ (FastAPI, dataclasses, pytest), TypeScript/Vite frontend, SQLite. No new dependencies.

**Spec:** [`docs/superpowers/specs/2026-06-01-model-purge-sampler-core-design.md`](../specs/2026-06-01-model-purge-sampler-core-design.md). Builds on Plan 1 ([`2026-06-01-sampler-real-and-scalar.md`](2026-06-01-sampler-real-and-scalar.md), merged). Safety tag `pre-model-purge` marks the pre-deletion commit.

---

## File Structure (what changes, by responsibility)

- **`python/spinlab/scheduler.py`** — collapse the all-estimators loop to the single sampler; drop estimator-switch/params/priors/bare-state machinery.
- **`python/spinlab/estimators/__init__.py`** — remove the registry/factory; keep the `Estimator`/`EstimatorState` seam, hard-wired to em_suite.
- **`python/spinlab/estimators/{kalman,death_aware_rolling,rolling_mean,bootstrap_resample,exp_decay}.py`** — deleted.
- **`python/spinlab/allocators/__init__.py`** — `selected_model` default → em_suite (input shape unchanged; allocators frozen).
- **`python/spinlab/routes/model.py`** — remove `/api/estimator`, `/api/estimator-params`; collapse `segment_history` and `/api/model` to one model.
- **`python/spinlab/cold_distribution.py`** + route — equal-weight, decoupled from death-aware-rolling.
- **`python/spinlab/capture/recorder.py`** + `reference.py` — tag replay events so model ingestion can exclude them.
- **`python/spinlab/api_schemas.py`** — drop estimator/tuning schemas; trim `ModelData`.
- **`python/spinlab/db/model_state.py`** — add a stale-row cleanup method.
- **`frontend/src/{model.ts,model-render.ts,model-api.ts}`, `frontend/index.html`** — remove the estimator selector + tuning panel.

---

## Task 1: Make EMA-Suite the sole active model (no deletions yet)

**Files:**
- Modify: `python/spinlab/scheduler.py` (default `estimator_name`), `python/spinlab/allocators/__init__.py:38,49` (`selected_model` default)
- Test: existing scheduler/allocator tests that assert the `kalman` default

- [ ] **Step 1: Baseline.** Run `python -m pytest` — must be fully green (1331+). Red baseline = stop and ask.

- [ ] **Step 2: Find the `kalman` defaults that must flip.**

Run: `grep -rn '"kalman"' python/spinlab/ tests/`
Expected hits: `scheduler.py:84` (`estimator_name: str = "kalman"`), `allocators/__init__.py:38` (`selected_model: str = "kalman"`) and `:49` (`load_all(... selected_model: str = "kalman")`), plus any tests asserting these.

- [ ] **Step 3: Write/adjust a failing test** asserting the default is em_suite. In the scheduler test module (find via `grep -rln "Scheduler(" tests/`), add:

```python
def test_scheduler_defaults_to_em_suite_sampler(tmp_db_game):
    db, game_id = tmp_db_game
    sched = Scheduler(db, game_id)  # no estimator_name override
    assert sched.estimator.name == "em_suite_sampler"
```

(Reuse the module's existing DB/game fixture; match its name. If none exists, mirror the fixture another test in that file uses.)

- [ ] **Step 4: Run it — fails** (default is still `kalman`).

- [ ] **Step 5: Flip the defaults.**
- `scheduler.py`: `estimator_name: str = "em_suite_sampler"` (the `__init__` default param).
- `allocators/__init__.py`: both `selected_model: str = "kalman"` → `"em_suite_sampler"` (the dataclass field default and `load_all`'s parameter default).

- [ ] **Step 6: Update any test that asserted kalman was the default** (from Step 2's grep). These assertions encoded the old default — flip them to em_suite (don't delete the test, retarget it).

- [ ] **Step 7: Run** `python -m pytest -q`. Green. The 6 estimators are still all computed; em_suite is now the *active* one and greedy ranks on its Plan-1 scalar.

- [ ] **Step 8: Commit.**
```bash
git add -A && git commit -m "feat(model): make em_suite_sampler the default active estimator"
```

---

## Task 2: Frontend stops using the estimator-switch + tuning endpoints

**Files:**
- Modify: `frontend/src/model.ts`, `frontend/src/model-render.ts`, `frontend/src/model-api.ts`, `frontend/index.html`
- Test: frontend tests referencing tuning/estimator-switch (`frontend/src` Vitest specs)

**Why first:** removing the endpoints (Tasks 5/7) before the frontend stops calling them would 404 the dashboard on load (`initModelTab` calls `fetchTuningParams`). This task makes the frontend single-model so the backend can be gutted safely.

- [ ] **Step 1: Remove the estimator selector + tuning wiring from `model.ts`.** Delete:
  - the `estimator-select` `change` listener (`initModelTab`, lines ~198–202),
  - the tuning toggle/reset listeners (lines ~208–217) and the trailing `fetchTuningParams()` call (line ~219),
  - functions `debouncedApply`, `collectTuningParams`, `applyTuningParams`, `resetTuningDefaults`, `fetchTuningParams`, `syncTuningWithGame`, `_resetTuningGameCache`,
  - state `_tuningParams`, `_tuningDebounce`, `_lastTuningGameId`, `TUNING_DEBOUNCE_MS`,
  - the `tuning-panel` show/hide lines in `showSegmentDetail`/`hideSegmentDetail` (they reference `document.getElementById("tuning-panel")` which is being deleted from the HTML),
  - imports now unused: `TuningData` type, `postEstimator`, `fetchTuningData`, `postTuningParams`, `renderTuningParams`.
  - Find every caller of `syncTuningWithGame` (`grep -rn syncTuningWithGame frontend/src`) — likely `app.ts` on SSE state; remove those call sites too.

- [ ] **Step 2: Remove `renderTuningParams` from `model-render.ts`** and any estimator-dropdown population it does. Run `grep -n "renderTuningParams\|estimator-select\|tuning" frontend/src/model-render.ts` and remove those blocks.

- [ ] **Step 3: Remove the dead API helpers from `model-api.ts`:** `postEstimator`, `fetchTuningData`, `postTuningParams` (confirm no other importers via `grep -rn "postEstimator\|fetchTuningData\|postTuningParams" frontend/src`).

- [ ] **Step 4: Remove the HTML.** In `frontend/index.html` delete `<select id="estimator-select">` (~line 82) and the entire `<div id="tuning-panel">…</div>` block (~lines 85–94). If there's a `.model-header` wrapper that only held the selector, simplify it (keep the `<h2>`).

- [ ] **Step 5: Delete/trim the frontend tests** that covered tuning/selector behavior (`grep -rln "syncTuningWithGame\|tuning\|estimator-select\|_resetTuningGameCache" frontend/src/**/*.test.ts`). Remove tests that only documented the deleted behavior; keep any that also assert still-valid model-table behavior (retarget those).

- [ ] **Step 6: Typecheck + build + test the frontend.**
```bash
cd frontend && npm run typecheck && npm run build && npm test
```
Expected: typecheck clean (no dangling refs to removed symbols), build succeeds, Vitest green.

- [ ] **Step 7: Run the Python smoke tests** that build/serve the frontend (`python -m pytest -m "not emulator" -k "smoke or frontend" -q`) to confirm the served bundle still loads. Green.

- [ ] **Step 8: Commit.**
```bash
git add -A && git commit -m "refactor(frontend): drop estimator selector + tuning panel (single model)"
```

---

## Task 3: Replay & Fast Replay stop seeding model data

**Files:**
- Modify: `python/spinlab/capture/recorder.py` (tag replay events), `python/spinlab/capture/reference.py` (pass the replay flag), `python/spinlab/scheduler.py` (`_events_from_rows` excludes replay) OR `python/spinlab/db/attempts.py` (`get_segment_event_rows`) — pick the seam in Step 3
- Test: a new regression test under `tests/` (unit or integration)

**Why:** today replay/fast-replay events are written with `source=AttemptSource.REFERENCE` and fed to the sampler; fast-replay especially pollutes the time pools with wall-clock-collapsed frame deltas. They must contribute zero model data.

- [ ] **Step 1: Confirm the write + read seam.**
- Write: `recorder.py` `_close_segment` builds `EventAttempt(..., source=AttemptSource.REFERENCE, ...)` for BOTH live and replay (replay is only distinguished by `capture_runs.kind == "replay"`, set in `reference.py`).
- Read for model: `scheduler.py:_events_from_rows` hydrates ALL rows with no source filter; `db.get_segment_event_rows` has no filter.

- [ ] **Step 2: Write the failing regression test.** Create `tests/unit/test_replay_no_seed.py`:

```python
"""Replay/fast-replay events must not seed the sampler."""
# Build a segment, write some REFERENCE-source events AND some REPLAY-source
# events for it, then assert the sampler state the scheduler builds reflects
# ONLY the non-replay events (pool sizes + EMA counts exclude replay).
```

Implement it concretely against the real DB + scheduler: insert events via `db.log_event_attempt(EventAttempt(..., source=AttemptSource.REFERENCE))` and `... source=AttemptSource.REPLAY ...`, then call the scheduler's per-segment rebuild and assert `state.n_attempts_total` and `len(state.success_time_pool)+len(state.death_time_pool)` count only the non-replay events. (Use the DB fixture pattern from a sibling unit test; look at `tests/unit/test_scheduler*.py` for setup.)

- [ ] **Step 3: Run it — fails** (replay events are currently ingested).

- [ ] **Step 4: Tag replay events at write time.** Give the recorder a `source` it stamps on events (default `AttemptSource.REFERENCE`). In `reference.py`, when starting the recorder for a replay run (`kind == "replay"`), pass `source=AttemptSource.REPLAY`. Concretely: add a `source: AttemptSource = AttemptSource.REFERENCE` field/param to the recorder, use it in `_close_segment`'s `EventAttempt(..., source=self._source, ...)`, and set it from `reference.py` where replay vs live is known.

- [ ] **Step 5: Exclude replay events from MODEL ingestion only.** In `scheduler.py:_events_from_rows`, skip rows whose source is replay:
```python
        if AttemptSource(r["source"]) is AttemptSource.REPLAY:
            continue
```
Add a one-line comment: replay/fast-replay events are recorded for provenance but never seed the model. **Do not** filter in `get_segment_event_rows` (the matrix/cold-distribution routes and provenance views may still want all rows) — filter at the sampler-ingestion seam.

- [ ] **Step 6: Run** the new test + `python -m pytest -m "not emulator" -q`. Green.

- [ ] **Step 7: Commit.**
```bash
git add -A && git commit -m "fix(replay): replay + fast-replay no longer seed the sampler"
```

---

## Task 4: Collapse the scheduler to the single sampler

**Files:**
- Modify: `python/spinlab/scheduler.py`
- Test: scheduler tests

**Why:** remove the multi-model machinery the deletion will orphan: the all-estimators loop, `switch_estimator`, the `"estimator"` config key, per-estimator params, and the `init_state`/priors/bare-state branching (em_suite ignores all of it — `process_attempt` just calls `rebuild_state`).

- [ ] **Step 1: Replace the estimator wiring in `__init__`.** Drop the saved-estimator lookup + `get_estimator`; hold the sampler directly:
```python
from spinlab.estimators.em_suite_sampler import EmSuiteSamplerEstimator
# ...
        self.estimator = EmSuiteSamplerEstimator()
```
Remove `estimator_name` parameter handling, the `saved_est`/`list_estimators` fallback, and the `if est_name not in list_estimators()` guard. (Keep the constructor signature accepting `estimator_name` ONLY if tests still pass it — otherwise drop it; reconcile with Task 1's test.)

- [ ] **Step 2: Collapse `update_state_after_episode`** — replace the `for est in [get_estimator(n) for n in list_estimators()]` loop with a single sampler update. The whole per-attempt path simplifies because em_suite rebuilds from events every time:
```python
        attempt_rows = self.db.get_segment_attempts(segment_id)
        all_attempts = _attempts_from_rows(attempt_rows)
        if not all_attempts:
            return
        event_rows = self.db.get_segment_event_rows(segment_id)
        events = _events_from_rows(event_rows)
        state = self.estimator.rebuild_state(all_attempts, events=events)
        output = self.estimator.model_output(state, all_attempts, events=events)
        self.db.save_model_state(
            segment_id, self.estimator.name,
            json.dumps(state.to_dict()), json.dumps(output.to_dict()),
        )
        self._maybe_refit_segment(segment_id)
```
Delete `_process_attempt_for_estimator` entirely (its init_state/priors/bare-state branching is dead). Keep `_maybe_refit_segment` (the v07 silent fit is unrelated).

- [ ] **Step 3: Collapse `rebuild_all_states`** the same way — single `self.estimator.rebuild_state(...)` per segment, no estimator loop, no `_load_estimator_params`.

- [ ] **Step 4: Delete dead methods/fields:** `switch_estimator`, `_load_estimator_params`, the estimator branch of `_sync_config_from_db` (keep the allocator-weights branch), and the `get_estimator`/`list_estimators` imports. Keep `EstimatorState` import only if still used; the collapsed path uses `SamplerState` indirectly via the estimator, so drop the now-unused `EstimatorState.deserialize` usage.

- [ ] **Step 5: Update scheduler tests.** Remove/retarget tests for `switch_estimator`, multi-estimator iteration, per-estimator params, and the bare-state-from-death-first path (that branching is gone). Tests that assert allocator behavior, `pick_next`, and persistence stay (retarget to em_suite). Run `grep -rn "switch_estimator\|_process_attempt_for_estimator\|_load_estimator_params" tests/` and fix each.

- [ ] **Step 6: Run** `python -m pytest -m "not emulator" -q`. Green.

- [ ] **Step 7: Commit.**
```bash
git add -A && git commit -m "refactor(scheduler): collapse all-estimators loop to the single sampler"
```

---

## Task 5: Collapse `routes/model.py` to one model; remove estimator/tuning endpoints

**Files:**
- Modify: `python/spinlab/routes/model.py`
- Test: route tests for `/api/model`, `/api/segments/{id}/history`, the removed endpoints

- [ ] **Step 1: Remove the `/api/estimator` (switch) and `/api/estimator-params` (GET + POST) handlers** (lines ~82–130) and their now-unused imports (`EstimatorParamsRequest`, `EstimatorSwitchRequest`, `EstimatorSwitchResponse`, `TuningData`, and `get_estimator`/`list_estimators`).

- [ ] **Step 2: Simplify `/api/model`** — drop the `estimators` list (no more selector). Keep `segments` with `model_outputs` (now a single-entry dict) and `allocator_weights`. Set `"estimators": []` is no longer needed; remove the field from the response dict and from `ModelData` (Task 7 trims the schema). For now return without the `estimators` key.

- [ ] **Step 3: Collapse the `segment_history` estimator loop.** Replace the `for est_name in list_estimators()` block (lines ~165–216) with a single em_suite entry built from its final event-replayed state. The per-attempt episode-indexed curves are meaningless for an event-level sampler, so emit empty series (the matrix endpoint is em_suite's real view; the segment-detail redesign is Spec #2):
```python
    from spinlab.estimators.em_suite_sampler import EmSuiteSamplerEstimator
    est = EmSuiteSamplerEstimator()
    final_state = est.rebuild_state(all_records, events=events)
    final_out = est.model_output(final_state, completed, events=events)
    estimator_curves = {
        est.name: {
            "total": {"expected_ms": [], "floor_ms": []},
            "clean": {"expected_ms": [], "floor_ms": []},
            "final_extras": (
                final_out.extras.to_dict() if final_out.extras is not None else None
            ),
        }
    }
    selected_model = est.name
```
(Drop the `sched`-based `selected_model` lookup and the `init_state`/`process_attempt` per-attempt loop.)

- [ ] **Step 4: Leave the cold-distribution block for Task 6** (it still imports `_resolve_halflife` here; Task 6 decouples it). The matrix endpoint (`get_em_suite_matrix`) is unchanged.

- [ ] **Step 5: Update route tests.** Add/keep: a test that `/api/estimator` and `/api/estimator-params` now 404 (or are gone from the router). Retarget `/api/model` and `segment_history` tests to the single-model shape. Run `grep -rn "estimator-params\|/api/estimator\b\|estimators\b" tests/` and fix.

- [ ] **Step 6: Run** `python -m pytest -m "not emulator" -q`. Green.

- [ ] **Step 7: Commit.**
```bash
git add -A && git commit -m "refactor(routes): single-model /api/model + segment_history; drop estimator/tuning endpoints"
```

---

## Task 6: Decouple the cold-distribution (equal-weight)

**Files:**
- Modify: `python/spinlab/cold_distribution.py`, `python/spinlab/routes/model.py` (cold block), `python/spinlab/api_schemas.py` (`ColdDistribution.halflife` / `ColdBin`)
- Test: `tests/` cold-distribution tests

**Why:** `compute_cold_distribution` currently borrows death-aware-rolling's halflife to recency-weight the histogram. A halflife is the same kind of knob as an α; for v0 the cold histogram becomes the equal-weighted raw empirical view (no recency knob), removing the dependency on a soon-deleted module.

- [ ] **Step 1: Read `cold_distribution.py` in full** — note `_compute_attempt_weights(n, halflife)` and every use of `halflife` (binning, weighting, the echoed `halflife` field).

- [ ] **Step 2: Write/adjust a failing test** asserting equal weighting: with N cold events, every event's weight is `1.0` (or weights are uniform), independent of any halflife. Put it alongside the existing cold-distribution tests (`grep -rln "compute_cold_distribution" tests/`).

- [ ] **Step 3: Make weighting uniform.** Change `compute_cold_distribution`'s signature to drop `halflife` (or default it and ignore it), and replace `_compute_attempt_weights(n, halflife)` with uniform weights `[1.0] * n`. Remove `_compute_attempt_weights` if now unused. Set the echoed `ColdDistribution.halflife` to `0` (or remove the field — see Step 5).

- [ ] **Step 4: Update the route** (`routes/model.py` cold block, ~lines 221–231): remove `from spinlab.estimators.death_aware_rolling import _resolve_halflife`, the `estimator_params:death_aware_rolling` lookup, and the `halflife=` argument:
```python
    cold_events = [ev for ev in events if not ev.is_hot]
    cold_distribution = (
        compute_cold_distribution(cold_events) if cold_events else None
    )
```

- [ ] **Step 5: Schema** — if you removed the `halflife` field from `ColdDistribution`, drop it from `api_schemas.py:ColdDistribution` too and regen types (Task 7 handles the regen batch); if you kept it echoing `0`, leave the field. Prefer keeping it `=0` to minimize schema churn this task; remove in Task 7 if desired.

- [ ] **Step 6: Run** `python -m pytest -m "not emulator" -q`. Green.

- [ ] **Step 7: Commit.**
```bash
git add -A && git commit -m "refactor(cold-dist): equal-weight, decoupled from death-aware-rolling"
```

---

## Task 7: Trim the API schemas + regen frontend types

**Files:**
- Modify: `python/spinlab/api_schemas.py`, then regen `frontend/src/api-types.ts` via the build pipeline
- Test: frontend typecheck/build, Python route tests

**Why:** the estimator/tuning schemas are now unreferenced (frontend stopped using them in Task 2; routes removed them in Task 5).

- [ ] **Step 1: Delete the now-dead schema classes** from `api_schemas.py`: `EstimatorInfo`, `TuningData`, `ParamDef`, `EstimatorSwitchRequest`, `EstimatorSwitchResponse`, `EstimatorParamsRequest`. From `ModelData` remove the `estimators: list[EstimatorInfo]` field (and `estimator` if the frontend no longer reads it — confirm via `grep -rn "\.estimator\b\|estimators" frontend/src`). Confirm no remaining importers: `grep -rn "EstimatorInfo\|TuningData\|ParamDef\|EstimatorSwitch\|EstimatorParamsRequest" python/`.

- [ ] **Step 2: Regen + build.**
```bash
cd frontend && npm run gen-types && npm run typecheck && npm run build
```
Expected: `api-types.ts` regenerates without the deleted types; typecheck clean (the frontend already doesn't use them); build OK. If typecheck flags a lingering reference, remove it (Task 2 should have caught all).

- [ ] **Step 3: Run** `python -m pytest -m "not emulator" -q` and `cd frontend && npm test`. Green.

- [ ] **Step 4: Commit.**
```bash
git add -A && git commit -m "refactor(schemas): drop estimator/tuning schemas; regen frontend types"
```

---

## Task 8: Delete the 5 estimators + the registry machinery

**Files:**
- Delete: `python/spinlab/estimators/{kalman,death_aware_rolling,rolling_mean,bootstrap_resample,exp_decay}.py` and their test files
- Modify: `python/spinlab/estimators/__init__.py`

**Why:** by now nothing references them (Tasks 4–6 removed every call site). This is the deletion the whole arc was for.

- [ ] **Step 1: Confirm no references remain.**
```bash
grep -rn "kalman\|death_aware_rolling\|rolling_mean\|bootstrap_resample\|exp_decay" python/spinlab/ | grep -v "estimators/__init__.py"
```
Expected: no production hits outside the registry import line. If anything remains (e.g. a stray import), STOP and remove it first — do not delete files out from under a live reference.

- [ ] **Step 2: Delete the 5 production files and their test files.** Find tests via `git ls-files "tests/**" | grep -E "kalman|death_aware|rolling_mean|bootstrap_resample|exp_decay"` and `git rm` both sets. (`death_aware_rolling`'s cold-distribution test, if any, was retargeted in Task 6 — verify it doesn't reference the deleted module.)

- [ ] **Step 3: Collapse `estimators/__init__.py`.** Remove `_ESTIMATOR_REGISTRY`, `register_estimator`, `get_estimator`, `list_estimators`, `_register_all`/its call, and `load_mature_states` (only Kalman/ExpDecay used it). In `em_suite_sampler.py` remove the `@register_estimator` decorator and the `EstimatorState.register_state("em_suite_sampler", SamplerState)` line (the scheduler instantiates the class directly now). Keep the `Estimator` and `EstimatorState` ABCs as the ingestion seam. If `EstimatorState.deserialize`/`register_state`/`_state_classes` and `ParamDef`/`declared_params`/`get_priors`/`init_state` are now unused, remove them too (confirm with grep); keep `to_dict`/`from_dict`, `rebuild_state`, `model_output`, `process_attempt` (whatever the scheduler + matrix route still call). Leave the `Estimator` ABC minimal but intact — it's the seam Spec #3 plugs into.

- [ ] **Step 4: Verify the ABC surface still matches em_suite.** `npx pyright python/spinlab/estimators/` — em_suite must still satisfy whatever `Estimator` declares. Trim abstract methods that no caller needs rather than leaving em_suite implementing dead ones.

- [ ] **Step 5: Run** `python -m pytest -m "not emulator" -q`, `ruff check python/spinlab/estimators/`, `npx pyright python/spinlab/estimators/`. Green / no new errors.

- [ ] **Step 6: Commit.**
```bash
git add -A && git commit -m "feat(model): delete the 5 dead estimators + registry indirection"
```

---

## Task 9: Reset the derived `model_state` cache (zero migrations)

**Files:**
- Modify: `python/spinlab/db/model_state.py` (new cleanup method), and one call site (CLI or startup)
- Test: a DB-level test

**Why:** orphan `model_state` rows for the 5 deleted estimators and stale `allocator_config` keys (`estimator`, `estimator_params:*`) linger in existing DBs. Event data is untouched; the derived cache is rebuilt from events.

- [ ] **Step 1: Write the failing test.** In the model-state DB test module: seed `model_state` rows for `"kalman"` + `"em_suite_sampler"` and `allocator_config` keys `estimator`, `estimator_params:kalman`; call the new cleanup; assert only `em_suite_sampler` rows + non-stale config remain, and the `attempts` table is untouched.

- [ ] **Step 2: Add the cleanup method** to `ModelStateMixin` (`db/model_state.py`):
```python
def purge_stale_model_state(self, keep_estimator: str = "em_suite_sampler") -> None:
    """Delete model_state rows for any estimator other than keep_estimator,
    and the stale estimator-selection / per-estimator-param config keys.
    Event data (attempts) is never touched — these are rebuildable caches."""
    self.conn.execute(
        "DELETE FROM model_state WHERE estimator != ?", (keep_estimator,),
    )
    self.conn.execute("DELETE FROM allocator_config WHERE key = 'estimator'")
    self.conn.execute(
        "DELETE FROM allocator_config WHERE key LIKE 'estimator_params:%'",
    )
    self.conn.commit()
```

- [ ] **Step 3: Call it once at startup.** Wire `db.purge_stale_model_state()` into dashboard startup (find where the DB is opened on `spinlab dashboard` — `grep -rn "def.*dashboard\|Database(" python/spinlab/cli.py python/spinlab/*.py`). It's idempotent and cheap; run it on boot so existing DBs self-heal. The em_suite rows then rebuild on the next episode via the scheduler (or call `scheduler.rebuild_all_states()` once after purge if you want eager rebuild).

- [ ] **Step 4: Run** the new test + `python -m pytest -m "not emulator" -q`. Green.

- [ ] **Step 5: Commit.**
```bash
git add -A && git commit -m "chore(db): purge stale model_state + config on startup (event data untouched)"
```

---

## Task 10: Full verification & wrap

**Files:** none (verification), possibly `docs/ARCHITECTURE.md`

- [ ] **Step 1: Static analysis.** `ruff check python/` (no new errors vs tracked baseline), `npx pyright python/spinlab/` (no new errors).
- [ ] **Step 2: Frontend.** `cd frontend && npm run typecheck && npm run build && npm test`. Green.
- [ ] **Step 3: FULL unfiltered suite** (project policy — unit + emulator + frontend smoke): `python -m pytest`. All pass; **skips count as failures** — if emulator tests skip with a launch failure, surface it, don't treat as green.
- [ ] **Step 4: Grep for stragglers.** `grep -rn "kalman\|death_aware\|switch_estimator\|estimator-select\|tuning-panel\|declared_params" python/ frontend/src/` — expect only historical mentions in docs/specs, nothing live.
- [ ] **Step 5: Docs.** If `docs/ARCHITECTURE.md` describes the multi-estimator system or the estimator selector, update it to the single-sampler reality. Update the `practice-allocator-spec.md` v0-status note if warranted.
- [ ] **Step 6: Commit** any doc/cleanup, then this plan is complete and ready for `finishing-a-development-branch`.

---

## Self-Review (plan author)

- **Spec coverage:** delete 5 estimators (T8) ✓; registry/factory/switch/config-key/params/selector removal (T4,T5,T7,T8,T2) ✓; keep `Estimator`/`EstimatorState` seam (T8) ✓; allocators frozen, input unchanged, default→em_suite (T1) ✓; replay/fast-replay no-seed (T3) ✓; cold-distribution decoupled equal-weight (T6) ✓; zero migrations, derived-cache reset, event data untouched (T9) ✓; UI selector/tuning removal as mechanical (T2) ✓. Out of scope (Spec #2/#3): broader UI redesign, the §4 allocator, the deeper allocator-input→scalar refactor (kept `model_outputs` dict, one entry).
- **Green-at-each-commit ordering:** frontend stops calling endpoints (T2) before they're removed (T5/T7); all estimator references removed (T4–T6) before files deleted (T8). Verified the dependency chain.
- **Placeholders:** none — non-trivial rewrites have full code; mechanical deletions have exact target lists + grep-confirm steps + the verifying commands. (A few "find the fixture/call-site via grep" steps are deliberate: exact test-fixture names weren't read here and must be matched to the sibling test in-file at execution time — each is bounded to a single grep.)
- **Risk:** highest-risk task is T8 (deletion); it's gated by a no-references grep (T8 Step 1) and the `pre-model-purge` tag. T4 (scheduler collapse) is the most intricate rewrite; full code given.

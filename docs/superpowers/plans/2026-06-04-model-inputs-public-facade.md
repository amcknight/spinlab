# Model-Inputs Public Facade — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote four underscore-prefixed helpers in `scheduler.py` and `estimators/em_suite_sampler.py` to a public surface, consolidate the duplicated `_running_min_clean` floor helper, and stop constructing a fresh `EmSuiteSamplerEstimator()` inside `routes/model.py:segment_history` when one already exists on the scheduler.

**Architecture:** Rename in place (drop the leading underscore — these are pure converters / classifiers, not encapsulated state). Update every call site in the same task as the rename so there's no half-public/half-private window. The session-snapshot path also collapses to a `scheduler.sampler_states()` lookup, removing one redundant event-replay per session start.

**Tech Stack:** Python 3.11+, pytest, ruff. No new dependencies.

**Context for the implementing agent:**

- **Spec:** `docs/superpowers/scans/2026-06-04-improve.md` — "Top wins → convergent win → Fix A". Do NOT restate rationale; this plan is the operational version.
- **Branch:** Already on `improve/model-inputs-facade-and-cleanups`. Two trivial commits (`909122f`, `854548e`) already shipped from the same /improve scan; this work continues on the same branch.
- **Baseline:** `python -m pytest` is GREEN at branch HEAD (1161 passed, 1 documented v07 warning). Re-verify before starting and after each task.
- **CLAUDE.md rules that bite here:**
  - "Skips count as failures." Don't silence emulator tests if they break.
  - Don't introduce new pyright errors. Existing 261 errors are tracked.
  - No magic constants, no fudge factors, no defensive `.get()` on contract fields.
  - Don't add comments unless the WHY is non-obvious; don't add backwards-compat shims for the rename.

**Scope guard:** This plan is RENAMES + ONE BEHAVIOR-EQUIVALENT REFACTOR. Do not retype unrelated `Any`s, do not add TypedDicts to the diff functions (C15 is deferred), do not touch the `_running_min_clean` stabilization comment until Task 4 (which deletes it). If a subagent proposes scope creep, stop and ask.

---

## File Structure

Files that will be modified (no new files):

| File | What changes |
|------|--------------|
| `python/spinlab/scheduler.py` | Rename `_attempts_from_rows`→`attempts_from_rows`, `_events_from_rows`→`events_from_rows`, `Scheduler._load_all_sampler_states`→`Scheduler.sampler_states`. Update internal usages. |
| `python/spinlab/estimators/em_suite_sampler.py` | Rename `_gate_passes`→`gate_passes`. Update internal usages. |
| `python/spinlab/estimators/session_snapshot.py` | Rename `_running_min_clean`→`running_min_clean` (public). Update internal usages. Update `_gate_passes` imports. |
| `python/spinlab/estimators/live_view.py` | Update `_gate_passes` import. |
| `python/spinlab/estimators/segment_progress.py` | Update `_gate_passes` import. |
| `python/spinlab/practice_engine/rollout_matrix.py` | Update `_gate_passes` import. |
| `python/spinlab/routes/model.py` | Drop import of underscored helpers; reuse `sched.estimator` in `segment_history`; delete `_running_min_clean_for_route` + its comment, import from session_snapshot instead. |
| `python/spinlab/routes/practice_engine.py` | Drop import of `_gate_passes`; call `sched.sampler_states()` instead of `sched._load_all_sampler_states()`. |
| `python/spinlab/session_manager.py` | `_snapshot_inputs` uses `scheduler.sampler_states()` cache; falls back to a fresh empty `SamplerState` for active segments without a saved model_state row. |
| `scripts/em_suite_replay.py` | Update import (verify; only matches if script imports `_gate_passes`). |
| Test files | Mechanical import / call-site updates wherever the renamed symbol appears. |

---

## Task 1: Promote `events_from_rows` and `attempts_from_rows` to public

**Files:**
- Modify: `python/spinlab/scheduler.py` (definitions at lines 39 and 53; internal call sites at lines 254, 255, 267, 271, 317, 319)
- Modify: `python/spinlab/routes/model.py` (line 21 import + lines 86, 91, 169, 207, 245, 292)
- Modify: `python/spinlab/session_manager.py` (line 558 import + use in `_snapshot_inputs`)
- Modify: any test file that imports `_attempts_from_rows` / `_events_from_rows` (find with grep in Step 1)

- [ ] **Step 1: Grep every reference to the two underscored helpers**

Run: `rg -n "_attempts_from_rows|_events_from_rows" --type py`

Expected: prints every file + line in `python/spinlab/` and `tests/`. Note them — every match is something you'll edit in this task.

- [ ] **Step 2: Run baseline tests to confirm green**

Run: `python -m pytest -q`

Expected: `1161 passed, 1 warning`. If anything is red here, STOP and surface to the user — this plan assumes a clean baseline.

- [ ] **Step 3: Rename the two functions in `scheduler.py`**

In `python/spinlab/scheduler.py`, change two function definitions (lines 39 and 53):

```python
# was: def _attempts_from_rows(rows: list[AttemptRow]) -> list[AttemptRecord]:
def attempts_from_rows(rows: list[AttemptRow]) -> list[AttemptRecord]:
```

```python
# was: def _events_from_rows(rows: list[EventAttemptRow]) -> list[EventAttempt]:
def events_from_rows(rows: list[EventAttemptRow]) -> list[EventAttempt]:
```

Then update every internal call site in the same file (use the grep output from Step 1; expect ~6 internal calls in `_load_all_sampler_states`, `update_state_after_episode`, `rebuild_all_states`).

- [ ] **Step 4: Update `routes/model.py`**

In `python/spinlab/routes/model.py`:

Change the import at line 21:
```python
# was: from spinlab.scheduler import _attempts_from_rows, _events_from_rows
from spinlab.scheduler import attempts_from_rows, events_from_rows
```

Then update every call site (drop the leading underscore). Per Step 1's grep, expect ~6 calls.

- [ ] **Step 5: Update `session_manager.py`**

In `python/spinlab/session_manager.py:_snapshot_inputs` (~line 558):

```python
# was: from spinlab.scheduler import _events_from_rows
from spinlab.scheduler import events_from_rows
```

And update the single call inside that function from `_events_from_rows(...)` to `events_from_rows(...)`.

- [ ] **Step 6: Update tests**

For every test file in Step 1's grep output, replace `_attempts_from_rows` → `attempts_from_rows` and `_events_from_rows` → `events_from_rows` (both imports and call sites). Use Edit's `replace_all=true` per file.

- [ ] **Step 7: Run tests**

Run: `python -m pytest -q`

Expected: `1161 passed, 1 warning`. If any `ImportError: cannot import name '_events_from_rows'` or `_attempts_from_rows` shows up, you missed a site — re-run the Step 1 grep on the still-failing names and fix it.

- [ ] **Step 8: Run pyright on the changed files**

Run: `npx pyright python/spinlab/scheduler.py python/spinlab/routes/model.py python/spinlab/session_manager.py`

Expected: no NEW errors compared to baseline. (Existing pyright errors are tracked; just confirm the rename didn't introduce fresh ones.)

- [ ] **Step 9: Commit**

```bash
git add python/spinlab/scheduler.py python/spinlab/routes/model.py python/spinlab/session_manager.py tests/
git commit -m "$(cat <<'EOF'
refactor(scheduler): promote events_from_rows + attempts_from_rows to public

Three call sites outside scheduler.py — routes/model.py, session_manager.py,
practice_engine.py — already reach past the underscore. Drop the leading
underscore so the public surface matches actual usage; no semantic change.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Promote `Scheduler._load_all_sampler_states` to public `sampler_states`

**Files:**
- Modify: `python/spinlab/scheduler.py` (method definition ~line 233; internal call from `engine` property ~line 227)
- Modify: `python/spinlab/routes/practice_engine.py` (line 70)
- Modify: any test file that calls `_load_all_sampler_states`

- [ ] **Step 1: Grep references**

Run: `rg -n "_load_all_sampler_states" --type py`

Expected: 3-4 hits — the definition, the internal call from the `engine` property, the route call, possibly one test.

- [ ] **Step 2: Rename the method on `Scheduler`**

In `python/spinlab/scheduler.py`:

```python
# was: def _load_all_sampler_states(self) -> dict[str, SamplerState]:
def sampler_states(self) -> dict[str, SamplerState]:
```

(Keep the existing docstring — it documents the "rebuild from event table, not from state_json" decision and stays accurate.)

- [ ] **Step 3: Update the internal call from the `engine` property**

In the same file, around line 227 (inside the `engine` property body):

```python
self._engine = PracticeEngine(
    sampler_states=self.sampler_states(),
    N=self._practice_engine_rollouts,
    rng_seed=_DEFAULT_PRACTICE_ENGINE_RNG_SEED,
)
```

(Note: the keyword argument `sampler_states=` to `PracticeEngine()` stays — it's a different name in `PracticeEngine`'s signature. Only the receiver call changes.)

- [ ] **Step 4: Update `routes/practice_engine.py:70`**

In `python/spinlab/routes/practice_engine.py`, change line 70 from:

```python
states = sched._load_all_sampler_states()
```

to:

```python
states = sched.sampler_states()
```

The comment immediately above (lines 67-69) still applies — keep it.

- [ ] **Step 5: Update any test that called the private method**

For each match from Step 1 in `tests/`, replace `_load_all_sampler_states` → `sampler_states`.

- [ ] **Step 6: Run tests**

Run: `python -m pytest -q`

Expected: `1161 passed, 1 warning`.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/scheduler.py python/spinlab/routes/practice_engine.py tests/
git commit -m "$(cat <<'EOF'
refactor(scheduler): promote sampler_states() to public method

The practice_engine route already calls it as a documented "deliberate"
reach past the engine to enumerate ungated segments. Make the method
public so the access isn't a structural violation; behavior unchanged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Promote `_gate_passes` to public `gate_passes`

**Files:**
- Modify: `python/spinlab/estimators/em_suite_sampler.py` (definition ~line 307; internal uses)
- Modify: `python/spinlab/estimators/session_snapshot.py` (import + uses at lines 31, 65, 104)
- Modify: `python/spinlab/estimators/live_view.py` (import + uses)
- Modify: `python/spinlab/estimators/segment_progress.py` (import + uses)
- Modify: `python/spinlab/practice_engine/rollout_matrix.py` (import + uses)
- Modify: `python/spinlab/routes/practice_engine.py` (line 23 import + line 84)
- Modify: `scripts/em_suite_replay.py` (if it imports `_gate_passes`)
- Modify: test files referencing `_gate_passes`

- [ ] **Step 1: Grep every reference**

Run: `rg -n "_gate_passes" --type py`

Expected: definition + ~6 production files + tests. Note them.

- [ ] **Step 2: Rename in `em_suite_sampler.py`**

In `python/spinlab/estimators/em_suite_sampler.py` (~line 307):

```python
# was: def _gate_passes(state: SamplerState) -> bool:
def gate_passes(state: SamplerState) -> bool:
    """Prediction gate: nil-until-2 of each outcome and overall."""
    return (
        ...
    )
```

Update every internal call site in the same file (drop underscore).

- [ ] **Step 3: Update consumer modules**

For each of the following files, update the import line + every call site:

- `python/spinlab/estimators/session_snapshot.py`
- `python/spinlab/estimators/live_view.py`
- `python/spinlab/estimators/segment_progress.py`
- `python/spinlab/practice_engine/rollout_matrix.py`
- `python/spinlab/routes/practice_engine.py` (line 23 import; line 84 call inside the `for seg_id, state in states.items():` loop)
- `scripts/em_suite_replay.py` (only if it actually imports the helper — your Step 1 grep will tell you)

Example import update:

```python
# was: from spinlab.estimators.em_suite_sampler import _gate_passes, ...
from spinlab.estimators.em_suite_sampler import gate_passes, ...
```

- [ ] **Step 4: Update test files**

For every test file in Step 1's grep output, do the same rename. Use Edit's `replace_all=true` per file.

- [ ] **Step 5: Run tests**

Run: `python -m pytest -q`

Expected: `1161 passed, 1 warning`. ImportError on `_gate_passes` means you missed a file — re-grep and fix.

- [ ] **Step 6: Run pyright**

Run: `npx pyright python/spinlab/estimators/em_suite_sampler.py python/spinlab/estimators/session_snapshot.py python/spinlab/estimators/live_view.py python/spinlab/estimators/segment_progress.py python/spinlab/practice_engine/rollout_matrix.py python/spinlab/routes/practice_engine.py`

Expected: no new errors versus baseline.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/estimators/em_suite_sampler.py python/spinlab/estimators/session_snapshot.py python/spinlab/estimators/live_view.py python/spinlab/estimators/segment_progress.py python/spinlab/practice_engine/rollout_matrix.py python/spinlab/routes/practice_engine.py scripts/ tests/
git commit -m "$(cat <<'EOF'
refactor(em-suite-sampler): promote gate_passes() to public

Six consumers across estimators/, practice_engine/, routes/ already import
it. Drop the underscore so the import lines stop lying.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Consolidate `_running_min_clean_for_route` into shared `running_min_clean`

**Files:**
- Modify: `python/spinlab/estimators/session_snapshot.py` (lines 85-94 — rename `_running_min_clean` → `running_min_clean`; update internal use at line 68 and 81)
- Modify: `python/spinlab/routes/model.py` (line 303 caller; delete the `_running_min_clean_for_route` helper at lines 322-334 along with its "intentional duplication while the three call sites stabilize" comment)

- [ ] **Step 1: Read both functions side by side**

Read: `python/spinlab/estimators/session_snapshot.py:85-94` (`_running_min_clean`)
Read: `python/spinlab/routes/model.py:322-334` (`_running_min_clean_for_route`)

Confirm the only differences:
- Underscore-prefix naming
- The route version uses `.get()`, the snapshot version uses `[]` indexing
- The route version has no type annotation

Both consume episodes from `db.get_segment_attempts()` which returns `list[AttemptRow]` (TypedDict, `total=True`). Direct indexing is the right call per `feedback_no_defensive_gets_on_contract_fields`.

- [ ] **Step 2: Rename + drop underscore in `session_snapshot.py`**

In `python/spinlab/estimators/session_snapshot.py`, line 85:

```python
# was: def _running_min_clean(episodes: Sequence[AttemptRow]) -> float | None:
def running_min_clean(episodes: Sequence[AttemptRow]) -> float | None:
    floor: float | None = None
    for e in episodes:
        if not e["completed"] or e["invalidated"]:
            continue
        clean = e["clean_tail_ms"]
        if clean is None:
            continue
        floor = float(clean) if floor is None else min(floor, float(clean))
    return floor
```

Update the two internal uses at lines 68 and 81 (in `_baseline_for_segment`) — both already call `_running_min_clean(episodes)`; change to `running_min_clean(episodes)`.

- [ ] **Step 3: Update `routes/model.py` to import and use the shared helper**

At line 303 of `python/spinlab/routes/model.py`, replace the call to the local `_running_min_clean_for_route` with the shared helper.

Add the import near the existing imports at the top of the file:

```python
from spinlab.estimators.session_snapshot import running_min_clean
```

At line 303, change:

```python
cur = _running_min_clean_for_route(episodes)
```

to:

```python
cur = running_min_clean(episodes)
```

- [ ] **Step 4: Delete the local helper and its comment**

In `python/spinlab/routes/model.py`, delete lines 322-334 in their entirety:

```python
def _running_min_clean_for_route(episodes):
    """Helper for the route-bar floor_improvement aggregation. Same scan as
    session_snapshot._running_min_clean (intentional duplication while the
    three call sites stabilize; consolidate in a later cleanup pass)."""
    floor: float | None = None
    for e in episodes:
        if not e.get("completed") or e.get("invalidated"):
            continue
        clean = e.get("clean_tail_ms")
        if clean is None:
            continue
        floor = float(clean) if floor is None else min(floor, float(clean))
    return floor
```

The stabilization condition the comment described is satisfied by this consolidation.

- [ ] **Step 5: Run tests**

Run: `python -m pytest -q`

Expected: `1161 passed, 1 warning`. If `route_summary` route tests assert specific `floor_improvement_ms` values, they should still pass — behavior is byte-for-byte identical (TypedDict `[key]` lookups return the same values as `.get()` on rows that have those keys).

- [ ] **Step 6: Run pyright on the two files**

Run: `npx pyright python/spinlab/estimators/session_snapshot.py python/spinlab/routes/model.py`

Expected: no new errors. Pyright may report a freshly-typed call site at the route — that's the typing tightening up, which is the win.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/estimators/session_snapshot.py python/spinlab/routes/model.py
git commit -m "$(cat <<'EOF'
refactor(estimators): consolidate running_min_clean floor helper

Delete the routes/model.py copy (with its 'consolidate later' comment) in
favor of the session_snapshot.py version, now public. Drops the route-side
defensive .get() calls — db.get_segment_attempts returns AttemptRow
(TypedDict), direct indexing is correct.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Reuse `sched.estimator` in `segment_history` route

**Files:**
- Modify: `python/spinlab/routes/model.py` (lines 73-140 — the `segment_history` function; specifically lines 103-104 inline estimator construction and the `sched` fetch at line 117)

- [ ] **Step 1: Read `segment_history` end-to-end**

Read: `python/spinlab/routes/model.py:73-140`. Note that:
- Line 117 already fetches `sched = session.get_scheduler() if session.game_id is not None else None`
- Lines 103-104 construct a fresh `EmSuiteSamplerEstimator()` to compute `final_state` and `final_out`
- The estimator is stateless — instantiation is cheap — so this is a cosmetic cleanup, not a perf fix

- [ ] **Step 2: Move the scheduler fetch earlier and reuse its estimator**

Rewrite the relevant section. Replace the current order:

```python
    from spinlab.estimators.em_suite_sampler import EmSuiteSamplerEstimator
    est = EmSuiteSamplerEstimator()
    final_state = est.rebuild_state(all_records, events=events)
    final_out = est.model_output(final_state, completed, events=events)
    estimator_curves: dict[str, dict] = {
        est.name: {
            "total": {"expected_ms": [], "floor_ms": []},
            "clean": {"expected_ms": [], "floor_ms": []},
            "final_extras": (
                final_out.extras.to_dict() if final_out.extras is not None else None
            ),
        }
    }

    sched = session.get_scheduler() if session.game_id is not None else None
    selected_model = sched.estimator.name if sched is not None else None
```

with (note the ordering swap — `sched` fetched first, estimator chosen with a fallback for the no-game case):

```python
    sched = session.get_scheduler() if session.game_id is not None else None
    if sched is not None:
        est = sched.estimator
    else:
        # No game loaded — segment_history still works for a segment row that
        # exists in the DB (e.g. a segment from a previously-loaded game),
        # so build a stateless estimator just for this rendering pass.
        from spinlab.estimators.em_suite_sampler import EmSuiteSamplerEstimator
        est = EmSuiteSamplerEstimator()
    final_state = est.rebuild_state(all_records, events=events)
    final_out = est.model_output(final_state, completed, events=events)
    estimator_curves: dict[str, dict] = {
        est.name: {
            "total": {"expected_ms": [], "floor_ms": []},
            "clean": {"expected_ms": [], "floor_ms": []},
            "final_extras": (
                final_out.extras.to_dict() if final_out.extras is not None else None
            ),
        }
    }

    selected_model = sched.estimator.name if sched is not None else None
```

The fallback path matters because `db.get_segment_by_id(segment_id)` can succeed without a game being loaded in the session, and we still want a renderable history.

- [ ] **Step 3: Run tests**

Run: `python -m pytest -q`

Expected: `1161 passed, 1 warning`.

- [ ] **Step 4: Run pyright on `routes/model.py`**

Run: `npx pyright python/spinlab/routes/model.py`

Expected: no new errors.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/routes/model.py
git commit -m "$(cat <<'EOF'
refactor(routes/model): reuse scheduler.estimator in segment_history

The scheduler already owns the canonical EmSuiteSamplerEstimator instance;
constructing a fresh one in the route was redundant. Keep the stateless
fallback for the no-game-loaded case so a segment row can still render
its history from raw DB rows.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `_snapshot_inputs` uses cached sampler states

**Files:**
- Modify: `python/spinlab/session_manager.py` (lines 550-568 — `_snapshot_inputs`)
- Modify: `tests/unit/test_session_manager_snapshot.py` (add a test pinning behavior equivalence)

**Why this matters:** `_snapshot_inputs` is called once at practice/hyper-play start, but it currently fully re-replays every segment's event log through `replay_with_history(...)` even though the scheduler maintains a `sampler_states()` map of exactly those states. The replay is bounded by segment count (not a hot loop, per the 2026-06-03 scan N2 confirmation), so this is an architectural cleanup, not a perf win — but it kills the last remaining import of the (now public) `events_from_rows` from the session manager.

**Behavior equivalence:**
- For active segments WITH a `model_state` row: `scheduler.sampler_states()` returns the same `SamplerState` as `replay_with_history(events_from_rows(...))` would build, because both are rebuilt from the same event rows by the same code path.
- For active segments WITHOUT a `model_state` row (newly added, no attempts yet): the current code replays an empty event list, producing a default-constructed `SamplerState` that fails `gate_passes()`. The new code substitutes a default `SamplerState()` for the same effect.

- [ ] **Step 1: Confirm `SamplerState` can be constructed empty**

Read: `python/spinlab/estimators/em_suite_sampler.py` around the `class SamplerState` definition (~line 141). Confirm `SamplerState()` with no args is callable (it's a dataclass with all defaulted fields).

If it isn't (subclass of `EstimatorState` with required init args), use the same factory the test suite uses — grep for `SamplerState(` in `tests/unit/estimators/` to find the canonical empty-state factory and use that.

- [ ] **Step 2: Write the equivalence test first (RED)**

Add to `tests/unit/test_session_manager_snapshot.py`:

```python
def test_snapshot_inputs_uses_scheduler_cache_for_segments_with_state(
    real_session_manager_with_segments,
):
    """When a segment has a saved model_state, _snapshot_inputs reads the
    SamplerState from scheduler.sampler_states() rather than re-replaying
    the event log."""
    sm = real_session_manager_with_segments  # fixture: session + 2 segments,
                                              # one with attempts, one without
    sched = sm.scheduler
    assert sched is not None

    # Build the cache once so the next call should NOT re-replay.
    cached = sched.sampler_states()
    seg_with_state = next(iter(cached.keys()))

    inputs = sm._snapshot_inputs()

    by_seg = {seg_id: (state, eps) for seg_id, state, eps in inputs}
    # The cached state and the snapshot state must be the SAME object,
    # not just equal — proves we read from the cache, not a fresh replay.
    assert by_seg[seg_with_state][0] is cached[seg_with_state]
```

NOTE: if `real_session_manager_with_segments` doesn't already exist as a fixture, look in `tests/unit/test_session_manager_snapshot.py` and `tests/conftest.py` for the closest existing fixture and adapt. The test's spirit is: build a SessionManager with a real DB + scheduler + one segment that has events. Don't invent new infrastructure if the existing fixtures already cover this.

- [ ] **Step 3: Run the test and confirm it fails**

Run: `python -m pytest tests/unit/test_session_manager_snapshot.py::test_snapshot_inputs_uses_scheduler_cache_for_segments_with_state -v`

Expected: FAIL with an `AssertionError` — because the current code calls `replay_with_history` fresh, the SamplerState in `_snapshot_inputs`'s output is a different object than `sched.sampler_states()[seg_with_state]`.

- [ ] **Step 4: Rewrite `_snapshot_inputs` to use the cache**

In `python/spinlab/session_manager.py`, replace `_snapshot_inputs`:

```python
    def _snapshot_inputs(self):
        """Sequence of (seg_id, SamplerState, episodes) for every active segment.

        Called by _take_session_snapshot. Pulls per-segment SamplerStates from
        the scheduler's cached map; segments without a saved model_state row
        fall back to an empty SamplerState (which fails gate_passes and yields
        a None-baseline, matching the prior replay-of-empty-events behavior).
        Tests can override this method to bypass DB/scheduler plumbing.
        """
        from spinlab.estimators.em_suite_sampler import SamplerState

        if self.scheduler is None or self.state.game_id is None:
            return []
        cached = self.scheduler.sampler_states()
        out = []
        for seg in self.db.get_active_segments(self.state.game_id):
            state = cached.get(seg.id) or SamplerState()
            episodes = self.db.get_segment_attempts(seg.id)
            out.append((seg.id, state, episodes))
        return out
```

The `from spinlab.scheduler import events_from_rows` import and the `events = ...; state, _hist = replay_with_history(events)` lines disappear.

- [ ] **Step 5: Run the new test (GREEN)**

Run: `python -m pytest tests/unit/test_session_manager_snapshot.py::test_snapshot_inputs_uses_scheduler_cache_for_segments_with_state -v`

Expected: PASS.

- [ ] **Step 6: Run the full snapshot test file to confirm no regressions**

Run: `python -m pytest tests/unit/test_session_manager_snapshot.py -v`

Expected: every existing test still passes (the closed-form baseline test at lines 142-222 is the load-bearing one — it must still produce the same numbers).

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest -q`

Expected: `1162 passed, 1 warning` (one more than baseline — the new test).

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/session_manager.py tests/unit/test_session_manager_snapshot.py
git commit -m "$(cat <<'EOF'
refactor(session-manager): snapshot reads cached sampler states

_snapshot_inputs no longer re-replays event history per segment; it pulls
SamplerStates from scheduler.sampler_states() and falls back to a fresh
empty state for active segments that have no model_state row yet (matches
the prior replay-of-empty-events behavior).

Removes the last consumer of events_from_rows outside scheduler.py.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Finalization

- [ ] **Step 1: Run the full test suite one more time**

Run: `python -m pytest -q`

Expected: `1162 passed, 1 warning`.

- [ ] **Step 2: Run pyright on all touched files**

Run: `npx pyright python/spinlab/scheduler.py python/spinlab/routes/model.py python/spinlab/routes/practice_engine.py python/spinlab/session_manager.py python/spinlab/estimators/em_suite_sampler.py python/spinlab/estimators/session_snapshot.py python/spinlab/estimators/live_view.py python/spinlab/estimators/segment_progress.py python/spinlab/practice_engine/rollout_matrix.py`

Expected: NO new errors versus the baseline count. If pyright reports something fresh, fix it before merging.

- [ ] **Step 3: Verify ruff is clean on touched files**

Run: `ruff check python/spinlab/`

Expected: no new errors beyond what was present at branch HEAD before the plan started.

- [ ] **Step 4: Verify the branch log reads cleanly**

Run: `git log --oneline main..HEAD`

Expected: the two pre-existing commits (`854548e`, `909122f`) plus 6 new commits from Tasks 1-6. Each commit message names what changed and why.

The scan file (`docs/superpowers/scans/2026-06-04-improve.md`) is already on this branch from the /improve scan that produced this plan; it will land with the merge.

- [ ] **Step 5: Hand off to the operator**

Surface the commit list and let the operator decide between fast-forward merge, PR, or leave-for-later (the /improve skill's Phase 10 finalize). DO NOT auto-merge.

---

## Self-Review Checklist (for the implementing agent)

After each task, before moving to the next:

1. **Did you re-grep the symbol you renamed?** If `_events_from_rows` shows up anywhere after Task 1's commit, you missed a site — pyright won't always catch it (e.g. string references in docs are fine but a stale call is not).
2. **Did the test count change?** Task 6 adds exactly one test. All other tasks should leave the test count untouched.
3. **Did you introduce a defensive `.get()` on a contract field?** Per `feedback_no_defensive_gets_on_contract_fields`, don't — index directly.
4. **Did you add a comment?** Per CLAUDE.md, only if the WHY is non-obvious. The `_running_min_clean_for_route` comment goes away with the function; do not replace it.
5. **Did the plan tell you to add a fallback for a scenario that can't happen?** It didn't — and you shouldn't either.

If a step has surprising fallout (a test that was supposed to be unaffected starts failing, an import you didn't think existed), STOP and surface to the user with the symptom and your hypothesis. Don't paper over it.

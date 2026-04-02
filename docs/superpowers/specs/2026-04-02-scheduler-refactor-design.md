# Scheduler Refactor: Extract Responsibilities from Private Functions

**Date:** 2026-04-02
**Approach:** Factory classmethod + push priors to estimators + inline/relocate helpers

## Motivation

`scheduler.py` has 8 private functions doing 3 distinct jobs beyond its core coordination role. This refactor moves each responsibility to the type that owns the data shape, improving legibility and unit testability without introducing new files or abstractions.

## Change 1: `SegmentWithModel.load_all` factory classmethod

**What moves:** `Scheduler._load_segments_with_model` (~50 lines) → `SegmentWithModel.load_all(db, game_id, selected_model)` classmethod in `allocators/__init__.py`.

**Logic:** Runs 3 DB queries (`get_all_segments_with_model`, `load_all_model_states_for_game`, `compute_golds`), deserializes `ModelOutput` from JSON, extracts `n_completed`/`n_attempts` from state JSON, assembles `SegmentWithModel` instances.

**Callers that change:**
- `Scheduler._load_segments_with_model()` → `SegmentWithModel.load_all(self.db, self.game_id, self.estimator.name)`
- `Scheduler` methods `pick_next`, `peek_next_n`, `get_all_model_states`, `get_model_api_state` all go through the above.

**Not changing:** `SessionManager._build_practice_state` uses `db.get_all_segments_with_model` for a lighter view — stays as-is.

## Change 2: Push priors down to `Estimator` base class

**What moves:** `Scheduler._get_priors` (isinstance-based dispatch) → `Estimator.get_priors(db, game_id)` default method returning `{}`, overridden by `KalmanEstimator`.

**Base class addition** (in `estimators/__init__.py`):
```python
def get_priors(self, db: "Database", game_id: str) -> dict:
    """Return population priors for init_state. Default: no priors."""
    return {}
```

**Kalman override:** Moves the DB-loading logic (load all kalman states, deserialize, call `get_population_priors`) into `KalmanEstimator.get_priors()`. The existing `get_population_priors(all_states)` math method stays, called internally by the new override.

**Rolling mean / exp_decay:** Inherit the empty-dict default. No changes.

**Scheduler caller becomes:** `est.get_priors(self.db, self.game_id)` — no isinstance, uniform dispatch.

**Future-proof:** The `(db, game_id)` signature gives future Bayesian estimators full access to cross-segment/cross-game population data.

## Change 3: Estimator helpers — inline and relocate

### Inline `_all_estimators*` family

`_all_estimators`, `_all_estimators_names`, `_all_estimators_info` are one-liner wrappers around the registry. Inline at their 2 call sites:
- `process_attempt` loop: `for est in [get_estimator(n) for n in list_estimators()]:`
- `get_model_api_state`: build info dicts inline from `list_estimators()` / `get_estimator()`

### Relocate `_deserialize_state`

**What moves:** `Scheduler._deserialize_state` + module-level `_STATE_CLASSES` dict → `EstimatorState.deserialize(estimator_name, state_json)` classmethod in `estimators/__init__.py`.

The `_STATE_CLASSES` dict moves to `EstimatorState` as a class-level registry. Registration stays explicit (3 estimators, straightforward).

**Scheduler caller becomes:** `EstimatorState.deserialize(est.name, row["state_json"])`

## What Scheduler looks like after

Scheduler retains only coordination:
- `__init__` — wire up estimator + allocator from DB config
- `_sync_config_from_db` — reload estimator/allocator preferences
- `pick_next` / `peek_next_n` — delegate to `SegmentWithModel.load_all` + allocator
- `process_attempt` — run all estimators, save state
- `get_model_api_state` / `get_all_model_states` — thin wrappers over `SegmentWithModel.load_all`
- `switch_allocator` / `switch_estimator` / `rebuild_all_states` — config + batch ops

Private function count drops from 8 to 1 (`_sync_config_from_db`).

## Testing changes

### New tests

**`SegmentWithModel.load_all`** — The assembly logic currently has zero direct tests. It's only tested indirectly through `Scheduler.pick_next` and dashboard integration. New tests should cover:
- Basic assembly: 3 DB queries → correct `SegmentWithModel` instances with model_outputs, golds, n_completed/n_attempts populated
- Empty game: no segments → empty list
- Segments with no model state yet (fresh game) → `SegmentWithModel` with empty `model_outputs`, zero counts
- Malformed JSON in model state rows → skipped gracefully (matching existing behavior)

These are fast, in-memory SQLite tests using the existing `db_with_segments` fixture pattern from `test_scheduler_kalman.py`.

**`EstimatorState.deserialize`** — Currently tested only as a side effect of `Scheduler.process_attempt` round-trips. New tests:
- Known estimator name + valid JSON → correct state type
- Unknown estimator name → ValueError
- Malformed JSON → error (not silent corruption)

**`KalmanEstimator.get_priors`** — Currently untested (the isinstance dispatch in Scheduler was never directly exercised). New tests:
- No mature states → returns defaults
- Several mature kalman states in DB → returns averaged population priors
- Can live in `test_kalman.py` alongside existing estimator tests

### Tests to remove or simplify

**Nothing to delete.** The existing `test_scheduler_kalman.py` tests all exercise public Scheduler API (`pick_next`, `process_attempt`, `peek_next_n`, `switch_allocator`, `rebuild_all_states`). These stay — they now also serve as integration tests proving the extracted pieces wire together correctly.

However, `test_scheduler_kalman.py` currently _implicitly_ tests assembly correctness through assertions like "pick_next returns a SegmentWithModel with non-None model output." Once `load_all` has its own focused tests, the scheduler tests can **relax** those assertions to just "pick_next returns a segment" without re-verifying model output shapes. This is optional cleanup — not blocking.

### Existing tests that should keep passing unchanged

- `test_scheduler_kalman.py` — public API unchanged
- `test_allocators.py` — `SegmentWithModel` shape unchanged, tests construct instances directly
- `test_estimator_sanity.py` — tests estimator public API, unaffected
- `test_kalman.py`, `test_rolling_mean.py`, `test_exp_decay.py` — individual estimator tests, unaffected
- `test_session_manager.py`, `test_practice.py`, `test_dashboard_integration.py` — use Scheduler indirectly, public API unchanged

## Files modified

1. `python/spinlab/allocators/__init__.py` — add `load_all` classmethod to `SegmentWithModel`
2. `python/spinlab/estimators/__init__.py` — add `get_priors` to `Estimator`, add `deserialize` to `EstimatorState`
3. `python/spinlab/estimators/kalman.py` — add `get_priors` override
4. `python/spinlab/scheduler.py` — remove 7 private functions, update callers

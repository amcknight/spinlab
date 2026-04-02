# Weighted Allocator Mix: Multiple Active Allocators with Configurable Weights

**Date:** 2026-04-02
**Approach:** MixAllocator wrapper with weighted random dispatch, multi-handle range slider UI

## Motivation

Currently the scheduler supports a single active allocator (Greedy, Random, or Round-Robin). This limits practice strategy flexibility. A weighted blend lets users configure allocator proportions (e.g., 70% Greedy, 20% Random, 10% Round-Robin), directly controlling how practice time is distributed across allocation strategies.

## Change 1: MixAllocator

**New file:** `python/spinlab/allocators/mix.py`

A wrapper that holds multiple allocator instances with weights and dispatches each pick via weighted random selection.

```python
@dataclass
class MixAllocator:
    entries: list[tuple[Allocator, float]]

    def pick_next(self, segment_states: list[SegmentWithModel]) -> str | None:
        if not segment_states or not self.entries:
            return None
        allocators, weights = zip(*self.entries)
        chosen = random.choices(allocators, weights=weights, k=1)[0]
        return chosen.pick_next(segment_states)
```

Key properties:
- **Not registered** in the allocator registry. It's an internal scheduler primitive, not user-selectable by name.
- Holds live allocator instances. Stateful allocators (Round-Robin) retain their internal state across picks even when other allocators win the die roll.
- If only one allocator has non-zero weight, behavior is identical to the current single-allocator system.
- Allocators at 0% weight are omitted from `entries` entirely.

## Change 2: Remove peek_next_n

With weighted random dispatch, the "next" segment is non-deterministic. Rather than show a misleading queue, remove the peek system entirely.

**Allocator ABC** (`allocators/__init__.py`): Remove `peek_next_n` abstract method.

**Individual allocators:** Remove `peek_next_n` from `GreedyAllocator`, `RandomAllocator`, `RoundRobinAllocator`.

**Scheduler:** Remove `Scheduler.peek_next_n()`.

**Practice loop** (`practice.py`): Remove `self.queue` assignment (line 95).

**Session manager** (`session_manager.py`): Remove queue building (lines 167-169) and `"queue"` from state broadcast.

**Dashboard HTML** (`static/index.html`): Remove `<h3>Up Next</h3>` and `<ul id="queue">`.

**Dashboard JS** (`static/model.js`): Remove queue rendering (lines 78-84).

**Dashboard CSS** (`static/style.css`): Remove `#queue` rules.

**Tests:** Remove `peek_next_n` tests from `test_allocators.py` and any other test files.

## Change 3: Persistence and Scheduler

### Database

Store weights as a JSON blob in the existing `allocator_config` key-value table:

- **Key:** `"allocator_weights"`
- **Value:** `{"greedy": 70, "random": 20, "round_robin": 10}`
- Values are integer percentages, must sum to 100.
- Allocators at 0% are omitted from the JSON.
- If key is missing, default to uniform distribution across all registered allocators.

The old `"allocator"` config key is deleted from `allocator_config` on startup if present (no migration needed).

No schema migration needed — reuses existing `allocator_config` table with `save_allocator_config` / `load_allocator_config`.

### Scheduler changes

- **`__init__`:** Load `"allocator_weights"` from DB, build a `MixAllocator`. Falls back to uniform distribution if no saved config.
- **`_sync_config_from_db`:** Compare stored weights JSON to current weights; rebuild `MixAllocator` if changed.
- **`set_allocator_weights(weights: dict[str, int])`:** Replaces `switch_allocator()`. Validates all names are registered and values sum to 100. Saves to DB, rebuilds `MixAllocator`.
- **`switch_allocator()`:** Removed.
- **`self.allocator`:** Type changes from `Allocator` to `MixAllocator`.

## Change 4: API and Dashboard UI

### API

Replace `POST /api/allocator` with `POST /api/allocator-weights`.

```
Request:  {"greedy": 70, "random": 20, "round_robin": 10}
Response: {"weights": {"greedy": 70, "random": 20, "round_robin": 10}}
```

Validation: all names must be registered allocators, values must be non-negative integers, sum must be 100. Returns 400 otherwise.

State broadcast sends `"allocator_weights": {...}` instead of `"allocator": "greedy"`.

### Dashboard UI

Replace the `<select>` dropdown with a **multi-handle colored range slider**.

**Slider behavior:**
- One horizontal bar divided into colored segments, one per allocator.
- Each allocator has a fixed color (e.g., Greedy = green, Random = blue, Round-Robin = orange).
- Draggable handles between segments resize the two adjacent segments.
- Snaps to integer percentages.
- 0% segments fully collapse; their handle remains as a thin grab line (2-3px) at the boundary so the user can drag it back open.
- On drag-end (mouseup/touchend), POST new weights to `POST /api/allocator-weights`.

**Legend row** below the slider:
```
|████████████████████|██████|█|

● Greedy 70%    ● Random 20%    ● Round-Robin 10%
```

Each allocator gets its colored dot + name + current percentage. Always visible regardless of segment width, always in the same order. No inline labels on the slider bar itself.

**SSE sync:** On state update, slider handle positions and legend percentages update to match received `allocator_weights`.

**Implementation:** Pure vanilla JS + CSS, no library dependencies (consistent with existing static assets).

## Files modified

1. `python/spinlab/allocators/mix.py` — new `MixAllocator` class
2. `python/spinlab/allocators/__init__.py` — remove `peek_next_n` from `Allocator` ABC
3. `python/spinlab/allocators/greedy.py` — remove `peek_next_n`
4. `python/spinlab/allocators/random.py` — remove `peek_next_n`
5. `python/spinlab/allocators/round_robin.py` — remove `peek_next_n`
6. `python/spinlab/scheduler.py` — replace single allocator with `MixAllocator`, replace `switch_allocator` with `set_allocator_weights`, remove `peek_next_n`
7. `python/spinlab/practice.py` — remove `self.queue` assignment
8. `python/spinlab/session_manager.py` — remove queue building, add `allocator_weights` to state broadcast, remove `allocator` from state
9. `python/spinlab/dashboard.py` — replace `/api/allocator` endpoint with `/api/allocator-weights`
10. `python/spinlab/static/index.html` — replace allocator `<select>` with slider container, remove queue HTML
11. `python/spinlab/static/model.js` — slider logic, legend rendering, weight sync
12. `python/spinlab/static/style.css` — slider and legend styles, remove queue styles

## Testing

### New tests

**`MixAllocator`:**
- Single allocator at 100% — behaves identically to that allocator alone
- Uniform weights — over many picks, each allocator is selected roughly equally
- Zero-weight allocators omitted — never picked
- Empty segment list — returns None
- Round-Robin state preservation — RR index advances only when RR wins the die roll, resumes correctly

**`Scheduler.set_allocator_weights`:**
- Valid weights save to DB and rebuild MixAllocator
- Weights not summing to 100 — raises error
- Unknown allocator name — raises error
- Sync from DB picks up external weight changes

**`POST /api/allocator-weights`:**
- Valid request returns weights
- Invalid sum returns 400
- Unknown allocator name returns 400

### Tests to update

- `test_allocators.py` — remove all `peek_next_n` tests
- `test_scheduler_kalman.py` — update `switch_allocator` calls to `set_allocator_weights`, remove `peek_next_n` usage
- `test_practice.py` — remove queue assertions
- `test_session_manager.py` — update state assertions (`allocator_weights` instead of `allocator`, no `queue`)
- `test_dashboard_integration.py` — update allocator endpoint tests

### Tests unaffected

- `test_estimator_sanity.py`, `test_kalman.py`, `test_rolling_mean.py`, `test_exp_decay.py` — estimator tests, no allocator involvement

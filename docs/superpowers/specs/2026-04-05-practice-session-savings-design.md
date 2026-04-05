# Practice Session Time Savings — Design

**Date:** 2026-04-05
**Status:** Approved

## Motivation

Practice mode currently shows per-segment estimates (`expected_ms`, `ms_per_attempt`) but has no headline metric answering "is this practice session paying off?". A user can see their trend per segment but cannot see the cumulative impact across the full run.

This design adds a **session-scoped "time saved" metric**: snapshot the sum of expected segment times at session start, then continuously display the delta as estimates improve.

## Why only the delta (no absolute sum)

Summing `expected_ms` across practiced segments does **not** yield a real run time. Overworld movement, cutscenes, and other non-practiced content are excluded. The absolute sum is effectively meaningless to the user, but the *difference* between two sums (initial vs. current, over the same segment set) is meaningful: it measures improvement in expectations since practice began.

We therefore emit only the savings delta, not the underlying sums.

## Data Flow

**Snapshot at session start.** `PracticeSession.start()` computes and caches:

- `initial_expected_total_ms` — sum of `model_outputs[selected].total.expected_ms`
- `initial_expected_clean_ms` — sum of `model_outputs[selected].clean.expected_ms`

The snapshot is frozen for the lifetime of the session. Switching estimators mid-session does **not** re-snapshot (see "Estimator switching" below).

**Live recompute on each state build.** `StateBuilder._build_practice_state()` sums the current `expected_ms` across the same segment set using the currently selected estimator, then emits on the `session` dict:

- `saved_total_ms = initial_expected_total_ms − current_total_sum`
- `saved_clean_ms = initial_expected_clean_ms − current_clean_sum`

Positive values mean time saved. Negative values mean regression.

## Segment Scope

The sum includes exactly the segments the scheduler would practice: the result of
`SegmentWithModel.load_all(db, game_id, estimator_name)` filtered to
`s.state_path and os.path.exists(s.state_path)` — the same filter used in
[`Scheduler.pick_next`](../../python/spinlab/scheduler.py) at line 83.

In the current codebase this set is effectively the Cold CP segments. If Hot variants
become practicable later, they will be included automatically.

## Edge Cases

**Missing estimates.** If a segment has no `expected_ms` for the selected estimator (e.g. never attempted), it is excluded from both the initial and current sums. A segment acquiring its first estimate mid-session will contribute to the current sum but not the initial, creating a small negative savings bump. This is acceptable and documented in code comments.

**All segments missing data.** If no segment has an estimate at session start, both `initial_expected_total_ms` and `initial_expected_clean_ms` are `None`. Savings fields emit as `None`. The UI hides the savings panel.

**Estimator switching.** If the user switches estimators mid-session, the initial snapshot stays fixed (computed with the original estimator), but the current sum uses the new estimator. This causes a discontinuous jump in savings, which reflects "compared to what the original estimator predicted at start." Acceptable.

**Server restart.** The snapshot is in-memory only. If the server restarts mid-session, savings reset to 0. Persisting the snapshot to the `sessions` table was considered (Approach B in brainstorm) and rejected as overkill for a modest edge case.

**Total vs. clean divergence.** Computed independently with their own initial snapshots. A segment without a clean estimate contributes 0 to the clean sum (not the total sum).

## Backend Implementation

**`PracticeSession`** ([python/spinlab/practice.py](../../python/spinlab/practice.py)):

New fields:
```python
initial_expected_total_ms: float | None = None
initial_expected_clean_ms: float | None = None
```

New helper (module-level or instance method):
```python
def _snapshot_expected_times(self, estimator_name: str) -> tuple[float | None, float | None]:
    """Sum expected_ms across practicable segments. (total, clean), None if all missing."""
```

The helper:
1. Calls `SegmentWithModel.load_all(self.db, self.game_id, estimator_name)`
2. Filters to segments where `state_path` exists on disk
3. Sums `model_outputs[estimator_name].total.expected_ms` (skipping None)
4. Sums `model_outputs[estimator_name].clean.expected_ms` (skipping None)
5. Returns `(None, None)` if every segment lacked both estimates

In `start()`, after `self.db.create_session(...)`, call the helper with
`self.scheduler.estimator.name` and store the results. Add a public method
`current_expected_times() -> tuple[float | None, float | None]` that calls the
same helper with the scheduler's *current* estimator name.

**`StateBuilder._build_practice_state`** ([python/spinlab/state_builder.py](../../python/spinlab/state_builder.py)):

After building the `session` dict, call `ps.current_expected_times()` and compute:

```python
saved_total = (ps.initial_expected_total_ms - current_total) if ps.initial_expected_total_ms is not None and current_total is not None else None
saved_clean = (ps.initial_expected_clean_ms - current_clean) if ps.initial_expected_clean_ms is not None and current_clean is not None else None
base["session"]["saved_total_ms"] = saved_total
base["session"]["saved_clean_ms"] = saved_clean
```

**Why in PracticeSession, not Scheduler:** the snapshot is session-scoped state. The Scheduler is long-lived per game. Keeping the snapshot next to `segments_attempted`/`segments_completed` on `PracticeSession` keeps session lifetime state localized.

## Frontend

**Types** ([frontend/src/types.ts](../../frontend/src/types.ts)):

Extend the session interface:
```ts
saved_total_ms: number | null
saved_clean_ms: number | null
```

**UI placement** — savings panel at the **top of the practice card**, above `#current-goal`:

```
┌────────────────────────────────────┐
│  TIME SAVED THIS SESSION           │
│  +3.2s total  ·  +1.8s clean       │
├────────────────────────────────────┤
│  Current: L1 start > cp1           │
│  ... (existing practice card)      │
```

- Title: small label "Time saved this session"
- Values: large/prominent, total and clean side-by-side
- Sign prefix: `+` for savings (positive delta), `-` for regression; green/muted-red coloring
- Formatting: reuse existing `formatTime()` from [format.ts](../../frontend/src/format.ts) with a sign prefix; sub-second as e.g. `+0.3s`
- Hide the whole panel when `saved_total_ms` is null

**DOM** — add `<div id="savings-panel">` in [frontend/index.html](../../frontend/index.html) inside the practice card, before `#current-goal`.

**Rendering** — new helper `updateSavingsPanel(session)` called from `updatePracticeCard` in [frontend/src/model.ts](../../frontend/src/model.ts).

## Testing

**Backend** ([tests/test_practice.py](../../tests/test_practice.py)):

1. `test_snapshot_expected_times_at_start` — `start()` populates both snapshot fields correctly
2. `test_snapshot_skips_segments_without_state_path` — segments lacking an on-disk state_path are excluded
3. `test_snapshot_skips_segments_without_estimate` — None `expected_ms` contributes 0
4. `test_snapshot_all_missing_returns_none` — no estimates → `(None, None)`
5. `test_current_expected_times_reflects_model_updates` — `process_attempt` changes the current sum

**StateBuilder** ([tests/test_dashboard_integration.py](../../tests/test_dashboard_integration.py) or equivalent):

6. `test_practice_state_emits_saved_ms` — state includes `saved_total_ms`, `saved_clean_ms` with correct sign
7. `test_practice_state_saved_ms_null_when_no_snapshot` — null snapshot propagates to null savings

**Frontend** ([frontend/src/](../../frontend/src/), Vitest):

8. `formatSavings` helper: `+3.2s` / `-1.1s` / hidden for null
9. `updateSavingsPanel` DOM test: renders both values, hides when both null

## Out of Scope

- Per-estimator savings comparison (only the selected estimator is shown)
- Persistence of snapshot across server restarts
- Uncertainty bands on savings (would require surfacing Kalman variance)
- Historical savings across multiple sessions

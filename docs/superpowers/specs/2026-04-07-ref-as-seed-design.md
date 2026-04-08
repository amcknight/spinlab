# Reference Run Seeding — Design

**Date:** 2026-04-07
**Status:** Draft

## Motivation

When a player completes a reference run, SpinLab captures segments and save states but discards all timing information. The estimator starts cold — it has no data until the player practices each segment at least once. This means the first practice session is flying blind: the scheduler has no expected times, the allocator has no model output to rank segments, and the overlay shows "?" for every comparison time.

Reference runs already produce real, observable segment times. Using them as seed data gives the estimator an initial estimate from the player's own gameplay, so practice is informed from the first attempt.

## Prerequisite: Death Penalty in Practice Total Time

Reference run times include real death overhead (death animation + respawn ≈ 2-4 seconds per death in SMW). Practice mode currently skips this: on death, the save state reloads instantly and the timer keeps running without accounting for the time a real run would have lost. This makes practice Total times systematically lower than reference Total times for the same quality of play.

Before seeding reference times, practice Total time must be made comparable.

## Part 1: Death Penalty

### Config

Add `death_penalty_ms` as a per-game config field in `conditions.yaml` (the existing per-game config location):

```yaml
# games/<game_id>/conditions.yaml
death_penalty_ms: 3200   # ms added to practice timer per death (SMW standard retry)
conditions:
  - name: powerup
    ...
```

Default: `3200` (standard SMW death animation + respawn). Fast-retry romhacks override to a lower value (e.g., `1000`). The value represents the time between the death frame and the frame the player regains control after respawn — measurable from any reference run by diffing the `death` and `spawn` event `timestamp_ms` values.

### Lua Changes

The `death_penalty_ms` value is sent to Lua as a new field on the `practice_load` command (alongside existing fields like `expected_time_ms`). This keeps the penalty segment-scoped and avoids separate config-sync commands.

On death during practice (`handle_practice`, `PSTATE_PLAYING`):

1. Increment `practice.deaths` counter (new field, starts at 0 on each segment load).
2. Add `practice.segment.death_penalty_ms` to `practice.elapsed_ms`.
3. Reload the save state (existing behavior).

The timer now reflects total time including death overhead. On `attempt_result`, include `deaths = practice.deaths` in the event payload.

### Python Changes

- `ConditionRegistry.load_for_game()` reads `death_penalty_ms` from `conditions.yaml` (default `3200`).
- `PracticeLoadCmd` gets a new `death_penalty_ms` field.
- `PracticeSession.run_one()` passes `death_penalty_ms` from the registry into the command.
- `practice.py` `_process_result()` already reads `deaths` from the event — no change needed there.

### Overlay

The practice overlay timer already shows elapsed time; it will now naturally include death penalty time. No overlay changes needed. The color comparison against `expected_time_ms` remains correct because the expected time (once seeded) also includes death overhead.

## Part 2: Reference Segment Timing

### When Timing Happens

During a reference or replay capture, the Lua script emits `level_entrance`, `checkpoint`, and `level_exit` events with `timestamp_ms` values. The Python `ReferenceCapture` already pairs these events to create segments. We add timing by recording `timestamp_ms` on `pending_start` and computing the delta when closing a segment.

### Data Captured Per Segment

When `_close_segment` runs, compute:

- `time_ms = end_timestamp_ms - start_timestamp_ms`
- `deaths` = count of death events between start and end (tracked via a counter, reset on each new `pending_start`)
- `clean_tail_ms` = if deaths > 0, time from last spawn to segment end; if deaths == 0, equals `time_ms`

These three values are exactly what `AttemptRecord` needs.

### Where Timing is Stored

Store segment times on `ReferenceCapture` as a list of `RefSegmentTime` dataclass instances accumulated during the capture run:

```python
@dataclass
class RefSegmentTime:
    segment_id: str
    time_ms: int
    deaths: int
    clean_tail_ms: int
```

The list is accessible via `ReferenceCapture.segment_times` after the run completes.

### No Schema Changes

Reference times are not stored in a new table. They flow into the existing `attempts` table as seed attempts (Part 3). The `RefSegmentTime` dataclass is transient — it lives only during the capture run and is consumed at draft-save time.

## Part 3: Seeding Attempts on Draft Save

### When Seeding Happens

When the user saves a draft (`CaptureController.save_draft()`), after promoting the capture run, insert seed attempts for each segment that has timing data.

### Seed Attempt Shape

For each `RefSegmentTime`:

```python
Attempt(
    segment_id=rst.segment_id,
    session_id=capture_run_id,       # use the capture run ID as session
    completed=True,                   # reference segments are always completed
    time_ms=rst.time_ms,
    deaths=rst.deaths,
    clean_tail_ms=rst.clean_tail_ms,
    source=AttemptSource.REFERENCE,   # new enum value
)
```

Add `REFERENCE = "reference"` to `AttemptSource`.

### Estimator Initialization

After inserting seed attempts, call `scheduler.process_attempt()` for each seed. This initializes the estimator state with real data so that the first practice session has model output from the start.

Alternatively, call `scheduler.rebuild_all_states()` once after all seeds are inserted. This is simpler and handles the case where multiple estimators need initialization. The rebuild approach is preferred since seeding happens once at draft-save time, not in a hot path.

### Idempotency

If a reference run is re-saved (same capture run ID), the existing seed attempts are already in the DB. `rebuild_all_states` is idempotent — it replays all attempts including seeds. No duplicate-detection logic needed beyond what the DB already provides (attempts have autoincrement IDs, not unique constraints on segment+session).

### What the Estimator Sees

The estimator processes attempts in `created_at` order. Seed attempts from the reference run have `created_at` set to the draft-save timestamp. Subsequent practice attempts have later timestamps. The estimator treats the seed as the first data point and updates from there — exactly what we want.

## Edge Cases

**Player dies during reference and never completes the segment.** The segment is never closed (`_close_segment` is not called), so no `RefSegmentTime` is recorded. No seed attempt is created. Correct behavior.

**Player walks away mid-level (AFK).** The `time_ms` delta will be inflated. This is acceptable: the estimator will quickly converge away from the outlier once real practice data arrives. If it becomes a problem, we could add a sanity cap (e.g., discard reference times > 5x the median), but YAGNI for now.

**Segment already has practice attempts.** Seed attempts are inserted alongside existing attempts. `rebuild_all_states` replays everything in order. The seed becomes one more data point — it doesn't override existing data.

**No deaths in reference segment.** `deaths=0`, `clean_tail_ms=time_ms`. Both total and clean estimators get seeded.

**Deaths in reference segment.** `deaths>0`, `clean_tail_ms` reflects only the final clean run from last spawn to segment end. Total estimator gets the full time; clean estimator gets the tail time. Both are real measurements.

## Not In Scope

- **Overworld / cutscene timing.** Different problem, different design.
- **Auto-measuring death penalty.** Could compute from reference death→spawn deltas in the future; config value is sufficient now.
- **Reference time display on dashboard.** The model tab already shows expected times once the estimator has data. No separate "reference time" column needed.
- **Replay-sourced seeding.** Replay fires the same events; this design works for replay too, but we don't need to call it out separately.

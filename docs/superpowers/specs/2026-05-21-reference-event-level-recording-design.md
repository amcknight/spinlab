# Reference event-level recording — design spec

**Date:** 2026-05-21
**Status:** Approved
**Sibling work:** Cold-state variant-selection fix (lands first, separate commit; see note at bottom).

## Problem

The reference recorder currently writes one aggregate row per segment to `recorded_segment_times` (`{time_ms, deaths, clean_tail_ms}`). At finalize, `_seed_reference_attempts` calls the legacy `log_attempt` shim, which invokes `_split_episode_into_events` to **synthesize** per-event rows by subtracting a 3.2s-per-death penalty constant and dividing the leftover wall-clock evenly across deaths. The synthesized `time_ms` values do not reflect real wall-clock between death events.

The v07 segments model treats each row in `attempts` as one died-or-survived event with raw wall-clock since the previous event (no penalty math) — the same shape practice writes today via `EventAttemptEmission` → `db.log_event_attempt`. Reference is the only source still going through the lossy synthesis path. Reference deaths are Andrew's very first data points for the v07 model; they need to carry real per-event times.

## Approach

The reference recorder buffers events in memory per segment and writes them as a batch at segment close (one row per death + one row for the closing checkpoint/goal). Same crash-safety bound as today (completed segments durable; in-flight segment lost). `recorded_segment_times` is dropped because its only role was being drained at finalize, and that role goes away.

The structural constraint that drove buffering over true mid-segment writes: a segment's `segment_id` is computed from both start and end waypoint hashes ([Segment.make_id](../../python/spinlab/models.py#L101)), so it isn't known until segment close. Mid-segment death events have no `segment_id` to attach to yet. Restructuring `segment_id` to not include the end waypoint would touch how segments are keyed everywhere — practice, replay, waypoint matching — for a marginal crash-recovery win. Out of scope here.

## Data model

**`attempts` table:** no schema change. Already supports the relevant columns: `(segment_id, capture_run_id, episode_id, outcome, time_ms, source='reference', created_at)`. We start populating it directly from the recorder during reference recording.

**`recorded_segment_times` table:** DROP via new migration `python/spinlab/db/migrations/0004_drop_recorded_segment_times.sql`.

Episode semantics for reference (decided in brainstorming): one fresh `episode_id` per segment-pass — minted when each segment starts (LevelEntranceEvent for entrance segments, CheckpointEvent for checkpoint-start segments). All deaths-in-this-segment plus the closing `survived` event share that ID. Mirrors practice exactly: 1 episode = 1 attempt at this segment. The legacy `get_segment_attempts` / `_roll_up_episode` adapter groups by `episode_id` assuming this shape, so it works unchanged.

## Recorder changes

New fields on `SegmentRecorder`:
- `_episode_id: str` — minted at segment-start, carried through every event of that segment.
- `_last_event_ms: int` — wall-clock of the previous event in the current episode (or segment-start). `time_ms = now - _last_event_ms` per event — raw delta, no penalty math.
- `_pending_events: list[EventAttempt]` — events accumulated during the current segment. Flushed at segment close.

Behavior:
- `handle_entrance`: mint fresh `_episode_id`, `_last_event_ms = event.timestamp_ms`, `_pending_events.clear()`. Existing waypoint/pending_start logic stays.
- `handle_death`: append `EventAttempt(outcome=died, episode_id=_episode_id, time_ms = event.timestamp_ms - _last_event_ms, ...)` to `_pending_events`. Update `_last_event_ms`. Keep the existing `_deaths_in_segment` counter for logging.
- `_close_segment` (called from `handle_checkpoint` and `handle_exit`): compute `segment_id`, `upsert_segment` as today, then append the closing `EventAttempt(outcome=survived, episode_id=_episode_id, time_ms = end_timestamp_ms - _last_event_ms, ...)` to `_pending_events`. Flush the entire buffer via `db.log_event_attempt()` per row, all inside `with db.transaction()` so the segment row + its events land atomically.
- `handle_checkpoint` (start of next segment, after closing the previous one): mint a new `_episode_id`, set `_last_event_ms = event.timestamp_ms`.
- `handle_exit` with `goal='abort'`: drop the buffer with the pending_start (today's behavior preserved — abort = lose).
- `clear()` / pause / disconnect: drop the buffer (today's behavior preserved — in-flight segment is lost on stop).

## Finalize changes

- `_seed_reference_attempts` deleted from `python/spinlab/capture/reference.py`. Events are already in `attempts` when each segment closes; nothing to seed.
- `ReferenceController.finalize_run`: drops the `drain_recorded_segment_times_for_run` + `_seed_reference_attempts` calls. Keeps `promote_draft` + `set_active_capture_run` + `scheduler.rebuild_all_states()`. Becomes: name the run, mark saved, activate.
- `ReferenceController.save_and_finish_run`: same simplification. The `atomic_save_and_finish_run` helper in `python/spinlab/capture/finalizer.py` loses the drain/seed step; just promotes + activates atomically.
- The old `seed: segment=...` log line goes away — replaced by per-event flush logs at segment close in the recorder (one short log line per segment with event count, not one per row).

## Cleanup

- `python/spinlab/db/recorded_segment_times.py` deleted.
- `RecordedSegmentTimesMixin` removed from the `Database` composition in `python/spinlab/db/__init__.py`.
- `_split_episode_into_events` STAYS — still used by the `log_attempt` shim for test fixtures and any non-reference legacy callers. Just not called from the reference path anymore.

## Tests

- **Recorder unit tests** ([tests/unit/capture/test_recorder.py](../../tests/unit/capture/test_recorder.py)): existing `test_segment_with_deaths_timing` and `test_death_via_handle_death_increments_counter` rewritten to assert event rows appear in `attempts` at segment close (instead of one summary row in `recorded_segment_times`). Per-event time-delta math verified.
- **Finalizer unit tests** ([tests/unit/capture/test_finalizer.py](../../tests/unit/capture/test_finalizer.py)): the `deaths=2` round-trip test removed — that synthesis path no longer exists. Replaced by a test that finalize is a pure promote (no row movement).
- **Multi-session test** ([tests/unit/capture/test_multi_session.py](../../tests/unit/capture/test_multi_session.py)): updated to verify events accumulate across sessions correctly via the recorder API.
- **Crash recovery test** ([tests/integration/test_crash_recovery.py](../../tests/integration/test_crash_recovery.py)): updated to assert the same crash-safety bound but via `attempts` rows instead of `recorded_segment_times`.
- **DB tests** ([tests/unit/db/test_db_recorded_segment_times.py](../../tests/unit/db/test_db_recorded_segment_times.py)): deleted (the table is gone).
- **Replay fixture** ([tests/integration/test_replay_fixture.py](../../tests/integration/test_replay_fixture.py)): expected to pass unchanged — replay goes through the same recorder, so events land in `attempts` instead of `recorded_segment_times`. The fixture gates on `sections_captured` (a milestone), not on a specific table.
- **New integration test:** a reference run with deaths (driven through the recorder API) → assert N `died` + 1 `survived` rows per segment with real wall-clock delta times.

## Crash-safety story

Identical to today.

- Completed segments → durable in `attempts`.
- In-flight segment → buffered in memory, lost on dashboard crash / stop / disconnect / explicit abort.

The current `recorded_segment_times` write also only happens at segment close, so today's crash-safety bound is the same. We are not regressing it; we are also not improving it.

## Deferred follow-ups

- **Mid-segment journaling.** Write `_pending_events` to a sidecar file every N events or N seconds; replay into DB on dashboard startup if found. Only do this if the "lose-in-flight-segment-on-crash" bound bites during long grinding sessions (Andrew's 2-hour cp→goal scenario). Don't preemptively add complexity.
- **`capture_session_id` column on `attempts`.** Would let queries answer "which session within a multi-session reference run did this event come from." No concrete reader needs it now; add when a use case appears.

## Out of scope

- Cold-state variant-selection fix. That's a separate one-SQL-clause change (prefer `cold` over `hot` when both exist for a checkpoint waypoint, in [python/spinlab/db/segments.py:123-124](../../python/spinlab/db/segments.py#L123-L124)) plus a regression test. Lands first in its own commit, before this refactor. Memory note: `project-cold-state-unused-bug`.
- Restructuring `segment_id` to not include the end waypoint. Would enable truly mid-segment writes but touches segment identity across the whole codebase. Not worth it for a marginal crash-recovery win.
- Adding journaling, retries, or other crash-recovery machinery beyond what `recorded_segment_times` already provided.

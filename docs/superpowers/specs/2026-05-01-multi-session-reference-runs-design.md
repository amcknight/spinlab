# Multi-Session Reference Runs

**Date:** 2026-05-01
**Status:** Draft — design approved, awaiting plan

## Goal

Enable a reference run to be captured across multiple sessions over hours or days, surviving dashboard restarts and Mesen crashes, with the ability to delete bad sessions or individual segments without losing the rest of the run. Single-session reference runs (the common case) must remain as ergonomic as today.

## Background

A reference run today is a single, atomic capture: start → record → stop → save-or-discard. The implementation hard-codes this in several ways:

- `recording.active` in `lua/spinlab.lua` is a single boolean; one spinrec per recording, capped at `MAX_RECORDING_FRAMES = 360000` (100 minutes) before auto-flush.
- `ReferenceController.stop_reference` always promotes captured segments into a draft — there is no "pause and resume."
- `ReferenceController.handle_disconnect` does the same thing, so closing the dashboard or losing Mesen mid-run forces the draft transition.
- `SegmentRecorder.segment_times` is an in-memory list, only persisted to the `attempts` table at finalize time. A dashboard crash mid-run preserves segment rows but loses their timing data.
- `DraftManager.recover` finds a draft on startup but only offers save/discard — there is no path back into recording mode.

A 50-hour run is not viable today: the 100-minute cap fires repeatedly, any disconnect ends the run, and a crash silently strands timing data.

## Scope

In:

1. New `capture_sessions` concept in the data model — runs become a 1:N parent of sessions.
2. State machine for the run lifecycle: IDLE → RECORDING → PAUSED → RECORDING → ... → finalized.
3. UI for pause/resume/finalize/discard at the run level, and delete-session at the session level (paused runs only).
4. Per-segment timing persisted immediately on segment close, not at finalize.
5. Crash-recovery: orphaned in-flight sessions get cleaned up on dashboard startup.
6. Removal of `MAX_RECORDING_FRAMES` (sessions are user-driven now).
7. Targeted refactors in `ReferenceController`/`SegmentRecorder`/`DraftManager` that the new code paths force us through (collapse three duplicate "stop and conditionally promote" code paths, drop the in-memory `segment_times` list, etc).
8. Crash-and-recover integration test (Python-only) plus a one-shot Playwright smoke for the UI.

Out (deferred or rejected):

- **Multi-session replay.** Replay still operates on one spinrec at a time. Chaining sessions in replay would require stashing save states at session boundaries (which aren't segment boundaries) and extending replay to chain spinrecs. Not a simple win.
- **Periodic in-session spinrec flush for crash safety.** Per the recovery analysis, segments and save states are already crash-safe (written at segment-close). The only thing lost on Mesen crash is the dead session's spinrec — and replay of crashed sessions wasn't a stated need.
- **Auto-promote `is_primary` on segment delete.** Confirmed working as desired today: deleting a primary segment leaves the geography out of practice scheduling, which is the intended salvage behavior.
- **Multiple in-flight reference runs per game.** One paused run per game, same as today's one-draft-per-game constraint.
- **Range deletion of segments.** Single-segment delete + delete-session covers the salvage workflow.

## State Model

Three runtime states the user perceives:

- **IDLE** — no run loaded, no run in progress.
- **RECORDING** — currently in a session, capturing inputs and segments.
- **PAUSED** — a run exists with `draft=1`, no active session. Segments and save states already in DB.

Transitions:

| From | Action | To | Effect |
|------|--------|----|----|
| IDLE | Start Reference Run | RECORDING | Create `capture_run` (draft=1) and first `capture_session` (ordinal=1). Issue `reference_start` to Lua with new spinrec path. |
| RECORDING | Stop Session | PAUSED | Issue `reference_stop` to Lua. Mark session `ended_at=now, end_reason="stopped"`. Run stays draft=1. |
| RECORDING | Save & Finish Run | finalized + IDLE | Stop Session, then immediately Finalize (open name dialog). |
| PAUSED | Resume | RECORDING | Create new `capture_session` (ordinal+1) under existing run. Issue `reference_start` with new spinrec path. |
| PAUSED | Finalize | finalized + IDLE | Drain `recorded_segment_times` for the run into `attempts`. Set `capture_runs.draft=0`, set as active. Rebuild model. |
| PAUSED | Discard Run | IDLE | Hard-delete the run (FK cascades through sessions, segments, save states, recorded_segment_times). |
| RECORDING | TCP disconnect | PAUSED | Same as Stop Session, but `end_reason="disconnected"`. |
| RECORDING/PAUSED | Start a new run (rejected) | (no change) | Returns `DraftPendingError`. User must finalize or discard first. |

The "draft" concept in the DB is preserved but its meaning sharpens: `draft=1` means "in-progress, paused or recording, not yet committed." There is no "draft awaiting save/discard prompt" UI affordance anymore.

## Data Model

### New table: `capture_sessions`

```sql
CREATE TABLE capture_sessions (
  id TEXT PRIMARY KEY,                         -- "sess_<8hex>"
  capture_run_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,                    -- 1, 2, 3... within the run
  started_at TEXT NOT NULL,
  ended_at TEXT,                               -- NULL while RECORDING
  spinrec_path TEXT NOT NULL,                  -- always set at session create; file may be missing if session crashed before flush
  end_reason TEXT,                             -- "stopped" | "disconnected" | "crashed" | NULL while open
  FOREIGN KEY (capture_run_id) REFERENCES capture_runs(id) ON DELETE CASCADE,
  UNIQUE (capture_run_id, ordinal)
);
```

### New table: `recorded_segment_times`

Crash-safe, per-session timing buffer. Drained into `attempts` at finalize.

```sql
CREATE TABLE recorded_segment_times (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  capture_session_id TEXT NOT NULL,
  segment_id TEXT NOT NULL,
  time_ms INTEGER NOT NULL,
  deaths INTEGER NOT NULL,
  clean_tail_ms INTEGER NOT NULL,
  recorded_at TEXT NOT NULL,
  FOREIGN KEY (capture_session_id) REFERENCES capture_sessions(id) ON DELETE CASCADE
);
```

### Schema additions

```sql
ALTER TABLE segments ADD COLUMN capture_session_id TEXT
  REFERENCES capture_sessions(id) ON DELETE CASCADE;
```

Segments deleted via session cascade. Note: `attempts` does not auto-cascade from `segments` today (deletion is explicit in `hard_delete_capture_run`); we keep that pattern. After finalize, when the user deletes an individual segment, attempts for that segment are removed in the existing `soft_delete_segment` flow (this is already its behavior).

### No rename for `attempts.session_id`

Originally proposed renaming `attempts.session_id` → `attempts.capture_run_id`, but that column is polymorphic: it stores a practice session id for practice attempts, a speed_run session id for speed_run attempts, and a capture_run_id only for reference attempts. The renamed name would be wrong for two of three cases. Leaving the column as-is.

The "session" word now means three distinct things across the codebase (`sessions` table = practice sessions, `attempts.session_id` = polymorphic parent grouping, new `capture_sessions` = recording sessions within a capture run). Unfortunate but no SQL-level collision; we accept the verbal overload.

### Why a buffer table instead of provisional flag on `attempts`

A "provisional" flag on `attempts` would force every consumer (scheduler, model rebuild, stats, dashboard widgets) to remember to filter by `provisional=0`. Forgetting in any one of them silently incorporates in-progress reference timing into scheduling/model state — exactly the kind of bug that's hard to test for.

The buffer table establishes a cleaner contract: `attempts` contains only finalized data. No filter discipline required across the codebase.

### Constraint: one paused run per game

Enforced in code at `start_reference` time: if any `capture_runs WHERE game_id=? AND draft=1` exists, raise `DraftPendingError`. Mirrors today's behavior.

## API / Control Flow

### Routes affected

| Route | Today | After |
|---|---|---|
| `POST /reference/start` | Creates run, starts recording | Creates run + session ordinal=1, starts recording |
| `POST /reference/stop` | Stops, promotes to draft (terminal) | Stops, ends current session row, run stays draft=1 |
| `POST /reference/save` (was save_draft) | Promote draft + seed attempts | Renamed `POST /reference/finalize` — drains `recorded_segment_times` into `attempts`, sets draft=0, rebuilds model |
| `POST /reference/discard` | Hard-delete capture_run | Renamed `POST /reference/discard_run` — extend `hard_delete_capture_run` to also remove `recorded_segment_times` and `capture_sessions` rows, plus their spinrec files from disk |

### New routes

- `POST /reference/resume` — given current paused run for the game, creates a new `capture_session` and issues `reference_start` to Lua.
- `POST /reference/save_and_finish` — combined Stop Session + Finalize for the single-session ergonomic path. Body carries the run name (same as today's save). Atomically: `_end_current_session("stopped")`, then drain + promote inside one DB transaction. On any failure, the user is left in PAUSED with a clear error and can manually retry finalize.
- `DELETE /capture_sessions/{session_id}` — only allowed when the run is paused (draft=1). Drops the session, its segments (via cascade), its `recorded_segment_times` (via cascade), and unlinks/removes the spinrec file from disk.

### Routing of disconnect / replay-error / replay-stop

Three duplicated "stop and conditionally promote" code paths in `ReferenceController` collapse into one private helper:

```python
def _end_current_session(self, end_reason: str) -> None:
    """End the current capture_session if any. Run stays draft=1.

    end_reason: "stopped" | "disconnected" | "crashed" | "replay_finished" | "replay_error"
    """
    ...
```

Used by `stop_reference`, `handle_disconnect`, `handle_replay_finished`, `handle_replay_error`, and `stop_replay`. Replay-related callers also follow up with their own state cleanup as today.

### Recovery on startup

`ReferenceController.recover` (renamed from `DraftManager.recover` after the refactor below) runs on game-load:

1. Find the most recent `capture_runs` row for `game_id` with `draft=1`. (If multiple — shouldn't happen in normal use — keep the most recent and hard-delete the rest, matching today's `recover_draft` defensive behavior.)
2. For any `capture_sessions` belonging to it with `ended_at IS NULL`, set `ended_at=now, end_reason="crashed"`.
3. Set in-memory state: paused run loaded. Mode = IDLE.

The user lands in IDLE with the paused run loaded; clicking Resume proceeds to RECORDING.

## Lua Side

Minimal change. Lua doesn't know about runs or sessions — Python provides a fresh spinrec path on every `reference_start`.

- Delete `MAX_RECORDING_FRAMES` and its handler ([spinlab.lua:25](../../lua/spinlab.lua#L25), [spinlab.lua:1276-1287](../../lua/spinlab.lua#L1276-L1287)). Sessions are user-driven now.
- `recording.active`, `recording.buffer`, `recording.output_path` stay as-is.
- Each `reference_start` from Python carries a different path; Lua flushes to that path on `reference_stop`.

No new TCP commands required. No spinrec format changes.

## UI Changes

All TypeScript, primarily in `frontend/src/manage.ts` and the reference panel.

### Reference panel (live capture)

When in PAUSED state with segments captured:

- **[Save & Finish Run]** (primary) — calls `save_and_finish`, opens name dialog. Single-session ergonomics path.
- **[Resume]** (secondary) — calls `resume`, returns to RECORDING.
- **[Discard]** (tertiary, behind confirm) — calls `discard_run`.

When in RECORDING state:

- **[Stop Session]** — calls `stop`, transitions to PAUSED.
- Segment count + session indicator ("Session 3 — 12 segments captured this session, 47 total").

### Manage page

- In-progress run shown prominently at top with state ("Paused — last session ended 14:32").
- Session sub-list: ordinal, started_at, ended_at, end_reason, segment count, [Delete Session] button (only enabled while run is paused).
- For finalized runs: same view, no Delete Session button.

### Segments tab

- Add `Session` column to segment list so the salvage workflow can see which session each segment came from.
- Existing per-segment delete already works — no change needed.

## Refactor Opportunities Bundled With This Work

These are touched because the new code paths flow through them, not as drive-by cleanup:

1. **Collapse three duplicate "stop and conditionally promote" paths** in `ReferenceController` (`stop_replay`, `handle_replay_error`, `handle_disconnect`) into the new `_end_current_session(reason)` helper described above.
2. **Drop `SegmentRecorder.segments_count` in favor of DB-derived counts.** Today it's tracked in-memory; with multi-session, in-session vs in-run counts diverge. Always read from DB: `SELECT COUNT(*) FROM segments WHERE reference_id = ? AND active = 1`.
3. **Drop `RecordedSegmentTime` dataclass and `recorder.segment_times` list.** Recorder writes a `recorded_segment_times` row directly to DB on segment close. No in-memory list to plumb through `enter_draft`.
4. **Fold `recorder.clear()` duplicate calls** in `stop_reference` ([reference.py:128-129](../../python/spinlab/capture/reference.py#L128-L129)) into one place.
5. **Remove `enter_draft()` on the recorder.** With segments_count and segment_times no longer in-memory, the method has no data left to hand off.
6. **Dissolve or simplify `DraftManager`.** With timing data persisted as we go and segment counts coming from the DB, what's left is "which run is the current paused run?" — one SQL query. Either move the recovery query directly to `ReferenceController` (preferred) or keep `DraftManager` as a thin façade. Decide during plan-writing based on what reads cleanest.
7. ~~Rename `attempts.session_id` → `attempts.capture_run_id`~~. Dropped — see Data Model section. Column is polymorphic across practice/speed_run/reference and the rename would be wrong for two of three sources.

What is explicitly *not* refactored:

- The Lua-side recording state machine (just deletes `MAX_RECORDING_FRAMES`).
- The spinrec file format.
- Replay code (single-session replay still works; multi-session replay never did).
- Condition registry / waypoint / segment models.

## Testing

### New tests

1. **Multi-session capture lifecycle (Python integration).** Start session 1 via fake TCP, capture segments, stop, resume → session 2, capture more segments, finalize. Assert: two `capture_sessions` rows, all segments linked correctly via `capture_session_id`, all `recorded_segment_times` drained into `attempts` at finalize, draft=0 on the run.
2. **Crash-and-recover (Python integration).** Start session via fake TCP, capture segments, tear down `SessionManager` without graceful shutdown, instantiate fresh `SessionManager` with same DB. Assert: orphaned session marked `end_reason="crashed"`, run stays draft=1, segments + `recorded_segment_times` preserved. Resume creates session ordinal+1.
3. **Delete session while paused.** Start, capture, stop, delete session 1. Assert: segments captured in session 1 are gone (cascade), `recorded_segment_times` for session 1 are gone, run still exists (now empty), spinrec file removed from disk.
4. **Delete session post-finalize is rejected.** Finalize a run, attempt to delete one of its sessions, assert error.
5. **Save & Finish Run (single-session path).** Start, capture, click Save & Finish. Assert: same end state as legacy save flow (run finalized, attempts seeded, model rebuilt). Verifies the ergonomic shortcut produces equivalent state.
6. **One paused run per game constraint.** Start a run, stop without finalizing, attempt a fresh start — assert `DraftPendingError`.

### Existing tests to update

- `tests/unit/capture/test_reference.py` — disconnect-during-reference no longer auto-promotes to draft; it pauses. Update assertions.
- `tests/unit/capture/test_draft.py` — DraftManager API may change (depending on dissolve-or-simplify decision).
- `tests/unit/test_session_manager.py` — `recover_draft` rename, paused-state semantics.

### Playwright smoke

One new test in `tests/integration/test_frontend_smoke.py` (or sibling): start a reference run via UI, force-stop the dashboard process, restart with same DB, click Resume, verify UI shows ordinal-2 session and previously-captured segments persist. This is the high-visibility one-shot — the Python integration test catches the data-layer cases more thoroughly.

## Recovery Behavior on Specific Crash Scenarios

| Scenario | Segments | `recorded_segment_times` | Spinrec | Resume? |
|---|---|---|---|---|
| Mesen crash mid-segment | Last (in-flight) segment lost; prior segments preserved | Preserved for closed segments | In-memory portion lost (file may be empty) | Yes — reboot Mesen, click Resume, new session N+1 |
| Mesen crash between segments | All preserved | All preserved | In-memory portion lost | Yes |
| Dashboard crash mid-session | All preserved (DB writes are immediate) | All preserved | In-memory portion lost | Yes — restart dashboard, click Resume |
| Clean stop | All preserved | All preserved | Flushed to disk | Yes |
| Clean disconnect (Mesen exits cleanly) | All preserved | All preserved | Flushed to disk | Yes |

The recurring "in-memory portion lost" is the spinrec buffer in Lua. Per the design rationale, this is acceptable: we explicitly do not need to replay crashed sessions.

## Risks

1. **Verbal overload of "session."** The codebase now has `sessions` (practice), `capture_sessions` (recording within a run), and `attempts.session_id` (polymorphic parent). Devs must read carefully. Mitigation: docstrings on the new mixin and on `Attempt.session_id` clarifying the polymorphism.
2. **`one paused run per game` constraint silently masking a stale draft.** If a previous run got into draft=1 state and the user forgot, starting a new one fails with a confusing error. Mitigation: `DraftPendingError` UI message points the user at the manage page where they can finalize or discard.
3. **Recovery picks the most recent draft and deletes others.** Inherits from today's `recover_draft`. Defensive but could surprise — if there are somehow two paused runs, one is silently destroyed. Low likelihood, low blast radius (only affects in-progress reference data, not finalized runs).
4. **`Save & Finish Run` is a compound action.** If the stop-session step succeeds but finalize fails, the user is left in PAUSED state with an unexpected error. Mitigation: implement as a single transaction at the API layer; on partial failure, surface clearly so the user can manually retry finalize.
5. **Spinrec files orphaned by crashed sessions.** A session that crashes before its spinrec is flushed leaves a `spinrec_path` pointing at a nonexistent file. UI must handle this gracefully ("session crashed, no replay available"). Not a data-integrity risk, just a presentation risk.

## Decision Log

- **Stop-always-parks state model.** Stop is non-destructive; the user explicitly clicks Finalize (or Save & Finish Run) to commit. Trades one extra click in the multi-session flow for a clear "I'm not done yet" mental model. `Save & Finish Run` preserves single-session ergonomics.
- **Explicit `capture_sessions` table rather than flat segments-under-run.** Sessions are addressable in the UI (delete-session as a coarse salvage tool) and the spinrec-per-session model maps directly onto a row.
- **Spinrec loss on Mesen crash is acceptable.** Segment data and save states are written immediately to disk/DB and survive crashes. Only the spinrec for the crashed session is lost — and replay of crashed sessions wasn't a stated need. Periodic flush rejected as YAGNI.
- **Buffer table for in-flight timing rather than a `provisional` flag on `attempts`.** The buffer table keeps `attempts` as a "finalized data only" surface, sparing every existing consumer (scheduler, model rebuild, stats) from filter discipline.
- **`MAX_RECORDING_FRAMES` removed entirely.** Sessions are user-driven; no need for a defensive cap. The 100-minute cap was added defensively in an unrelated commit and has no principled justification.

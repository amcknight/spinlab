# Multi-Session Follow-ups — Correctness, Dashboard Flow, Observability, Hygiene

**Date:** 2026-05-01
**Status:** Draft — design approved, awaiting plan

## Goal

Land the high-leverage follow-ups from the multi-session reference run work (commit `f362972`) as a single cohesive pass. The lens is **clean + correct**: fix one pre-existing correctness bug exposed by the new tables, complete the salvage UI so the multi-session feature is actually usable, add observability for the silent-decision points, and tighten DB/API naming while the schema is still cheap to change.

Explicitly **not** in this pass: model rework (the deeper "the models are kinda not yet working" elephant), real migrations (deferred until the first irreplaceable multi-session run exists), and miscellaneous UX polish that doesn't touch correctness.

## Background

The multi-session work merged 2026-05-01 with three known classes of follow-up captured in `multi-session-followups.txt`: direct deferrals from the implementation, issues from the final review that didn't block merge, and pre-existing bugs the work surfaced. The full backlog has 39 items; this spec covers the cluster where **clean** (DB hygiene before migrations land), **correct** (one pre-existing bug, dashboard flow gaps), and **observable** (silent-decision logging) reinforce each other.

A separate session will tackle the model work. Real migrations stay deferred — they become urgent the first time a user records a multi-session run they don't want to repeat, not before.

## Scope

In:

1. **Pre-existing correctness:** fix `clean_tail_ms` always equalling `time_ms` for segments with deaths.
2. **Scheduler rebuild semantics:** decide and document what `finalize_run` / `save_and_finish_run` do when a paused run finalises with zero completed segments.
3. **Dashboard flow correctness:** per-session segment count column, "Session" column on segments tab, `sections_captured` TS type fix, frontend handler tests, empty-state pass.
4. **Observability:** structured logging for the silent decisions in `recover_paused_capture_run`, session lifecycle, and spinrec unlink failures; minimal recovery audit trail.
5. **Replay/paused integration test:** end-to-end test for the paused-A → replay-B → restart → recover-A flow.
6. **DB/API hygiene while greenfield:** `attempts.session_id` rename, `NoPausedRunError` split, `RunPendingError` alias decision, partial unique index for one-paused-run-per-game, `delete_capture_session` guard against active recording.

Out (deferred to future specs):

- **Model work** — the `ModelOutput`, Bayesian estimators, hyperparameter tuning, and the broader "find The Model" thread. Owns its own session.
- **Real migrations** — `db/core.py:158-181` still rebuilds on schema drift. Becomes urgent once a user records a multi-session run they care about. Tracked but not done here.
- **Architecture refactors:** the replay-creates-ephemeral-`capture_run` hack (item #23), `is_primary` auto-promotion on segment delete (#21), `paused_run_id`/`recorder.capture_run_id` desync invariants (#7).
- **Efficiency improvements:** `get_paused_state` query caching (#25), `_compute_is_primary` batching (#26), `recorder._close_segment` ordinal O(n) (#22), spinrec file accumulation sweep (#27).
- **UX polish:** modal Save & Finish dialog (#30), paused-vs-recording visual differentiation (#31), per-segment delete confirmation copy (#32), "Session N of M" indicator (#29).
- **Documentation:** ARCHITECTURE.md "session" disambiguation (#37), plan-doc deviations section (#38), CLAUDE.md multi-session note (#39).
- **Long-run load test (#14)** — wants its own design alongside the run-fixture library work.
- **Mutation-test sweep (#16)** — useful but a different shape of work.

The deferrals above all map to numbered items in `multi-session-followups.txt`; see that file for the per-item rationale.

## 1. `clean_tail_ms` correctness fix

### Diagnosis

`SegmentRecorder.handle_spawn_timing(timestamp_ms=...)` only updates `_last_spawn_ms` when `timestamp_ms is not None`. `python/spinlab/capture/reference.py:425` calls it with `None`. Result: `_last_spawn_ms` is never set on the path that fires for deaths-tracking, so `clean_tail_ms` falls back to `time_ms` for every segment with deaths. The deaths-tracking feature has been silently broken in main since it landed.

The new `recorded_segment_times` table now persists `clean_tail_ms` per segment, so the wrong value is propagating into attempts and downstream into the model — exactly the kind of silent garbage that makes "the models are kinda not yet working" hard to diagnose.

### Fix

`SpawnEvent` carries the spawn frame from Lua but the dashboard-side timestamp is lost by the time `handle_spawn_timing` is called. Two options:

- **A.** Plumb the spawn timestamp through from `SpawnEvent` (Lua → TCP → recorder), so `_last_spawn_ms` gets a real value. Correct fix.
- **B.** Compute `clean_tail_ms` server-side from the existing `time_ms` minus a derived spawn-to-segment-end delta. Doesn't address the underlying plumbing gap.

Take (A). The TCP event already includes a frame number; convert at the recorder boundary using the same frame-to-ms conversion already used elsewhere.

### Test

Replay-fixture test that covers a segment with a mid-segment death: assert `clean_tail_ms < time_ms` and matches the post-spawn duration within tolerance.

## 2. Scheduler rebuild semantics on zero-segment finalize

### Diagnosis

`finalize_run` and `save_and_finish_run` skip `rebuild_all_states` when `seeded == 0` (no completed segments to seed). But `set_active_capture_run` ran first, so the active reference for the game changed even though no new attempt data was added. The scheduler's notion of which segments belong to "the active reference" is now stale relative to whatever it built from prior attempts.

This is item #6 in the follow-ups and the call needs to be made explicitly, not by accident.

### Decision

**Always rebuild after `set_active_capture_run`, even with `seeded == 0`.** The scheduler's state is parameterised by which reference is active; changing the active reference invalidates that state regardless of whether new attempts were added. Skipping the rebuild is a micro-optimisation that produces silently wrong scheduling.

### Implementation

Move `rebuild_all_states` out of the `if seeded > 0` branch in both `finalize_run` and `save_and_finish_run`. Add a one-line comment explaining the active-reference invariant. Update the existing tests that asserted no-rebuild on zero-seed to assert rebuild-with-empty-attempts.

## 3. Dashboard flow correctness

The salvage UI is the user-facing payoff of the multi-session work and currently can't actually be used to triage a multi-session run — you can't see at a glance which session contributed which segments.

### 3.1 Per-session segment count

`list_capture_sessions_for_run` returns sessions but no segment count; the manage UI sessions table shows `—`. Add `(SELECT COUNT(*) FROM segments WHERE capture_session_id = s.id) AS segment_count` to the query. Extend `CaptureSession` TypedDict and the matching frontend type. Render the count.

### 3.2 "Session" column on segments tab

Segments already carry `capture_session_id`. Add a "Session" column to the manage page segments table that renders the session ordinal (not the raw id). Joins to `capture_sessions` for the ordinal.

### 3.3 `sections_captured` TS type

`frontend/src/types.ts:163` declares `sections_captured: number`. `state_builder.py` emits `int | None`. Frontend papers over with `?? 0`. Change the TS type to `number | null` so the contract is honest. Update the API contract test if it pins the shape.

### 3.4 Frontend handler tests

Vitest covers render but not the new `btn-resume` / `btn-save-and-finish` / `btn-discard-run` click handlers. Add tests that mock `fetch`, click each button, and assert the URL and request body shape. Same pattern as existing handler tests in `frontend/src/`.

### 3.5 Empty-state pass

Manually verify the manage page reads correctly with: zero references and no paused run; one paused run and zero finalised; one finalised and one paused. Adjust copy where needed. No new tests required; this is a one-time manual pass.

## 4. Observability

The recovery and session-lifecycle paths make silent decisions today. When something feels off — a paused run vanishes, a session ends unexpectedly, a spinrec doesn't get cleaned up — there's no trail.

### 4.1 Recovery decisions

`recover_paused_capture_run` keeps the newest stranded draft and `hard_delete_capture_run`s the rest. Add `logger.warning("Discarding stranded draft capture_run %s for game %s during recovery (kept newer draft %s)", ...)` per discarded run. Same logger for the orphan-session cleanup ("Marking orphan session %s as crashed").

### 4.2 Session-end summary

When `_end_current_session` fires, log `logger.info("Session %s (ordinal %d) ended after %.1f minutes, captured %d segments, reason=%s", ...)`. Aids debugging when a session ends earlier than expected.

### 4.3 Spinrec unlink failures

`hard_delete_capture_run` and `delete_capture_session` swallow `OSError` on `unlink`. Replace `pass` with `logger.warning("Failed to unlink spinrec %s: %s", path, exc)`. Disk-full and permissions errors deserve to be heard.

### 4.4 Recovery audit trail

Lightweight: structured logger calls suffice for now (the warnings above plus a summary `logger.info("Recovery complete: kept_run=%s discarded_drafts=%d crashed_sessions=%d", ...)` at the end of `recover_paused_capture_run`). A dedicated `recovery_log` table is overkill until we actually need to query history.

## 5. Replay ↔ paused-run integration test

The interaction between the `id NOT LIKE 'replay_%'` filter and replay's lifecycle is the most fragile part of the merged work. It was unit-tested but not end-to-end tested. Add an integration test:

1. Create paused run A (draft, multi-session).
2. Start replay of finalised reference B.
3. Let replay run to completion (or interrupt mid-replay).
4. Restart the dashboard.
5. Assert `recover_paused_capture_run` returns A, the replay-derived `capture_run` row is gone, and the replay's segments do not appear under A.

Goes alongside the existing `test_crash_recovery.py`. Mark `@pytest.mark.slow` if needed; this exercises the real recovery path.

## 6. DB/API hygiene while greenfield

These changes are mechanical now and progressively more expensive once real migrations land. Doing them in this pass keeps the eventual migration history shorter.

### 6.1 `attempts.session_id` → `attempts.parent_id`

Three meanings of "session" in the codebase: `sessions` table (practice sessions), `attempts.session_id` (polymorphic parent — practice session or capture session), `capture_sessions` (multi-session run pieces). The `attempts.session_id` use is the odd one — it's *not* a session in either of the other senses; it's a polymorphic foreign key. Rename column to `parent_id`. Greenfield, so no migration; just update the schema, the model, and grep-and-replace callers.

### 6.2 `NoPausedRunError` split

`NotInReferenceError` is currently raised for both "wrong mode" and "no paused run exists." Split out `NoPausedRunError` for the latter. Mechanical; clearer API. Riding along with the rename in 6.1.

### 6.3 `RunPendingError` alias decision

`errors.py` has `RunPendingError = DraftPendingError` as wire-format preservation with a comment claiming Task 14 will migrate callers. That didn't happen. **Decision:** complete the rename to `DraftPendingError` everywhere and drop the alias. The wire format preservation argument no longer applies (no external consumers); the alias is just lingering ambiguity.

### 6.4 Partial unique index for one-paused-run-per-game

Today the "one paused run per game" invariant is enforced only by `recover_paused_capture_run` deleting older drafts. Add a partial unique index:

```sql
CREATE UNIQUE INDEX idx_one_paused_run_per_game
  ON capture_runs(game)
  WHERE draft = 1 AND id NOT LIKE 'replay_%';
```

Belt + suspenders: makes bad states impossible at the DB level rather than relying on recovery to clean them up. The recovery code stays (it handles other orphan cases), but the warning from §4.1 should now never fire in practice.

### 6.5 `delete_capture_session` active-recording guard

The API accepts any `session_id`. If `recorder.current_capture_session_id == session_id` (user is mid-segment in the session being deleted), the next segment-close hits an FK violation. Add a guard in the handler: if the target session is the recorder's current session, raise an explicit error (new `SessionInUseError` or reuse an existing one). Not exposed in the UI today but the API surface is.

## Test Plan

- **Existing suite:** full `pytest` green before and after each section.
- **§1 fix:** new replay-fixture test asserting `clean_tail_ms < time_ms` for a segment with a death.
- **§2 decision:** existing `test_save_and_finish_seeds_attempts_and_finalizes` and siblings updated to assert rebuild fires regardless of `seeded` count.
- **§3.4:** new Vitest tests for the three button handlers.
- **§5:** new integration test for the paused-A → replay-B → restart flow.
- **§6.4 index:** existing tests should still pass; add a small test that inserting a second draft for the same game raises `IntegrityError`.
- **§6.5 guard:** new unit test for the delete-while-recording case.

## Order of Implementation

Plans are written just-in-time, but the sections are roughly ordered to land low-risk-high-signal first:

1. §1 `clean_tail_ms` fix — silent data corruption, do first.
2. §4 observability bundle — pure additions, makes everything below easier to debug.
3. §3 dashboard flow — finishes the salvage UI; now the feature is actually usable.
4. §2 scheduler rebuild — small but a real semantic decision; document it.
5. §5 replay/paused integration test — locks down the fragile interaction before §6 touches anything.
6. §6 hygiene cluster — mechanical sweep last so it doesn't fight any of the above.

Each section is independently mergeable.

## Out of Scope — Reminders

- **Models.** Whatever this pass exposes about model behaviour stays a note in `multi-session-followups.txt` or `future.txt`. Do not start fixing model issues here.
- **Migrations.** When the first user-irreplaceable multi-session run is recorded, item #19 graduates from deferred to urgent. Until then the drop-on-mismatch behaviour stays.
- **Run-fixture library / load test / Bayesian estimators.** All `future.txt` items, all dependent on better model substrate. Out.

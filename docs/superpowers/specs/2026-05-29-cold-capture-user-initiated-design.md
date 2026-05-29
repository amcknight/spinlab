# Cold Capture: user-initiated, run-scoped, escapable

**Status:** approved (2026-05-29)
**Scope:** Rework how Cold Capture is triggered and exited. Add diagnostic
instrumentation for a known death-detection bug; the fix for that bug is a
follow-up task, not part of this spec.

## Background

Cold Capture (`cold_fill`) loads each missing-cold segment's hot save state,
waits for the user to die and respawn, and captures the deterministic
post-respawn ("cold") state. Today it is triggered **automatically** when a
reference run is finalized, and it scans **all segments in the game** for
missing cold states — not just the run that was recorded.

A 2026-05-29 testing session surfaced the consequences:

- After recording half of level 1, the dashboard auto-entered Cold Capture for
  a level-4 segment left over from a previous run.
- Dying + respawning did **not** advance/complete the capture. The
  `spinlab.log` shows the detector activated at 10:21:13 and then **zero
  events** for five minutes — the `ColdFillSpawnDetector` never registered the
  death (`_waiting_spawn` never flipped).
- There was **no way to leave** Cold Capture — no abort/X — so the user was
  stuck and could not reach the Replay button. (Recovery required restarting
  the dashboard.)

## Goals

1. **No auto-trigger.** Finishing a reference run returns to IDLE.
2. **Run-scoped, user-initiated start.** A button starts Cold Capture for the
   missing-cold segments of the **active run** only.
3. **Escape hatch.** Skip the current segment (when you can't die/respawn there)
   and Abort the whole queue, mirroring Reference Run's stop.
4. **Diagnostic instrumentation** for the death-detection bug — enough that the
   next reproduction is conclusive. (The fix itself is a separate task.)

## Non-goals

- Fixing the death-detection bug in `ColdFillSpawnDetector` (follow-up task,
  informed by the instrumentation added here).
- Filtering the Segments tab to the selected run (the known "shows all
  segments regardless of run" gap is out of scope).
- Reducing the ~2s checkpoint-segment registration latency (out of scope).
- Per-segment "capture just this one" buttons. Batch-for-run + Skip covers the
  surgical case.

## Behavior (user's POV)

- Finalizing a reference run → mode returns to **IDLE** (no Cold Capture).
- **Segments tab** gains a **Start Cold Capture** button. It is enabled only
  when (a) mode is IDLE and (b) the game has an active run. Clicking it queues
  the missing-cold segments belonging to the active run and enters Cold
  Capture. If nothing is missing, it surfaces a "no gaps" message and stays
  IDLE.
- While in Cold Capture, the progress UI (header + Manage banner) shows two
  controls:
  - **Skip** — abandon the current segment, advance to the next. If the queue
    drains, Cold Capture completes (→ IDLE).
  - **✕ / Exit** — abort the entire queue, return to IDLE. The emulator is
    **left where it is** (no power-cycle to title — revisit by feel later).

## Architecture / data flow

```
Segments tab "Start Cold Capture"
  → POST /api/cold-fill/start
      → resolve active run: db.get_active_capture_run(game_id)
      → ColdFillController.start(game_id, run_id=<active>)
          → db.segments_missing_cold(game_id, run_id=<active>)   # run-scoped
          → load first segment's hot state, mode → COLD_FILL

[user dies + respawns]  → ColdFillSpawnDetector emits SpawnEvent
  → SessionManager._handle_spawn → cold_fill.handle_spawn → save cold state,
    advance queue (existing behavior, unchanged)

Skip:   POST /api/cold-fill/skip   → ColdFillController.skip()  → _load_next()
Abort:  POST /api/cold-fill/abort  → clear queue, deactivate detector, mode → IDLE
```

The "active run" is the persistent `capture_runs.active = 1` row for the game
(set by finalize and by activating a run in Manage). It is distinct from the
transient in-memory `capture.active_run_id` (only set while recording).

## Backend changes

### Trigger model
- **Remove auto-trigger.** Delete the `cold_fill.start()` blocks in
  `SessionManager.finalize_run` (`session_manager.py:438-440`) and
  `save_and_finish_run` (`:450-452`). Both keep their finalize/notify behavior;
  they simply no longer enter COLD_FILL.
- **Run-scoped query.** `SegmentsMixin.segments_missing_cold(game_id, run_id:
  str | None = None)` gains an optional `AND s.capture_run_id = ?` clause
  (`db/segments.py:149`). `run_id=None` preserves the whole-game behavior (kept
  for any caller that still wants it / tests).
- **Active-run getter.** Add `get_active_capture_run(game_id) -> str | None` to
  the capture-runs mixin (`SELECT id FROM capture_runs WHERE game_id = ? AND
  active = 1`). No row → None.
- **Controller.** `ColdFillController.start(game_id, run_id: str | None = None)`
  threads `run_id` into `segments_missing_cold`.

### Skip
- `ColdFillController.skip() -> ActionResult` — pop `self.queue[0]` without
  saving, clear that segment's retry counter, then `await self._load_next()`.
  If the queue is now empty, reset (`current=None`, `cold_waypoint_id=None`)
  and return a completion result so the caller sets mode → IDLE.
- `SessionManager.skip_cold_fill()` wraps it and `_notify_sse()`.
- `POST /api/cold-fill/skip` — 409 if mode≠COLD_FILL; otherwise call through.

### Abort
- `ColdFillController.abort()` — `clear()` the queue/state.
- New `Poller.deactivate_cold_fill()` → `ColdFillSpawnDetector.deactivate()`
  (sets `_active = False`, clears `_segment_id`/`_waiting_spawn`). Routed
  through the orchestrator/emu backend the same way `activate_cold_fill` is.
- `SessionManager.abort_cold_fill()` — call `cold_fill.abort()`, deactivate the
  detector via the backend, set mode → IDLE, `_notify_sse()`. No `ResetCmd`.
- `POST /api/cold-fill/abort` — 409 if mode≠COLD_FILL; otherwise call through.

### Endpoint summary (all under `/api`, in `routes/system.py`)
| Route | Method | Guard | Effect |
|---|---|---|---|
| `/cold-fill/start` | POST | game loaded; mode==IDLE | scope to active run; → COLD_FILL or no_gaps |
| `/cold-fill/skip` | POST | mode==COLD_FILL | advance queue; → COLD_FILL or IDLE if drained |
| `/cold-fill/abort` | POST | mode==COLD_FILL | clear + → IDLE, leave emulator as-is |

## Frontend changes

- **AppState gains `has_active_run: bool`** (`api_schemas.py` + `state_builder.py`),
  computed as `db.get_active_capture_run(game_id) is not None`. This keeps the
  Segments tab from having to fetch the run list just to enable a button.
- `segments-view.ts`: add **Start Cold Capture** button. Enabled iff
  `state.mode === "idle"` and `state.has_active_run`. POSTs
  `/api/cold-fill/start`, surfaces the `no_gaps` response as an inline message.
- `header.ts` and the `manage.ts` cold-fill banner: add **Skip** and **✕**
  buttons wired to `/api/cold-fill/skip` and `/api/cold-fill/abort`.
- Regenerate `api-types.ts` from the OpenAPI schema (`npm run gen-types`,
  automatic on build/dev) after the routes land.

## Detector instrumentation (diagnostic only)

Add a change-triggered trace to `ColdFillSpawnDetector.step`: while `_active`,
when any of `player_anim`, `exit_mode`, `level_start`, `fanfare`, `io_port`, or
`_waiting_spawn` changes from the previous frame, log one compact line with
those values (the detector already tracks `_prev_*` for three of them). This
turns the next death reproduction into a definitive trace of which death
signal was (not) seen, without per-frame log spam.

**No change to detection logic.** `died_sprite`, `died_via_exit`, the
spawn conditions, and `resync_after_state_load` are untouched in this pass.

## Testing

- **Unit (`tests/unit`):**
  - `segments_missing_cold` honors `run_id` (scoped vs whole-game).
  - `get_active_capture_run` returns the active row / None.
  - `ColdFillController`: `start(run_id=...)` queues only that run's gaps;
    `skip()` advances and completes when drained; `abort()` clears state.
  - `SessionManager.finalize_run` / `save_and_finish_run` no longer enter
    COLD_FILL (regression test against the deleted auto-trigger).
- **Route tests:** start (run-scoped happy path, no_gaps, 409 when not IDLE),
  skip (advance + drain→IDLE, 409 when not COLD_FILL), abort (→IDLE, 409 when
  not COLD_FILL).
- **Frontend (`vitest`):** Start button enable/disable predicate; skip/exit
  button wiring posts the right endpoints.
- **Detector unit:** instrumentation logs on change and is silent on
  steady-state (no behavior regression).
- **Full suite:** `python -m pytest` (unit + emulator + frontend) green before
  done, per CLAUDE.md. Emulator suite must actually run (no skips).

## Follow-up (separate task)

Reproduce the death-detection failure live with the instrumentation above, then
root-cause and fix `ColdFillSpawnDetector` (Phase 1 of systematic debugging).
Candidate hypotheses to confirm/refute with the trace: death signature
mismatch for this hack (neither `player_anim==9` nor a clean `exit_mode` 0→non-0
edge), a frozen/deep-frozen emulator after loading a stale hot state (cf.
`project_framadvance_probe_too_aggressive`), or a non-gameplay "death" the user
performed. Skip mitigates this meanwhile.

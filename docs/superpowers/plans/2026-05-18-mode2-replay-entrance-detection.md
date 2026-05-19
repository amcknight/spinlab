---
date: 2026-05-18
status: deferred
focus: "Detector misses LevelEntranceEvent at replay start when RA was paused on a title-demo level_start=1 frame"
flake_rate: ~7% on full-suite (1/15 runs after Mode 1 fix landed)
test: tests/integration/test_replay_fixture.py::TestReplayFixture::test_replay_produces_segments
---

## Symptom

Intermittent failure of `test_replay_produces_segments`:
```
Failed: Replay did not produce 2 segments within 120s.
Last state: mode=replay, sections_captured=0
```

Replay starts correctly (`mode=replay`, `replay.total=2273`, `capture_run_id=replay_<hex>`), RA plays the recorded inputs, RA's WRAM advances (the playback-verify check passes) — but the dashboard's segment-recording pipeline never sees a `LevelEntranceEvent`, so no sections are recorded.

## Root cause

`TransitionDetector.step` fires `LevelEntranceEvent` only on a **rising edge** of SMW's level_start byte (`$1935`):

```python
# python/spinlab/retroarch/detector.py:125
edge_spawn = curr.level_start == LEVEL_START_ACTIVE and prev.level_start == 0
```

The `replay_ra_dashboard` fixture uses `ra_harness_love_yourself_no_reset`, which deliberately omits the per-launch fresh-boot savestate (`use_fresh_state=False`) because the user's actual `savestate_dir` must remain RA's working dir for `MovieController._stage_and_play` to write/read replay slots there.

Without a fresh-state load, RA boots fresh and starts the SMW **title-screen demo**. The harness's `PAUSE_TOGGLE` lands at a non-deterministic point in that demo:

- Sometimes RA is paused on the title-screen splash (level_start=0)
- Sometimes RA is paused on a frame mid-demo where the demo entered a level (level_start=1)

The dashboard's poller starts polling once the orchestrator connects. The detector's `_prev` snapshot becomes whatever RA's frozen state shows.

When the test sends `/api/replay/start`:
- RA loads the replay file's embedded savestate (level entrance, `level_start=1`)
- Poller's next tick reads the new state
- Detector compares `prev.level_start` (the frozen frame) vs `curr.level_start=1`:
  - If `prev=0`: rising edge → `LevelEntranceEvent` → segments captured ✓
  - If `prev=1`: no edge → entrance missed → segments never captured ✗

In Love Yourself's replay, SMW's level_start drops to 0 a few frames after the level entrance splash, then never returns to 1 within the 2273-frame replay (only one level). So a missed first edge is a missed entrance forever — no segment recording starts, checkpoints are captured into a non-existent segment, sections stays at 0.

## Why fresh-state harnesses don't hit this

`ra_harness_love_yourself` and `ra_harness_vanilla_smw` use `use_fresh_state=True`. The harness `LOAD_STATE_SLOT`s the pre-recorded fresh-boot state, which calls through `raclient.load_state`. That path bumps `state_version`, which the poller's `state_version` callback observes on the next tick, triggering `detector.resync_after_state_load`. The detector's prev is reset deterministically.

The replay path doesn't go through `raclient.load_state` — `MovieController.start_playback` calls `_movie_io.play_movie` which fires NCI `PLAY_REPLAY`. RA loads the replay's embedded savestate without the dashboard's `state_version` knowing. So the detector never resyncs.

## Why a simple resync isn't enough

Adding a `state_version` bump in `MovieController.start_playback` (the obvious first attempt) wouldn't actually fix this. `resync_after_state_load(snap)` sets `_prev = snap` (the current snapshot). If the replay's first polled frame has `level_start=1` and the next polled frame also has `level_start=1` (which it does — the splash holds for many frames), the edge check still sees `prev=1, curr=1` → no edge. Resync alone moves the bug from "depends on harness pause-frame" to "depends on the splash being captured across the resync boundary," which is still racey.

The fix has to synthesize a level entrance, not just resync state.

## Fix candidates

### A. Synthetic LevelEntranceEvent from MovieController (preferred)

After `play_movie` succeeds, MovieController emits a synthetic `LevelEntranceEvent` alongside `ReplayStartedEvent`. The poller's event handler routes it to `session_manager._handle_level_entrance`, which starts segment recording.

**Challenge:** the event needs `level`, `room`, `frame` fields. These come from RA's WRAM, which only the poller reads. MovieController would need to either:
1. Hold a reference to the poller / latest snapshot
2. Defer event emission to the poller via a "mark next snapshot as entrance" flag

Option 2 is cleaner. Concretely:
- Add a `force_next_level_entrance` flag to `TransitionDetector` (cleared after firing)
- Expose `detector.mark_replay_entrance()` 
- Pass the detector reference (or a setter callable) into MovieController via build_orchestrator
- MovieController calls `mark_replay_entrance()` before sending ReplayCmd

Bonus: solves the same class of bug for other "RA state changes without going through load_state" paths if they appear later.

### B. NCI RESET + repause in the no_reset harness path

Have the harness do an NCI `RESET` followed by a pause, ensuring level_start=0 at the moment the poller takes its first snapshot. Less invasive on production code; only the no_reset harness path changes.

**Challenge:** the RESET puts RA in PLAYING; we'd need a tight PAUSE within the title-demo's pre-level-entry window. That race is what gave us Mode 1 originally. Could be mitigated by:
- Calling RESET (which RA queues), THEN PAUSE_TOGGLE immediately after — both NCI commands; tight timing.
- Verifying via direct RAM read that `$1935 == 0` after pause; retry if not.

### C. RA RESET inside MovieController.start_playback

Production-side version of B. Before staging the replay file, send RESET, wait briefly, then PLAY_REPLAY. The replay's savestate load OVERWRITES the post-RESET title state, so the detector's prev (= post-RESET, level_start=0) → curr (= replay savestate, level_start=1) is a clean rising edge.

**Challenge:** RESET is a user-visible action. If anyone is watching the screen during replay start, they'd see a flicker through title. Probably acceptable but worth noting.

## Recommendation

Option A. It addresses the architectural gap (replay is a state-change channel that bypasses load_state's `state_version` signal). Options B and C are workarounds that paper over the gap.

Implementation steps:

1. Add `force_next_level_entrance: bool = False` to `TransitionState` (or as a separate flag on `TransitionDetector`).
2. In `detector.step`, treat `force_next_level_entrance and curr.level_start == LEVEL_START_ACTIVE` as a synthesized rising edge: clear the flag, take the entrance branch.
3. Add `detector.mark_replay_entrance()` that sets the flag.
4. Plumb a callable through wiring: `build_orchestrator` passes `poller.mark_replay_entrance` (a method that forwards to the detector) into MovieController constructor.
5. `MovieController.start_playback` calls this AFTER `play_movie` succeeds but BEFORE emitting ReplayStartedEvent.
6. Unit test: detector with `force_next_level_entrance=True` fires LevelEntranceEvent on the next tick where curr.level_start=1, regardless of prev. Verify the flag clears after firing.
7. Integration: re-run the 15-iteration stress test; flake rate should drop to 0%.

## Out of scope (for the Mode 2 fix specifically)

- Cleaning up the wider pyright surface (261 remaining errors across the codebase). Tracked separately.
- Frontend smoke fixture diagnostic gap (M3 from prior scan).
- Diagnostic hook duck-typing (`fixture_val[0].startswith("http")`, M from 2026-05-18 scan).

## History

- Mode 1 (PAUSE_VERIFY) fixed 2026-05-18 by bumping `PAUSE_VERIFY_RETRIES` 10 → 60 (~18s budget). 0 occurrences across 30+ subsequent full-suite runs.
- Diagnostic infrastructure fixes that made Mode 2 visible:
  - `_diagnostics.py` DB query had wrong column (`draft = 1` instead of `status = 'draft'`); replaced.
  - `install_log_handler` now sets `spinlab` logger to INFO so the replay-poll trajectory (`spinlab.replay_fixture_diag.info(...)`) lands in the ring; previously the ring was full of WARN+ noise only.
  - `pause_verify` loop emits warnings at attempts ≥ 10 every 5 retries so the ring shows the stall trajectory in the failure diagnostic.

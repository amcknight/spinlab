# Path to Full Parity

**Phase G shipped 2026-05-09. Mesen+Lua is gone. RA is the only backend.**

The items below are post-migration follow-ups, not prerequisites for daily use. P0 blockers (cold-fill, practice reload-on-death, Phase E option (b)) are the highest priority. P1 and P2 are quality-of-life and architecture cleanup.

*History: Plan 2 RA test harness landed 2026-05-08 (closed P1.2). Phase E option (a) landed 2026-05-08. Phase G landed 2026-05-09 (closed P2.2).*

## P0 — Blockers for daily-driving RA only

### P0.1 — Fix cold-fill on cp-respawn hacks

**Status (2026-05-08): Resolved.** Field-confirmed on Toothpaste and Love Yourself. Detector: `exit_mode==0 and anim!=9` while `_waiting_spawn=True`. Hack-independent.

**If the level-triggered fix still fails:** instrument the poller to dump raw `MemorySnapshot` values to the log every time `_waiting_spawn` is True. We're guessing at SMW behavior in this hack; one death's worth of real data settles the question.

### P0.2 — Verify practice reload-on-death after the first death

**Status:** Diagnostic logging added (`practice reload-on-death triggered/skipped`). Andrew's last test exposed first-time success, second-time silence. Until logs prove otherwise, the working hypothesis is one of:

1. `PracticeTiming` flipped to RESULT/IDLE between the two deaths (auto-advance fired prematurely).
2. The second Death/LevelExit event didn't fire from the detector.
3. `_practice_state_path` got cleared by an interleaving `_on_practice_attempt_result`.

The added logging discriminates between these. Run, read the log, fix the actual cause.

### P0.3 — Phase E: movie input recording + replay

**Status (2026-05-08):** Option (a) shipped. The historical "BSV" framing in the original spec was wrong on RA 1.22.2 — the actual NCI commands are `RECORD_REPLAY` / `HALT_REPLAY` / `PLAY_REPLAY`, the format is `.replay<slot>`, output dir is `<savestate_directory>/<core_name>/`, and the implementation was renamed `MovieRecorder` / `MoviePlayer` / `movie.py` accordingly.

**What landed:**
- `MovieRecorder` integrated into reference-run flow; `.replay` written alongside `.mss` state files
- `MoviePlayer` wired into `RetroArchOrchestrator._on_replay`; `ReplayCmd` no longer 501s
- Smoke tests cover record-toggle + playback-determinism (both pass on live RA)
- `tests/integration/test_replay_fixture.py` ported off Mesen, fixture `one_level.replay` recorded
- `replay_max_keep = "99"` and `run_ahead_secondary_instance = "true"` documented as required cfg in README

**Remaining for option (b) full parity:**
- **Throttle playback speed via NCI** so the production poller can keep up. Under uncapped playback the poller hits ~32Hz instead of 60Hz, missing transitions during segment capture. `test_poller_runs_during_playback` and `test_replay_fixture` xfail on this. NCI candidates: `slowmotion_ratio`, `--speed=` flag, or temp cfg override. Once landed, both xfails should auto-clear (strict=False).
- `.spinrec` → `.replay` converter — low priority; Andrew has acknowledged re-recording is acceptable.

**Resolved 2026-05-09:** the BSV+SAVE_STATE-during-record bug (slot-management.md) is fixed on a vendored RA build at `C:/RetroArch-Win64-fixed/`. Root cause was a one-line C99 update-mode violation in upstream `replay_get_serialized_data`; fix is `intfstream_seek(handle->file, file_end, SEEK_SET)` after the read. Verified end-to-end: `count_replay_frames.py` shows `parsed=1201, header=1201, MATCH` for a 20s recording with three saves. See `upstream-fix-findings-2026-05-09.md` and memory `project_ra_vendored_build.md`.

Phase E's planning artifacts: spec at [`docs/superpowers/specs/2026-05-08-phase-e-movie-replay-design.md`](../superpowers/specs/2026-05-08-phase-e-movie-replay-design.md), plan at [`docs/superpowers/plans/2026-05-08-phase-e-movie-replay.md`](../superpowers/plans/2026-05-08-phase-e-movie-replay.md). The 2026-05-06 frozen spec is now historical context only.

## P1 — Quality-of-life

### P1.1 — Hot-swap ROM mid-session

User opens dashboard with ROM A, then changes RA's ROM to ROM B. The basename auto-refresh works (next save targets ROM B's slot files), but the dashboard's game context (segment list, scheduler, etc.) is still on ROM A. Detection of the swap and a clean re-init would be a real workflow improvement.

### P1.2 — Skip Mesen-only tests when backend is RetroArch

**Resolved 2026-05-08/2026-05-09.** RA poke harness landed 2026-05-08 — all 9 transition scenarios run through `RAHarness`/`RAPokeEngine`. Phase G (2026-05-09) deleted the remaining Mesen-bound tests; the suite is now fully RA-backed.

### P1.3 — Cold-fill timeout / abort

Current behavior: cold-fill hangs forever if the player can't trigger a spawn. The player has no signal to give up; closing the dashboard is the only out. Add either:

- A "Skip this segment" UI button.
- A timeout (10 min?) that abandons the segment and continues.

Either way, leaves the segment in the gap list to retry.

### P1.4 — RA verbose logging on demand

When something doesn't work, RA's verbose log (`log_verbosity = "true"` + `log_to_file = "true"`) is the diagnostic of last resort. Currently the user has to manually edit `retroarch.cfg` to turn it on. A dashboard "diagnostic mode" toggle that does this AND ensures it gets turned back off would shorten the next "why doesn't this work" session.

## P2 — Architecture cleanup

### P2.1 — Unify timing.py with attempt-result event flow

`PracticeTiming.observe_event` and `SpeedRunTiming.observe_event` both inspect `event_dict.get("event")` strings. The dispatch is fine, but the two state machines are nearly disjoint and could share less. Worth a refactor only if a third timing mode appears.

### P2.2 — Remove Mesen-specific code paths

**Resolved 2026-05-09.** Phase G shipped: `lua/`, `tcp_manager.py`, `spinrec.py`, `spinrec_path` DB column, and Mesen-aware code paths across config / dashboard / routes / capture / protocol / session_manager all gone. README is RA-only. ARCHITECTURE.md rewritten for RA-only. Existing pre-Phase-G databases require `spinlab db reset` to upgrade.

### P2.3 — `state_path_for` resolver leaks anonymous keys

`StateIO.resolve_event_path` returns paths keyed by `entrance_<level>_<room>` and `cp_<level>_<ord>_hot` for events whose true segment_id isn't known yet. F-live does the bridging downstream. Cleaner: the orchestrator looks up the segment_id from the event before resolving, and `resolve_event_path` only takes a segment_id. Requires teaching the orchestrator about the segment table.

## P3 — Worth thinking about, low priority

### P3.1 — RA core selection at runtime

Today: `routes/system._launch_retroarch` hardcodes `snes9x_libretro.dll`. Other libretro cores (bsnes, mesen-s, etc.) might support different memory access patterns. If a user has a strong preference, they currently can't get it without editing source. Surface as config.

### P3.2 — Network RetroPad as input fallback

Phase 0's spike validated that NCI hotkey-sim doesn't work for some hotkeys (`MENU_TOGGLE`, etc.) — they're filtered at RA's input layer. Network RetroPad (UDP virtual controller, port 55400) bypasses this. Not currently used by SpinLab; could be useful if BSV replay needs supplementary input injection.

### P3.3 — Multi-game session (one dashboard, multiple ROMs in series)

Out of scope today (one game per session). RA supports core swapping which would in principle allow it. Big lift and not a current priority.

---

## Definition of "daily-driver quality" (post-migration follow-ups)

Phase G shipped and the Mesen backend is gone. The remaining items below are quality-of-life improvements, not migration gates:

1. Reference recording captures inputs (`.replay`) AND state files. ✓ states  ✓ inputs (verified 2026-05-09 on vendored RA with BSV-checkpoint fix; stock RA still produces truncated `.replay`)
2. Reference run saved, finished, resumed, discarded end-to-end. ✓ mostly field-tested; discard cascade FK needs re-verify
3. Practice loop runs N segments, reload-on-death after every death. ⚠ second-death failure unresolved
4. Cold-fill captures cold variants for cp-respawn hacks. ✓ field-confirmed (2026-05-08)
5. Speed-run mode runs a full level start-to-finish with checkpoint splits. Untested on RA.
6. Replay loads a `.replay` and reproduces transitions identically. ⚠ partial — isolated playback deterministic; segment capture during replay xfailed on poller starvation. (BSV+SAVE_STATE blocker resolved 2026-05-09; only the throttle remains.)
7. Full pytest suite green. ✓ (Phase E xfails are intentional, strict=False)
8. At least one full real speedrun (e.g. "Love Yourself" any%) completed end-to-end with no manual workaround. Untested.

# RetroArch Migration — Lessons Learned

For whoever picks this up next. Things that ate hours and would have saved them.

## The runahead trap

Single-instance runahead silently corrupts NCI `SAVE_STATE`. RA logs `[State] Saving... File already exists. Saving to backup buffer... Loading state...` because runahead is using the same save buffer that NCI just wrote to.

The symptom is "SAVE_STATE returns OK but no file appears" — looks identical to the command being silently swallowed. Debugged for an entire session. The fix is one line in `retroarch.cfg`:

```
run_ahead_secondary_instance = "true"
```

Phase 0's spike script flagged SAVE_STATE as INCONCLUSIVE; that flag was load-bearing. Don't dismiss inconclusive results in pre-flight as "probably works."

## The basename trap

`SAVE_STATE` over NCI writes to `<savestate_dir>/<basename>.state<auto_index>`. The `<basename>` comes from RA's currently-loaded ROM, NOT from the SpinLab config. If your config says `ra_game_basename = "Toothpaste World"` but the user opened `Toothpaste.smc`, mtime polling watches files that never get written and times out forever.

Fix: orchestrator overrides config's `ra_game_basename` from `GET_STATUS` at every connect AND before every save. The config field is now optional and the dashboard logs which basename it's actually using.

If you're chasing a "saves silently fail" bug, first sanity-check what RA reports for `GET_STATUS.game` and what your StateIO thinks the basename is. They should be identical strings.

## Don't trust `pytest -m "not emulator"` as your gate

Andrew called this out mid-migration: I was running `pytest -m "not (emulator or slow or frontend)"` (~23s) and reporting work as done. That subset misses entire categories of failure — most importantly, the integration tests that exercise the full FastAPI + DB + emulator stack. CLAUDE.md says "the full unfiltered suite" and that's load-bearing. A fast subset is for inner-loop development, not for declaring done.

## SMW death detection has more edges than `anim == 9`

The Lua reference implementation only watches `player_anim` 0→9 (sprite hit). Live testing on Toothpaste exposed two failure modes:

- **Pit-falls.** Mario falls off the bottom of the screen with anim=0 the whole time. The only signal is `exit_mode` going 0→non-zero with no goal flag (no `fanfare`, no `io_port` of orb/goal/key).
- **CP-respawns.** When the player dies with a CP active, some hacks just teleport them back to the CP without reloading the level. `level_start` stays at 1 throughout. `edge_spawn` (the standard "respawn" signal: `level_start` 0→1) never fires.

Both are real and both are common. The Python implementation in `cold_fill.py` and the orchestrator's reload-on-death path accept all three death signals and a level-triggered "back to playable" spawn signal.

If you're porting any other Lua detection logic, **drive it with a real death scenario from at least three different hacks before declaring it correct.** The Lua version was correct in the limited cases it was exercised on.

## State-load resync must reset detection state, not just `prev`

Lua's `state_just_loaded` re-sync replaces the prev snapshot to suppress phantom edges. Direct port: `_prev = snapshot`. Done, right?

No. The detector also tracks `died_flag`, `cp_acquired`, `cp_ordinal`, `first_cp_entrance`, and `_exit_this_frame`. After a state load, all of these MUST be reset to baseline. Otherwise:

- `died_flag` stuck → no Death event ever fires again for the rest of the session
- `cp_acquired` stuck → first respawn after load is misclassified as cold spawn
- `_exit_this_frame` stuck → next frame's LevelEntrance gets suppressed

This bit practice mode catastrophically — every reload-on-death after the first one stopped firing because `died_flag` was sticky. The fix is `TransitionDetector.resync_after_state_load` calling `_state.reset()` plus clearing the related instance fields.

If you're porting more state-loading logic, **always treat a state load as semantically a fresh start.** Anything that says "did X happen?" must be reset to "no, not yet."

## Windows process trees need Job Objects

`subprocess.Popen("npm.cmd run dev").terminate()` only kills `npm.cmd`. The `node.exe` child it spawned keeps running, pinned to port 5173, until the next reboot.

Fix: create a Windows Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and assign the child process to it. When the Job handle closes (parent exits), Windows kills every process in the job. `python/spinlab/vite.py` does this via ctypes.

If you're spawning anything from Python on Windows that itself spawns children, assume the children outlive `terminate()`. They will.

## Move-with-retry on Windows

`shutil.move` raises `PermissionError` when the source file is still mapped by another process. RA holds the slot file open for ~100-300ms after writing. Five retries × 100ms backoff handles this; falling back to `shutil.copyfile` (leaving the source for the next sweep) handles the worst case.

## NCI is not a file-based protocol

NCI commands are sent over UDP and may be silently dropped. There is no transactional guarantee that "I sent SAVE_STATE" means "RA saved." The mtime-polling pattern in `state_io._try_one_save` is the workaround: snapshot mtimes, fire the command, watch for any matching file to change. If no change in `save_timeout_sec`, the command was almost certainly dropped — retry.

If you ever add a new NCI command that has side effects, you need an equivalent verification pattern. Don't trust the "OK" reply.

## "It worked once" is not the same as "it works"

Andrew found one stale `entrance_46_10.state` file from an earlier session — proof that NCI SAVE_STATE had succeeded at least once, before runahead overwrote subsequent saves. That single file changed the diagnosis from "the command is broken" to "the command works but something destroys the result." Look for accidental success signals — they narrow the search dramatically.

## Don't simulate keypresses

The temptation when NCI doesn't work is to fall back to `pyautogui` / SendInput / equivalent. Andrew called this out: "avoid the method of simulating presses in Windows or whatever until last resort, because I worry about desync slippage." Keypress injection is asynchronous to the emulator frame loop; a save fired by simulated F2 might land between any two frames, including frames the user cares about for input timing. NCI is synchronous to the emulator's command queue. Always prefer NCI.

## RA 1.22.2 rejects `--video=null` CLI flag

The expected null-driver invocation `retroarch --video=null --audio=null` works in some RA builds and fails to launch in others (1.22.2 specifically — silently exits with no error to stdout/stderr). The portable form is `--appendconfig <path>` pointing at a temp `retroarch.cfg` containing:

```
video_driver = "null"
audio_driver = "null"
```

The cfg-file form has worked across every RA build we've tried. Caught during Plan 2 live-integration when `RAHarness.launch` hung on NCI ping retries. Don't trust a CLI flag form just because the docs list it.

## Headless RA launches already-paused

With `video_driver = "null"`, RA boots into a paused state with no rendering loop. The `is_core_running(tick_addr)` heuristic (read a byte, sleep 50ms, read again, compare) returns False not because the core is hung but because there are no frames to advance against the wall clock — same observation, different cause.

Use `GET_STATUS` instead. Its reply explicitly reports `PAUSED` / `PLAYING` / etc. For pause-state confirmation, `GET_STATUS` is the right primitive; `is_core_running` is for detecting the deep-pause failure mode where the core thread froze. Don't conflate the two.

## `.poke` scenario keys carry Lua-era normalization

The poke-format key for the SPC I/O port is `io`, not `io_port`. This came from `lua/addresses.lua` defining `ADDR_IO = 0x1DFB` and the integration `addresses.py` parser normalizing `ADDR_IO` → `io` (strip `ADDR_`, lowercase). Every `.poke` scenario file was written against `io`.

When porting `ADDR_MAP` to a hand-maintained Python re-export, it's tempting to clean up "io" to "io_port" — DON'T. The `.poke` files are the source of truth for the user-facing key vocabulary; the Python `ADDR_MAP` keys must match them character-for-character. A spec that says "io_port" needs to be silently corrected to "io" at implementation time.

## Subagent-driven plans don't survive the smoke test

The migration was planned and executed via the subagent-driven-development skill: a multi-phase plan (B / C / D / E / F-live) with implementer + spec-reviewer + code-quality-reviewer subagents per task. The unit-test pass rates on each phase were near-perfect. The number of bugs that survived to live testing was double-digit.

This isn't an indictment of the methodology — the unit tests caught what they covered, and the live-testing phase was the explicit gate. But it's a reminder that **plan reviews and tests are a floor, not a ceiling.** A green CI on a complex migration is not a green light to merge to main; budget at least as much time for live-testing as for implementation.

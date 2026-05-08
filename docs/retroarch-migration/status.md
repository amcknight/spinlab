# RetroArch Migration — Status (as of 2026-05-07)

What works, what's broken, what's untested. The frozen historical artifacts are in `docs/superpowers/specs/2026-05-06-retroarch-migration-design.md` plus the per-phase plans in `docs/superpowers/plans/`. This doc supersedes them as the live picture.

## TL;DR

The RetroArch backend (NCI + Python orchestrator) replaces the Lua-in-Mesen path for everything except **Replay** (Phase E, deferred). The full reference → cold-fill → practice loop runs end-to-end against `snes9x_libretro.dll` on Windows.

Mesen + Lua still works in parallel — backend selected via `config.yaml` `emulator.backend`. No code path runs both at once.

## What works

- **NCI transport.** UDP 55355 to RetroArch. `NCIClient` (`python/spinlab/retroarch/nci.py`) — VERSION, GET_STATUS, READ_CORE_RAM, SAVE_STATE, LOAD_STATE_SLOT, RESET. Reconnect-on-failure with suppression-after-first to avoid log spam.
- **Memory polling.** `Poller` (`python/spinlab/retroarch/poller.py`) reads SMW WRAM at 60 Hz via `read_snapshot`, drives a stateful `TransitionDetector`, and emits typed events (`Death`, `LevelExit`, `Checkpoint`, `LevelEntrance`, `Spawn`).
- **Save/Load via filesystem shuffle.** `StateIO` (`python/spinlab/retroarch/state_io.py`) — fires NCI `SAVE_STATE`, mtime-polls for the new `<game>.state*` file RA wrote, moves it to a SpinLab-keyed path. Load is the reverse via reserved slot 9999. Retries the save (3×) and the move (5×) to ride out transient locks/no-ops.
- **Reference recording.** State captures at level entrance and checkpoint events. Segment DB rows are written from those events. **No `.spinrec` input recording** — that's Phase E.
- **Cold-fill capture.** Loads a hot CP state, watches for death-then-respawn, captures the post-respawn frame as the cold variant. Detects death via three paths: (a) `player_anim` 0→9 (sprite hit), (b) `exit_mode` 0→non-zero with no goal flag (pit-fall / other deaths that skip anim=9), (c) post-death `exit_mode` non-zero→0 with `level_start=1` (cp-respawn hacks where the level isn't reloaded).
- **Practice loop.** Loads segment start state on `practice_load`. Reload-on-death triggers when either a `Death` event OR a `LevelExit(goal='abort')` fires while `PracticeTiming.is_armed`. Replicates Lua's `pending_loads` behavior in async.
- **Speed-run mode.** `SpeedRunTiming` (`timing.py`) ports the Lua state machine. Untested in the field under RA backend.
- **RA auto-launch.** `routes/system._launch_retroarch` starts `retroarch.exe` with the configured ROM if NCI ping fails.
- **Auto-recovery of basename.** Orchestrator sets `state_io.game_basename` from RA's `GET_STATUS` at connect, ignoring `config.ra_game_basename`. Eliminates the silent save-failure when configured basename != loaded ROM.
- **Stale slot file cleanup.** Reserved slot file gets unlinked on connect and after every load.
- **Vite descendant cleanup.** Windows Job Object with `KILL_ON_JOB_CLOSE` ensures `node.exe` children die when the dashboard exits.

## Known broken / untested

- **Replay (Phase E).** `ReplayCmd` and `ReplayStopCmd` raise `BackendNotImplementedError` → HTTP 501. Replay needs BSV (libretro deterministic movie format) for input playback. Mesen's `.spinrec` (joypad bitmask per frame) doesn't translate. **Replay-fixture tests fail under RA backend** (they exercise Mesen via `TcpManager`, which the RA backend replaces).
- **Practice reload-on-death after first death.** Reproducer (Andrew, 2026-05-07): first koopa hit reloads correctly; second identical hit does not. Diagnostic logging now lives on `PracticeSession.handle_death` (the reload-on-death logic moved out of `RetroArchOrchestrator` in the 2026-05-07 backend-layering refactor — see `docs/superpowers/plans/2026-05-07-backend-layering.md`). Next test should reveal whether `_current_state_path` is None when expected to be set, or whether the Death/LevelExit event itself stopped firing.
- **Cold-fill cp-respawn capture (two fixes attempted, neither sticks yet).** First attempt (edge-triggered `exit_recover` on `exit_mode` 1→0) failed in v10 of the reference run — 2 LevelExits, 0 Spawns. Second attempt (level-triggered `playable` check: emit Spawn on the first frame where `exit_mode=0 AND level_start=1 AND anim != 9` while in `waiting_spawn`) added 2026-05-07 with regression tests; not yet field-tested. If even this misses, the next debugging step is to log raw memory snapshots while the player dies during cold-fill — something about the SMW state values isn't matching expectations.
- **Integration tests under `tests/integration/test_transitions.py` and `test_replay_fixture.py`.** All Mesen-based; they connect via `TcpManager`, which is bypassed under RA. Failures are pre-existing under the RA backend, not regressions. Either skip them when `backend == 'retroarch'` or wait until Phase E ports them.
- **Multi-ROM hot-swap.** If the user changes RA's loaded ROM mid-session, basename auto-refresh works on the next save attempt but the dashboard's game context doesn't follow. Tested only one-direction: open dashboard → RA already has ROM loaded.
- **Cheevos hardcore mode silently disables NCI savestate commands.** Set `cheevos_hardcore_mode_enable = "false"` in `retroarch.cfg`. Documented in `spike-log.md`.
- **Runahead with `run_ahead_secondary_instance = false` corrupts saves.** This was the root cause of Phase 0's "INCONCLUSIVE" SAVE_STATE finding. Single-instance runahead overwrites the save buffer. Force `run_ahead_secondary_instance = "true"` in `retroarch.cfg`.

## Untested but should work

- Speed-run mode end-to-end. Unit tests pass; never exercised live.
- `.smw`-anything-other-than-Toothpaste. Address map is SMW-specific; RA backend assumes SMW core memory layout via `addresses.py`.
- Conditions framework (`ConditionRegistry`). Wired through Poller; not exercised in practice tests.

## Next test pass priorities

1. Cold-fill cp-respawn — re-run under Toothpaste, confirm Spawn fires and cold CP state is captured.
2. Practice reload-on-death — second-hit failure. Logs will tell us where it stopped.
3. Save & finish run — end-to-end with the new state-only-no-spinrec path.
4. Delete reference run — was 500ing on cascade FK; nulling `capture_session_id` first should have fixed it. Re-verify.

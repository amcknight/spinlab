# RetroArch Migration — Status (Phase G shipped 2026-05-09)

The migration is complete. RA is the only backend. SpinLab no longer ships or supports Mesen+Lua. This doc is now historical — post-migration follow-ups are tracked in `path-to-parity.md`.

## What shipped

- **NCI transport** (`retroarch/nci.py`). UDP 55355 to RetroArch. `VERSION`, `GET_STATUS`, `READ_CORE_RAM`, `SAVE_STATE`, `LOAD_STATE_SLOT`, `RESET`, `RECORD_REPLAY`, `HALT_REPLAY`, `PLAY_REPLAY`. Reconnect-on-failure with log-spam suppression.
- **Memory polling** (`retroarch/poller.py`). `Poller` reads SMW WRAM at 60 Hz, drives `TransitionDetector` and `ColdFillSpawnDetector`, emits typed events.
- **Save/Load via filesystem shuffle** (`retroarch/state_io.py`). NCI `SAVE_STATE` → mtime-poll → move to SpinLab-keyed path. Load is reverse via reserved slot 9999. Retries to handle Windows file-lock delays.
- **RA poke test harness** (`tests/integration/conftest.py`, `RAHarness`, `RAPokeEngine`). All 9 `.poke` transition scenarios pass through the production `TransitionDetector`. Landed 2026-05-08.
- **Reference recording**. State captures at entrance and checkpoint events. Segment DB rows written.
- **Cold-fill capture**. Field-confirmed on Toothpaste and Love Yourself (2026-05-08). Death detected via `player_anim` 0→9 OR `exit_mode` 0→non-zero. Spawn captured on `exit_mode==0 and anim!=9` while `_waiting_spawn=True`. Hack-independent.
- **Practice loop**. Loads segment start state on `practice_load`. Reload-on-death on `Death` or `LevelExit(goal='abort')` while `PracticeTiming.is_armed`.
- **Phase E option (a): movie record + isolated playback** (`retroarch/movie.py`). Landed 2026-05-08. See section below.
- **Phase G: Mesen deletion**. Shipped 2026-05-09. `lua/`, `tcp_manager.py`, `spinrec.py`, `spinrec_path` DB column, and all dual-backend conditionals gone. README is RA-only.

## Phase E state — read carefully

Phase E option (a) "validate movie record/playback works in isolation" **SHIPPED 2026-05-08**. Phase E option (b) "full parity" — replay-during-reference + segment capture during replay — **DID NOT SHIP**. The nuance matters: the code paths for BSV record and play exist and work in isolation, but they cannot be combined with SpinLab's normal reference flow without corrupting the recording.

### What works (Phase E option a)

- Movie record via NCI `RECORD_REPLAY` / `HALT_REPLAY` (`MovieRecorder` in `python/spinlab/retroarch/movie.py`). Produces `.replay` files discovered by mtime baseline diff and moved to `<data_dir>/<game_id>/rec/<ref_id>.replay`.
- Movie playback via NCI `PLAY_REPLAY` (`MoviePlayer`). A WRAM-advance verification heuristic detects whether RA actually loaded the file; emits `ReplayErrorEvent` on failure instead of leaving the dashboard stuck.
- Determinism smoke: `tests/integration/test_movie_smoke.py::test_movie_playback_deterministic` passes on live RA. **Caveat: this test may be a false positive** — see slot-management.md for why.
- Three smoke tests in `tests/integration/test_movie_smoke.py` pass on live RA.
- The dashboard's `_on_reference_start` triggers `MovieRecorder.start` alongside state captures (BUT see "what's broken" below).

### What's broken / xfailed

**1. SAVE_STATE during BSV recording corrupts the recording.**

This is a hard limitation in RA 1.22.2 with snes9x_libretro and bsnes_libretro. When SpinLab's reference flow fires `SAVE_STATE` on segment events while `RECORD_REPLAY` is active, RA's `bsv_movie_write_checkpoint()` returns -1 and silently terminates the recording. Result: `.replay` files written during reference runs are truncated — the input track ends at the first `SAVE_STATE`. The state files are unaffected and written correctly; only the BSV is broken.

Confirmed test matrix (`replay_checkpoint_interval = "0"` and `"1"` both fail; `"0"` truncates silently, `"1"` logs `[ERROR] [Replay] failed to write checkpoint, exiting record`).

Documented exhaustively in `slot-management.md` including four workaround paths (decouple recording from saves, multi-segment recording, core swap, RA patch) — none implemented. The current de facto state: `_on_reference_start` starts `MovieRecorder`, but the resulting `.replay` files are corrupt and unused. The code path is wired but not delivering working output.

**2. Poller starvation under uncapped playback.**

The production poller hits ~32 Hz instead of 60 Hz during `PLAY_REPLAY` (NCI bandwidth contention). Transitions are missed at that rate. Spec mitigation: throttle playback speed via NCI (`slowmotion_ratio` or `--speed=` flag). Not implemented.

xfailed: `tests/integration/test_movie_smoke.py::test_poller_runs_during_playback`.

**3. End-to-end replay → segment capture broken.**

Depends on (2) above. Even when playback eventually loads, segment capture fails due to missed transitions.

xfailed: `tests/integration/test_replay_fixture.py::TestReplayFixture::test_replay_produces_segments`.

**4. Replay playback — RA slot resolution.**

`MoviePlayer` stages our `<ref_id>.replay` as `<game_basename>.replay<N>` for the slot RA's runtime expects. The runtime slot differs from the cfg slot because `replay_auto_index = "true"` persists across sessions in an internal RA file (not `retroarch.cfg`, not the `.lrtl` tracker). The orchestrator parses RA's log file for the latest `[Replay] Replay slot: N` line and uses that as the staged slot. This is fragile and tightly coupled to RA's log format. `log_to_file = "true"` is required cfg for this to work. Without it, falls back to slot 0 which may not match.

### Slot-management hackiness (worth reading before touching this code)

`docs/retroarch-migration/slot-management.md` documents the full picture: RA's slot system (state slot 9999 strategy, replay slot inferred from log parsing), file-discovery via mtime baseline, WRAM-advance verification heuristic, and what "good" would look like. **The smoke test that "validates determinism" may be a false positive** — documented in slot-management.md. Don't trust it as evidence movie playback works without verifying with a content-based check (two different `.replay` files producing different WRAM bytes).

## Post-migration follow-ups

These are not blockers for daily use. Tracked in `path-to-parity.md`.

- Practice doesn't end on goal (need trace logging to confirm — possibly `session_manager._handle_level_exit` swallows the event).
- Practice reload-on-death fails after the first death (diagnostic logging added; next test pass needed).
- Spurious checkpoint+entrance events on overworld between level exits and entrances.
- Stale segments leak into cold-fill and practice queues (segments from prior runs show up).
- "Cold already captured" mismatch — reference logs `state_captured=True` but cold-fill still prompts.
- Reset button needs pressing twice (likely debounce guard).
- Speed-run mode end-to-end untested on live RA.
- Phase E option (b) full parity (replay → segment capture) — depends on BSV+SAVE_STATE fix and poller starvation fix.
- Hot-swap ROM mid-session (basename auto-refresh works; game context in dashboard doesn't follow).

## RetroArch cfg requirements (RA 1.22.2)

Required in `retroarch.cfg` for SpinLab to work:

```
network_cmd_enable = "true"
network_cmd_port = "55355"
cheevos_hardcore_mode_enable = "false"
run_ahead_secondary_instance = "true"
replay_max_keep = "99"
log_to_file = "true"
log_to_file_timestamp = "true"
log_verbosity = "true"
```

See README for explanations of each. `cheevos_hardcore_mode_enable` and `run_ahead_secondary_instance` are non-obvious and cause silent failures if wrong.

## Historical debug-session notes

**2026-05-08 cold-fill debug session.** Three rounds of fixes. Key traps: (a) `$1935`/`level_start` is Lunar-Magic-deprecated free RAM — not a reliable "in-level" signal; (b) `game_mode` uses 20, 22, and 3 for valid playable contexts across hacks, so single-value gates fail; (c) `ColdFillSpawnDetector` needs its own `resync_after_state_load` (same class of bug as `TransitionDetector`'s `mark_state_loaded` — without it, phantom deaths fire on the first poll after a hot-state load). Final detector: `exit_mode==0 and anim!=9` while `_waiting_spawn=True`.

**`anim` value 22 observed (2026-05-08).** `$0071` is documented to max at `0x0D`. Seen after a hot-state load on Love Yourself L58 — likely a stale snapshot mid-load. Phantom-death fix sidesteps this for cold-fill. If a future bug points at `anim` reading garbage, ask "is the snapshot consistent with the just-loaded state?" first.

**Phase 0 INCONCLUSIVE `SAVE_STATE` finding.** The Phase 0 spike couldn't get NCI `SAVE_STATE` to work. Root cause: `run_ahead_secondary_instance = "false"` (the default). Single-instance runahead corrupts the state buffer; `SAVE_STATE` produces wrong/missing output silently. Fix: force `run_ahead_secondary_instance = "true"`.

**2026-05-08 RA test harness integration lessons.** Three harness-level bugs found: RA 1.22.2 rejects `--video=null` CLI form (use `--appendconfig` with a temp cfg writing `video_driver = "null"` instead); RA launches already-paused with null video driver, so `GET_STATUS` is the right pause-detection primitive (not `is_core_running`); `.poke` files use the symbolic key `io` (not `io_port`).

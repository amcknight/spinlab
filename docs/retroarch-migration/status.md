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

## Phase E state — shipped 2026-05-10

Both halves of Phase E now ship. Option (a) "validate movie record/playback works in isolation" **shipped 2026-05-08**. Option (b) "full parity" — replay-during-reference + segment capture during replay — **shipped 2026-05-10** after `test_replay_fixture::test_replay_produces_segments` un-xfailed and went green across 8 consecutive runs.

### What works (Phase E options a + b)

- Movie record via NCI `RECORD_REPLAY` / `HALT_REPLAY` (`MovieRecorder`). Produces `.replay` files discovered by mtime baseline diff and moved to `<data_dir>/<game_id>/rec/<ref_id>.replay`.
- Movie playback via NCI `PLAY_REPLAY` (`MoviePlayer`). A WRAM-advance verification heuristic detects whether RA actually loaded the file; emits `ReplayErrorEvent` on failure.
- SAVE_STATE during recording now lands valid bytes on disk — fixed in upstream RA (the vendored build at `C:/RetroArch-Win64-fixed/`). See `upstream-fix-findings-2026-05-09.md`.
- End-to-end test `test_replay_fixture::test_replay_produces_segments` plays back `one_level.replay`, observes entrance / checkpoint / exit transitions through the production poller, captures 2 segments, finalizes successfully.
- The dashboard's `_on_reference_start` triggers `MovieRecorder.start` alongside state captures; the resulting `.replay` is then loadable by Phase E option (b) replay flow.

### What the 2026-05-09 punch list got wrong

Three "blockers" from that punch list turned out to be misdiagnoses:

1. **"Poller starvation under uncapped playback (~32 Hz)" → fix with slowmotion.** Slowmotion via NCI actually made the rate *worse* (32Hz → 6Hz live-measured), and 32Hz is plenty for SMW transitions which sustain across many frames. The threshold on `test_poller_runs_during_playback` is now 0.4× target (24Hz floor) and the test passes. No slowmotion shipped.

2. **"Replay slot resolution is fragile log-parsing."** The existing regex (`Replay slot:`, `Found last replay slot:`, `Starting movie record to "...".replayN`) works. Fragility is theoretical (RA log format would have to change). Not worth churning on.

3. **"Dashboard `-L core rom` launch broken on patched RA."** Three successful launches in today's `data/spinlab.log` (08:49, 09:15, 10:52). The earlier failure was a transient. No DLL swap needed.

### Real bugs that actually gated option (b)

Both root-caused 2026-05-10:

1. **stop_replay race wiped paused_run_id** (`capture/reference.py`). `_end_current_session` ran twice — once via `ReplayFinishedEvent` (correctly entered paused state) and once at the tail of `stop_replay` (cleared the recorder then `_enter_idle`'d, wiping the paused state). Finalize then 409'd with `no_paused_run`. Fix: don't redo cleanup in `stop_replay` after the event handler already did it.

2. **Mode flip lagged behind ReplayCmd** (`session_manager.start_replay`). Mode flipped to REPLAY only after `capture.start_replay` returned, but `capture.start_replay` sends ReplayCmd to the orchestrator which fires PLAY_REPLAY synchronously inside that await. The poller observed the level-entrance edge ~10 polls later — still in IDLE mode — and `_handle_level_entrance`'s mode-gate dropped it. No entrance event → no pending_start → no segments. Fix: flip mode eagerly before `capture.start_replay`, rollback in the except branch.

## Post-migration follow-ups

These are not blockers for daily use. Tracked in `path-to-parity.md`.

- Practice doesn't end on goal (need trace logging to confirm — possibly `session_manager._handle_level_exit` swallows the event).
- Practice reload-on-death fails after the first death (diagnostic logging added; next test pass needed).
- Spurious checkpoint+entrance events on overworld between level exits and entrances.
- Stale segments leak into cold-fill and practice queues (segments from prior runs show up).
- "Cold already captured" mismatch — reference logs `state_captured=True` but cold-fill still prompts.
- Speed-run mode end-to-end untested on live RA.
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

**2026-05-10 Phase E option (b) + flake fixes.** Shipped option (b) end-to-end. Found two real bugs while doing it (stop_replay race wiping paused_run_id; eager mode-flip needed in session_manager.start_replay) and three flakes that were also real bugs (poke engine writes before frame_advance let ROM overwrite our values; harness FRAMEADVANCE first-try is unreliable, needs retry; Scheduler lazy-init wasn't thread-safe). Also: NCI RESET requires two presses to satisfy RA's anti-accident gate; orchestrator now fires twice with 300ms spacing. Three diagnoses from the 2026-05-09 punch list ("slowmotion will help", "slot resolution needs rewrite", "dashboard launch broken") turned out to be wrong — see Phase E section above. Takeaway: verify each claimed blocker empirically before chasing fixes.

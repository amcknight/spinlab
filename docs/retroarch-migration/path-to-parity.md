# Path to Full Parity

What would be needed to fully retire the Mesen+Lua backend and use RetroArch exclusively. Items roughly ordered by what unblocks the most.

## P0 — Blockers for daily-driving RA only

### P0.1 — Fix cold-fill on cp-respawn hacks

**Status:** Two attempts landed (`exit_recover` edge-trigger, then `playable` level-trigger). Neither field-tested as of 2026-05-07.

**If the level-triggered fix still fails:** instrument the poller to dump raw `MemorySnapshot` values to the log every time `_waiting_spawn` is True. We're guessing at SMW behavior in this hack; one death's worth of real data settles the question.

### P0.2 — Verify practice reload-on-death after the first death

**Status:** Diagnostic logging added (`practice reload-on-death triggered/skipped`). Andrew's last test exposed first-time success, second-time silence. Until logs prove otherwise, the working hypothesis is one of:

1. `PracticeTiming` flipped to RESULT/IDLE between the two deaths (auto-advance fired prematurely).
2. The second Death/LevelExit event didn't fire from the detector.
3. `_practice_state_path` got cleared by an interleaving `_on_practice_attempt_result`.

The added logging discriminates between these. Run, read the log, fix the actual cause.

### P0.3 — Phase E: BSV input recording + replay

The single biggest missing piece. Replay is currently a hard 501 under RA backend.

**What's needed:**

- BSV writer: capture libretro deterministic movie format from a live RA session. Probably driven by NCI commands `BSV_RECORD_TOGGLE` and friends, plus filesystem capture of the resulting `.bsv`.
- BSV → events: replay a `.bsv` through RA, watch transitions emit naturally (same code path as live), tag events with `source: "replay"`.
- File format bridge: existing `.spinrec` files (Mesen format) are not BSV; either retain Mesen replay capability or write a one-time converter.

**Estimate:** large. Phase E's spec lives in `docs/superpowers/specs/2026-05-06-retroarch-migration-design.md`. Treat it as a starting point but expect new realities — the live-testing phase has changed our understanding of every other phase substantially.

## P1 — Quality-of-life

### P1.1 — Hot-swap ROM mid-session

User opens dashboard with ROM A, then changes RA's ROM to ROM B. The basename auto-refresh works (next save targets ROM B's slot files), but the dashboard's game context (segment list, scheduler, etc.) is still on ROM A. Detection of the swap and a clean re-init would be a real workflow improvement.

### P1.2 — Skip Mesen-only tests when backend is RetroArch

`tests/integration/test_transitions.py` and `tests/integration/test_replay_fixture.py` connect via `TcpManager`. Under RA backend they fail with `ConnectionError: Not connected`. Either:

- Mark them with a backend-aware skip (`pytest.mark.skipif(backend == 'retroarch')`).
- Or port them to drive the RA orchestrator directly.

The first is easy and unblocks "full pytest is green." The second is more thorough but doubles the integration-test surface.

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

### P2.2 — Remove Mesen-specific code paths once Phase E lands

`TcpManager`, `lua/spinlab.lua`, the `.spinrec` reader/writer, the Lua-aware addresses in `lua/poke_engine.lua`, the dual-backend conditional in `routes/system.py`. Big delete-fest. Don't do this until Phase E is stable AND at least one full speedrun has been completed end-to-end on RA.

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

## Definition of "full parity"

When the following are true, the Mesen backend can be deleted:

1. Reference recording captures inputs (.bsv) AND state files. Both are written to disk at known paths. ✓ states  ✗ inputs
2. Reference run can be saved, finished, resumed, and discarded end-to-end. Field-tested. ✓ except discard 500 (cascade FK fix needs verify)
3. Practice loop runs N segments without intervention, including reload-on-death after every death. ✗ second-death issue
4. Cold-fill captures cold variants for hacks that use cp-respawn. ✗ in-progress
5. Speed-run mode runs a full level start-to-finish with checkpoint splits. Untested.
6. Replay loads a `.bsv` and reproduces transitions identically. Not implemented (Phase E).
7. Full pytest suite green under `backend == "retroarch"` config. ✗ (10 Mesen-specific failures)
8. At least one full real speedrun (e.g. "Love Yourself" any%) completed end-to-end with no manual workaround.

Items 1-5 are P0. Item 6 is Phase E. Items 7-8 are gates; clearing them is the actual ship signal.

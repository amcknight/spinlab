# RetroArch Migration — Findings, Open Decisions, Working Plan

**Date:** 2026-05-06
**Status:**
- Phase 0 complete (Mesen+Lua incompatible with runahead).
- Phase 2 spike complete (RetroArch + NCI viable, including under runahead).
- Phase A complete (Lua surface audit at `docs/retroarch-migration/lua-audit.md`).
- Phase B complete (Python NCIClient: 26 unit tests, end-to-end validated against live RA with runahead = 3). Required cfg gotcha discovered: `cheevos_hardcore_mode_enable = "false"`.
- Phases C-G pending. Phase C plan written at `docs/superpowers/plans/2026-05-06-retroarch-phase-c-poller.md`.

**Architectural direction settled:** rewrite Lua logic in Python; replay via BSV (Phase E, last).
**Branch:** `worktree/retroarch-port`

## Goal

Replace SpinLab's Mesen 2 + Lua emulator integration with **RetroArch + snes9x_libretro core**, controlled from Python over the libretro **Network Command Interface (NCI)**. The motivating change is that runahead — RetroArch's frame-prediction latency reducer — is incompatible with Lua scripting in Mesen, and Andrew needs runahead to make the practice loop feel like real-hardware play.

The migration also removes the in-emulator HUD overlay (already planned); state I/O, transition detection, and rating capture all move to Python and the existing dashboard.

## Background — Why Migrate

### Phase 0 finding: Mesen 2 disables Lua under runahead

Three converging pieces of evidence:

1. **Empirical latency test.** With the Mesen Script Window closed, Toothpaste World's jump latency was 1 frame from button press to visible response. With the Script Window open (any non-trivial Lua loaded), latency jumped to 4–5 frames regardless of the runahead setting (1, 2, or 3 frames).
2. **Persistence test.** A Lua callback that writes a heartbeat file each second froze the moment the Script Window was closed. Closing the window halts execution; runahead operation requires the window closed.
3. **Documentation.** A speedrun.com forum thread states "Mesen disables scripts in runahead mode." Mesen 2 docs do not contradict this. The original 2021 nesdev forum claim that prompted this verification was correct.

Workaround search:
- `--testrunner` is fully headless (no display) — useless for live play.
- No `--script` CLI flag exists for the GUI mode (per Mesen 2 docs and unresolved feature requests #313, #526 in the archived Mesen 1 repo).
- Closing the Script Window stops the script, ruling out a "load and detach" strategy.

Conclusion: There is no way to keep SpinLab's current Lua-based architecture *and* enable runahead. One must go.

### Phase 2 finding: RetroArch + snes9x_libretro + NCI is viable

A 30-minute spike (`scripts/spike_retroarch.py`) validated the foundational assumptions. Five tests, results from runs 2026-05-06:

| Test | Result | Notes |
|------|--------|-------|
| NCI alive (`VERSION`) | PASS | RA 1.22.2 responded to UDP on `127.0.0.1:55355` |
| Memory read | PASS | `READ_CORE_RAM 94 4` returned plausible Mario X bytes |
| 60Hz polling, 30 sec | PASS | 1800 reads / 0 timeouts, mean RTT 11ms, p95 20ms, max 20ms |
| Memory polling under runahead | PASS | 600 samples / 245 distinct X values over 10 sec |
| Save/load round-trip via NCI | INCONCLUSIVE | NCI `SAVE_STATE`/`LOAD_STATE` simulate hotkey presses; in our setup the simulation didn't trigger — but **F2/F4 work manually**, so the underlying functionality is intact and the issue is hotkey routing or empty-slot, not core capability. To diagnose during Phase D. |

Address-mapping note: snes9x_libretro does **not** publish a memory descriptor map, so `READ_CORE_MEMORY` returns `-1 no memory map defined`. The older `READ_CORE_RAM` command works and uses **WRAM-flat addressing** (`$7E0094` → offset `0x0094`). Plan code accordingly.

Latency budget note: an 11ms mean RTT means a single round-trip read fits in a 16.67ms frame. p95 of 20ms means ~5% of reads spill into the next frame. This is acceptable for transition detection (which doesn't need same-frame precision), but argues against blocking the practice loop on a single read; design for batched/pipelined polling.

## Direction

**Migrate to RetroArch + snes9x_libretro + Python NCI. Drop Lua. Rewrite the Lua logic in Python.** Replay (the only piece that genuinely benefits from staying in a Lua-shim shape) moves to **BSV** — RetroArch's native deterministic movie format — which is a clean break from the `.spinrec` + `emu.setInput()` pattern.

Path comparison considered:

- **Shim (preserve Lua):** would let us keep 1.5k lines of `spinlab.lua` running in a standalone interpreter that translates `emu.*` calls to NCI. Tempting as a faster MVP, but breaks down at the replay layer because input injection via NCI alone doesn't work (`WRITE_CORE_RAM` to controller-state addresses gets clobbered by the NMI; see spike log). The shim would have to special-case replay through Network RetroPad or BSV at the shim boundary, awkward and partial.
- **Rewrite (chosen):** port transition detection, state machine, and replay logic into Python modules that talk to NCI directly. Larger upfront work but simpler runtime, single language for application logic, BSV-based replay falls out cleanly.

This is the same conclusion Andrew reached earlier in the session ("if it does work, I think we move to drop all Lua, the longterm solution"). Confirmed after spike testing showed every primitive needed (memory R/W, save/load, pause, runahead coexistence) is reachable from Python via NCI.

## Continued Phase 2 — Tests Still To Run

Before committing to (A) or (B), exhaust the basic-functionality spike:

1. **Savestate via NCI.** Diagnose why `SAVE_STATE` / `LOAD_STATE` simulation didn't trigger when manual F2/F4 do. Likely candidates: hotkey not bound, RA window not focused at command time, empty-slot edge case. Verify by sending the command and watching the savestates directory for file creation.
2. **Input injection.** Determine whether NCI (or an adjacent RA mechanism) can drive controller input from Python. Candidates to evaluate: `WRITE_CORE_RAM` into controller-state addresses (likely overwritten each frame by RA's input driver), RA's "Network Gamepad" / "Remote RetroPad" feature, or BSV playback as the only deterministic path. Don't build the full replay system — just confirm at least one path works.
3. **(Stretch) BSV record/playback.** Quick smoke: can NCI start/stop a BSV recording, and does playback work? Do not build SpinLab's replay layer here — just enough to know BSV is usable.

These run in this same `worktree/retroarch-port` branch, extending [`scripts/spike_retroarch.py`](../../../scripts/spike_retroarch.py) or as standalone probes. Outcomes get folded back into this doc before the (A) vs (B) decision is locked.

## Scope

### In

1. Python NCI client wrapping the commands SpinLab needs (`READ_CORE_RAM`, `WRITE_CORE_RAM`, `SAVE_STATE`, `LOAD_STATE`, `SAVE_STATE_SLOT`/`LOAD_STATE_SLOT`, `PAUSE_TOGGLE`, `FRAMEADVANCE`, `VERSION`).
2. Python-side memory polling and transition detection replacing the Lua frame-callback driver.
3. Python-side savestate slot management — file copy/move on top of RA's slot files.
4. BSV-based replay record/playback replacing the `.spinrec` + `emu.setInput()` Lua mechanism. Existing `.spinrec` files become obsolete or get a one-time converter.
5. Launch scripts: replace `launch.sh`/`launch.bat` with a RetroArch launcher (or document running RA manually + dashboard).
6. README and `docs/ARCHITECTURE.md` updates reflecting the new architecture.
7. Removal of `lua/spinlab.lua` and `lua/poke_engine.lua` from the codebase (preserved in git history).
8. Test rewrites: `tests/integration/` currently spawns Mesen via `--testRunner`. Replace with RetroArch-based equivalents.

### Out (deferred to future specs)

- Cross-emulator portability (a "neutral layer" to support multiple cores). The original plan flirted with this; defer until there's a second consumer. Keep snes9x-specific code in a single adapter module so a future port is mechanical.
- Mesen-as-fallback. Once the migration lands we commit fully. No conditional code.
- Dashboard HUD overlay rework — already planned independently; not part of this migration.
- Other libretro cores (bsnes, etc.).

## Refined Phase Plan

The plan replaces the original Phases 1–6 with a more concrete sequence given what the spike taught us. **Replay (BSV) is intentionally last** — Andrew's preference, and the right call: live practice is the hot path, and getting it working end-to-end before touching replay de-risks the migration.

Each phase produces a working tree that the dashboard can be exercised against (with the existing or replacement subsystem). Per-phase implementation plans live at `docs/superpowers/plans/2026-05-XX-<phase-name>.md` and are written just-in-time before each phase starts (per the project's standard cleanup-pass pattern).

### Execution order

1. **Phase A — Audit** Lua surface area
2. **Phase B — Python NCI client** (foundation)
3. **Phase C — Memory polling + transition detection** (replaces Lua's main job)
4. **Phase D — Savestate I/O via NCI + filesystem**
5. **Phase F-live — Setup, launch, config** (just enough to get live practice end-to-end against RA)
6. **🛑 Live-practice end-to-end smoke** — Andrew exercises the dashboard practice loop against RetroArch (with runahead). If it works, the migration's hot path is done.
7. **Phase E — Replay via BSV** (the deferred subsystem)
8. **Phase G — Drop Lua, drop Mesen, final docs sweep**

The original Phase F (full setup/docs polish) is split: the minimum needed for live practice happens at step 5, the complete README/ARCHITECTURE rewrite happens at step 8 alongside the Lua/Mesen removal.

### Phase A — Audit Lua surface area

Goal: a frozen artifact (markdown table) listing every `emu.*` call in `lua/spinlab.lua` and `lua/poke_engine.lua`, categorized by:

- Memory reads / writes (and whether bank `$7E` or registers/PPU)
- Save / load state
- Input reads (player buttons) — currently used for rating capture
- Input writes (`emu.setInput`) — currently used for replay
- Drawing calls (HUD — to be deleted, not ported)
- Memory callbacks (`addMemoryCallback`)
- Event callbacks (NMI, frame, etc.)
- `getState()` / register access

Deliverable: `docs/retroarch-migration/lua-audit.md` (or similar). This artifact informs Phases B–E. Without it, we'll discover surprises mid-migration.

### Phase B — Python NCI client

Goal: a minimal, tested Python module that wraps NCI as a synchronous client.

Module: `python/spinlab/retroarch/nci.py`. Tests: `tests/retroarch/test_nci.py`.

Public surface:
- `class NCIClient` with methods `version()`, `read_ram(addr, length)`, `write_ram(addr, bytes)`, `save_state()`, `load_state()`, `pause_toggle()`, `frame_advance()`.
- Sync UDP transport with timeouts and retries; configurable host/port; per-call instrumentation hooks.
- Response parsing that explicitly ignores the echoed command name and address (the spike script's bug — see [`scripts/spike_retroarch.py`](../../../scripts/spike_retroarch.py:55-72)).

This phase has no integration with the rest of SpinLab. The dashboard still uses the Lua TCP path during Phase B.

### Phase C — Memory polling and transition detection

Goal: Python-side polling loop that replaces `spinlab.lua`'s endFrame callback for transition detection.

Module: `python/spinlab/retroarch/poller.py`. Subscribes to the NCI client, runs an asyncio loop at ~60Hz, computes the same transition events the Lua emits today (level-entrance, level-exit, key-grab, etc.), feeds them into the existing `session_manager` event pipeline.

Address constants live in `python/spinlab/retroarch/addresses.py` — single source of truth replacing the three current address maps (`spinlab.lua` lines 43–53, `poke_engine.lua` ADDR_MAP, `tests/integration/addresses.py` ADDR_MAP).

This is the largest single phase. End state: the dashboard practice loop works against RetroArch with the existing Lua-side state I/O still in place. (One subsystem at a time — overlapping migrations across two emulator backends is more pain than it's worth.)

Open question: poll cadence. The spike showed 11ms mean RTT but 20ms p95. Naive 60Hz polling ate ~33% of the read budget; pipelined or batched reads can cut that. Determine empirically during Phase C whether a single-shot 60Hz read is sufficient, or we need to issue multiple addresses in one packet.

### Phase D — Savestate I/O via NCI + filesystem

Goal: Python-side save/load replacing the Lua save/load.

Module: `python/spinlab/retroarch/state_io.py`. Triggers NCI `SAVE_STATE` / `LOAD_STATE`, then manages slot files on disk (RA writes them to `<savestate_directory>/<game>.state<slot>`).

First task: diagnose why the spike's NCI `SAVE_STATE` didn't trigger when manual F2 does. Likely candidates: hotkey not bound on this RA install, or empty-slot. Document and fix, then build on top.

Slot management: spinlab needs to capture multiple states per session (one per segment boundary). Strategy: assign one rotating slot for "the current capture target," save into it via NCI, then immediately copy the slot file to a deterministic filename keyed by segment ID. Loading reverses this.

### Phase E — Replay via BSV

Goal: replace `.spinrec` + `emu.setInput()` with RetroArch's native BSV (Bsnes Movie) format.

Module: `python/spinlab/retroarch/bsv.py`. Drives RA into BSV record mode at the start of a reference run, finalizes the BSV file at the end. Replay drives RA into BSV playback mode anchored to a savestate.

`.spinrec` files from the Mesen era become obsolete. Either provide a one-time converter (low priority — Andrew has acknowledged re-recording is acceptable) or just discard them; document the break in `docs/ARCHITECTURE.md`.

vJoy is **not** in scope. The original plan considered vJoy as an input-injection layer; BSV makes it unnecessary, since libretro reads BSV inputs directly without going through the controller driver.

### Phase F — Setup, launch, README, ARCHITECTURE

Goal: project documentation reflects the new architecture, launch scripts work, config schema updated.

Replace `scripts/launch.sh` and `scripts/launch.bat` with RetroArch launchers (or document RA-then-dashboard workflow).

Update `config.yaml` schema:
- `emulator.path` → `emulator.retroarch_path`
- `emulator.lua_script` removed
- `emulator.script_data_dir` → `emulator.savestate_dir` (or similar)
- `network.port` (Lua TCP) removed; replaced by `network.nci_port` (default `55355`).

Update README and `docs/ARCHITECTURE.md` end-to-end.

### Phase G — Drop Lua, drop Mesen

Goal: delete `lua/`, drop Mesen from documentation, remove any Mesen-specific code paths still lingering. Final sweep.

After this phase, `git grep -i mesen` returns nothing relevant (history only).

## Open Questions

1. **Slot management strategy.** Andrew uses manual sequential save states (auto-index) and wants that flow preserved. SpinLab's automated saves shouldn't pollute the user's slot history. Options for Phase D:
   - SpinLab uses a reserved high slot range (e.g. 9000+) — needs `SAVE_STATE_SLOT N` to exist (untested; documented `LOAD_STATE_SLOT N` exists but save-equivalent unconfirmed).
   - SpinLab navigates via `STATE_SLOT_PLUS/MINUS` then `SAVE_STATE`, then navigates back — disturbs user briefly.
   - SpinLab does its own state-file management: `SAVE_STATE` then immediately copies the resulting file to a SpinLab-keyed path under a separate directory. Loads reverse: copy back, then `LOAD_STATE_SLOT N`. Bumps user's slot counter by 1 per capture but keeps files cleanly separated.
   The third option is the leading candidate — simplest from Python's side and respects user's slot counter as theirs.
2. **Polling architecture.** Single sync read per frame, or batched/pipelined? Decide during Phase C with timing measurements. Spike showed 11ms mean RTT, 20ms p95 — fine for a single read per frame, but batching multiple addresses into one packet is cheap insurance.
3. **Pause primitive coordination.** Spike encountered a "deep pause" state where `PAUSE_TOGGLE` and `MENU_TOGGLE` could not unstick the emulator core (NCI service still responsive, but no frames advancing). Phase B's NCI client must avoid issuing pause toggles blindly — always read `GET_STATUS` or a memory frame counter first, and never rely on toggle-from-unknown-state. See spike log entry 2026-05-06 for reproduction.
4. **BSV anchor strategy.** Does BSV record/playback need to start from power-on, or can it anchor to a savestate? Affects whether reference runs need full power-on starts. Investigate during Phase E.
5. **Replay-fixture test.** The current `Love Yourself.smc` replay-fixture test (per `docs/superpowers/specs/2026-04-11-replay-fixture-design.md`) currently uses Mesen `--testRunner`. Replace with a RetroArch-based equivalent during Phase G; until then, gate the test on emulator availability.
6. **Address-map source-of-truth migration.** Today there are three address maps (Lua + poke_engine.lua + tests). Phase C consolidates to one in Python. Confirm during Phase A whether `poke_engine.lua` (test-only) survives in some form or gets replaced by NCI `WRITE_CORE_RAM` for tests too.

## Acceptance Criteria

The migration is "done" when:

- All Mesen and Lua references are removed from active code paths (history is fine).
- `python -m pytest` passes against the RetroArch + snes9x_libretro setup, including emulator-marked tests.
- The dashboard practice loop works end-to-end against RetroArch with runahead enabled, on Andrew's Windows machine.
- Latency improvement is measurable and matches what the manual frame-advance test predicted (1-frame jump response with runahead).
- README and ARCHITECTURE describe the new system without "migration in progress" caveats.

## References

- Spike validation script: [`scripts/spike_retroarch.py`](../../../scripts/spike_retroarch.py)
- RetroArch NCI docs: https://docs.libretro.com/development/retroarch/network-control-interface/
- snes9x_libretro core: installed via RetroArch Online Updater
- Original 6-phase strategy: lives in this conversation's history (not separately checked in)

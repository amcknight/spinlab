# Phase A — Lua Surface Area Audit

**Date:** 2026-05-06
**Scope:** every `emu.*` call across `lua/` plus the Lua↔Python TCP protocol, used as the implementation checklist for Phases B–E of the RetroArch migration.

## Files in scope

| File | Lines | Role | Migration disposition |
|------|-------|------|-----------------------|
| `lua/spinlab.lua` | 1347 | Main script: TCP server, transition detection, practice/speed-run state machines, replay/recording, save/load, HUD calls. | Replace logic in Python. |
| `lua/poke_engine.lua` | 182 | Test harness — drives memory pokes for emulator-marked tests via `--testRunner`. | Replace with NCI `WRITE_CORE_RAM` calls in Python tests. |
| `lua/addresses.lua` | 23 | SNES memory address constants (kaizosplits-derived). | Port to `python/spinlab/retroarch/addresses.py` as the single source of truth. |
| `lua/json.lua` | (not read) | Custom JSON helpers (`json_get_str/num/arr`). | Delete — Python stdlib `json` covers this. |
| `lua/overlay.lua` | (not read) | HUD drawing (`draw_practice_overlay`, `draw_speed_run_overlay`). | **Delete entirely** — HUD lives in the dashboard, not in-emulator. |
| `lua/spinrec.lua` | 80 | `.spinrec` binary I/O + SNES-joypad bitmask encoding. | Replaced by RA's BSV format in Phase E. |
| `lua/NewScript.lua` | (not read, named like a stub) | Inspect during cleanup; likely dead. | Likely delete. |

## `emu.*` call inventory

### Memory I/O

| Call | Where | Purpose | NCI equivalent |
|------|-------|---------|----------------|
| `emu.read(addr, emu.memType.snesMemory, false)` | `spinlab.lua:357-367, 403, 652-653` | Read 1 byte of SNES memory (uses bus addressing) | `READ_CORE_RAM <addr> 1` (WRAM-flat addressing) |
| `emu.readWord(d.address, SNES, false)` | `spinlab.lua:405` | Read 2 bytes (conditions API) | `READ_CORE_RAM <addr> 2` |
| `emu.write(addr, value, SNES)` | `poke_engine.lua:68, 155` | Write 1 byte (test harness only) | `WRITE_CORE_RAM <addr> <hex>` |

**Address translation note.** Mesen's `emu.read(addr, snesMemory)` uses **SNES bus** addresses; NCI's `READ_CORE_RAM` uses **WRAM-flat** addresses. For most of the fixed addresses in `addresses.lua` (e.g. `ADDR_GAME_MODE = 0x0100`), bank `$00` low-page is a WRAM mirror so the same numeric address works in both. **Exception flagged:** `ADDR_CP_ENTRANCE = 0x1B403`. In Mesen's snesMemory that's bus address `$00:B403` (likely ASM-patch ROM/IO area in a kaizo hack); in WRAM-flat it would be byte `111619` (`$7F:B403`). These are **not the same memory**. Verify during Phase B/C against the live game what's actually being read — and update either the address constant or the read mechanism (e.g. `READ_CORE_MEMORY` with bus address — which currently fails on snes9x because of "no memory map defined", but might be addressable via other means).

### Save / load state

| Call | Where | Purpose | NCI equivalent |
|------|-------|---------|----------------|
| `emu.createSavestate()` | `spinlab.lua:242` | Returns binary blob in memory | None directly. Strategy: NCI `SAVE_STATE` writes a slot file, Python copies that file to a SpinLab-managed path. |
| `emu.loadSavestate(data)` | `spinlab.lua:270` | Restores from binary blob | NCI `LOAD_STATE_SLOT N`. Python first copies the SpinLab-managed file into RA's slot path, then issues the load. |
| `emu.reset()` | `spinlab.lua:1259` | Hard-reset SNES | NCI `RESET`. |

**Architectural change.** Today: SpinLab serializes savestates as `.mss` files in its own directory and reads/writes them through Lua. Post-migration: SpinLab still owns `.state` files, but the path is RA's slot file (e.g. `Toothpaste.state9999`) shuffled in/out as needed. Spike log entry 2026-05-06 documents the auto-index behavior to design around.

### Input I/O

| Call | Where | Purpose | NCI equivalent |
|------|-------|---------|----------------|
| `emu.getInput(0)` | `spinlab.lua:1218, 1272` | Read controller 1 state (table with `b`/`y`/`a`/`start`/...) | **None.** NCI does not expose controller input. Workarounds: (1) read WRAM `$7E0015`-`$7E001D` where SMW post-processes input; (2) BSV recording captures inputs implicitly. |
| `emu.setInput(tbl)` | `spinlab.lua:1276` | Inject controller state for one frame (replay) | **None directly.** Phase E uses BSV playback. |

**The hardest two ports.** Input read is needed for (a) the L+Select invalidate-combo and (b) `.spinrec` recording. Input write is replay only. See "Tricky patterns" below.

### Callbacks

| Call | Where | Purpose | NCI equivalent |
|------|-------|---------|----------------|
| `emu.addEventCallback(fn, emu.eventType.startFrame)` | `spinlab.lua:1345`, `poke_engine.lua:171` | Run `fn` at start of every frame | Python polling loop running at ~60Hz against NCI. |
| `emu.addEventCallback(fn, emu.eventType.inputPolled)` | `spinlab.lua:1346` | Run `fn` after RA polls inputs (frame-perfect input capture/inject) | **No equivalent.** Replaced by BSV (recording = native RA movie record; playback = native RA movie play). |
| `emu.addMemoryCallback(fn, emu.callbackType.exec, 0x0000, 0xFFFF)` | `spinlab.lua:1304` | Fires inside CPU exec — required spot for save/load to actually take effect | **Not needed.** NCI save/load fires from any context, no callback gating. |

### Misc

| Call | Where | Purpose | NCI / Python equivalent |
|------|-------|---------|-------------------------|
| `emu.getRomInfo()` | `spinlab.lua:44` | ROM filename | NCI `GET_STATUS` returns `<system>,<game>,<crc>` — same info, different format. |
| `emu.getScriptDataFolder()` | `spinlab.lua:37` | Mesen's per-script data dir | Replace with `config.yaml`-driven path. |
| `emu.log(msg)` | many | Lua-side logging into Mesen log window | Replace with Python `logging`. |
| `emu.isKeyPressed("T"/"Y")` | `spinlab.lua:1204` | Keyboard testing shortcuts (manual save/load to a test slot). Currently `pcall`-guarded because crashes in `--testRunner`. | Drop, or move to dashboard hotkeys / debug endpoint. |
| `emu.setSpeed(speed)` | `spinlab.lua:899-901, 1025-1027, 1039-1040, 1284-1285` | Replay speed override (and restore) | NCI `FAST_FORWARD_HOLD` is closest; arbitrary-speed not directly available. RA has cfg-side `slowmotion_ratio`/`fastforward_ratio`. Phase E concern. |
| `emu.stop(0)` | `poke_engine.lua:123` | Quit emulator (testRunner only) | NCI `QUIT`. |

### Constants used

`emu.memType.snesMemory`, `emu.callbackType.exec`, `emu.eventType.startFrame`, `emu.eventType.inputPolled`. All become Python module constants or are simply not needed.

## Lua ↔ Python TCP protocol catalog

The current architecture has Lua running a TCP server on port `15482` and Python connecting to it. Post-migration this is **inverted** — RetroArch is the daemon (UDP NCI on `55355`), Python is the orchestrator. The TCP server in Lua and `tcp_manager.py` in Python both go away; replaced by Python directly issuing NCI commands and polling.

### Python → Lua commands (today)

| Command | Payload | Lua handler | Post-migration owner |
|---------|---------|-------------|----------------------|
| `game_context` | `{game_id, game_name}` | sets `game_id`, ensures state dir | Python (config / startup) |
| `reference_start` | `{path}` | starts input recording into buffer | Python — drives BSV record via NCI |
| `reference_stop` | `{}` | flushes buffer to `.spinrec`, sends `rec_saved` | Python — stops BSV record |
| `replay` | `{path, speed, prev_speed}` | starts injecting recorded inputs frame-by-frame | Python — drives BSV playback via NCI |
| `replay_stop` | `{}` | aborts replay | Python — stops BSV playback |
| `practice_load` | `{segment{...}}` | queues state load + enters PSTATE_LOADING | Python `practice.py` already owns the orchestration; NCI `LOAD_STATE_SLOT` replaces queued load |
| `practice_stop` | `{}` | resets practice state | Python |
| `speed_run_load` | `{segment{...}}` | enters speed-run mode | Python |
| `speed_run_stop` | `{}` | resets speed-run state | Python |
| `fill_gap_load` | `{state_path}` | loads state, watches for next death/spawn | Python |
| `cold_fill_load` | `{state_path, segment_id}` | as above with explicit segment | Python |
| `set_conditions` | `{definitions[]}` | dynamic per-event memory probe registry | Python (read directly via NCI) |
| `set_invalidate_combo` | `{combo[]}` | overrides L+Select default | Dashboard — moved out of emulator entirely |
| `reset` | `{}` | drains pending I/O, queues `emu.reset()` | Python — `RESET` over NCI |
| `poke_scenario` (test) | `{settle_frames, pokes[]}` | poke harness for integration tests | Python tests use `WRITE_CORE_RAM` directly |
| `quit` (test) | `{}` | stops emulator | Python tests use NCI `QUIT` |

### Lua → Python events (today)

These move from "Lua emits over TCP" to "Python computes from polled memory":

| Event | Trigger (current) | Post-migration source |
|-------|-------------------|-----------------------|
| `rom_info` | TCP connect | NCI `GET_STATUS` at session start |
| `heartbeat` | every 60 frames | Python keepalive — see "Tricky patterns: pause sync" |
| `level_entrance` | `level_start` 0→1 in `detect_entrance` | Python polling loop computes from same edge |
| `death` | `player_anim` 0→9 | same |
| `level_exit` | `exit_mode` 0→non-zero | same |
| `checkpoint` | midway/cp_entrance edges in `check_checkpoint_hit` | same |
| `spawn` | level_start respawn / fast retry edge | same |
| `attempt_invalidated` | L+Select rising edge | dashboard button (not emulator) |
| `attempt_result` | end of practice timer | Python `practice.py` already computes most of this |
| `speed_run_*` | speed-run state transitions | Python `speed_run.py` already exists |
| `rec_saved` / `replay_*` | recording/replay lifecycle | Python BSV adapter |
| `error` / `ok:*` | various ack | not applicable (Python has its own error paths) |

## Tricky patterns (need careful porting)

### 1. `inputPolled` callback — the big one

Lua fires `on_input_polled` after RA polls the controller, every frame. Two consumers:

- **Recording** (`spinlab.lua:1265-1294`): captures `emu.getInput(0)` into `recording.buffer` as a `uint16` bitmask, frame-indexed.
- **Replay** (`spinlab.lua:1275-1294`): consumes `replay.frames[index]` and calls `emu.setInput(...)` to inject for that frame.

NCI has no equivalent callback. Implications:

- **Recording → BSV.** RA's BSV (Bsnes Movie) format records inputs natively and deterministically. Drive `BSV_MOVIE_TOGGLE` (or equivalent NCI command — verify name) at the start/end of a reference run. Output is `.bsv` (file format we don't control), so `.spinrec` becomes obsolete; `lua/spinrec.lua` becomes obsolete.
- **Replay → BSV playback.** Same mechanism in reverse.
- **Open question:** can BSV anchor to a savestate, or must it start from power-on? Affects whether reference runs need full power-on starts. **Defer to Phase E.**

### 2. Input read for invalidate-combo

`combo_pressed()` reads `emu.getInput(0)` directly, every frame, looking for a button combination. With NCI, no input read.

**Solution:** the dashboard already has the user's interaction context. Move the invalidate trigger to a dashboard button or hotkey. The "press L+Select to invalidate the current attempt" UX becomes "click Invalidate in the dashboard." Andrew's call whether that's acceptable.

**Alternative:** if in-game button is desired, poll the SMW WRAM addresses where the game has already read+stored the controller state (`$7E0015`/`$7E0017`). Polling at 60Hz catches edge transitions reliably, with the same caveat as transition detection.

### 3. cpuExec-deferred save/load

Mesen requires save/load to be initiated from inside a CPU exec callback (`spinlab.lua:1242-1263`). SpinLab queues paths into `pending_saves`/`pending_loads` from the frame callback, and the cpuExec callback drains them. NCI has no such constraint — `SAVE_STATE`/`LOAD_STATE_SLOT` can fire any time.

**Disposition:** simplifies in port. Drop the deferred-queue pattern entirely.

### 4. `state_just_loaded` re-sync

After a save-state load, memory is wholesale replaced (`spinlab.lua:1319-1322`). The frame-callback re-reads `prev = read_mem()` to suppress phantom edge transitions on the next frame. **Port this pattern verbatim** in Python's polling loop — same logic applies (after `LOAD_STATE_SLOT`, re-poll fresh memory before computing diffs).

### 5. Pause sync / "deep pause" gotcha

Documented in `docs/retroarch-migration/spike-log.md` 2026-05-06: NCI service can stay responsive while RA's core thread freezes (memory reads succeed, `PAUSE_TOGGLE`/`MENU_TOGGLE` don't recover). Phase B's NCI client must:

- Never blindly toggle pause based on assumed state.
- Use `GET_STATUS` and/or a memory-tick check (e.g., poll a known-changing address like a frame counter) to confirm "actually running" before commands that require running state (`SAVE_STATE`, `RESET`).
- Surface "core stuck" as a distinct error so the dashboard can prompt the user.

### 6. Dynamic conditions API

`set_conditions` lets the dashboard register arbitrary `(name, address, size)` tuples that Lua reads at every event. Currently used for things like "log this address with each level_entrance event for diagnostics." Trivial to port: Python polling loop reads the same address set.

### 7. The poke engine

`poke_engine.lua` runs as the entry script in `--testRunner` mode and `dofile`-loads `spinlab.lua`. It registers its `startFrame` callback **first** so pokes happen before `spinlab.lua`'s `read_mem`. Python tests post-migration: write memory via `WRITE_CORE_RAM`, then poll memory (`READ_CORE_RAM`), all in the same Python process. The whole "two-script ordering" pattern dissolves.

## Per-phase impact summary

| Phase | Drivers from this audit |
|-------|-------------------------|
| **A — Audit** | This document. |
| **B — Python NCI client** | Cover `READ_CORE_RAM`, `WRITE_CORE_RAM`, `SAVE_STATE`, `LOAD_STATE_SLOT`, `RESET`, `PAUSE_TOGGLE`, `FRAMEADVANCE`, `VERSION`, `GET_STATUS`, `QUIT`. Build the "running-state probe" helper to dodge gotcha #5. |
| **C — Memory polling + transition detection** | Port `read_mem`, `detect_transitions`, `detect_finish`, `check_checkpoint_hit`, `is_death_frame`, `is_exit_frame`, `goal_type`. Port the `state_just_loaded` re-sync pattern. Port the cold-fill state machine. |
| **D — Savestate I/O** | Replace `save_state_to_file`/`load_state_from_file` + `pending_saves`/`pending_loads` with NCI `SAVE_STATE` + filesystem shuffle into a SpinLab-managed slot range. Drop the cpuExec deferral pattern. |
| **E — Replay (BSV)** | Replace `recording`/`replay` modules with BSV record/playback adapter. `lua/spinrec.lua` and `.spinrec` files become obsolete. `emu.setInput`/`getInput` consumers route through BSV. |
| **F-live** | Setup scripts, config schema, NCI port in YAML, RetroArch launch path. |
| **G** | Delete `lua/` entirely; remove TCP-server/`tcp_manager.py`; final README/ARCHITECTURE rewrite. |

## Open questions surfaced by the audit

1. **`ADDR_CP_ENTRANCE = 0x1B403` semantics.** Is this WRAM (would be `$7F:B403` in the SNES bus, accessed via `READ_CORE_RAM 1B403`) or ROM/IO at `$00:B403` (currently snesMemory in Mesen)? Verify against the live game during Phase B once RA is responsive again. May need either a different read mechanism or a corrected address.
2. **Invalidate-combo UX.** Move L+Select trigger to dashboard button, or implement WRAM-poll fallback? Andrew's preference.
3. **`emu.setSpeed` for replay.** Does RA's fast-forward / slow-motion ratio cover the speed cases SpinLab needs (`SPEED_UNCAPPED` / `100`)? Phase E.
4. **BSV anchor strategy** (deferred from spec).
5. **Conditions API: at-event vs continuous polling.** Currently `read_conditions()` runs *only* at event emission. Post-migration we'd be polling all the time anyway. Decide whether to keep the "event-time snapshot" semantic or stream all condition values continuously to the dashboard. Probably the former — keeps semantics identical.

## Phase C followups for Phase D / F

Captured during Phase C closeout review. None block Phase D, but each has a natural home in a later phase:

1. **`Spawn` event needs `segment_id` for cold-fill.** Lua's `handle_cold_fill` (lines 671, 678) emits `segment_id` on the spawn event so the dashboard knows which segment the cold capture belongs to. The Python `ColdFillTracker` accepts `segment_id` on `activate()` but doesn't put it on the emitted `Spawn`. Fix in Phase D: add `segment_id: str = ""` to `Spawn`, populate from `_segment_id` in `cold_fill.py`.
2. **`state_path` computation deferred.** `LevelEntrance.state_path`, `Checkpoint.state_path`, `Spawn.state_path` are populated by the detector to `""`. Phase D's `state_io` module owns path generation; the poller (or a thin adapter) should fill these before forwarding events.
3. **`Poller.run()` swallows all exceptions silently** (`poller.py:65-70`). Bare-bones acceptable for Phase C unit tests; before Phase F wiring, add a logger hook (or narrow the catch to `NCIError` subclasses) so a recurring NCI fault is visible.
4. **`TransitionState.last_event_key` is dead state.** Defined and reset, never written. The Lua source has the same dead field. Either delete from both or document a future use (event-debounce keying). Phase F.
5. **Cold-fill activation through poller has no integration test.** `Poller.activate_cold_fill()` is plumbed but only `_FakeClient`-based snapshot-sequence tests would prove it works end-to-end. Add when Phase F integration tests land.
6. **`_FakeClient.read_calls` in `tests/unit/retroarch/test_poller.py:15` is unused.** Trivial cleanup or assert against it.
7. **`addresses.py` ↔ `tests/integration/addresses.py` ADDR_MAP duplication.** Phase C added the Python source-of-truth but the integration shim that parses `lua/addresses.lua` still exists. Phase G (Lua removal) consolidates.
8. **`ADDR_CP_ENTRANCE = 0x1B403` partially verified (2026-05-07).** Pre-flight live probe (`scripts/probe_cp_entrance.py`) against a hack without ASM checkpoint patches showed cp_entrance constant at 0x15 across 534 polls — predicate-safe (no false positives). **Not yet verified** against a hack with ASM cp patches; if SpinLab is later used with such a hack, this address may need re-investigation (likely `READ_CORE_MEMORY` with a SNES bus address, which currently fails on snes9x_libretro with "no memory map defined"). Defer until a user actually needs it.

## Lines of code displaced

Rough numbers — useful for sizing Phase B–G work:

- `spinlab.lua` (1347 LOC): ~80% becomes Python (transition detection, state machines, save/load, conditions, lifecycle). ~10% deletes (HUD calls, JSONL logger, keyboard shortcuts). ~10% replaced (TCP server → NCI client; recording/replay → BSV).
- `poke_engine.lua` (182 LOC): becomes ~40 LOC of Python test helpers using `WRITE_CORE_RAM`.
- `lua/addresses.lua` (23 LOC): direct port to `python/spinlab/retroarch/addresses.py`.
- `lua/json.lua`, `lua/overlay.lua`, `lua/spinrec.lua`, `lua/NewScript.lua`: deleted entirely.

Total Lua headed for retirement: ~1700 LOC. Total Python added (rough estimate): ~1200–1500 LOC including tests.

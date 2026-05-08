# RetroArch Backend — Architecture

How the RA backend differs from the Mesen+Lua backend, and where to look for what.

The Mesen backend is described in `docs/ARCHITECTURE.md`. This doc covers only the parts that change when `config.emulator.backend == "retroarch"`.

## Two backends, one dashboard

Selected at startup from `config.emulator.backend`:

- `mesen` — historical default. `lua/spinlab.lua` runs inside Mesen2; Python connects to the Lua TCP server on port 15482. All transition detection, save-state I/O, and the `.spinrec` recorder/replayer live in Lua.
- `retroarch` — RA runs the libretro core (snes9x_libretro). Python talks NCI (UDP 55355) directly to RetroArch and owns transition detection itself. No Lua involved; RA is just the emulator process.

The dashboard, session manager, capture controllers, scheduler, allocators, and DB layer are backend-agnostic. The only seam is `TcpManager` (Mesen) vs `RetroArchOrchestrator` (RA), both of which expose the same duck-typed surface.

## The orchestrator surface

`RetroArchOrchestrator` (`python/spinlab/retroarch/orchestrator.py`) implements the same public methods `TcpManager` exposes:

- `is_connected: bool`
- `events: asyncio.Queue[dict]`
- `on_disconnect: Callable | None`
- `connect(timeout) -> bool`
- `disconnect()`
- `send_command(cmd)`
- `recv_event(timeout) -> dict | None`

This is intentional. The session manager and capture controllers don't know which backend they're talking to. Tests substitute fakes against the same surface.

`build_orchestrator(config)` is the factory; it wires up `NCIClient`, `StateIO`, `Poller`, `ConditionRegistry`, `PracticeTiming`, and `SpeedRunTiming` and returns a fully assembled instance.

## Module map

```
python/spinlab/retroarch/
├── nci.py              UDP transport. Synchronous client; one in-flight command at a time.
├── snapshot.py         MemorySnapshot dataclass + read_snapshot(NCIClient) helper.
├── addresses.py        SMW WRAM offsets (single source of truth for the RA backend).
├── predicates.py       Pure detection functions: is_death_frame, is_exit_frame, goal_type, check_checkpoint_hit.
├── transition_state.py Mutable per-segment state (died_flag, cp_ordinal, first_cp_entrance).
├── detector.py         TransitionDetector. step(snap, ts) -> list of protocol events.
├── cold_fill.py        ColdFillSpawnDetector. step(snap, ts) -> SpawnEvent | None. Activated externally.
├── poller.py           Async loop. Owns TransitionDetector + ColdFillSpawnDetector. ~60 Hz.
├── state_io.py         SAVE_STATE/LOAD_STATE_SLOT + filesystem shuffle. Sync (call via to_thread).
├── timing.py           PracticeTiming + SpeedRunTiming state machines.
├── exceptions.py       NCIError hierarchy.
└── orchestrator.py     RetroArchOrchestrator — TcpManager-shaped façade.

Both backends share the canonical event vocabulary in
`spinlab.protocol` (LevelEntranceEvent, CheckpointEvent, …). The detector
constructs them directly; the orchestrator serializes via
`dataclasses.asdict` for the SSE / route_event dict consumers. Conditions
likewise share `spinlab.condition_registry.ConditionRegistry`.
```

## Event flow

1. `Poller.run()` calls `read_snapshot(client)` → `MemorySnapshot`.
2. If `mark_state_loaded()` was called (after a save-state load), the next snapshot is fed to `TransitionDetector.resync_after_state_load(snap)` — replaces `prev` and clears `died_flag` / `cp_acquired` / `exit_this_frame`. The frame is otherwise skipped to avoid phantom edges.
3. `TransitionDetector.step(snap, ts)` returns 0..N protocol events. The poller stamps `state_path` (via `StateIO.resolve_event_path`) and `conditions` (via `ConditionRegistry.read_all`), then calls `on_event(ev)`.
4. `ColdFillSpawnDetector.step(snap, ts)` runs after the detector. Returns at most one `SpawnEvent` per activation; deactivates after emitting.
5. `RetroArchOrchestrator.on_poller_event(ev)` is the registered callback. It:
   - Converts `ev` to a JSON dict via `dataclasses.asdict` (the protocol classes carry the discriminator `event` field).
   - Feeds the dict to `PracticeTiming.observe_event` and `SpeedRunTiming.observe_event`.
   - Enqueues to `events`.
6. The dashboard's event loop reads from `events` via `recv_event` and calls `session_manager.route_event(d)`.

**Save-on-event and reload-on-death live in the application layer**, not the orchestrator. Capture controllers and `PracticeSession` call `EmuBackend.save_state(seg_id)` / `EmuBackend.load_state(path)` directly:
- `ReferenceController.handle_entrance` / `handle_checkpoint` save when `is_recording`.
- `ColdFillController.handle_spawn` saves the cold variant.
- `PracticeSession.handle_death` / `handle_level_exit_abort` reload the segment's start state on Death and pit-fall events.

Under RA the orchestrator's `save_state` / `load_state` wrap `StateIO` in worker threads; under Mesen they're no-ops because Lua handles state I/O autonomously.

## Save-state I/O

RA's NCI `SAVE_STATE` writes to whatever slot RA's auto-index counter is on — not a slot we control. So we don't watch a fixed file; we snapshot `<savestate_dir>/<game>.state*` mtimes before the save, fire the command, then look for whichever file got created or had its mtime advance, and move that to the SpinLab path.

Loads are the reverse: copy the SpinLab file to a reserved slot path (default 9999) and fire `LOAD_STATE_SLOT 9999`.

Three correctness gotchas, all enforced by `state_io.py`:

- **Game basename must match the loaded ROM exactly.** RA writes `<basename>.state<slot>`. If the basename is wrong, mtime polling watches files that never get written and times out. Solution: orchestrator updates basename from `GET_STATUS` at connect time and refreshes before every save.
- **Move can race RA's open file handle on Windows.** `shutil.move` raises `PermissionError` if RA still has the slot file mapped. Five retries × 100ms; falls back to a copy if all fail (segment file lands; source leaks for the next sweep).
- **Save command sometimes silently no-ops.** During level loads / fades. Three attempts × 1s timeout each.

## Detection differences vs Lua

Most of the detection logic is a direct port of `lua/spinlab.lua`'s `detect_transitions`. Three places where the Python version is more permissive:

- **Cold-fill death detection.** Lua only watches `player_anim` 0→9 (sprite hit). Python ALSO accepts `exit_mode` 0→non-zero with no goal flag (pit-falls / death-falls), AND `exit_mode` non-zero→0 while `level_start=1` (cp-respawn hacks). Each path has a regression test in `tests/unit/retroarch/test_cold_fill.py`.
- **Practice reload-on-death.** Same expansion: Lua only fires on the sprite-anim death frame; Python fires on Death OR LevelExit(goal='abort').
- **Resync after state-load.** Lua's `state_just_loaded` only updates the prev snapshot. Python ALSO clears `died_flag`, `cp_acquired`, and `exit_this_frame`. Without this, practice-mode reload-on-death stuck `died_flag=True` after the first death and suppressed every subsequent Death event for the rest of the session.

## Required RetroArch config

The dashboard CLI does not patch these for you. They have to be set in `C:/RetroArch-Win64/retroarch.cfg`:

```
network_cmd_enable = "true"
cheevos_hardcore_mode_enable = "false"
run_ahead_enabled = "true"
run_ahead_secondary_instance = "true"
config_save_on_exit = "false"
input_save_state = "f2"
input_load_state = "f4"
```

The `secondary_instance` flag is the load-bearing one; without it, runahead overwrites manual saves and SAVE_STATE silently no-ops. Took a full session to root-cause.

## Filesystem layout

```
<config.data.dir>/
├── spinlab.db
├── spinlab.log               (rotating)
└── spinlab_states/           (configured as emulator.spinlab_state_dir)
    ├── entrance_<level>_<room>.state
    ├── cp_<level>_<ord>_hot.state
    └── <segment_id>.state    (cold-fill output)

C:/RetroArch-Win64/states/Snes9x/    (RA's savestate_directory + per-core subdir)
└── <game>.state<N>           (RA's per-save files, transient — moved out by StateIO)
```

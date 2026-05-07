# RetroArch Migration — Phase F-live: Wire the Dashboard Practice Loop to RetroArch

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Get the SpinLab dashboard's practice loop running end-to-end against RetroArch+snes9x_libretro instead of Mesen+Lua. This is the minimum integration step — wire Phase C's poller, Phase D's `StateIO`, and Phase B's `NCIClient` into `session_manager`, `practice.py`, `speed_run.py`, `recorder.py`, and the cold-fill flow. The Lua TCP path keeps working in parallel behind a config flag; one of the two backends gets removed in Phase G alongside the full README rewrite.

**Architecture:** A new `RetroArchOrchestrator` module owns NCIClient + Poller + StateIO and exposes a thin command surface that mirrors the Lua TCP commands (`practice_load`, `practice_stop`, `cold_fill_load`, `reference_start/stop`, `replay`/`replay_stop`, `reset`, `set_conditions`). It also publishes events to `session_manager` via a `TransitionEvent → protocol.py event` adapter. The orchestrator implements a duck-typed subset of `TcpManager`'s public surface (`is_connected`, `send_command`, `on_disconnect`, `recv_event` shape), so existing callers (practice, speed_run, recorder, capture controllers) need no changes if we wire it through the right seam. Backend selection is a config flag (`emulator.backend`); both paths coexist in F-live.

**Tech Stack:** Python 3.11+, stdlib `asyncio`, `dataclasses`, `pathlib`, pytest, pytest-asyncio. Builds on Phases B, C, and D. No new third-party dependencies.

**Phase A audit reference:** [`docs/retroarch-migration/lua-audit.md`](../../retroarch-migration/lua-audit.md) (Python→Lua command catalog and Lua→Python event catalog). Spec: [`docs/superpowers/specs/2026-05-06-retroarch-migration-design.md`](../specs/2026-05-06-retroarch-migration-design.md) (Phase F section + "Refined Phase Plan" F-live split). Phase C plan: [`2026-05-06-retroarch-phase-c-poller.md`](2026-05-06-retroarch-phase-c-poller.md). Phase D plan: [`2026-05-07-retroarch-phase-d-state-io.md`](2026-05-07-retroarch-phase-d-state-io.md).

**What this phase does NOT do:**
- Full README/`docs/ARCHITECTURE.md` rewrite — Phase G alongside Lua/Mesen removal.
- Delete `lua/spinlab.lua`, `lua/poke_engine.lua`, `tcp_manager.py`, or any `protocol.py` Lua command — Phase G.
- BSV record/replay — Phase E. F-live keeps the Lua replay path working through the existing TCP flow when backend is `mesen-lua`; under `retroarch` backend, replay/record raise `NotImplementedError("BSV not yet wired — Phase E").`
- WRAM-poll fallback for the L+Select invalidate combo — explicitly stubbed as a dashboard `/api/practice/invalidate` button only (per user direction, no in-game hotkey).
- Unified launcher / one-click bootstrap — F-live ships a manual workflow doc (`docs/retroarch-migration/launch-retroarch.md`) and a launcher hook in `routes/system.py`. The "press one button to start everything" experience is Phase G.
- Recording new reference runs against RA — Phase E. Existing `.spinrec` files keep replaying through the Lua backend during F-live.
- Address-map cleanup (the three-way duplicate from Phase C's followups) — Phase G.

---

## File Structure

| Path | Purpose |
|------|---------|
| `python/spinlab/retroarch/orchestrator.py` | **New.** `RetroArchOrchestrator` — duck-typed `TcpManager` replacement. Owns NCIClient + Poller + StateIO. Exposes `is_connected`, `send_command(cmd)`, `on_disconnect`, plus `start()/stop()` lifecycle. Translates the typed `*Cmd` dataclasses into NCI calls and StateIO operations. |
| `python/spinlab/retroarch/event_adapter.py` | **New.** Pure functions that map Phase C `TransitionEvent` dataclasses → the JSON-dict shape `protocol.parse_event` consumes. Single-direction adapter: poller→session_manager. |
| `python/spinlab/retroarch/timing.py` | **New.** Practice-segment timing computed Python-side (replaces Lua's segment-load → goal/death timer that today emits `attempt_result`). The poller-side state machine that watches for goal-or-death after a `practice_load` and emits the synthesized `attempt_result` event. |
| `python/spinlab/retroarch/conditions_loader.py` | **New, small.** Receives `SetConditionsCmd.definitions` from `install_condition_registry`, mirrors them into the existing Phase C `ConditionRegistry`. The orchestrator owns one such registry; the poller consumes from it during event emission. |
| `python/spinlab/config.py` | **Amended.** Add `EmulatorConfig.backend: Literal["retroarch", "mesen-lua"]` (default `mesen-lua`), `EmulatorConfig.retroarch_path: Path | None`, `EmulatorConfig.savestate_dir: Path | None`, `EmulatorConfig.spinlab_state_dir: Path | None`, `EmulatorConfig.ra_game_basename: str | None`, `NetworkConfig.nci_port: int = 55355`. Existing keys (`path`, `lua_script`, `script_data_dir`, `port`) keep working but are tagged "deprecated for retroarch backend". |
| `config.example.yaml` | **Amended.** Add commented RetroArch block alongside the Mesen one. |
| `python/spinlab/dashboard.py` | **Amended.** `create_app` branches on `config.emulator.backend`. For `retroarch`, build `RetroArchOrchestrator` instead of `TcpManager`, pass it as `tcp=` (it duck-types). Lifespan starts/stops orchestrator; `event_loop` becomes a no-op for the RA backend (the orchestrator's poller pushes events directly into `session.route_event`). |
| `python/spinlab/session_manager.py` | **Minor amendment.** Constructor accepts `tcp: TcpManager | RetroArchOrchestrator` (typed as a `Protocol` for clarity). No method-body changes — the duck-type contract is exhaustive enough that existing callers Just Work. |
| `python/spinlab/routes/practice.py` | **Amended.** Add `POST /api/practice/invalidate` endpoint that calls the existing `_handle_attempt_invalidated` path, equivalent to the Lua-emitted `AttemptInvalidatedEvent`. Backend-agnostic (works for both). |
| `python/spinlab/routes/system.py` | **Amended.** `POST /api/emulator/launch` branches on `config.emulator.backend`. For `retroarch`: log a hint and return 501 — F-live ships a documented manual launch workflow only. |
| `python/spinlab/routes/_deps.py` | **Amended.** Expose `get_orchestrator` for new endpoints (returns the orchestrator if backend is `retroarch`, else None). |
| `docs/retroarch-migration/launch-retroarch.md` | **New.** Manual launch instructions Andrew follows: which RA cfg keys to set, how to start RA pre-loaded with the ROM, then start the dashboard. |
| `tests/unit/retroarch/test_event_adapter.py` | **New.** Adapter conversion tests, one per `TransitionEvent` subclass. |
| `tests/unit/retroarch/test_orchestrator.py` | **New.** `RetroArchOrchestrator` unit tests with fake NCIClient and fake StateIO. Covers each command: `PracticeLoadCmd`, `PracticeStopCmd`, `ColdFillLoadCmd`, `FillGapLoadCmd`, `ReferenceStartCmd`/`Stop`, `ReplayCmd`/`Stop`, `ResetCmd`, `SetConditionsCmd`, `SetInvalidateComboCmd`, `GameContextCmd`. Verifies the connected-state lifecycle. |
| `tests/unit/retroarch/test_timing.py` | **New.** Practice attempt-result timing state machine: load→goal=completed, load→death=fail, multi-death scenarios, manual stop. |
| `tests/unit/retroarch/test_orchestrator_publishes_events.py` | **New.** End-to-end (still all fakes): orchestrator runs a poller against a scripted snapshot stream, the adapter runs, the resulting JSON dicts route through `parse_event` cleanly. |
| `tests/unit/test_config_retroarch.py` | **New.** Backend selection + new keys parse correctly; old keys still parse (back-compat). |
| `tests/integration/test_retroarch_practice_smoke.py` | **New, gated `pytest -m emulator`.** Live-RA smoke: launch dashboard, expect orchestrator to connect over NCI, manually verify a single segment load+save+goal cycle. Fails open (skipped) when RA isn't running. Andrew runs this; not run in CI. |

---

## Design Decisions

These four decisions are locked in for F-live. They preempt the open questions in the spec section "What this phase does" and answer the F-live-specific "where does the new code live?" questions.

### Decision 1: Backend selection — explicit config flag, default `mesen-lua`

`emulator.backend: "retroarch" | "mesen-lua"` (default `mesen-lua` for back-compat during the migration). Dashboard reads the value at startup. No runtime auto-detection — no "which port answers first" probing. Reasoning:

- **Auto-detection is fragile.** A user with both Mesen and RA running (likely during the migration) would get nondeterministic backend selection.
- **Explicit beats implicit.** The config file is the user's decision-making surface; we honor it.
- **Phase G removes the flag.** Once the migration lands, `mesen-lua` goes away. Until then, keeping the flag explicit means the user can revert to Mesen in 5 seconds if RA flakes.

The default stays `mesen-lua` for F-live so that existing config files keep working unchanged. Andrew will flip it to `retroarch` when he's ready to start using the new path.

### Decision 2: Adapter direction — `TransitionEvent → JSON dict` (one-way)

Phase C emits `TransitionEvent` dataclasses (`LevelEntrance`, `Death`, `LevelExit`, `Checkpoint`, `Spawn`). `session_manager.route_event` consumes JSON dicts via `parse_event` from `protocol.py`, which produces a different but overlapping set of dataclasses (`LevelEntranceEvent`, `DeathEvent`, `LevelExitEvent`, `CheckpointEvent`, `SpawnEvent`).

We adopt **one-way conversion**: the adapter (`event_adapter.to_protocol_dict`) takes a `TransitionEvent`, returns the JSON-dict shape that `parse_event` produces a matching `protocol.*Event` from. The orchestrator's `on_event` callback wraps `session.route_event(adapter.to_protocol_dict(ev))`.

**Why not refactor session_manager to accept dataclasses directly?** Two reasons:
1. Session_manager has 18 event handlers and a substantial test surface. Refactoring its event-input shape is a Phase G–scale change.
2. The protocol dataclasses include events the poller doesn't emit (`AttemptResultEvent`, `RecSavedEvent`, `ReplayStartedEvent`, etc.) that come from elsewhere. Keeping the JSON-dict bus as the single ingress point preserves the unified flow.

Cost: small adapter module with ~5 conversion functions. Worth it.

Field-level mapping (the only nontrivial bits — most are 1:1):

| TransitionEvent class | protocol.py shape | Notes |
|---|---|---|
| `LevelEntrance(level, room, frame, state_path, timestamp_ms, conditions)` | `{"event": "level_entrance", "level": …, "state_path": …, "timestamp_ms": …, "conditions": {…}}` | `room` and `frame` dropped (the Lua emits don't include them either, per protocol.py). |
| `Death(level_num, timestamp_ms, conditions)` | `{"event": "death"}` | Lua event has no body in `protocol.py`. We drop everything except the marker. timestamp_ms isn't on `DeathEvent`. |
| `LevelExit(level, room, goal, elapsed_ms, frame, timestamp_ms, conditions)` | `{"event": "level_exit", "level": …, "goal": …, "timestamp_ms": …, "conditions": {…}}` | `elapsed_ms`, `room`, `frame` dropped. |
| `Checkpoint(level_num, cp_type, cp_ordinal, state_path, timestamp_ms, conditions)` | `{"event": "checkpoint", "level_num": …, "cp_ordinal": …, "state_path": …, "timestamp_ms": …, "conditions": {…}}` | `cp_type` dropped (the protocol shape doesn't carry it). |
| `Spawn(level_num, is_cold_cp, cp_ordinal, state_captured, state_path, segment_id, timestamp_ms, conditions)` | `{"event": "spawn", "level_num": …, "state_captured": …, "state_path": …, "is_cold_cp": …, "cp_ordinal": …, "timestamp_ms": …, "conditions": {…}}` | `segment_id` is consumed by the orchestrator before emission (used as the StateIO key) and not forwarded — `SpawnEvent` doesn't carry it. |

**Caveat surfaced:** `protocol.LevelEntranceEvent.state_path: str | None = None`, but Phase C's resolver populates `LevelEntrance.state_path: str = ""`. Convert empty-string → None at the adapter boundary so the rest of session_manager sees None-or-real-path, matching its existing assumptions in `_handle_level_entrance` and downstream `recorder.handle_entrance`.

### Decision 3: Orchestrator structure — duck-typed `TcpManager` replacement

A new `RetroArchOrchestrator` class implements the public surface that practice/speed_run/recorder/capture controllers actually call on `TcpManager`:

```python
class RetroArchOrchestrator:
    is_connected: bool
    on_disconnect: Callable | None
    async def send_command(self, cmd: object) -> None: ...
    async def disconnect(self) -> None: ...
    # Plus orchestrator-only:
    async def start(self) -> None: ...
    def get_event_handler(self) -> Callable[[dict], Awaitable[None]]: ...
```

`send_command(cmd)` is the heart. It dispatches on `type(cmd)` to:
- `PracticeLoadCmd` → `state_io.load_segment_state(seg_id)`, then arm `timing.start_attempt(cmd)` and start watching for goal/death/manual-stop.
- `PracticeStopCmd` → `timing.stop_attempt()`.
- `ColdFillLoadCmd` → load via state_io, then `poller.activate_cold_fill(segment_id)`.
- `FillGapLoadCmd` → load via state_io, no cold-fill activation; capture controller's `handle_fill_gap_spawn` already gates on `state_captured`.
- `ResetCmd` → `client.reset()`.
- `SetConditionsCmd` → `conditions_loader.apply(cmd.definitions)` updates the registry.
- `SetInvalidateComboCmd` → ignored. Logged at INFO ("invalidate combo handled by dashboard button under retroarch backend").
- `GameContextCmd` → ignored on outbound (RA doesn't need it). `RomInfoEvent` flows the other direction at startup via `client.get_status()` → adapter → session.
- `ReferenceStartCmd` / `ReferenceStopCmd` / `ReplayCmd` / `ReplayStopCmd` → raise `NotImplementedError("BSV not yet wired — Phase E")`. F-live's RA backend genuinely doesn't support these yet.
- `SpeedRunLoadCmd` / `SpeedRunStopCmd` → state_io.load + arm a speed-run timing watcher (the timing module covers this in the same state machine).

This is duck typing, not formal subclassing — reasonable because the stubs come at TcpManager's full surface, and a `Protocol` annotation on session_manager's `tcp:` parameter documents the contract without a forced inheritance.

**Why not a thinner adapter?** Considered making the orchestrator just publish `TransitionEvent`s and putting the practice/timing/state_io plumbing inside session_manager. Rejected: it would push a lot of new logic into session_manager, growing an already-large class. Keeping the new logic in `orchestrator.py` localizes the migration.

### Decision 4: Test strategy — unit-first, single live smoke

Three tiers of tests in F-live:

1. **Unit tests** (default, run in `pytest -m "not (emulator or slow or frontend)"`): cover the adapter, the orchestrator's command dispatch (with fake NCIClient/StateIO/Poller), the timing state machine, the config parsing. Maybe 25 new tests.
2. **End-to-end-style with fakes** (also in default suite): `test_orchestrator_publishes_events.py` runs a real Poller against scripted snapshots, threads through `event_adapter.to_protocol_dict`, and asserts `parse_event` accepts the result. No NCI, no live RA.
3. **Live-RA integration smoke** (`pytest -m emulator`): one test, `test_retroarch_practice_smoke.py`, designed to be skipped when RA isn't running. Andrew runs this manually post-implementation. Validates: dashboard starts, orchestrator connects, can issue a `PracticeLoadCmd` for a segment that exists, RA visibly loads the state. Does not assert against live game memory — that's the manual smoke gate.

The spec's "live-practice end-to-end smoke" gate (step 6 in the Refined Phase Plan) is **not** automated. It's Andrew exercising the dashboard. The integration test above is a startup sanity check, not the gate.

---

## Task 1: Config schema additions

Extend `EmulatorConfig` and `NetworkConfig` with RA-relevant keys. Keep all old keys readable (back-compat). Add `backend` selector. Update `config.example.yaml`.

**Files:**
- Edit: `python/spinlab/config.py`
- Edit: `config.example.yaml`
- Create: `tests/unit/test_config_retroarch.py`

- [ ] **Step 1: Write failing tests**

Tests (sketch — they live in `tests/unit/test_config_retroarch.py`):
- `test_default_backend_is_mesen_lua` — empty `emulator:` block parses as `backend == "mesen-lua"`.
- `test_backend_retroarch_explicit` — `emulator.backend: retroarch` parses correctly.
- `test_unknown_backend_raises_value_error` — defensive.
- `test_retroarch_paths_parse` — `emulator.retroarch_path`, `emulator.savestate_dir`, `emulator.spinlab_state_dir`, `emulator.ra_game_basename` come through as `Path` (or str for `ra_game_basename`).
- `test_nci_port_default_55355` — when `network.nci_port` omitted, default is 55355.
- `test_nci_port_override` — explicit value honored.
- `test_old_mesen_keys_still_parse` — feed in a Mesen-shaped config, expect no errors and the legacy fields populate.

- [ ] **Step 2: Run tests, expect failure**

```
python -m pytest tests/unit/test_config_retroarch.py -v
```

- [ ] **Step 3: Implement**

In `config.py`, extend `EmulatorConfig`:
```python
@dataclass
class EmulatorConfig:
    backend: str = "mesen-lua"   # "mesen-lua" | "retroarch"
    # Mesen-Lua keys (deprecated under retroarch backend):
    path: Path | None = None
    lua_script: Path | None = None
    script_data_dir: Path | None = None
    # RetroArch keys:
    retroarch_path: Path | None = None
    savestate_dir: Path | None = None         # RA's <savestate_directory>
    spinlab_state_dir: Path | None = None     # SpinLab-managed segment states
    ra_game_basename: str | None = None       # e.g. "Toothpaste World"
```

`NetworkConfig.nci_port: int = 55355`. Add to `from_yaml` parsing with sane defaults.

Validate `backend in {"mesen-lua", "retroarch"}` at parse time; raise `ValueError` otherwise.

Update `config.example.yaml`:
```yaml
emulator:
  backend: mesen-lua          # "mesen-lua" or "retroarch"
  # RetroArch keys (used when backend == "retroarch"):
  retroarch_path: "C:/RetroArch-Win64/retroarch.exe"
  savestate_dir: "C:/RetroArch-Win64/saves/states"
  spinlab_state_dir: "data/spinlab_states"
  ra_game_basename: "Toothpaste World"
  # Mesen2 keys (used when backend == "mesen-lua"):
  path: "C:/path/to/Mesen.exe"
  lua_script: "lua/spinlab.lua"
  script_data_dir: "C:/Users/<you>/Documents/Mesen2/LuaScriptData/spinlab"
network:
  port: 15482            # Lua TCP server (mesen-lua backend only)
  nci_port: 55355        # RetroArch NCI (retroarch backend only)
  dashboard_port: 15483
  host: "127.0.0.1"
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/unit/test_config_retroarch.py -v
```

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/config.py config.example.yaml tests/unit/test_config_retroarch.py
git commit -m "feat(config): add retroarch backend selector and RA path keys"
```

---

## Task 2: Event adapter — `TransitionEvent` → JSON dict

Pure functions, one per Phase C event type, returning the dict shape that `parse_event` accepts.

**Files:**
- Create: `python/spinlab/retroarch/event_adapter.py`
- Create: `tests/unit/retroarch/test_event_adapter.py`

- [ ] **Step 1: Write failing tests**

Per the field mapping table in Decision 2, write one test per event type. Each test:
1. Constructs a `TransitionEvent` (e.g. `LevelEntrance(level=5, room=2, frame=120, state_path="/p/seg.state", timestamp_ms=1000, conditions={"x": 1})`).
2. Calls `event_adapter.to_protocol_dict(ev)`.
3. Asserts the dict has the right `event` key and field values.
4. Asserts `protocol.parse_event(dict_result)` returns the correct `protocol.*Event` instance.

Edge cases to pin:
- `state_path == ""` → emitted as `None` (matches `LevelEntranceEvent.state_path: str | None = None`).
- `Death` always emits exactly `{"event": "death"}` (the protocol class has no fields).
- `Spawn` with `segment_id="seg-x"` does not include `segment_id` in the output dict (it's consumed orchestrator-side; protocol shape doesn't have it).

Add a roundtrip test: `parse_event(to_protocol_dict(ev))` is a valid `protocol.*Event` for every concrete TransitionEvent subclass.

- [ ] **Step 2: Run tests, expect failure**

```
python -m pytest tests/unit/retroarch/test_event_adapter.py -v
```

- [ ] **Step 3: Implement**

Single dispatch table keyed on `type(event)`:

```python
def to_protocol_dict(event: TransitionEvent) -> dict:
    if isinstance(event, LevelEntrance):
        return {
            "event": "level_entrance",
            "level": event.level,
            "state_path": event.state_path or None,
            "timestamp_ms": event.timestamp_ms,
            "conditions": dict(event.conditions),
        }
    if isinstance(event, Death):
        return {"event": "death"}
    if isinstance(event, LevelExit):
        return {
            "event": "level_exit",
            "level": event.level,
            "goal": event.goal,
            "timestamp_ms": event.timestamp_ms,
            "conditions": dict(event.conditions),
        }
    if isinstance(event, Checkpoint):
        return {
            "event": "checkpoint",
            "level_num": event.level_num,
            "cp_ordinal": event.cp_ordinal,
            "state_path": event.state_path or None,
            "timestamp_ms": event.timestamp_ms,
            "conditions": dict(event.conditions),
        }
    if isinstance(event, Spawn):
        return {
            "event": "spawn",
            "level_num": event.level_num,
            "state_captured": event.state_captured,
            "state_path": event.state_path or None,
            "is_cold_cp": event.is_cold_cp,
            "cp_ordinal": event.cp_ordinal,
            "timestamp_ms": event.timestamp_ms,
            "conditions": dict(event.conditions),
        }
    raise TypeError(f"unsupported TransitionEvent subclass: {type(event).__name__}")
```

Plus a `to_rom_info_dict(status: StatusInfo) -> dict` helper for the startup `client.get_status()` → `RomInfoEvent` flow, since the orchestrator emits this once at start.

- [ ] **Step 4: Run tests**

```
python -m pytest tests/unit/retroarch/test_event_adapter.py -v
```

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/event_adapter.py tests/unit/retroarch/test_event_adapter.py
git commit -m "feat(retroarch): TransitionEvent → protocol-dict adapter"
```

---

## Task 3: Practice attempt-result timing state machine

The Lua side today computes the `attempt_result` event by timing from `practice_load` to a goal/death/exit/timeout. Phase C does NOT do this. F-live needs to.

A small state machine: `arm(segment_id, end_type, expected_time_ms, death_penalty_ms, auto_advance_delay_ms)` resets timers; subsequent `Death`/`LevelExit`/`Spawn` events are observed and an `AttemptResultEvent`-shaped dict is emitted when the segment ends. End conditions:
- `LevelExit` matching `end_type == "goal"` → `completed=True`, `time_ms = exit_ts - load_ts`, `clean_tail_ms = exit_ts - last_spawn_ts (or load_ts)`.
- `Checkpoint` matching `end_type == "checkpoint"` and `cp_ordinal == expected` → `completed=True` similarly.
- N consecutive deaths reaching `death_penalty_ms * deaths > expected_time_ms * tolerance` → `completed=False`. (Match Lua's behaviour; verify against `lua/spinlab.lua` during implementation.)
- Manual `disarm()` (sent on `PracticeStopCmd`) → no event emitted.

**Caveat surfaced:** the Lua-side practice timing is the most complex bit of `lua/spinlab.lua`. F-live ports the **observable behaviour** (the `attempt_result` event shape and trigger rules) but doesn't aim for byte-for-byte compatibility. Re-read `lua/spinlab.lua` lines covering `practice_load` → `attempt_result` during implementation to ensure no surprises (Andrew should review the timing logic in code review).

**Files:**
- Create: `python/spinlab/retroarch/timing.py`
- Create: `tests/unit/retroarch/test_timing.py`

- [ ] **Step 1: Write failing tests**

Test cases (each builds a `PracticeTiming` and feeds it scripted events):
- `test_arm_then_goal_emits_completed_attempt_result`
- `test_arm_then_death_increments_deaths`
- `test_arm_then_n_deaths_emits_failed_attempt_result` (N picked from death_penalty_ms math)
- `test_disarm_emits_nothing`
- `test_clean_tail_ms_uses_last_spawn_ts`
- `test_speed_run_load_arms_separately` (speed-run uses similar machinery; could share or split — sketch as a separate `SpeedRunTiming` if simpler)
- `test_checkpoint_endtype_completes_on_matching_cp_ordinal`

- [ ] **Step 2: Run tests, expect failure**

- [ ] **Step 3: Implement**

`PracticeTiming.arm(...)`, `.observe(event_dict)`, `.disarm()`, with an `on_attempt_result: Callable[[dict], None]` callback that fires the `attempt_result` JSON dict shaped for `parse_event`.

Speed-run timing: do **the simplest thing first**. Re-read `lua/spinlab.lua`'s `speed_run_load` flow during implementation. If it's substantially the same as practice's, share the state machine. If it differs, add a minimal `SpeedRunTiming` and emit `speed_run_checkpoint` / `speed_run_death` / `speed_run_complete` dicts. **Caveat surfaced:** speed_run timing was less explored in this plan than practice; expect a small discovery during Task 3 — budget time for it. If it gets large, split out as a separate task.

- [ ] **Step 4: Run tests**

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/timing.py tests/unit/retroarch/test_timing.py
git commit -m "feat(retroarch): practice attempt-result timing state machine"
```

---

## Task 4: Conditions registry loader (Phase C ConditionRegistry adapter)

`session_manager.install_condition_registry` pushes per-game probe definitions to Lua via `SetConditionsCmd`. Under RA backend, the orchestrator catches that command and applies it to the existing Phase C `conditions.ConditionRegistry`. The poller already reads from a registry instance to populate `event.conditions`.

**Files:**
- Create: `python/spinlab/retroarch/conditions_loader.py`
- Create: `tests/unit/retroarch/test_conditions_loader.py`

Smaller task. ~20 LOC. The wrinkle is that Phase C's poller doesn't currently call `ConditionRegistry.read_all` — Phase C left this for F-live. Add a hook to the poller (or the detector) that, when emitting events, populates `event.conditions` from the registry.

**Caveat surfaced:** Phase C's poller emits events with `conditions={}`. Threading the registry into the poller means a small Phase C amendment (similar to Phase D's `state_path_for` resolver):
- Extend `PollerDeps` with `conditions_registry: ConditionRegistry | None = None`.
- Before `on_event(ev)`, if registry is set, populate `event.conditions = registry.read_all(client)` via `dataclasses.replace`.

This is a single-line amendment in the poller; do it in this task. Tests cover both: the loader and the poller integration.

- [ ] **Step 1: Write failing tests**
- [ ] **Step 2: Run tests, expect failure**
- [ ] **Step 3: Implement** (loader + poller amendment)
- [ ] **Step 4: Run tests**
- [ ] **Step 5: Commit**

```bash
git commit -m "feat(retroarch): conditions registry loader + poller integration"
```

---

## Task 5: `RetroArchOrchestrator` — duck-typed TcpManager replacement

The big task. Build the orchestrator with full dispatch on `type(cmd)` for every command in `protocol.py`, plus the lifecycle `start()`/`disconnect()`.

**Files:**
- Create: `python/spinlab/retroarch/orchestrator.py`
- Create: `tests/unit/retroarch/test_orchestrator.py`

Public surface (matches `TcpManager`'s surface):
- `is_connected: bool` — True after `start()` succeeds (NCI VERSION roundtrip OK), False otherwise.
- `on_disconnect: Callable | None` — settable.
- `async def send_command(self, cmd) -> None`
- `async def disconnect(self) -> None`
- `async def start(self) -> bool` — connects to NCI, runs VERSION check, starts poller and timing modules. Returns True on success.

Internals (constructor takes them, simplifying tests):
- `client: NCIClient`
- `state_io: StateIO`
- `poller: Poller`
- `practice_timing: PracticeTiming`
- `speed_run_timing: SpeedRunTiming`
- `conditions: ConditionRegistry`
- `event_callback: Callable[[dict], Awaitable[None]]` — wired to `session.route_event`. The poller's `on_event` is `lambda ev: asyncio.create_task(self.event_callback(adapter.to_protocol_dict(ev)))` (with care: the poller is async, the callback is async, so we use `asyncio.run_coroutine_threadsafe` or a queue pattern — sketch below).

**Critical detail:** the poller's `on_event` is sync (called synchronously inside the asyncio loop). `session.route_event` is async. The orchestrator publishes via `asyncio.create_task` from inside the poller's running loop — both are in the same event loop. Need to verify in implementation that ordering doesn't break (events delivered in-order to session_manager). Backstop: a single `asyncio.Queue` between poller and an orchestrator-owned consumer task.

Command dispatch table (per Decision 3):

```python
async def send_command(self, cmd) -> None:
    handler = self._dispatch.get(type(cmd))
    if handler is None:
        logger.warning("RetroArchOrchestrator: ignoring unknown cmd %r", cmd)
        return
    await handler(cmd)

self._dispatch = {
    PracticeLoadCmd: self._on_practice_load,
    PracticeStopCmd: self._on_practice_stop,
    SpeedRunLoadCmd: self._on_speed_run_load,
    SpeedRunStopCmd: self._on_speed_run_stop,
    ColdFillLoadCmd: self._on_cold_fill_load,
    FillGapLoadCmd: self._on_fill_gap_load,
    ResetCmd: self._on_reset,
    SetConditionsCmd: self._on_set_conditions,
    SetInvalidateComboCmd: self._on_set_invalidate_combo,
    GameContextCmd: self._on_game_context,
    ReferenceStartCmd: self._unsupported_phase_e,
    ReferenceStopCmd: self._unsupported_phase_e,
    ReplayCmd: self._unsupported_phase_e,
    ReplayStopCmd: self._unsupported_phase_e,
}
```

`_unsupported_phase_e(cmd)` raises `NotImplementedError` with a clear message; capture controllers call this from REFERENCE/REPLAY mode only, so the dashboard surfaces the error in the standard way (the existing `ActionError` handler).

`_on_practice_load(cmd: PracticeLoadCmd)`:
- `self.state_io.load_segment_state(cmd.id)` — Decision 5 from Phase D handles the rest.
- After the load, fire `self.poller.mark_state_loaded()` so the next poll re-syncs.
- `self.practice_timing.arm(cmd.id, cmd.end_type, cmd.expected_time_ms, cmd.death_penalty_ms, cmd.auto_advance_delay_ms)`.

`_on_cold_fill_load(cmd: ColdFillLoadCmd)`:
- `self.state_io.load_state_from_path(cmd.state_path)` — variant of `load_segment_state` that accepts a path directly. **Caveat surfaced:** Phase D's `StateIO.load_segment_state` takes a segment_id, but `ColdFillLoadCmd.state_path` is a path. Add a small `StateIO.load_state_from_path(path: Path)` helper as part of this task (or in Task 6).
- `self.poller.mark_state_loaded()`.
- `self.poller.activate_cold_fill(cmd.segment_id)`.

`_on_set_invalidate_combo(cmd)` logs and ignores (per the user's invalidate-button-only choice). The adapter never emits `attempt_invalidated` from RA. The `/api/practice/invalidate` route in Task 8 is the only source under RA backend.

- [ ] **Step 1-5:** TDD pattern as before. Aim for ~10 unit tests covering each command.

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(retroarch): RetroArchOrchestrator — TcpManager-shaped command dispatch"
```

---

## Task 6: `StateIO.load_state_from_path` helper + small Phase D extension

ColdFill and FillGap pass `state_path` directly (not a segment_id). Phase D's `load_segment_state(segment_id)` looks up the path internally; `ColdFillLoadCmd` already has the path.

Add `StateIO.load_state_from_path(path: Path) -> None` that copies the path into RA's reserved slot and fires `LOAD_STATE_SLOT`. Trivially small, but keeps Phase D's API clean.

**Files:**
- Edit: `python/spinlab/retroarch/state_io.py`
- Edit: `tests/unit/retroarch/test_state_io_load.py` (add cases)

- [ ] TDD as usual. ~5 LOC implementation + 2 unit tests.

```bash
git commit -m "feat(retroarch): StateIO.load_state_from_path for direct-path loads"
```

---

## Task 7: Orchestrator end-to-end test (still all fakes)

Build a `RetroArchOrchestrator` with a real `Poller` driven by a fake `NCIClient` that returns scripted snapshots. Send a `PracticeLoadCmd`, run the loop briefly, watch for `attempt_result` arrival via the orchestrator's `event_callback`. Tests the full fake-RA → poller → adapter → protocol-dict pipeline.

**Files:**
- Create: `tests/unit/retroarch/test_orchestrator_publishes_events.py`

This is the integration of Tasks 2–5 without live RA.

- [ ] **Step 1: Write the test first** (it will catch wiring bugs across the whole pipeline).
- [ ] **Step 2: Run, fix, commit.**

```bash
git commit -m "test(retroarch): orchestrator publishes events through full pipeline"
```

---

## Task 8: Dashboard wiring + `/api/practice/invalidate` endpoint

Branch `create_app` on `config.emulator.backend`. Add the invalidate route. Deprecate the launcher route for retroarch.

**Files:**
- Edit: `python/spinlab/dashboard.py`
- Edit: `python/spinlab/session_manager.py` (`tcp:` parameter type only — duck-typed Protocol)
- Edit: `python/spinlab/routes/practice.py` (add `/api/practice/invalidate`)
- Edit: `python/spinlab/routes/system.py` (`/api/emulator/launch` 501 for retroarch)
- Edit: `python/spinlab/routes/_deps.py` (add `get_orchestrator`, optional)
- Create: `tests/unit/test_dashboard_backend_select.py` — verifies `create_app` instantiates the right backend.

Dashboard wiring sketch (in `create_app`):

```python
if config.emulator.backend == "retroarch":
    from spinlab.retroarch.orchestrator import build_orchestrator
    tcp = build_orchestrator(config)   # returns RetroArchOrchestrator
    # event_loop becomes a no-op — orchestrator pushes events directly into session.route_event
else:
    tcp = TcpManager(config.network.host, config.network.port)
session = SessionManager(db, tcp, ...)

if config.emulator.backend == "retroarch":
    tcp.event_callback = session.route_event
```

In `lifespan`, for retroarch backend, `await tcp.start()` instead of `event_loop`.

`/api/practice/invalidate`:
```python
@router.post("/practice/invalidate")
async def invalidate(session: SessionManager = Depends(get_session)):
    await session._handle_attempt_invalidated(AttemptInvalidatedEvent())
    return {"status": "ok"}
```

(Use the existing handler — it's already written for the Lua-emitted event. This is backend-agnostic.)

- [ ] TDD pattern. ~5 unit tests for dashboard backend selection.

```bash
git commit -m "feat(dashboard): retroarch backend wiring + invalidate button endpoint"
```

---

## Task 9: Manual launch documentation

Document the RetroArch launch workflow Andrew follows during F-live. No code; markdown only.

**Files:**
- Create: `docs/retroarch-migration/launch-retroarch.md`

Contents (outline):
1. Required RetroArch settings (link to spec for `cheevos_hardcore_mode_enable=false`, runahead config).
2. Required SpinLab `config.yaml` keys for retroarch backend.
3. Step-by-step: start RA with the ROM loaded → confirm NCI alive (e.g., `python -m spinlab.scripts.smoke_nci_client`) → `python -m spinlab dashboard` → check the dashboard log for "RetroArch backend connected".
4. Reserved slot 9999 note (per Phase D Decision 6).
5. Known limitations during F-live: no record/replay; no in-game invalidate combo; speed_run timing should be cross-verified against Lua reference.
6. Troubleshooting: NCI not responding, deep-pause symptom, savestate dir mismatch.

```bash
git commit -m "docs(retroarch): manual launch workflow for Phase F-live"
```

---

## Task 10: Live-RA smoke test (gated `pytest -m emulator`)

Single integration test that runs only when RA is reachable.

**Files:**
- Create: `tests/integration/test_retroarch_practice_smoke.py`

Test pseudocode:
```python
@pytest.mark.emulator
def test_orchestrator_connects_to_live_retroarch():
    """Smoke: orchestrator can talk to a running RetroArch."""
    try:
        client = NCIClient()
        client.version()
    except NCITimeout:
        pytest.skip("RetroArch not running on default NCI port")

    # Build minimal config + DB; instantiate orchestrator.
    # Issue VERSION via orchestrator's client; assert non-empty version string.
```

Andrew runs `pytest -m emulator -v` after launching RA. Skipped in CI.

- [ ] Implement test. Note in plan: this is **not** the smoke gate; the gate is Andrew exercising the dashboard.

```bash
git commit -m "test(retroarch): live-RA smoke test (emulator-marked)"
```

---

## Phase F-live exit criteria

- `python -m spinlab dashboard` started against `emulator.backend: retroarch` config connects to a live RetroArch over NCI on port 55355.
- Practice loop end-to-end works against RetroArch: dashboard picks a segment → orchestrator loads the saved state via NCI+filesystem → user plays → death/goal events fire → `attempt_result` propagates → next segment loaded.
- Cold-fill flow works: dashboard finalize → `cold_fill_load` → user dies → cold state captured to SpinLab dir.
- Fill-gap flow works: hot state loaded → user dies → cold state captured.
- Speed-run loop runs at least one level end-to-end.
- Dashboard "Invalidate" button (POST `/api/practice/invalidate`) marks the most recent attempt as invalidated.
- `pytest -m "not (emulator or slow or frontend)"` is fully green; ~25 new unit tests added.
- `pytest -m emulator` runs (skipped if RA is not running, passing if it is).
- `emulator.backend: mesen-lua` config still works exactly as it did before — zero regressions in the Lua path.
- `pyright` clean on the new files.
- `ruff` clean on the new files.

## Manual smoke-test checklist (Andrew runs)

After all tasks land, Andrew exercises the practice loop:

1. [ ] Start RetroArch with snes9x_libretro core, ROM pre-loaded, runahead enabled (3 frames).
2. [ ] Set `emulator.backend: retroarch` in `config.yaml`. Set `retroarch_path`, `savestate_dir`, `spinlab_state_dir`, `ra_game_basename`.
3. [ ] Run `python -m spinlab dashboard --config config.yaml`.
4. [ ] Open the dashboard. Confirm game name shows up (RomInfo flow).
5. [ ] Run cold-fill if needed. Confirm dies and respawns produce captured cold states.
6. [ ] Start practice. Confirm:
   - First segment loads (visible state load in RA).
   - On goal: dashboard advances to next segment after auto-advance delay.
   - On death: timer counts deaths; eventual fail or completion is reported.
   - "Invalidate" button on the last attempt does the right thing in the DB.
7. [ ] Stop practice. Confirm dashboard returns to IDLE without errors.
8. [ ] Reload dashboard with `mesen-lua` backend. Confirm everything still works as before (regression check).
9. [ ] Latency check: visually confirm 1-frame jump response in TPW.
10. [ ] Report results in the spec doc: did F-live succeed?

If 1–10 pass, the smoke gate is met and Phase F-live is done. If anything fails, file a followup at the end of the lua-audit and address before proceeding to Phase E.

---

## What's deliberately not in F-live

- **Record/replay against RetroArch.** Needs BSV (Phase E). F-live's RA backend raises `NotImplementedError` for `ReferenceStartCmd`/`ReplayCmd`/etc. Under `mesen-lua` backend, record/replay continues to work as it did before.
- **Removing `lua/`, `tcp_manager.py`, `protocol.py`'s Lua-specific commands.** Phase G.
- **Full README/`docs/ARCHITECTURE.md` rewrite.** Phase G alongside the cleanup.
- **WRAM-poll fallback for L+Select invalidate.** User chose dashboard-button-only — F-live ships only the button.
- **Unified launcher (one command starts RA + dashboard).** Manual workflow only in F-live. Phase G.
- **`ADDR_CP_ENTRANCE` cross-verification with ASM cp-patch hacks.** Phase C followup; not blocking F-live.
- **Address-map source-of-truth deduplication.** Phase G alongside the Lua removal.
- **`emu.setSpeed`-equivalent for replay speed control.** Phase E concern.
- **Speed_run timing perfect parity with Lua.** F-live ports the observable behaviour and Andrew sanity-checks during the smoke gate. Refinements get followup tickets.

---

## Plan caveats and gaps

**Honest disclosures, not hand-waving — these will likely require small mid-phase adjustments:**

1. **Speed_run timing complexity is under-explored in this plan.** I read `speed_run.py` but did not closely re-read `lua/spinlab.lua`'s `speed_run_load` flow during plan-writing. Task 3 budgets some discovery time. If it balloons (LevelPlan/checkpoint flow has knock-on consequences for the Lua state machine), split it out of Task 3 into a dedicated Task 3a.

2. **Practice attempt_result detail.** Lua's `attempt_result` event includes `clean_tail_ms`, `deaths`, etc. The exact rules for how Lua decides "this is a fail" (vs "give the user another retry") came from `auto_advance_delay_ms` and `death_penalty_ms` semantics that I sketched but did not exhaustively read. Task 3 implementer should pull `lua/spinlab.lua` and pin the ruleset before writing tests.

3. **RomInfo bootstrap timing.** Under the Lua backend, `RomInfoEvent` arrives shortly after TCP connect. Under RA, the orchestrator must call `client.get_status()` once at startup and synthesize the equivalent dict. Tasks 5 + 8 both touch this; verify in Task 8's dashboard wiring that the timing works (orchestrator startup completes BEFORE `session.route_event` is first invoked).

4. **Async-bridge correctness.** The poller's sync callback firing `asyncio.create_task(session.route_event(d))` from inside an async loop should work, but ordering guarantees need a small validation. If event ordering matters across multiple events emitted on the same poll tick, switch to a queue + consumer pattern. Address during Task 5 implementation, not the plan.

5. **`heartbeat`/`pong` semantics.** Lua's TCP server emits heartbeats; `tcp_manager.py` filters them. The RA backend has no heartbeat — connection liveness comes from NCI VERSION roundtrip. Document in `launch-retroarch.md` that "no heartbeat" is expected; session_manager's TcpManager-shape callers don't depend on heartbeats.

6. **Disconnection semantics.** `TcpManager.on_disconnect` fires when the read loop drops; under RA backend, "disconnect" is fuzzy — NCI is UDP, no connection state. Decision: treat N consecutive NCI VERSION timeouts (e.g., 3 × 1s) as "disconnected", trigger `on_disconnect`. Implementation detail for Task 5; sketch test in `test_orchestrator.py`.

7. **State_path resolution for non-cold-fill spawns during practice.** Phase D's `resolve_event_path` returns paths for `LevelEntrance`, `Checkpoint`, `Spawn(cold)`. During practice (not cold-fill), spawns don't trigger a state save. The poller's resolver currently runs unconditionally. Verify Task 7 covers this — the resolver returning `""` for non-cold spawns is correct, and the orchestrator does NOT call `save_segment_state` automatically. State saves happen explicitly via the recorder during reference recording (which is Phase E for RA backend; not exercised in F-live).

---

## Plan self-review

- **File structure:** 4 new implementation files (`orchestrator.py`, `event_adapter.py`, `timing.py`, `conditions_loader.py`), 5 amended files (`config.py`, `dashboard.py`, `session_manager.py`, two routes), 1 doc, 6 new test files. Each file has one clear responsibility. The orchestrator is the largest single file but matches the scope of `tcp_manager.py` it parallels.
- **TDD discipline:** every implementation task has a failing-test step before the implementation step. Tests are unit-level except for Task 7 (cross-module fakes) and Task 10 (live RA, gated).
- **Coverage:** every Python→Lua command in the audit's "Python → Lua commands" table has a dispatch path in the orchestrator (live, deferred, or explicitly Phase E). Every Lua→Python event in the audit has either a Phase C poller emission path or a synthesized event source (timing module, RomInfo bootstrap, dashboard invalidate button).
- **Coexistence with Lua path:** the `mesen-lua` backend continues to work, gated by `emulator.backend`. Phase G removes it.
- **No placeholders.** Every method has a body, a `NotImplementedError` with a Phase reference, or a clear deferred-task pointer.
- **Type consistency:** `tcp:` parameter on session_manager typed as a `Protocol` (or `TcpManager | RetroArchOrchestrator`) — duck-typed cleanly.
- **Honest gaps:** the "Plan caveats and gaps" section above flags 7 known unknowns. Implementer should expect 1–2 mid-phase course corrections.
- **Reserved-slot decision (9999) is locked in from Phase D** — F-live does not revisit it.
- **The smoke gate is a manual user step**, not an automated test. The plan documents what Andrew does and what passes mean.

## Next phase after F-live

Phase E — BSV record/replay. Wires the RA backend's `ReferenceStartCmd`, `ReferenceStopCmd`, `ReplayCmd`, `ReplayStopCmd` to BSV. Once Phase E lands, the RA backend is feature-complete relative to the Mesen-Lua backend, and Phase G can delete `lua/`, `tcp_manager.py`, the Lua-specific commands in `protocol.py`, and rewrite README/ARCHITECTURE.

---

### Critical Files for Implementation

- `python/spinlab/retroarch/orchestrator.py`
- `python/spinlab/retroarch/event_adapter.py`
- `python/spinlab/retroarch/timing.py`
- `python/spinlab/dashboard.py`
- `python/spinlab/config.py`

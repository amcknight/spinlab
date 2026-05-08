# Headless RetroArch Test Harness

## Goal

Port `tests/integration/test_transitions.py` from the in-process Mesen+Lua harness to a headless RetroArch + Python harness. The same `.poke` scenarios drive the same assertions; the difference is what's under test — the production `TransitionDetector` (Python) instead of the Lua reimplementation.

## Why now

Two reasons converging:

1. The 2026-05-08 field-test session surfaced multiple bugs (`status.md`) that unit tests didn't catch. Most are at the seam between memory transitions and event handling — exactly the territory the poke harness covers under Mesen but not under RA. Today, running the integration tests with `backend=retroarch` produces 10 pre-existing failures because the harness still drives Mesen via `TcpManager`. We need an RA-native equivalent before we can call those bugs "tests catch this now."
2. Phase E (BSV replay) was the natural-feeling next step but is sized "large" by `path-to-parity.md`. A poke-driven harness gets us most of the regression-test value at a fraction of the cost — pokes simulate any memory state directly via `WRITE_CORE_RAM`, so we don't need controller-input replay (which is what BSV would buy us). BSV stays valuable for replay-the-feature; it stops being a prerequisite for "tests that catch the kind of bugs we just hit."

## Out of scope

Explicitly deferred to other plans:

- **BSV input recording or replay.** Phase E.
- **`tests/integration/test_replay_fixture.py`.** Depends on `.spinrec` input replay. Already skips when `backend=retroarch`. Phase E ports it.
- **Deleting the Mesen+Lua side** (`lua/poke_engine.lua`, `lua/spinlab.lua`, `TcpManager`, `addresses.py` duplication beyond the source-of-truth swap, the Mesen branch in `conftest.py`). Phase G.
- **Hermetic RA config for runtime.** A future-work follow-on; tests use the user's existing `retroarch.cfg`.
- **Fixing the deferred bugs from `status.md`** (practice doesn't end on goal, overworld phantom checkpoints, stale-segment leak, "cold already captured" mismatch, second-death reload). The harness makes them testable; subsequent plans fix them.

## Architecture

```
tests/integration/
├── ra_harness.py          NEW  RAHarness: launch + lifecycle + NCI client
├── ra_poke_engine.py      NEW  RAPokeEngine: per-frame poke→advance→read loop
├── poke_parser.py         (unchanged)
├── addresses.py           UPDATED  re-export from spinlab.retroarch.addresses
├── conftest.py            UPDATED  run_scenario fixture switches on backend
├── test_transitions.py    (unchanged)
├── test_replay_fixture.py (untouched, still Mesen-only)
└── scenarios/*.poke       (unchanged — 9 scenario files)
```

**Process model.** One RA process per pytest *session* (scope="session"). The fixture launches `retroarch.exe --video=null --audio=null -L <core> <rom>` from `config.rom_path` and `config.emulator.ra_core_path`, waits for NCI ping (1s timeout × 5 retries), confirms the core is running via `is_core_running(tick_addr=...)`, then `pause_toggle()` to halt. RA stays paused for the entire session except during `frame_advance()` calls inside scenarios. Teardown sends `client.quit()` and `Popen.terminate()` as fallback.

**Per-test flow.** No RA reset between tests. The harness:

1. Zeros all `ADDR_MAP` addresses (~11 single-byte writes — option D from brainstorming).
2. Constructs a fresh `TransitionDetector` (clean state).
3. Walks the scenario frame-by-frame, applying held + scheduled pokes, frame-advancing, snapshotting, stepping the detector.
4. Collects emitted events as `dataclasses.asdict(event)` dicts and returns them.

**What's not reset between tests:** RA itself, the loaded ROM, WRAM bytes outside `ADDR_MAP`, RA's internal state. The detector only reads `ADDR_MAP` addresses, so this isolation is sufficient — see "Initial-state strategy" below for the rationale.

## Components

### `ra_harness.py` — RAHarness

Owns RA process lifecycle and the NCI client. Single instance per pytest session.

```python
class RAHarness:
    @classmethod
    def launch(cls, rom_path: Path, core_path: Path, retroarch_exe: Path) -> "RAHarness":
        # Popen RA with null drivers
        # Loop: NCIClient.version() until reply or 5s elapsed
        # Confirm running via is_core_running()
        # pause_toggle()
        # Confirm paused via is_core_running() returning False
        # Return harness with active client + engine

    def teardown(self) -> None:
        # client.quit(); proc.wait(2s); proc.terminate() if alive

    @property
    def engine(self) -> RAPokeEngine: ...
```

If launch fails (no RA exe, no ROM, NCI never replies), the fixture raises and tests skip with a clear message. The skip pattern matches the existing Mesen-test skip-on-missing-binary behavior in `conftest.py`.

### `ra_poke_engine.py` — RAPokeEngine

Runs scenarios. Stateless across scenarios except for the NCI client reference.

```python
class RAPokeEngine:
    def __init__(self, client: NCIClient): ...

    def run_scenario(self, scenario: dict) -> list[dict]:
        # 1. Zero ADDR_MAP addresses
        # 2. Build per-frame poke schedule from scenario["pokes"]
        # 3. Fresh TransitionDetector + held_values dict
        # 4. For frame in 1..(last_poke_frame + settle_frames):
        #      apply scheduled pokes for this frame to held_values
        #      write_ram for each held byte (per-byte, fire-and-forget)
        #      client.frame_advance()
        #      snap = read_snapshot(client)        # acts as implicit barrier
        #      events.extend(detector.step(snap, frame * 16))
        # 5. Return events as list of dicts
```

**Per-byte writes.** Each held address is written as a single-byte UDP packet per frame. Roughly 5-10 packets per frame; cheap on localhost. Matches Lua's per-byte `emu.write` exactly — easier to debug if a value mismatches.

**No explicit sync barrier.** The `read_snapshot` at the end of each frame loop is request/reply, which forces RA to flush all queued commands before replying. Localhost UDP is in-order in practice; this matches the existing `smoke_nci_client.py` round-trip pattern.

**Timestamp.** `frame * 16` (16ms ≈ 60Hz frame). Detector uses timestamps for elapsed-time fields in events; tests assert `elapsed_ms > 0` not exact values, so the exact rate doesn't matter as long as it's monotonically increasing.

### `tests/integration/addresses.py` — source-of-truth swap

Today this file re-implements ADDR_MAP independently of Lua. Under the RA harness it should re-export from `spinlab.retroarch.addresses` so all paths (production poller, harness, and the deferred Mesen tests during the transition) read from the same source. The Mesen-side `lua/addresses.lua` stays separate until Phase G.

```python
# tests/integration/addresses.py (new contents)
from spinlab.retroarch.addresses import (
    ADDR_GAME_MODE as game_mode,
    ADDR_LEVEL_NUM as level_num,
    # ...
)
ADDR_MAP = {
    "game_mode": game_mode,
    "level_num": level_num,
    # ...
}
```

`poke_parser.py` already imports `ADDR_MAP` from this file — no changes there.

### `tests/integration/conftest.py` — backend-aware run_scenario

The existing `run_scenario` fixture targets Mesen. Add a backend gate so it picks the right engine:

```python
@pytest.fixture(scope="session")
def ra_harness(emu_config) -> Iterator[RAHarness]:
    if emu_config.backend != "retroarch":
        pytest.skip("ra_harness requires backend=retroarch")
    harness = RAHarness.launch(
        rom_path=emu_config.rom_path,
        core_path=emu_config.ra_core_path,
        retroarch_exe=emu_config.retroarch_exe,
    )
    try:
        yield harness
    finally:
        harness.teardown()


@pytest.fixture
def run_scenario(emu_config, ra_harness, mesen_run_scenario, request):
    if emu_config.backend == "retroarch":
        def _run(filename: str) -> list[dict]:
            scenario = parse_poke_file(SCENARIOS_DIR / filename)
            return ra_harness.engine.run_scenario(scenario)
        return _run
    else:
        return mesen_run_scenario  # existing behavior
```

Tests are oblivious to the switch — they just call `await run_scenario("...")`.

(Async note: the existing Mesen `run_scenario` is async because Lua-via-TCP is. The RA version can be synchronous, but for fixture-shape compatibility we'll wrap it with `asyncio.to_thread` if test code calls it under `await`. Decided in implementation; the public test signature stays `await run_scenario(...)`.)

## The per-frame loop

```python
held: dict[int, int] = {}
schedule: dict[int, list[Poke]] = group_by_frame(scenario["pokes"])
last_poke_frame = max(schedule, default=0)
end_frame = last_poke_frame + scenario["settle_frames"]
events: list[dict] = []

# Initial-state zeroing (D)
for addr in ADDR_MAP.values():
    client.write_ram(addr, b"\x00")

detector = TransitionDetector()

for frame in range(1, end_frame + 1):
    for poke in schedule.get(frame, []):
        held[poke["addr"]] = poke["value"]
    for addr, value in held.items():
        client.write_ram(addr, bytes([value]))
    client.frame_advance()
    snap = read_snapshot(client)              # implicit barrier
    for ev in detector.step(snap, frame * 16):
        events.append(asdict(ev))

return events
```

## Initial-state strategy (option D)

Before each scenario, the engine writes `0` to every address in `ADDR_MAP` (~11 bytes). RA stays paused, so the writes stick until the scenario's first `frame_advance()`.

**Rationale.** Zeroing only the addresses the detector reads gives deterministic per-scenario state without the cost of `client.reset()` (~1-2s per test) or the overhead of generating, version-controlling, and shipping a savestate file. CPU state, OAM, sprite tables, and the rest of WRAM are irrelevant — the detector is a pure function of `MemorySnapshot`, which is built only from `ADDR_MAP` reads.

**Trade-off.** If a scenario depends on an `ADDR_MAP` address starting at a non-zero value (e.g., to test detector behavior when prev=22), the scenario's first frame must poke that value explicitly. Existing scenarios already do this for every address they care about, so no scenario edits are needed.

**Comparison with Mesen.** Lua's `poke_engine.lua` does NOT zero memory between scenarios; it only resets the schedule and held values. So the RA harness has *strictly more* per-test isolation than Mesen does today.

## Migration story

`test_transitions.py` test bodies don't change. Same 9 `.poke` files in `scenarios/`. Same assertions. The fixture switches engines based on `config.emulator.backend`.

`test_replay_fixture.py` stays Mesen-only and untouched. It will move to BSV in Phase E.

`lua/poke_engine.lua` stays in the tree and stays runnable under `backend=mesen` until Phase G. Useful for parity-checking if a result on RA looks suspicious.

## RetroArch gotchas (from the Phase 0 spike log)

The harness has to live with these:

1. **`cheevos_hardcore_mode_enable` must be `false`.** Otherwise `PAUSE_TOGGLE`, `FRAMEADVANCE`, `SAVE_STATE`, `LOAD_STATE_SLOT` silently no-op. Documented in `retroarch.cfg`; harness can't enforce.
2. **`run_ahead_secondary_instance` must be `true`** if runahead is on. Single-instance runahead corrupts state operations. Probably harmless for `WRITE_CORE_RAM` but follows the same path as `SAVE_STATE`; assume same restriction.
3. **"Deep pause" failure mode.** A specific sequence of `PAUSE_TOGGLE` calls can leave RA in a state where neither `PAUSE_TOGGLE` nor `FRAMEADVANCE` advances frames. Mitigation: the harness only toggles pause once at startup (running → paused) and once at shutdown (well, doesn't bother — `client.quit()` instead). Never blind-toggles. Always confirms state via `is_core_running()` before/after pause-related operations.
4. **`WRITE_CORE_RAM` is fire-and-forget.** No reply means no positive confirmation. Mitigation: the snapshot read at the end of each frame is request/reply; receiving the reply implies all prior commands were processed.
5. **Writes to actively-CPU-touched addresses get clobbered.** This is *the* reason the held-values pattern exists. We re-poke held addresses every frame, identical to Lua's startFrame callback approach.
6. **RA's auto-state-index can drift.** Not relevant here — the harness doesn't use SAVE_STATE.

## Risks and mitigations

- **Risk: NCI command throughput is too low for per-frame poking at speed.** ~10-15 UDP packets per frame at 60Hz target = ~900 packets/sec, plus reads. Localhost UDP can handle this easily; not a concern.
- **Risk: RA process won't launch headless on Windows / null drivers don't compose.** Mitigation: detect at fixture launch, skip the test session with a clear error message rather than hanging. If discovered to be a real problem during implementation, fall back to launching with the default driver and tolerating the window — the tests still work, just with a visible RA window.
- **Risk: a deferred bug from `status.md` causes a scenario to fail under RA when it passed under Mesen.** This is *exactly the desired outcome* — the bugs are real and the harness's job is to surface them. We accept those failures and fix the bugs in subsequent plans. The plan ships when the existing scenarios that *should* pass (per the detector's intended behavior) pass.
- **Risk: `WRITE_CORE_RAM` to specific SMW addresses gets clobbered immediately by ROM CPU activity.** Plausible for the more-actively-used bytes (e.g., `game_mode`, `player_anim`). The held-values re-poke each frame defends against this for normal cases, but if the CPU writes mid-frame *between* our poke and the detector's read, we'd see drift. Mitigation if observed: pause+frame-step is supposed to give us atomicity (we write while paused, RA runs exactly one frame, we read), so this should be fine. If it isn't, investigate; the harness gives us the tools to.

## Definition of done

- [ ] `RAHarness.launch()` brings up RA paused with a working NCI client; teardown is clean.
- [ ] `RAPokeEngine.run_scenario()` runs all 9 existing `.poke` scenarios.
- [ ] `tests/integration/test_transitions.py` passes under `backend=retroarch` for the scenarios whose detector behavior is correct under both backends.
- [ ] Any scenario that fails *only* under RA traces to a real production bug from `status.md` (not to a harness bug). Each such failure gets a `pytest.mark.xfail(reason="bug-tracker-link")` so the suite stays green and the failures stay visible.
- [ ] `tests/integration/addresses.py` re-exports from `spinlab.retroarch.addresses` (single source of truth on the RA side).
- [ ] `pytest -m emulator` runs cleanly under both backends (no crashes, no hangs, RA process exits cleanly).
- [ ] Full `python -m pytest` runs cleanly with the chosen backend.

## Future work

- **Hermetic test config.** Generate a test-only `retroarch.cfg` at fixture launch and point RA at it via `--config`. Removes the dependency on the user's production cfg matching test expectations. Worth applying to runtime too — see Andrew's note from §3 of the brainstorm.
- **Port `test_replay_fixture.py`.** Phase E. Adds BSV-driven scenarios on top of the same harness.
- **Delete the Mesen+Lua side of `conftest.py`.** Phase G.
- **Performance.** A 100-frame scenario with 5 held bytes is ~600 UDP packets. If the suite gets slow, batch held-value writes into a single per-frame command (NCI supports `WRITE_CORE_RAM addr v1 v2 v3 ...`).

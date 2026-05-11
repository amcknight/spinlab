# Integration Tests: RetroArch Poke Harness

These tests exercise the production transition pipeline (`Poller`,
`TransitionDetector`, `ColdFillSpawnDetector`) against a live, headless
RetroArch. The harness writes to SNES WRAM via NCI's `WRITE_CORE_RAM` and
steps frame-by-frame using `FRAMEADVANCE`, so every scenario flows through
the same code paths used in production.

## How it works

```
pytest                         RetroArch (headless, null drivers)
  |                              |
  |  launches (once per session) |
  |----------------------------->| paused, with ROM loaded
  |                              |
  |  NCI on UDP <free_port>      |
  |<============================>|
  |                              |
  |  RAPokeEngine.run_scenario   |
  |  - zero ADDR_MAP             |   per-scenario isolation
  |  - for each frame:           |
  |    1. apply pokes            |
  |    2. FRAMEADVANCE           |   ROM runs 1 frame
  |    3. re-poke held values    |   defeat ROM overwrites
  |    4. READ_CORE_RAM snapshot |   read what detector sees
  |    5. detector.step(snap)    |   produce events
  |                              |
  |  collect events              |
  |  teardown: quit + terminate  |
```

One RetroArch launch per pytest session (session-scoped `ra_harness` /
`ra_harness_love_yourself` fixtures). Each test gets its own scenario run
under a fresh `TransitionDetector`. Tests cooperate on a single RA process,
but each starts from a zeroed WRAM region (within `ADDR_MAP`) so they
don't observe each other's pokes.

## Running

```bash
# All emulator-marked tests (one RA launch, ~10 scenarios + the replay fixture)
pytest -m emulator -v

# One specific test
pytest tests/integration/test_transitions.py::test_entrance_goal -v

# Parser unit tests only (no RetroArch required, instant)
pytest tests/integration/test_poke_parser.py -v
```

Requires RetroArch installed. Paths come from `config.yaml`
(`emulator.retroarch_path`, `emulator.ra_core_path`) or the harness skips.

## RetroArch cfg requirements

The harness reuses the user's existing `retroarch.cfg`. Required settings
(see `docs/retroarch-migration/status.md` for full list):

- `network_cmd_enable = "true"`
- `cheevos_hardcore_mode_enable = "false"` (RA silently drops NCI savestate
  commands when hardcore is on)
- `run_ahead_secondary_instance = "true"` (single-instance runahead
  corrupts save state buffers)

The harness picks a free UDP port per session and writes
`network_cmd_port = "<port>"` into an appendconfig so multiple harnesses
in one session don't fight over `:55355`.

## The `.poke` scenario format

Each scenario is a text file with a single-line header and memory writes
keyed by frame number:

```
# scenario_name — expected_event_1, expected_event_2
settle: 60

0: level_start=0 exit_mode=0 fanfare=0 player_anim=0 io=0 midway=0
1: game_mode=20 level_num=105 room_num=1
2: level_start=1
15: exit_mode=1 fanfare=1
```

**Critical concept: held values.** Once you set an address, the poke
engine re-writes it on every frame until you override it. This is
necessary because the ROM actively writes to memory every frame — a
single-frame poke would be immediately overwritten before the production
poller reads it.

This means scenarios describe **state machines**, not point-in-time pokes:

- Frame 0 sets the baseline (all flags to 0)
- Frame 1 sets context (level number, game mode)
- Frame 2 triggers an entrance by changing `level_start` from 0 to 1
- The 0→1 transition fires `TransitionDetector.step()`
- If you later want `exit_mode` to be 0 again, you must explicitly poke it

### Available addresses

Names are the keys in `tests/integration/addresses.py::ADDR_MAP`, which
imports from `python/spinlab/retroarch/addresses.py`.

| Name | Address | Notes |
|------|---------|-------|
| `game_mode` | `0x0100` | 20 = in level |
| `level_num` | `0x13BF` | **Single byte** (0–255). Use decimal, not 0x105. |
| `room_num` | `0x010B` | Current room/sublevel |
| `level_start` | `0x1935` | 0→1 triggers level entrance |
| `player_anim` | `0x0071` | 9 = death animation |
| `exit_mode` | `0x0DD5` | 0→non-zero triggers level exit |
| `io` | `0x1DFB` | 3=orb, 4=goal, 7=key, 8=fadeout |
| `fanfare` | `0x0906` | 1 = goal reached |
| `boss_defeat` | `0x13C6` | 0→non-zero = boss defeated |
| `midway` | `0x13CE` | 0→1 = checkpoint tape touched |
| `cp_entrance` | `0x1B403` | ASM-style checkpoint entrance |

## Adding a new scenario

1. Create `tests/integration/scenarios/my_test.poke`
2. Single-line header: `# name — expected events`
3. Always start with a frame-0 baseline that zeros all flags you care about
4. Use `settle: 60` (frames after last poke before scenario completes)
5. Add a test function in `test_transitions.py`:

```python
async def test_my_thing(run_scenario):
    events = await run_scenario("my_test.poke")
    exits = [e for e in events if e["event"] == "level_exit"]
    assert len(exits) == 1
```

## Gotchas

### Frame 0 baseline is mandatory

Without it, the ROM's existing memory state creates unpredictable `prev`
values in `TransitionDetector`, so 0→1 transitions may not fire.

### `level_num` is a single byte

`READ_CORE_RAM` returns one byte. Level numbers like `0x105` (261)
overflow — use `105` instead.

### Write-after-frame-advance ordering

`RAPokeEngine` writes held values *after* `FRAMEADVANCE`, not before.
Writing before lets the ROM overwrite our pokes during the frame, causing
the next snapshot to see ROM state instead of poked state. This produced
intermittent failures in `test_orb_exit` (io_port=0 instead of 3) until
the ordering was inverted. Don't flip it back.

### Settle time matters

60 frames gives the poller time to surface all transitions before the
scenario completes. Increase `settle` for scenarios with many transitions.

### Per-harness UDP port allocation

The session-scoped `ra_harness` and `ra_harness_love_yourself` fixtures
both launch their own RA. Each gets a free UDP port via
`_free_udp_port()` in `tests/integration/conftest.py` so the second
process doesn't fail to bind. See `test_harness_isolation.py` for the
regression guard.

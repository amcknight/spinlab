# Integration Tests: Mesen2 Headless Poke Engine

These tests exercise the real Lua-Python IPC boundary by running `spinlab.lua` inside Mesen2's `--testrunner` headless mode and poking SNES memory addresses to trigger transitions.

## How it works

```
pytest                              Mesen2 (headless, max speed)
  |                                   |
  |  launches                         |
  |---------------------------------->| poke_engine.lua
  |                                   |   dofile("spinlab.lua")
  |  TCP connect                      |   TCP server on :15482
  |<=================================>|
  |  <- rom_info                      |
  |  -> game_context                  |
  |  -> poke_scenario (JSON)          |
  |                                   |   writes memory each frame
  |  <- level_entrance                |   spinlab detects transitions
  |  <- level_exit                    |   sends events over TCP
  |                                   |
  |  connection closed                |   emu.stop(0)
  |  assert on collected events       |
```

## Running

```bash
# All integration tests (~7 min, launches Mesen2 per test)
pytest -m integration -v

# One specific test
pytest tests/integration/test_transitions.py::TestEntranceGoal -v

# Parser unit tests only (no Mesen2 needed, instant)
pytest tests/integration/test_poke_parser.py -v
```

Requires Mesen2 installed. Path resolved from `config.yaml` (`emulator.path`) or `MESEN_PATH` env var.

## The `.poke` scenario format

Each scenario is a text file describing memory writes keyed by frame number:

```
# comment
settle: 60

0: level_start=0 exit_mode=0 fanfare=0 player_anim=0 io_port=0 midway=0
1: game_mode=20 level_num=105 room_num=1
2: level_start=1
15: exit_mode=1 fanfare=1
```

**Critical concept: held values.** Once you set an address, it stays at that value on every subsequent frame until you override it. This is necessary because the ROM actively writes to memory every frame — a single-frame poke would be immediately overwritten before `spinlab.lua` reads it.

This means scenarios describe **state machines**, not point-in-time pokes:
- Frame 0 sets the baseline (all flags to 0)
- Frame 1 sets context (level number, game mode)
- Frame 2 triggers an entrance by changing `level_start` from 0 to 1
- The 0->1 transition fires `detect_transitions()` in spinlab.lua
- If you later want `exit_mode` to be 0 again, you must explicitly poke it

### Available addresses

| Name | Address | Notes |
|------|---------|-------|
| `game_mode` | `0x0100` | 20 = in level |
| `level_num` | `0x13BF` | **Single byte** (0-255). Use decimal, not 0x105. |
| `room_num` | `0x010B` | Current room/sublevel |
| `level_start` | `0x1935` | 0->1 triggers level entrance |
| `player_anim` | `0x0071` | 9 = death animation |
| `exit_mode` | `0x0DD5` | 0->non-zero triggers level exit |
| `io_port` | `0x1DFB` | 3=orb, 4=goal, 7=key, 8=fadeout |
| `fanfare` | `0x0906` | 1 = goal reached |
| `boss_defeat` | `0x13C6` | 0->non-zero = boss defeated |
| `midway` | `0x13CE` | 0->1 = checkpoint tape touched |
| `cp_entrance` | `0x1B403` | ASM checkpoint entrance |

## Adding a new scenario

1. Create `tests/integration/scenarios/my_test.poke`
2. Always start with a frame-0 baseline that zeros all flags
3. Use `settle: 60` (frames after last poke before `emu.stop`)
4. Add a test in `test_transitions.py` (or new test file):

```python
async def test_my_thing(self, run_scenario):
    events = await run_scenario("my_test.poke")
    exits = [e for e in events if e["event"] == "level_exit"]
    assert len(exits) == 1
```

## Gotchas

### `level_num` is a single byte
`emu.read` returns one byte. Level numbers like `0x105` (261) overflow — use `105` instead.

### Frame 0 baseline is mandatory
Without it, the ROM's existing memory state creates unpredictable `prev` values in `detect_transitions()`, so 0->1 transitions may not fire.

### Settle time matters
60 frames gives spinlab.lua time to process all transitions and send events over TCP before `emu.stop()` kills the connection. If you add scenarios with many transitions, increase settle.

### TCP TIME_WAIT between tests
Each test launches a new Mesen2 on port 15482. Windows TCP TIME_WAIT lasts ~60s. The conftest adds a 3s cooldown between tests, which usually suffices because `SO_REUSEADDR` is set. If tests fail with connection errors, increase the cooldown or wait and re-run.

### `emu.isKeyPressed` crashes in headless mode
`spinlab.lua` wraps `check_keyboard()` in `pcall` to handle this. If you add other keyboard-dependent code, guard it similarly.

### `poke_handler` must intercept before JSON dispatch
The hook runs at the top of `tcp_dispatch()`, before `handle_json_message()`. This is because `poke_scenario` is a JSON message and would otherwise be consumed by the JSON handler (which doesn't know about it) and silently ignored.

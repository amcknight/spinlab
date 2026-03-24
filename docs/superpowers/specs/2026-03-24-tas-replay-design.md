# Input Recording & Replay for SpinLab

**Date:** 2026-03-24
**Status:** Approved

## Problem

Regenerating reference runs requires manually playing through levels in Mesen2. This is tedious when iterating on segment detection, schema changes, or new features. There's also no way to run end-to-end integration tests without a human at the controller.

## Solution

Add input recording to passive mode and a new replay mode to `spinlab.lua`. Every passive session automatically captures controller inputs. Those recordings can be replayed at any speed to regenerate reference data identically, and short curated recordings serve as integration test fixtures.

## Goals

1. **Reference regeneration**: Replay a recorded run to recreate all segments, save states, and events without human input.
2. **Integration testing**: Short input recordings as version-controlled test fixtures, runnable at max emulation speed.
3. **Zero frame loss**: Recording and replay must not introduce frame-level side effects that alter emulation behavior.

## Non-Goals

- Input editing tools (rewind, frame advance, splicing). This is playback only.
- Movie format interop (`.bk2`, `.smv`, `.lsmv`, `.mmo`). A converter could be added later.
- Performance timing from replayed runs. Replay times are not human performance data.

## File Format: `.spininput`

Compact binary format. The replay file is the contract — any recording method (Lua capture, future `.mmo` converter) produces this format.

### Header (32 bytes)

| Offset | Size | Field |
|--------|------|-------|
| 0 | 4 | Magic bytes: `SINP` |
| 4 | 2 | Version: `1` (uint16 LE) |
| 6 | 16 | Game ID (truncated SHA-256 hex, ASCII, NOT null-terminated) |
| 22 | 4 | Frame count (uint32 LE) |
| 26 | 6 | Reserved (zeroed) |

### Initial State Save

A `.spininput` file is always paired with a companion save state file at the same path with a `.mss` extension (e.g., `run_001.spininput` + `run_001.mss`). The save state captures the emulator state at frame 0 of the recording — before any inputs are applied.

- **During recording**: When passive recording starts, the first `inputPolled` callback also triggers a save state capture via the existing `pending_save` queue. This is the "frame 0" state.
- **During replay**: Before feeding any inputs, Lua loads the companion `.mss` file to restore the exact initial state. Replay begins on the next frame after the load completes.

Without deterministic initial state, replay would desync immediately. The companion save state guarantees identical starting conditions.

### Body

- 2 bytes per frame (uint16 little-endian)
- Bit-to-button mapping (matches SNES joypad register order):

| Bit | Button |
|-----|--------|
| 0   | b      |
| 1   | y      |
| 2   | select |
| 3   | start  |
| 4   | up     |
| 5   | down   |
| 6   | left   |
| 7   | right  |
| 8   | a      |
| 9   | x      |
| 10  | l      |
| 11  | r      |

Bits 12–15 are reserved (zeroed).

### Encode/Decode

`emu.getInput(0)` and `emu.setInput()` both use Lua tables with named boolean fields (`{a=true, b=false, ...}`), not bitmasks. The Lua script must convert between these representations:

- **Encode** (table → uint16): For each button, set the corresponding bit if the field is true. Used during recording.
- **Decode** (uint16 → table): For each bit, set the named field to true/false. Used during replay before calling `emu.setInput(decoded_table, 0)`.

### Size

- 5-minute run at 60fps = 18,000 frames = 36KB
- 10-minute run = 36,000 frames = 72KB
- Negligible for both git (test fixtures) and disk (user data)

## Architecture

### Mode Changes

No new "record" mode. Recording is a transparent addition to passive mode. Replay is a new peer mode:

```
Modes: PASSIVE (now with recording) | PRACTICE | REPLAY
```

### Recording (Passive Mode Addition)

A new `inputPolled` callback is registered alongside the existing `startFrame` callback:

```lua
emu.addEventCallback(on_input_polled, emu.eventType.inputPolled)
```

During passive mode, this callback captures controller state every frame:

```
on_input_polled():
  if mode == PASSIVE and recording then
    if frame_index == 0 then
      pending_input_save = initial_state_path  -- capture frame 0 state
    end
    bitmask = encode(emu.getInput(0))
    append bitmask to input_buffer
    frame_index = frame_index + 1
  end
```

- `input_buffer` is an in-memory Lua table of uint16 values
- Memory footprint: ~70KB for a 10-minute run
- Recording starts when Python sends `reference_start` (new TCP command) and stops on `reference_stop` (new TCP command). Python's `start_reference()` and `stop_reference()` in SessionManager must be extended to send these commands over TCP.
- Python passes the desired `.spininput` path in `reference_start` (e.g., `{"event": "reference_start", "path": "..."}`) so Python controls file naming. Lua constructs the companion `.mss` path by replacing the extension.
- On stop, Lua flushes the buffer to disk with the `.spininput` header and companion `.mss` save state
- Lua sends `input_recorded` event to Python with the file path
- If the reference run is discarded, both the `.spininput` and `.mss` files are discarded with it
- **Disconnect cleanup**: If TCP disconnects mid-recording, Lua resets `input_buffer`, `frame_index`, and `recording` flag. Partial `.mss` files are deleted. No `.spininput` is written for incomplete recordings.
- **Frame 0 save state contention**: The initial state capture uses a dedicated `pending_input_save` variable (separate from the existing `pending_save` used by `detect_transitions`) to avoid conflicts if a level entrance happens on the same frame.

### Replay Mode

State machine:

```
IDLE → REPLAY_LOADING → REPLAYING → IDLE
```

On `replay` TCP command:
1. Read `.spininput` file from disk
2. Validate header: magic bytes, version, game_id matches current ROM
3. Parse body into `replay_frames` array
4. Load companion `.mss` save state via `pending_load` (existing mechanism)
5. Set emulation speed — `emu.setSpeed(speed)` with 0 = max, 100 = normal. **PoC validation item**: confirm exact API signature in Mesen2's Lua environment. Fallback: `emu.setExecutionSpeed()` or keyboard simulation of Mesen2's turbo hotkey.
6. Set `replay_index = 1`
7. Enter REPLAY_LOADING state (waits for save state load to complete in `cpuExec`)
8. After load completes, enter REPLAYING state

On each `inputPolled` callback while REPLAYING:
1. If `replay_index <= #replay_frames`: call `emu.setInput(decode(replay_frames[replay_index]), 0)`, increment
2. If past end: send `replay_finished`, restore speed, return to IDLE

On `replay_stop` TCP command:
- Abort replay, restore speed, return to IDLE

**Preconditions**: `replay` is rejected if practice or reference mode is active. SessionManager checks mode before sending the command. Lua also validates and returns `replay_error` if it cannot enter replay state.

**File validation on load**: Lua checks `(file_size - 32) / 2 == header.frame_count` to detect truncated files early.

**Progress reporting**: Lua checks `os.clock()` on each `inputPolled` during replay and sends `replay_progress` only when ≥100ms has elapsed since the last progress event.

### Passive Detection Runs During Replay

During replay mode, `on_start_frame` runs `detect_transitions()` exactly as in passive mode — it does not know or care who is pressing the buttons. The existing `if practice.active then ... else ... end` branch in `on_start_frame` must be extended:

```lua
if practice.active then
  -- practice mode logic (unchanged)
else
  -- passive and replay both use the same detection path
  detect_transitions()
end
```

Replay and passive share the same `detect_transitions()` path — no special branching needed in `on_start_frame`. The `replay.active` flag is only checked where behavior diverges: `inputPolled` (input injection), `send_event` (source tagging), and TCP command handlers (speed control).

### Source Tagging

All existing events (`level_entrance`, `checkpoint`, `level_exit`, `death`, `spawn`) emitted during replay include `"source": "replay"`. This is implemented via a wrapper around the TCP send function rather than conditionals at each event site:

```lua
local function send_event(event)
  if replay.active then
    event.source = "replay"
  end
  tcp_send(json.encode(event))
end
```

### No Frame Loss Design

Both recording and replay happen in `inputPolled`, which fires exactly once per emulated frame:
- **Recording**: Appending a uint16 to a Lua table is microseconds. No emulation stall.
- **Replay**: `emu.setInput()` is a host-side register write. No emulation stall.
- **Save state capture**: Already handled in `cpuExec` callback via `pending_save` queue. `emu.createSavestate()` is a host-side snapshot, does not consume an emulated frame.
- **TCP sends**: Already non-blocking (`socket:settimeout(0)`).

No operation in the recording or replay path can stall or skip an emulated frame.

## TCP Protocol

### Wire Format

All new commands use JSON objects, consistent with the existing `game_context` pattern. Lua parses them via the existing JSON command handler.

### Commands (Python → Lua)

| Command | Wire format | Purpose |
|---------|-------------|---------|
| `reference_start` | `{"event": "reference_start", "path": "..."}` | Begin passive recording (captures inputs + frame 0 state). Path specifies `.spininput` output location. |
| `reference_stop` | `{"event": "reference_stop"}` | Stop recording, flush `.spininput` + `.mss` to disk |
| `replay` | `{"event": "replay", "path": "...", "speed": 0}` | Load `.spininput` + `.mss` and begin replay |
| `replay_stop` | `{"event": "replay_stop"}` | Abort replay, restore speed |

### Events (Lua → Python)

| Event | Fields | Purpose |
|-------|--------|---------|
| `input_recorded` | `path: string`, `frame_count: number` | Recording flushed to disk |
| `replay_started` | `path: string`, `frame_count: number` | Replay is running |
| `replay_progress` | `frame: number`, `total: number` | Progress update (~every 100ms wall-clock, not frame-based, to avoid flooding at high speed) |
| `replay_finished` | `path: string`, `frames_played: number` | Replay completed |
| `replay_error` | `message: string` | File not found, wrong game_id, desync, etc. |

## Python Side

### SessionManager

Replay is transparent to the reference capture pipeline. `route_event()` handles replay-sourced events identically to human events, with two exceptions:

1. **Elapsed times**: Events with `source: "replay"` do not produce meaningful elapsed times. Python stores frame counts instead (deterministic, comparable between replays). The scheduler ignores replay-sourced segments for difficulty modeling.
2. **`.spininput` association**: On `input_recorded`, Python stores the file path in the DB alongside the reference run, enabling future replay.

### CLI

- `spinlab replay <path>` — Replay a `.spininput` file, generating a reference run
- `spinlab replay --speed 100` — Replay at normal speed (for visual verification)

### Dashboard

- Manage tab: "Replay" button next to reference runs that have an associated `.spininput` file
- Progress bar during replay (driven by `replay_progress` SSE events)

### Integration Tests

- Short `.spininput` fixtures live in `tests/fixtures/inputs/`, version-controlled
- Test harness: launch Mesen2 headless (`--testRunner`), send `replay` at max speed, assert expected segment events and save state files are produced
- `spinlab test-replay` CLI command runs all test fixtures

## Storage

| Recording type | Location | Version controlled |
|----------------|----------|--------------------|
| Full reference runs | `{data_dir}/{game_id}/inputs/{timestamp}.spininput` + `.mss` | No (user data) |
| Test fixtures | `tests/fixtures/inputs/{descriptive_name}.spininput` + `.mss` | Yes (binary; `.mss` ~128-256KB per fixture, LFS optional unless fixtures multiply) |

## Future Extensions (Not In Scope)

- `.mmo` converter: Parse Mesen2 movie files into `.spininput` format as a fallback recording method
- Desync detection: Record memory values at segment boundaries during recording, assert during replay
- Input editing: Simple tool to trim or splice `.spininput` files
- Multi-controller support: Currently player 1 only (port 0)

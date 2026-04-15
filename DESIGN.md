# SpinLab — Design Document

## 1. System Overview

SpinLab turns speedrun practice into a spaced-repetition loop. It has five runtime modes:

**Idle Mode** (default): Lua is passive. Watches SNES memory for section transitions, logs times. Zero overhead — just lightweight memory reads on frame callbacks.

**Reference Mode**: Extension of idle mode during a designated "reference run." On each transition, saves a state file to disk and records controller inputs to a `.spinrec` binary file. Produces segments with save states in the database.

**Replay Mode**: Loads a `.spinrec` recording + companion `.mss` save state, injects recorded inputs via `emu.setInput()` at configurable speed. Segment events fire through `detect_transitions()` tagged with `source: "replay"`.

**Practice Mode**: The core loop. Python orchestrator connects via TCP, tells Lua which state to load. Player completes the section (or dies). Lua detects completion, shows overlay with time and rating prompt. Orchestrator picks next segment via estimator+allocator pipeline. Repeat.

**Fill-gap Mode**: Loads a "hot" save state for a segment so the player can die and capture the missing "cold" (respawn) variant.

---

## 2. Lua Script Operations

The Lua script (`lua/spinlab.lua`) is a single always-on script loaded at Mesen2 startup. It handles all emulator-side logic.

### 2.1 Startup / Initialization

- Load game-specific memory address config (from a Lua table at top of script — `ADDR_*` constants)
- Initialize state tracking variables (current_level, current_room, player_state, timer)
- Register `startFrame` event callback (main loop hook)
- Register `stateLoaded` / `stateSaved` event callbacks (detect manual save/load for passive logging)
- Start TCP server on configurable port (default 15482) using LuaSocket
  - Non-blocking accept: `server:settimeout(0)`
- Set mode to PASSIVE
- Initialize overlay drawing state

### 2.2 Per-Frame Operations (startFrame callback)

This runs every frame (~60fps). Must be lightweight.

```
EVERY FRAME:
  1. Read critical memory addresses:
     - level_number (1-2 bytes)
     - room_id / sublevel (1-2 bytes)
     - player_state (alive/dead/transitioning/goal)
     - goal_type (if at goal)
     - checkpoint_flags
     - frame_counter (for timing)

  2. Detect transitions (via `detect_transitions()`):
     - Level entrance: `level_start` 0→1 (fires `level_entrance` event)
     - Death: `player_anim` transitioned to 9 (fires `death` event)
     - Spawn: `level_start` 0→1 after death flag set (fires `spawn` event)
     - Checkpoint: midway 0→1 or cp_entrance changed (fires `checkpoint` event)
     - Level exit: `exit_mode` 0→non-zero (fires `level_exit` event)
     - Exit order matters: exit detection runs before entrance to prevent spurious re-entries

  3. On any transition:
     IF NOT in practice mode:
       - Optionally log transition event to JSONL (if `JSONL_LOGGING = true`)
       - Send event over TCP via `send_event()` (tagged with `source: "replay"` if replaying)
       - If `game_id` is set, save state files at level entrances, checkpoints, and cold spawns

     IF mode == PRACTICE:
       - `send_event()` is suppressed — practice mode uses its own completion detection
       - IF transition is "section complete" (goal reached):
         - Record completion time
         - Enter RESULT state (substate of practice)
       - IF transition is "death":
         - Record death, increment death counter
         - Reload current segment's save state (retry loop)

  4. IF mode == PRACTICE and state == RESULT:
     - Draw overlay: segment name, goal, your time vs expected, auto-advance timer
     - After `auto_advance_delay_ms`, send `attempt_result` event over TCP
     - Wait for next `practice_load:` command from orchestrator
     - Load state: data = io.open(path, "rb"):read("*a"); emu.loadSavestate(data)
     - Clear overlay, resume play

  5. IF mode == PRACTICE and state == PLAYING:
     - Draw minimal overlay: segment name, end condition, running timer
     - Non-blocking TCP check for abort/skip commands (very cheap)

  6. IF idle (no practice/replay active):
     - Non-blocking TCP accept (check for dashboard connecting)
     - If connection received, send `rom_info` event for game auto-discovery
```

### 2.3 Overlay Drawing

Using Mesen2's `emu.drawString()` / `emu.drawRectangle()`:

**During practice (playing):**
```
┌─────────────────────────────┐
│ Forest of Illusion 2        │
│ End: GOAL                   │
│ ⏱ 12.43                    │
└─────────────────────────────┘
```

**During practice (result — auto-advancing):**
```
┌─────────────────────────────┐
│ Forest of Illusion 2        │
│ End: GOAL                   │
│ Time: 34.21  (exp: 34.20)  │
│ Auto-advance in 1.0s        │
└─────────────────────────────┘
```

**During practice (death — auto-retries):**
```
┌─────────────────────────────┐
│ Forest of Illusion 2  DIED  │
│ Deaths: 2                   │
│ Reloading...                │
└─────────────────────────────┘
```

### 2.4 TCP Server Protocol

Lua hosts a TCP server. Python connects as client. One connection at a time.

### 2.5 File I/O

- **JSONL log** (`{scriptDataFolder}/passive_log.jsonl`): Append-only, one JSON object per line per transition event (only if `JSONL_LOGGING = true`)
- **Save state files** (`{scriptDataFolder}/states/{game_id}/{level}_{room}.mss`, `{level}_cp{n}_hot.mss`, `{level}_cp{n}_cold.mss`): Binary blobs from `emu.createSavestate()`
- **Input recordings** (`data/{game_id}/rec/{run_id}.spinrec`): Binary `.spinrec` files with companion `.mss` save states

---

## 3. IPC Contract

TCP socket, newline-delimited JSON messages. Port 15482 (configurable).

### 3.1 Python → Lua Messages

Messages use `"event"` as the type field. Two formats are supported:

**JSON messages** (start with `{`):
```jsonc
// Set game context (sent after rom_info auto-discovery)
{"event": "game_context", "game_id": "abc123", "game_name": "SMW Hack"}

// Start recording controller inputs during reference run
{"event": "reference_start", "path": "/abs/path/to/run.spinrec"}

// Stop recording
{"event": "reference_stop"}

// Start replay of a .spinrec file
{"event": "replay", "path": "/abs/path/to/run.spinrec", "speed": 0}

// Stop replay
{"event": "replay_stop"}

// Load state for fill-gap mode
{"event": "fill_gap_load", "state_path": "/abs/path/to/state.mss", "message": "Die to capture cold start"}
```

**Text commands** (plain strings, no JSON):
```
ping                                    → pong
save                                    → ok:queued (save test state)
load                                    → ok:queued (load test state)
save:/abs/path.mss                      → ok:queued
load:/abs/path.mss                      → ok:queued
practice_load:{json}                    → ok:queued (load segment for practice)
practice_stop                           → ok
reset                                   → ok (stops practice, queues SNES reset)
quit                                    → bye (closes connection)
```

The `practice_load` JSON payload matches `SegmentCommand.to_dict()`:
```jsonc
{"id": "gameid:105:entrance.0:goal.0",
 "state_path": "/abs/path/to/state.mss",
 "description": "Level description",
 "end_type": "goal",
 "expected_time_ms": 34200,
 "auto_advance_delay_ms": 1000}
```

### 3.2 Lua → Python Messages

**JSON events** (sent via `send_event()`):
```jsonc
// Auto-discovery: sent immediately on TCP connect
{"event": "rom_info", "filename": "hack.sfc"}

// Transition events (sent in idle, reference, and replay modes):
{"event": "level_entrance", "level": 105, "room": 1, "frame": 0, "ts_ms": 12345, "state_path": "/path.mss"}
{"event": "death", "level_num": 105, "timestamp_ms": 12345}
{"event": "spawn", "level_num": 105, "is_cold_cp": true, "cp_ordinal": 1, "state_captured": true, "state_path": "/path.mss"}
{"event": "checkpoint", "level_num": 105, "cp_type": "midway", "cp_ordinal": 1, "state_path": "/path.mss"}
{"event": "level_exit", "level": 105, "room": 1, "goal": "normal", "elapsed_ms": 34210, "frame": 2050, "ts_ms": 46555}

// Replay events fire with "source": "replay" added to transition events
{"event": "replay_started", "path": "/path.spinrec", "frame_count": 12000}
{"event": "replay_progress", "frame": 6000, "total": 12000}
{"event": "replay_finished", "path": "/path.spinrec", "frames_played": 12000}
{"event": "replay_error", "message": "game_id mismatch"}

// Recording saved
{"event": "rec_saved", "path": "/path.spinrec", "frame_count": 12000}

// Practice result (sent after auto-advance delay)
{"event": "attempt_result", "segment_id": "gameid:105:entrance.0:goal.0",
 "completed": true, "time_ms": 34210, "goal": "goal"}
```

**Plain text responses**: `pong`, `ok`, `ok:queued`, `ok:recording`, `ok:stopped`, `ok:replay_stopped`, `bye`, `err:unknown_command`, `err:no_state_path`, `heartbeat`

### 3.3 Connection Lifecycle

1. Lua starts TCP server on init, non-blocking accept
2. Dashboard's `TcpManager` connects (auto-reconnect loop)
3. Lua sends `rom_info` immediately; dashboard responds with `game_context`
4. Dashboard sends `reference_start` / `replay` / `practice_load:` as needed
5. Practice loop: Lua sends `attempt_result` → Python sends next `practice_load:`
6. Reference/replay: Lua sends transition events → Python routes via `SessionManager.route_event()`
7. Heartbeat: Lua sends `heartbeat` every N frames to detect dead connections
8. On disconnect, `SessionManager.on_disconnect()` enters draft mode if segments were captured

### 3.4 SSE Contract

The dashboard uses Server-Sent Events (`GET /api/events`) as the primary update mechanism with polling fallback (`GET /api/state`).

**Endpoint**: `GET /api/events` (text/event-stream)

Each SSE message is a `data:` frame containing the full state JSON (same shape as `GET /api/state`):

```jsonc
{
  "mode": "practice",           // "idle" | "reference" | "practice" | "replay" | "fill_gap"
  "tcp_connected": true,
  "game_id": "abc123def456",
  "game_name": "SMW Hack",
  "current_segment": {          // null if not practicing
    "id": "abc123:105:entrance.0:goal.0",
    "description": "Level name",
    "level_number": 105,
    "state_path": "/path.mss",
    "attempt_count": 3,
    "model_outputs": {          // all estimator outputs for current segment
      "kalman": {"expected_time_ms": 34200, "clean_expected_ms": 30000, ...},
      "model_a": {...},
      "model_b": {...}
    },
    "selected_model": "kalman"
  },
  "queue": [],                  // next 2 segments to practice
  "recent": [],                 // last 8 attempts
  "session": {                  // null if not practicing
    "id": "uuid",
    "started_at": "2026-03-27T...",
    "segments_attempted": 5,
    "segments_completed": 3
  },
  "sections_captured": 0,      // segments captured during reference/replay
  "allocator": "greedy",
  "estimator": "kalman",
  "draft": null,                // or {"run_id": "...", "segments_captured": N}
  "replay": null                // or {"rec_path": "/path.spinrec"} during replay
}
```

Keepalive comments (`: keepalive\n\n`) are sent every 30s when idle. The frontend reconnects automatically on disconnect.

---

### 3.5 Architecture: Python Module Decomposition

The Python backend is decomposed as follows:

- **`SessionManager`** (`session_manager.py`): Central state owner. Owns mode, game context, scheduler reference, practice session. Single `route_event()` entry point dispatches TCP events to handlers. Pushes state updates to SSE subscribers.

- **`ReferenceCapture`** (`reference_capture.py`): Stateful handler for pairing transition events into segments during reference runs and replays. Tracks pending start, segment count, death flag. Extracted from SessionManager.

- **`DraftManager`** (`draft_manager.py`): Manages draft capture run lifecycle (save/discard/recover). Extracted from SessionManager.

- **`TcpManager`** (`tcp_manager.py`): Async TCP client wrapper. Single reader coroutine dispatches events to an `asyncio.Queue`. Both reference capture and practice loop read from the same queue.

- **`Scheduler`** (`scheduler.py`): Wires estimators + allocator together. Runs all registered estimators on each attempt; the active estimator selection only affects which `ModelOutput` the allocator reads.

- **`PracticeSession`** (`practice.py`): Manages the practice loop — picks segments via scheduler, sends `practice_load:` commands, receives `attempt_result` events, logs attempts.

**Mode enum with legal transitions** (`models.py`):
```
IDLE → REFERENCE, PRACTICE, FILL_GAP
REFERENCE → IDLE, REPLAY
PRACTICE → IDLE
REPLAY → IDLE
FILL_GAP → IDLE
```

---

## 4. Database Schema

SQLite. File: `data/spinlab.db`

```sql
-- A game + category combination (auto-discovered from ROM checksums)
CREATE TABLE games (
  id TEXT PRIMARY KEY,          -- truncated SHA-256 of ROM file (16 hex chars)
  name TEXT NOT NULL,           -- derived from ROM filename
  category TEXT NOT NULL,       -- "any%"
  created_at TEXT NOT NULL      -- ISO 8601
);

-- A segment that can be practiced (deterministic ID from game state)
CREATE TABLE segments (
  id TEXT PRIMARY KEY,          -- "{game_id}:{level}:{start_type}.{start_ord}:{end_type}.{end_ord}"
  game_id TEXT NOT NULL REFERENCES games(id),
  level_number INTEGER NOT NULL,
  start_type TEXT NOT NULL,       -- 'entrance', 'checkpoint'
  start_ordinal INTEGER NOT NULL DEFAULT 0,
  end_type TEXT NOT NULL,         -- 'checkpoint', 'goal'
  end_ordinal INTEGER NOT NULL DEFAULT 0,
  description TEXT DEFAULT '',
  strat_version INTEGER DEFAULT 1,
  active INTEGER DEFAULT 1,       -- 0 if archived
  ordinal INTEGER,                -- display/practice order
  reference_id TEXT REFERENCES capture_runs(id),  -- which capture run created this
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Save state variants per segment (cold = respawn, hot = checkpoint moment)
CREATE TABLE segment_variants (
  segment_id TEXT NOT NULL REFERENCES segments(id),
  variant_type TEXT NOT NULL,     -- 'cold', 'hot'
  state_path TEXT NOT NULL,
  is_default INTEGER DEFAULT 0,
  PRIMARY KEY (segment_id, variant_type)
);

-- Every practice attempt
CREATE TABLE attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  segment_id TEXT NOT NULL REFERENCES segments(id),
  session_id TEXT NOT NULL,
  completed INTEGER NOT NULL,
  time_ms INTEGER,
  goal_matched INTEGER,
  rating TEXT,
  strat_version INTEGER NOT NULL,
  source TEXT DEFAULT 'practice',
  deaths INTEGER DEFAULT 0,
  clean_tail_ms INTEGER,          -- time from last death to finish
  created_at TEXT NOT NULL
);

-- Raw transition log (denormalized, append-heavy)
CREATE TABLE transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id TEXT NOT NULL,
  event TEXT NOT NULL,
  level_number INTEGER NOT NULL,
  room_id INTEGER,
  goal_type TEXT,
  timestamp_ms INTEGER NOT NULL,
  session_type TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- Practice sessions for grouping
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  game_id TEXT NOT NULL REFERENCES games(id),
  started_at TEXT NOT NULL,
  ended_at TEXT,
  segments_attempted INTEGER DEFAULT 0,
  segments_completed INTEGER DEFAULT 0
);

-- Per-segment estimator state (multi-estimator: one row per segment per estimator)
CREATE TABLE model_state (
  segment_id TEXT NOT NULL REFERENCES segments(id),
  estimator TEXT NOT NULL,        -- estimator name (e.g. 'kalman', 'model_a', 'model_b')
  state_json TEXT NOT NULL,       -- serialized estimator-specific state
  output_json TEXT NOT NULL DEFAULT '{}',  -- serialized ModelOutput
  updated_at TEXT NOT NULL,
  PRIMARY KEY (segment_id, estimator)
);

-- Persistent allocator/estimator selection
CREATE TABLE allocator_config (
  key TEXT PRIMARY KEY,           -- 'allocator', 'estimator'
  value TEXT
);

-- Reference capture runs (groups of segments from one recording/replay)
CREATE TABLE capture_runs (
  id TEXT PRIMARY KEY,
  game_id TEXT NOT NULL REFERENCES games(id),
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  active INTEGER DEFAULT 0,       -- currently active reference
  draft INTEGER DEFAULT 0         -- pending save/discard
);
```

### Index Strategy

```sql
CREATE INDEX idx_attempts_segment ON attempts(segment_id, created_at);
CREATE INDEX idx_attempts_session ON attempts(session_id);
CREATE INDEX idx_transitions_game ON transitions(game_id, created_at);
```

---

## 5. Scheduler: Estimator + Allocator Pipeline

The scheduler has been decomposed into two pluggable components:

### 5.1 Estimators

Estimators track per-segment performance and produce `ModelOutput` predictions. **All registered estimators run on every attempt**, but only the "active" estimator's output feeds the allocator.

`ModelOutput` fields:
- `expected_time_ms` — E[total_time] for next attempt
- `clean_expected_ms` — E[clean_tail] (time from last death to finish)
- `ms_per_attempt` — improvement rate (positive = improving)
- `floor_estimate_ms` — E[total_time | infinite practice]
- `clean_floor_estimate_ms` — E[clean_tail | infinite practice]

Registered estimators (`python/spinlab/estimators/`):
- **`kalman`** — Kalman filter (default)
- **`model_a`** — Alternative model A
- **`model_b`** — Alternative model B

Estimator interface (`Estimator` ABC):
- `init_state(first_attempt, priors)` — initialize from first completed attempt
- `process_attempt(state, new_attempt, all_attempts)` — update state
- `model_output(state, all_attempts)` — produce `ModelOutput`
- `rebuild_state(attempts)` — replay all attempts to reconstruct state

States are persisted per-segment per-estimator in the `model_state` table. The dashboard's model tab shows all estimators side-by-side.

### 5.2 Allocators

Allocators pick the next segment to practice from the list of `SegmentWithModel` objects.

Registered allocators (`python/spinlab/allocators/`):
- **`greedy`** — picks the segment with highest expected improvement (default)
- **`round_robin`** — cycles through segments in order
- **`random`** — random selection

Allocator interface (`Allocator` ABC):
- `pick_next(segment_states)` — pick next segment_id
- `peek_next_n(segment_states, n)` — preview next N without side effects

### 5.3 Plugin Registries

Both estimators and allocators use decorator-based registries:
```python
from spinlab.estimators import register_estimator, get_estimator, list_estimators
from spinlab.allocators import register_allocator, get_allocator, list_allocators
```

The active estimator and allocator are persisted in the `allocator_config` table and can be switched at runtime via the dashboard API (`POST /api/estimator`, `POST /api/allocator`).

### 5.4 Strat Changes

When the player resets a strat for a segment:

```python
def reset_strat(segment_id):
    db.increment_strat_version(segment_id)
    # Historical attempts are preserved but tagged with old strat_version
```

---

## 6. Reference Capture & Draft Lifecycle

Reference captures are now managed through a database-backed flow with draft/save/discard semantics, replacing the old YAML manifest approach.

### 6.1 Capture Flow

1. User clicks "Start Reference" → `reference_start` sent to Lua with `.spinrec` path
2. Lua records controller inputs and saves states at level entrances, checkpoints, and spawns
3. As transition events arrive, `ReferenceCapture` pairs them into segments and writes to DB
4. User clicks "Stop Reference" → `reference_stop` sent; segments enter draft state
5. User saves or discards the draft via the dashboard

### 6.2 Replay Capture

Same segment creation flow as reference, but driven by replaying a `.spinrec` file:
1. User clicks "Replay" → `replay` sent to Lua with path and speed
2. Lua injects recorded inputs; transition events fire naturally with `source: "replay"`
3. On `replay_finished`, captured segments enter draft state

### 6.3 Draft Manager (`DraftManager`)

After a reference or replay capture, segments are in draft state (`capture_runs.draft = 1`):
- **Save**: promote draft to saved reference (`draft = 0`), set as active
- **Discard**: hard-delete the capture run and all associated segments/variants
- **Recovery**: on startup, checks for orphaned drafts and restores draft state

---

## 7. Configuration

```yaml
# config.yaml
emulator:
  path: "C:/path/to/Mesen2.exe"  # or just "mesen" if on PATH
  lua_script: "lua/spinlab.lua"

rom:
  dir: "C:/roms"  # directory containing ROM files (listed in dashboard)

data:
  dir: "data"  # relative to project root
```

Memory addresses are hardcoded in the Lua script's CONFIG section (`ADDR_*` constants), not in config.yaml. Game discovery is automatic from ROM checksums — no manual `game.id` configuration needed.

---

## 8. Build Order

### Step 0 — Launch Harness [15 min]

Create `scripts/launch.sh` (and `.bat` for Windows) that starts Mesen2 with the Lua script auto-loaded. Verify the Lua script loads and prints a message. Establish that you never manually load scripts.

### Step 1 — Save State Proof of Concept [30-60 min]

**Goal**: Validate that we can programmatically save and load state files from Lua.

Minimal Lua script:
1. On startup, register `startFrame` callback
2. On a keyboard press (e.g., F5 via `emu.isKeyPressed()`):
   - `data = emu.saveSavestate()`
   - Write data to `test_state.mss` via `io.open`
   - Print confirmation
3. On another key (e.g., F6):
   - Read `test_state.mss` via `io.open`
   - `emu.loadSavestate(data)`
   - Print confirmation
4. Verify the emulator jumps back to the saved state

Then extend: write a tiny Python script that connects via TCP to a Lua TCP server, sends a "load this file" command, and the Lua script loads it. This validates the full IPC path.

**This is the critical risk mitigation step.** If this doesn't work cleanly, we need to investigate alternatives before building anything else.

### Step 2 — Passive Recorder [2-4 hours]

Port memory addresses from kaizosplits into the Lua script. Implement transition detection logic. Log events to JSONL. Play through a few levels and verify the log is accurate.

Deliverables:
- Lua config section with memory addresses
- Transition detection (level entrance, checkpoint, death, spawn, goal)
- JSONL logging with timestamps
- Segment timing (start → goal)

### Step 3 — Reference Capture [1-2 hours]

Extend the passive recorder: on each transition, also save a state file and record controller inputs to `.spinrec`. Build `ReferenceCapture` to pair transitions into segments with save state variants.

Deliverables:
- Save state capture on transitions
- `.spinrec` input recording
- Segments created in database with cold/hot variants

### Step 4 — Practice Loop MVP [4-8 hours]

The big integration step. Build:
- Lua practice mode (state loading, overlay, auto-advance)
- Python orchestrator/practice session (TCP client, segment selection)
- SQLite setup (`db.py`, schema creation)
- Attempt logging with deaths and clean_tail_ms

Deliverables:
- Can start a practice session from dashboard
- Lua loads states, detects completion, shows overlay
- Auto-advance after configurable delay, next state loads automatically
- All attempts logged to SQLite

### Step 5 — Estimator/Allocator Pipeline [2-3 hours]

Build pluggable scheduler with estimator + allocator registries:
- `scheduler.py` wiring all estimators and active allocator
- Kalman filter estimator (default), model_a, model_b
- Greedy allocator (default), round-robin, random
- Model state persisted per-segment per-estimator in `model_state` table

### Step 6 — Dashboard & Polish [ongoing]

- FastAPI dashboard with SSE-based live updates
- Reference management (capture, replay, draft lifecycle)
- Model tab showing all estimators side-by-side
- Segment editing, fill-gap mode for missing cold variants
- Multiple game support (auto-discovery from ROM checksums)
- Emulator launch from dashboard

---

## 9. Open Questions

1. **Multi-exit detection**: For romhacks with custom ASM, the standard goal_type memory address might not capture all exit types. May need per-game hooks.

2. **Overlay positioning**: Need to avoid covering important gameplay elements. The overlay should be configurable (top/bottom, opacity, font size) or auto-positioned based on player position on screen.

3. **SNES9X-rr support**: The Lua API surface is small enough to abstract. Would require file-based IPC instead of TCP. Low priority given Mesen2 works well.

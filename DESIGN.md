# SpinLab — Design Document

## 1. System Overview

SpinLab turns speedrun practice into a spaced-repetition loop. It has three runtime modes:

**Passive Mode**: Always on during normal play. Watches SNES memory for section transitions, logs times to a JSONL file. Zero overhead — just lightweight memory reads on frame callbacks. Real-run data feeds into practice scheduling.

**Capture Mode**: Extension of passive mode during a designated "reference run." On each transition, also saves a state file to disk. Produces a manifest mapping split IDs to state files and reference times.

**Practice Mode**: The core loop. Python orchestrator connects via TCP, tells Lua which state to load. Player completes the section (or dies). Lua detects completion, shows overlay with time and rating prompt. Player rates via L+D-pad. Orchestrator picks next split. Repeat.

---

## 2. Lua Script Operations

The Lua script (`lua/split_tank.lua`) is a single always-on script loaded at Mesen2 startup. It handles all emulator-side logic.

### 2.1 Startup / Initialization

- Load game-specific memory address config (from a Lua table at top of script, or a separate `addresses.lua` file)
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

  2. Detect transitions:
     - Level start: level_number changed AND player_state == alive
     - Room change: room_id changed within same level
     - Death: player_state transitioned to dead
     - Goal reached: player_state == goal (check goal_type for normal/secret)
     - CP reached: checkpoint_flag newly set

  3. On any transition:
     IF mode == PASSIVE or mode == CAPTURE:
       - Log transition event to JSONL (append to file)
       - IF mode == CAPTURE:
         - Save state: data = emu.saveSavestate()
         - Write to file: io.open(path, "wb"):write(data)
         - Log state file path in capture manifest

     IF mode == PRACTICE:
       - IF transition is "section complete" (goal reached or next-split-start):
         - Record completion time
         - Enter RATING state (sublstate of practice)
       - IF transition is "death":
         - Record death
         - Enter RATING state with completed=false

  4. IF mode == PRACTICE and state == RATING:
     - Draw overlay: split name, goal, your time vs reference, rating prompt
     - Read controller via joypad/input:
       L + Left  = "again"  (needs much more practice)
       L + Down  = "hard"   (keep at current frequency)
       L + Up    = "good"   (can space out more)
       L + Right = "easy"   (push way back)
       R         = skip (no rating, just next)
       Select+Start = exit practice mode
     - On rating received:
       - Send result to orchestrator via TCP
       - Wait for next command from orchestrator
       - Load state: data = io.open(path, "rb"):read("*a"); emu.loadSavestate(data)
       - Clear overlay, resume play

  5. IF mode == PRACTICE and state == PLAYING:
     - Draw minimal overlay: split name, goal variant, running timer
     - Non-blocking TCP check for abort/skip commands (very cheap)

  6. IF mode == PASSIVE:
     - Non-blocking TCP accept (check for orchestrator connecting)
     - If connection received and handshake valid, switch to PRACTICE mode
```

### 2.3 Overlay Drawing

Using Mesen2's `emu.drawString()` / `emu.drawRectangle()`:

**During practice (playing):**
```
┌─────────────────────────────┐
│ Forest of Illusion 2        │
│ Goal: SECRET EXIT           │
│ ⏱ 12.43                    │
└─────────────────────────────┘
```

**During practice (rating):**
```
┌─────────────────────────────┐
│ Forest of Illusion 2        │
│ Goal: SECRET EXIT           │
│ Time: 34.21  (ref: 34.20)  │
│                             │
│ L+←Again  L+↓Hard          │
│ L+↑Good   L+→Easy   R=Skip │
└─────────────────────────────┘
```

**During practice (death):**
```
┌─────────────────────────────┐
│ Forest of Illusion 2  DIED  │
│ Goal: SECRET EXIT           │
│                             │
│ L+←Again  L+↓Hard          │
│ L+↑Good   L+→Easy   R=Skip │
└─────────────────────────────┘
```

### 2.4 TCP Server Protocol

Lua hosts a TCP server. Python connects as client. One connection at a time.

### 2.5 File I/O

- **JSONL log** (`data/passive_log.jsonl`): Append-only, one JSON object per line per transition event
- **Save state files** (`data/states/{split_id}.mss`): Binary blobs from `emu.saveSavestate()`
- **Capture manifest** (`data/captures/{timestamp}_manifest.yaml`): Generated during reference runs

---

## 3. IPC Contract

TCP socket, newline-delimited JSON messages. Port 15482 (configurable).

### 3.1 Python → Lua Messages

```jsonc
// Handshake (first message after connect)
{"type": "hello", "version": 1}

// Enter practice mode with first split
{"type": "practice_start", "split": {
  "id": "foi2-secret",
  "state_path": "/abs/path/to/foi2_start.mss",
  "goal": "secret_exit",
  "description": "Take pipe at CP, keyhole in hidden room",
  "reference_time_ms": 34200
}}

// Load next split (sent after receiving rating result)
{"type": "load_split", "split": {
  "id": "vanilla_dome_2-normal",
  "state_path": "/abs/path/to/vd2_start.mss",
  "goal": "normal_exit",
  "description": "",
  "reference_time_ms": 28100
}}

// Abort current practice
{"type": "practice_stop"}

// Request current status
{"type": "status"}
```

### 3.2 Lua → Python Messages

```jsonc
// Handshake response
{"type": "hello_ack", "game": "smw_cod", "emulator": "mesen2"}

// Section completed
{"type": "split_result", "split_id": "foi2-secret",
 "completed": true, "time_ms": 34210,
 "goal_matched": true,  // did they take the right exit?
 "death_count": 0}

// Player rated the split
{"type": "rating", "split_id": "foi2-secret", "rating": "good"}
// rating is one of: "again", "hard", "good", "easy", "skip"

// Practice mode exited by player (Select+Start)
{"type": "practice_exit"}

// Status response
{"type": "status_ack", "mode": "passive", "current_level": 105,
 "session_splits_completed": 0}

// Passive mode event (logged to JSONL too, but also sent over TCP if connected)
{"type": "transition", "event": "level_start", "level": 105,
 "room": 1, "timestamp_ms": 1234567890}
```

### 3.3 Connection Lifecycle

1. Lua starts TCP server on init, non-blocking accept
2. Python orchestrator connects when user starts a practice session
3. Python sends `hello`, Lua responds `hello_ack`
4. Python sends `practice_start` with first split
5. Loop: Lua sends `split_result` + `rating` → Python sends `load_split`
6. Session ends via `practice_stop` (from Python) or `practice_exit` (from Lua/player)
7. TCP connection closes, Lua reverts to passive mode

---

## 4. Database Schema

SQLite. File: `data/split_tank.db`

```sql
-- A game + category combination
CREATE TABLE games (
  id TEXT PRIMARY KEY,          -- e.g. "smw_cod"
  name TEXT NOT NULL,           -- "SMW: City of Dreams"
  category TEXT NOT NULL,       -- "any%"
  created_at TEXT NOT NULL      -- ISO 8601
);

-- A section/split that can be practiced
CREATE TABLE splits (
  id TEXT PRIMARY KEY,          -- deterministic from game state: "{game_id}:{level}:{room}:{goal}"
  game_id TEXT NOT NULL REFERENCES games(id),
  level_number INTEGER NOT NULL,
  room_id INTEGER,
  goal TEXT NOT NULL,            -- "normal_exit", "secret_exit", "checkpoint", etc.
  description TEXT DEFAULT '',   -- human notes, displayed on overlay
  state_path TEXT,               -- path to .mss save state file (null if not yet captured)
  reference_time_ms INTEGER,     -- from reference run
  strat_version INTEGER DEFAULT 1,  -- incremented on strat change
  active INTEGER DEFAULT 1,      -- 0 if archived (removed from reference)
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Spaced repetition schedule state per split
CREATE TABLE schedule (
  split_id TEXT PRIMARY KEY REFERENCES splits(id),
  ease_factor REAL DEFAULT 2.5,    -- SM-2 ease factor, min 1.3
  interval_minutes REAL DEFAULT 5, -- adapted from SM-2 days to minutes
  repetitions INTEGER DEFAULT 0,   -- consecutive successful reviews
  next_review TEXT NOT NULL,       -- ISO 8601 datetime
  updated_at TEXT NOT NULL
);

-- Every practice attempt
CREATE TABLE attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  split_id TEXT NOT NULL REFERENCES splits(id),
  session_id TEXT NOT NULL,        -- groups attempts within a practice session
  completed INTEGER NOT NULL,      -- 0 or 1
  time_ms INTEGER,                 -- null if died before completion
  goal_matched INTEGER,            -- did they take the correct exit?
  rating TEXT,                     -- "again"/"hard"/"good"/"easy"/"skip"/null
  strat_version INTEGER NOT NULL,  -- snapshot of split's strat_version at time of attempt
  source TEXT DEFAULT 'practice',  -- "practice" or "passive" (from real runs)
  created_at TEXT NOT NULL
);

-- Raw transition log from passive mode (denormalized, append-heavy)
CREATE TABLE transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id TEXT NOT NULL,
  event TEXT NOT NULL,              -- "level_start", "room_change", "death", "goal", "cp"
  level_number INTEGER NOT NULL,
  room_id INTEGER,
  goal_type TEXT,
  timestamp_ms INTEGER NOT NULL,   -- emulator frame-based timestamp
  session_type TEXT NOT NULL,       -- "real_run" or "practice"
  created_at TEXT NOT NULL
);

-- Practice sessions for grouping
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,              -- UUID
  game_id TEXT NOT NULL REFERENCES games(id),
  started_at TEXT NOT NULL,
  ended_at TEXT,
  splits_attempted INTEGER DEFAULT 0,
  splits_completed INTEGER DEFAULT 0
);
```

### Index Strategy

```sql
CREATE INDEX idx_attempts_split ON attempts(split_id, created_at);
CREATE INDEX idx_attempts_session ON attempts(session_id);
CREATE INDEX idx_schedule_next ON schedule(next_review);
CREATE INDEX idx_transitions_game ON transitions(game_id, created_at);
```

---

## 5. Scheduler: Adapted SM-2

### 5.1 How SM-2 Works

SM-2 tracks three variables per item: **ease factor** (EF, starts 2.5, min 1.3), **interval** (time until next review), and **repetitions** (consecutive successes). After each review, the user gives a quality rating 0-5.

The core formula:
- If quality < 3 (failure): reset repetitions to 0, interval = first step
- If quality >= 3 (success):
  - First review: interval = base interval
  - Second review: interval = base × 6
  - Subsequent: interval = previous_interval × ease_factor
- EF adjustment: `EF' = EF + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))`

### 5.2 Our Adaptation: 4 Buttons → Quality Score

We map 4 controller buttons to SM-2 quality scores:

| Button    | Meaning                        | SM-2 Quality | EF Change  |
|-----------|--------------------------------|-------------|------------|
| L+Left    | **Again** — need much more     | 1           | -0.30, reset reps |
| L+Down    | **Hard** — keep current freq   | 3           | -0.14      |
| L+Up      | **Good** — can space out       | 4           | 0.00       |
| L+Right   | **Easy** — push way back       | 5           | +0.10      |

This matches Anki's 4-button system (Again/Hard/Good/Easy).

### 5.3 Time Scale: Minutes, Not Days

SM-2 was designed for daily review over weeks. We're in a single 60-90 minute session. So we remap:

- SM-2 "1 day" → our "5 minutes" (configurable base interval)
- SM-2 "6 days" → our "30 minutes"
- The ease factor multiplier works the same way
- `next_review` is a datetime; during a session, we just pick from splits where `next_review <= now`

Between sessions: if you come back tomorrow, splits that were due "30 minutes from now" in yesterday's session are now overdue and get prioritized. This naturally handles multi-session learning.

### 5.4 Selection Algorithm

When the orchestrator needs the next split:

```python
def pick_next_split(db) -> Split:
    now = datetime.utcnow()

    # 1. Get all due splits (next_review <= now), ordered by most overdue first
    due = db.get_due_splits(now)

    if due:
        # Pick the most overdue one
        return due[0]

    # 2. If nothing is due, find the split that will be due soonest
    upcoming = db.get_next_due_split()
    if upcoming:
        # Optionally wait, or just serve it early
        return upcoming

    # 3. If everything has been reviewed and nothing is coming up,
    #    pick the split with the worst historical performance
    return db.get_worst_performing_split()
```

### 5.5 Strat Changes

When the player resets a strat for a split:

```python
def reset_strat(split_id):
    # Increment strat version on the split
    db.increment_strat_version(split_id)

    # Reset schedule to "new card" state
    db.update_schedule(split_id,
        ease_factor=2.5,
        interval_minutes=5,
        repetitions=0,
        next_review=now
    )
    # Historical attempts are preserved but tagged with old strat_version
```

### 5.6 Incorporating Passive Data

When passive mode records a real-run completion of a section, the orchestrator can optionally treat it as a practice attempt with an auto-derived rating based on time vs reference:

```python
def auto_rate_from_passive(time_ms, reference_ms, completed):
    if not completed:
        return "again"  # death
    ratio = time_ms / reference_ms
    if ratio <= 1.0:
        return "easy"   # at or better than reference
    elif ratio <= 1.15:
        return "good"
    elif ratio <= 1.3:
        return "hard"
    else:
        return "again"  # way too slow
```

This is optional and configurable. The player might not want real-run data to affect practice scheduling (different pressure, different context).

### 5.7 Future: VoI-Style Optimizer

The scheduler interface is designed so that `pick_next_split()` can be replaced with a more sophisticated optimizer later. The key data for a VoI approach:

- **Expected time saved per minute of practice** = f(current_failure_rate, time_variance, time_vs_reference, section_weight_in_run)
- Section weight = how much of the total run time this section represents
- Practice has diminishing returns per session (fatigue, tilt)

The `attempts` table logs everything needed to compute these. The scheduler module is deliberately isolated to make swapping algorithms easy.

---

## 6. Manifest Format

Generated during reference capture, manually editable after.

```yaml
# data/captures/2026-03-15_cod_any_percent.yaml
game_id: smw_cod
category: any%
captured_at: "2026-03-15T14:30:00Z"
emulator: mesen2
rom_hash: abc123def456  # for integrity checking

splits:
  - id: "smw_cod:105:1:normal_exit"
    level_number: 105
    room_id: 1
    goal: normal_exit
    description: "Yoshi's Island 2 — standard clear"
    state_path: "states/smw_cod_105_1_normal.mss"
    reference_time_ms: 28100

  - id: "smw_cod:106:1:secret_exit"
    level_number: 106
    room_id: 1
    goal: secret_exit
    description: "Donut Plains 1 — cape flight to key"
    state_path: "states/smw_cod_106_1_secret.mss"
    reference_time_ms: 34200

  - id: "smw_cod:106:1:normal_exit"
    level_number: 106
    room_id: 1
    goal: normal_exit
    description: "Donut Plains 1 — standard orb finish"
    state_path: "states/smw_cod_106_1_normal.mss"
    reference_time_ms: 28100
    # Note: same level_number and room as above, different goal
```

### Reference Run Diffing

When re-capturing a reference run:

1. Generate new manifest from the new capture
2. Diff split IDs between old and new manifests:
   - **New splits** (in new, not in old): Add to DB with fresh schedule
   - **Removed splits** (in old, not in new): Set `active=0`, keep history
   - **Unchanged splits** (same ID in both): Update `reference_time_ms` and `state_path`, keep practice history and schedule
3. The diff is shown to the user for confirmation before applying

---

## 7. Configuration

```yaml
# config.yaml
emulator:
  path: "C:/path/to/Mesen2.exe"  # or just "mesen" if on PATH
  type: mesen2  # or snes9x_rr for fallback
  lua_script: "lua/split_tank.lua"

rom:
  path: "C:/roms/smw_cod.smc"

game:
  id: smw_cod
  name: "SMW: City of Dreams"
  category: "any%"

network:
  port: 15482
  host: "127.0.0.1"

scheduler:
  algorithm: sm2  # future: voi
  base_interval_minutes: 5
  auto_rate_passive: false  # auto-create ratings from real-run data

data:
  dir: "data"  # relative to project root

# Game-specific memory addresses (can also be in a separate file)
memory_addresses:
  level_number: {address: 0x0013BF, size: 2}
  room_id: {address: 0x00141A, size: 1}
  player_state: {address: 0x000071, size: 1}
  goal_type: {address: 0x001493, size: 1}
  # ... more addresses from kaizosplits
```

---

## 8. Build Order

### Step 0 — Launch Harness [15 min]

Create `scripts/launch.sh` (and `.bat` for Windows) that starts Mesen2 with:
```bash
mesen --lua lua/split_tank.lua --rom "$ROM_PATH"
```
Verify the Lua script loads and prints a message. Establish that you never manually load scripts.

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
- Transition detection (level start, room change, death, goal)
- JSONL logging with timestamps
- Split timing (start → goal)

### Step 3 — Reference Capture [1-2 hours]

Extend the passive recorder: on each transition, also save a state file. After the run, a Python script processes the JSONL + state files into a YAML manifest.

Deliverables:
- Save state capture on transitions
- Python `capture.py` that generates manifest YAML
- A complete manifest from one reference run

### Step 4 — Practice Loop MVP [4-8 hours]

The big integration step. Build:
- Lua practice mode (state loading, overlay, controller rating input)
- Python orchestrator (TCP client, round-robin split selection)
- SQLite setup (`db.py`, schema creation)
- Attempt logging

Deliverables:
- Can start a practice session from Python CLI
- Lua loads states, detects completion, shows overlay
- Player rates via controller, next state loads automatically
- All attempts logged to SQLite

### Step 5 — SM-2 Scheduling [2-3 hours]

Replace round-robin with SM-2 adapted scheduler:
- `scheduler.py` implementing the adapted SM-2 algorithm
- Schedule table populated on first session
- `pick_next_split()` respects intervals and ease factors
- Strat reset command in CLI

### Step 6 — Polish [ongoing]

- TUI with `rich`/`textual`: session stats, split history, improvement curves
- Reference run diffing (re-capture without losing practice data)
- Passive data integration (optional auto-rating from real runs)
- Multiple game/category support
- Session summary on exit (splits practiced, time spent, ratings distribution)
- Edge cases: what if the player soft-resets? What if they pause for 10 minutes?

---

## 9. Open Questions

1. **Mesen2 command-line Lua loading**: Need to verify exact CLI syntax. May need `--script` instead of `--lua`. Check Mesen2 docs or source.

2. **Save state size**: SMW states in Mesen2 are probably 100-200KB each. A full romhack might have 50-100 splits. That's 5-20MB total — no concern.

3. **TCP latency in Lua**: LuaSocket's non-blocking receive in a `startFrame` callback adds microseconds of overhead. Not a concern. But need to verify `settimeout(0)` works correctly in Mesen2's Lua environment.

4. **SNES9X-rr save state file format**: If we want to support SNES9X as an alternative, we need to understand whether its state files are saved to disk in a predictable location when using `savestate.create()` + `savestate.save()`.

5. **Multi-exit detection**: For romhacks with custom ASM, the standard goal_type memory address might not capture all exit types. May need per-game hooks. The config system should support this.

6. **Timer accuracy**: Frame-counting gives 1/60th second precision (~16.7ms). That's probably fine, but for very short sections, we might want sub-frame timing. Check if Mesen2 exposes cycle counts.

7. **Overlay positioning**: Need to avoid covering important gameplay elements. The overlay should be configurable (top/bottom, opacity, font size) or auto-positioned based on player position on screen.

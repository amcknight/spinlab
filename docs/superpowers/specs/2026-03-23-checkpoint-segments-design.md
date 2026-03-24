# Checkpoint Segments Design Spec

## Problem

SpinLab currently models practice as entrance→goal splits — one split per level. But levels with checkpoints have natural sub-sections that deserve independent practice. When you die in a real run, you respawn at the checkpoint, not the entrance. Practicing from checkpoint starts is essential for building consistency where it matters most.

## Core Concept: Segments

Replace the "split" model with a generalized **segment** model. A segment is a `(start_event, end_event)` pair within a level, with one or more associated save states (variants) for the start condition.

### Segment Types

| Segment             | start_type   | start_ordinal | end_type     | end_ordinal |
|---------------------|-------------|---------------|-------------|-------------|
| Entrance → CP1      | `entrance`  | 0             | `checkpoint`| 1           |
| CP1 → CP2           | `checkpoint`| 1             | `checkpoint`| 2           |
| CP1 → Goal          | `checkpoint`| 1             | `goal`      | 0           |
| Entrance → Goal     | `entrance`  | 0             | `goal`      | 0           |

Full-level (entrance→goal) segments are only created when a level has no checkpoints. Segment unions (combining sub-sections into full-level practice) are deferred.

### Segment Identity

```
{game_id}:{level_number}:{start_type}.{start_ordinal}:{end_type}.{end_ordinal}
```

Example: `a1b2c3d4:105:entrance.0:checkpoint.1`

Ordinals are assigned sequentially during a reference run. This works for linear romhacks (the initial target). Nonlinear routing with secrets, start-select, etc. may need a richer identity scheme later.

### Start Variants

A segment can have multiple save states representing different starting conditions:

- **Cold start**: Fresh level entry (for entrance segments) or respawn after death (for CP segments). This is the most common real-run scenario.
- **Hot start**: Arriving at a checkpoint mid-run without dying. Captured during reference runs. Useful when momentum, sprite spawns, or other game state matters.

Cold is the default variant for practice. Hot is captured automatically during reference runs but treated as a bonus for v1.

The variant model is extensible — powerup state, speed, etc. could become variant types later, but v1 only implements cold/hot.

## Memory Detection (Lua)

### New Memory Watches

Ported from kaizosplits:

| Address   | Name          | Type | Purpose |
|-----------|--------------|------|---------|
| `$13CE`   | midway       | byte | Tape-style checkpoint, detect 0→1 |
| `$1B403`  | cpEntrance   | byte | ASM-style checkpoint, detect value shift excluding initial room |
| `$0071`   | playerAnimation | byte | Already partially watched; detect →9 for death |

### Detection Logic

Composite conditions (matching kaizosplits Watchers.cs):

```
Midway      = StepTo(midway, 1) && !GotOrb && !GotGoal && !GotKey && !GotFadeout
CPEntrance  = Shifted(cpEntrance) && !ShiftTo(cpEntrance, firstRoom) && !GotOrb && !GotGoal && !GotKey && !GotFadeout
CP          = Midway || CPEntrance
DiedNow     = ShiftTo(playerAnimation, 9)
Put         = GmPrepareLevel && !died      -- fresh entry, no prior death
Spawn       = GmPrepareLevel && died       -- respawn after death
```

### State Tracking

- `died` flag: sticky, set on `DiedNow`, cleared on `Spawn`
- `firstRoom`: the initial value of `$1B403` (cpEntrance) at level entry — NOT `$010B` (roomNum). Set when `levelNum` shifts, cleared on `CP`. Used by `CPEntrance` condition to filter out the initial entrance as a false positive.
- `cp_acquired`: set on `CP` event, tracks whether current level has a new checkpoint without a cold start recorded yet. Used for option-3 spawn capture logic.
- `cp_ordinal`: per-level counter, initialized to 0 at level entrance (`Put`); incremented to 1 on first CP, 2 on second CP, etc. If the player backtracks and re-hits the same checkpoint, the ordinal does NOT increment — only new CPs increment it. (In linear romhacks this is straightforward; nonlinear cases are deferred.)

### Save State Capture Rules

During reference runs:
1. **Level entrance** (`Put`): always capture (existing behavior)
2. **Checkpoint touch** (`CP`): always capture (hot variant)
3. **First spawn after new CP** (`Spawn` where `cp_acquired` and no cold variant exists): capture (cold variant)
4. **Goal**: no save state needed, used for timing/segment-end detection

This is "option 3" — only capture a cold spawn state when a new CP was acquired that doesn't already have one. Avoids cluttering save state directory with repeated death respawns.

## TCP Event Protocol

### New Events (Lua → Python)

**checkpoint:**
```json
{"event": "checkpoint", "level_num": 105, "cp_type": "midway", "cp_ordinal": 1, "timestamp_ms": 12345}
```
Fired on `Midway || CPEntrance`. Save state captured and written to disk.

**death:**
```json
{"event": "death", "level_num": 105, "timestamp_ms": 12346}
```
Fired on playerAnimation→9. No save state.

**spawn:**
```json
{"event": "spawn", "level_num": 105, "is_cold_cp": true, "cp_ordinal": 1, "timestamp_ms": 12400, "state_captured": true}
```
Fired on `GmPrepareLevel`. `is_cold_cp` true when `died` was set and a CP was acquired. `state_captured` indicates whether option-3 logic decided to save.

### Existing Events (unchanged)

- `level_start` — entrance detection
- `goal` / `exit` — level completion

## Database Schema

### `segments` table (replaces `splits`)

```sql
CREATE TABLE segments (
  id TEXT PRIMARY KEY,
  game_id TEXT NOT NULL REFERENCES games(id),
  level_number INTEGER NOT NULL,
  start_type TEXT NOT NULL,       -- 'entrance', 'checkpoint'
  start_ordinal INTEGER NOT NULL DEFAULT 0,
  end_type TEXT NOT NULL,         -- 'checkpoint', 'goal'
  end_ordinal INTEGER NOT NULL DEFAULT 0,
  description TEXT DEFAULT '',
  strat_version INTEGER DEFAULT 1,
  active INTEGER DEFAULT 1,
  ordinal INTEGER,                -- ordering within reference run
  reference_id TEXT REFERENCES capture_runs(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

### `segment_variants` table (new)

```sql
CREATE TABLE segment_variants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  segment_id TEXT NOT NULL REFERENCES segments(id),
  variant_type TEXT NOT NULL,     -- 'cold', 'hot'
  state_path TEXT NOT NULL,
  is_default INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(segment_id, variant_type)
);
```

### Changes to existing tables

All existing data (splits, attempts, model_state, sessions, transitions) will be deleted. Tables are dropped and recreated — `CREATE TABLE IF NOT EXISTS` won't alter columns, so a clean slate is required.

- `splits` table: dropped, replaced by `segments` + `segment_variants`
- `model_state`: recreated with `segment_id` column (was `split_id`)
- `attempts`: recreated with `segment_id` column (was `split_id`)
- `sessions`: `splits_attempted` / `splits_completed` renamed to `segments_attempted` / `segments_completed`
- `reference_time_ms`: dropped from the segment table. Reference times are only used as seed data for Kalman filter initialization — they can be computed from transition timestamps and stored directly in `model_state` when the model is first created.

### Indexes

```sql
CREATE INDEX idx_segments_game ON segments(game_id, active);
CREATE INDEX idx_segment_variants_segment ON segment_variants(segment_id);
CREATE INDEX idx_attempts_segment ON attempts(segment_id, created_at);
```

## Reference Run Capture Flow

### During Reference Run

As events arrive sequentially, segments are built from consecutive pairs:

1. `level_start` at level 105 → record entrance, capture save state
2. `checkpoint` at level 105, ordinal 1 → capture hot save state, create segment `entrance.0→checkpoint.1`
3. (optional) `death` → set died flag
4. (optional) `spawn` with `cp_acquired` → capture cold save state for cp.1 if first cold spawn
5. `checkpoint` at level 105, ordinal 2 → capture hot save state, create segment `checkpoint.1→checkpoint.2`
6. `goal` at level 105 → create segment `checkpoint.2→goal.0`

If no checkpoints in a level, the standard `entrance.0→goal.0` segment is created.

### Fill-Gaps Flow

For capturing missing cold CP starts after a deathless reference run:

1. Dashboard Manage tab shows segments with missing cold variants (❌)
2. User clicks ❌ on a segment
3. `POST /api/segments/{id}/fill-gap` triggers fill-gap capture mode
4. System loads the **hot CP save state** for that segment via TCP
5. Lua overlay shows "Die to capture cold start"
6. User dies from the CP start
7. On spawn event, cold variant is captured and saved to `segment_variants`
8. Fill-gap mode ends, UI updates ❌ to ✅

Key: the user does NOT need to replay from the entrance. Loading the hot CP and dying from there produces the cold CP state directly.

## Session Manager Changes

### route_event() Additions

- `checkpoint`: record event, capture hot save state, create segment leading up to this CP, set `cp_acquired`
- `death`: record in transition log, set internal died state
- `spawn`: check if first cold spawn for a known CP. If so, create cold variant in `segment_variants`. Uses `INSERT OR REPLACE` so re-recording (via fill-gaps on an existing variant) overwrites cleanly.

**Pending-start tracking**: The current `ref_pending_entrance` (a single dict) is replaced by a `ref_pending_start` structure:
```python
ref_pending_start: {
    "type": "entrance" | "checkpoint",
    "ordinal": int,
    "state_path": str,
    "timestamp_ms": int,
    "level_num": int
}
```
Set on entrance or checkpoint events. Consumed when the next event (checkpoint or goal) arrives to create the segment pair.

### Practice Mode

**SegmentCommand** (replaces SplitCommand):
```python
@dataclass
class SegmentCommand:
    id: str                        # segment ID
    state_path: str                # variant's state path
    description: str               # overlay text (e.g., "entrance → cp.1")
    end_type: str                  # 'checkpoint' or 'goal'
    expected_time_ms: int | None   # Kalman μ*1000, used for overlay timer
    auto_advance_delay_ms: int = 2000
```

Key changes from SplitCommand:
- `goal` field replaced by `end_type` — Lua uses this to decide whether to terminate on CP touch or goal fanfare
- `reference_time_ms` removed — timer uses `expected_time_ms` (Kalman estimate) exclusively. For new segments with no attempts, no timer comparison is shown.
- `end_on_goal` removed — subsumed by `end_type`

**Lua practice mode end-condition**: The `practice_load` message includes `end_type`. When `end_type == "checkpoint"`, Lua terminates the segment on the next `CP` event (same composite condition used in passive mode). When `end_type == "goal"`, existing goal detection applies. If the player overshoots (e.g., reaches goal during a cp→cp segment), the attempt counts as completed — the player went further than needed.

**Orchestrator flow**: picks a segment → picks the default variant (cold for CP segments) → sends SegmentCommand with that variant's `state_path` → Lua loads and monitors for end condition.

## Dashboard UI (Manage Tab)

Flat segment table with level name as a column:

```
┌───────────────────┬──────────────────────┬────────┐
│ Level             │ Segment              │ State  │
├───────────────────┼──────────────────────┼────────┤
│ Scary Munchers    │ entrance → cp.1      │   ✅   │
│ Scary Munchers    │ cp.1 → goal          │   ✅   │
│ Pipe Nightmares   │ entrance → goal      │   ✅   │
│ Shell Jumps       │ entrance → cp.1      │   ✅   │
│ Shell Jumps       │ cp.1 → cp.2          │   ✅   │
│ Shell Jumps       │ cp.2 → goal          │   ❌   │
└───────────────────┴──────────────────────┴────────┘
```

- **State column**: ✅ = has a usable save state (cold for CP segments, entrance state for entrance segments). ❌ = missing, clickable to trigger fill-gap.
- v1 shows a single status column. Cold/hot variant breakdown deferred to when it matters.

## Scheduler Integration

Segments are independent schedulable items — no hierarchy, no grouping. The Kalman filter treats each segment exactly as it treats current splits. `entrance→cp.1` and `cp.1→goal` are scheduled independently.

## Data Model Changes (Python)

`Split` dataclass → `Segment` dataclass. `SplitCommand` → `SegmentCommand` (see Practice Mode section for fields). `SplitWithModel` (in `allocators/__init__.py`) → `SegmentWithModel` — fields like `room_id`, `goal`, `end_on_goal` are replaced by `start_type`, `start_ordinal`, `end_type`, `end_ordinal`. All references throughout the codebase update accordingly.

`TransitionEvent` (currently a str subclass, not a true enum) gets a new `SPAWN = "spawn"` constant. `CHECKPOINT` and `DEATH` already exist.

New `SegmentVariant` dataclass:
```python
@dataclass
class SegmentVariant:
    segment_id: str
    variant_type: str        # 'cold', 'hot'
    state_path: str
    is_default: bool = False
```

**Key DB query change**: `get_all_splits_with_model()` becomes `get_all_segments_with_model()`. It JOINs `segments` with `model_state` on `segment_id`, and LEFT JOINs `segment_variants` (filtering `is_default = 1`) to get the default `state_path`. Returns segment-specific columns (`start_type`, `start_ordinal`, `end_type`, `end_ordinal`) instead of `room_id`, `goal`.

### Save State File Naming

Save states are organized per-game in subdirectories. Naming convention:

- Entrance: `{level_num}_entrance.mss` (existing pattern, renamed from `{level_num}_{room_num}.mss`)
- Hot CP: `{level_num}_cp{ordinal}_hot.mss`
- Cold CP: `{level_num}_cp{ordinal}_cold.mss`

### Unchanged Components

- `capture_runs` table and reference management endpoints remain as-is. The Manage tab still groups by reference run; segments replace splits within each run.

## Deferred

- Arbitrary practice points (room changes, manual markers)
- Powerup/speed/position variants
- Nonlinear routing / complex ordinal identity
- Segment unions (full-level practice)
- Hot variant UI emphasis / variant selection UI
- Re-recording existing variants via UI click
- Multiple save states per variant type
- "Start Reference Run" button in Manage tab

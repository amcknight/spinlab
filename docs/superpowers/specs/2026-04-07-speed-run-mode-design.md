# Speed Run Mode Design

**Date:** 2026-04-07
**Status:** Approved

## Summary

A new "Speed Run" mode for fast, sequential playthrough of the entire game. Plays each level start-to-goal continuously, skipping all non-gameplay time (death animations, goal fanfares, overworld movement) via save state loading — identical to how practice mode eliminates dead time. Checkpoints are passed through naturally without stopping; only death or goal ends interaction with a level.

## Goals

- Fast playthrough of the full game with no interruptions between gameplay segments
- Record cold-start attempt data as a side effect of natural play
- Reuse existing practice infrastructure where possible

## Mode Lifecycle

New `Mode.SPEED_RUN` enum value. Legal transitions:

- `IDLE → SPEED_RUN` (start)
- `SPEED_RUN → IDLE` (stop, or last level completed)

### SpeedRunSession

New class, parallel to `PracticeSession`. Responsibilities:

1. Query all active segments for the current game
2. Group by level, order levels by reference run `ordinal` (not `level_number` — levels can repeat)
3. Within each level, order segments by `start_ordinal` to build the checkpoint list
4. Send one level at a time via `SpeedRunLoadCmd`
5. Track cold/hot state for recording decisions
6. On goal → advance to next level. On last level goal → stop session.

### Start Preconditions

- TCP connected
- Game loaded
- No draft pending
- All segments in the run have save states on disk. If any are missing, refuse to start and surface an error: "Missing save state for segment X — run Cold Fill first."

## Protocol

### New Command: `speed_run_load`

Sent from Python to Lua when starting a new level.

```json
{
  "cmd": "speed_run_load",
  "id": "segment_id_of_first_segment",
  "state_path": "/path/to/entrance/state",
  "description": "Level 1",
  "checkpoints": [
    {"ordinal": 1, "state_path": "/path/to/cp1/state"},
    {"ordinal": 2, "state_path": "/path/to/cp2/state"}
  ],
  "expected_time_ms": null,
  "auto_advance_delay_ms": 1000
}
```

- `state_path`: entrance save state (initial load + default respawn)
- `checkpoints`: ordered list of CP save states within the level. May be empty for single-segment levels (entrance→goal).
- `auto_advance_delay_ms`: delay after goal before sending result (same default as practice mode)

### Lua State Machine: `handle_speed_run`

New function parallel to `handle_practice`. States: `LOADING`, `PLAYING`, `RESULT`.

**PLAYING state behavior:**

1. **Death (highest priority):** Reload current respawn path. Reset timer. The respawn path is the entrance state initially, and advances as CPs are passed. Send event:
   ```json
   {"event": "speed_run_death", "elapsed_ms": 5230}
   ```
   Death reload is instant (no delay), same as practice mode.

2. **Checkpoint hit:** Advance respawn path to matching checkpoint's `state_path`. Send event:
   ```json
   {"event": "speed_run_checkpoint", "ordinal": 1, "elapsed_ms": 12340}
   ```
   Keep playing — no state transition, no reload, no timer reset.

3. **Goal/exit detected:** Transition to `RESULT` state. After `auto_advance_delay_ms`, send:
   ```json
   {"event": "speed_run_complete", "elapsed_ms": 45600}
   ```

**RESULT state:** Show overlay (same as practice result screen), then send completion event and reset.

### Events from Lua to Python

| Event | When | Fields |
|---|---|---|
| `speed_run_checkpoint` | CP passed naturally | `ordinal`, `elapsed_ms`, `split_ms` |
| `speed_run_death` | Player died | `elapsed_ms`, `split_ms` |
| `speed_run_complete` | Goal reached, after delay | `elapsed_ms`, `split_ms` |

- `elapsed_ms`: time since level start (entrance save state load)
- `split_ms`: time since last save state load (entrance or death respawn). This is the value used for cold attempt recording.

## Cold Attempt Recording

### State Tracking

Python tracks a `cold_since` boolean per level:
- **Set to `true`** when a save state is loaded (level entrance or death respawn)
- **Set to `false`** when a CP is passed naturally (hot continuation)

### Recording Rules

| Event | `cold_since` | Action |
|---|---|---|
| `speed_run_checkpoint` | `true` | Record completed attempt for sub-segment ending at this CP |
| `speed_run_checkpoint` | `false` | Skip (hot attempt, no recording) |
| `speed_run_death` | either | Don't record (incomplete). Mark next sub-segment as cold. |
| `speed_run_complete` | `true` | Record completed attempt for final sub-segment |
| `speed_run_complete` | `false` | Skip (hot attempt) |

### Example: Deathless Level (entrance→cp1→cp2→goal)

1. Load entrance: `cold_since = true`
2. CP1 hit: record entrance→cp1 (cold). `cold_since = false`
3. CP2 hit: skip (hot)
4. Goal: skip (hot)

### Example: Die Between CP1 and CP2

1. Load entrance: `cold_since = true`
2. CP1 hit: record entrance→cp1 (cold). `cold_since = false`
3. Death: `cold_since = true` (respawn at cp1)
4. CP2 hit: record cp1→cp2 (cold). `cold_since = false`
5. Goal: skip (hot)

### Attempt Metadata

Recorded attempts use `source = "speed_run"`. Segment ID comes from the sub-segment whose boundaries match (e.g., entrance→cp1 segment ID when cp1 is reached from a cold entrance start).

## Level Ordering

Levels are ordered by reference run `ordinal`, not by `level_number`. This is critical because:
- Levels can be visited multiple times in a romhack
- The reference run captures the actual game order
- Ordinal is assigned during reference capture and is the source of truth

Within a level, segments are ordered by `start_ordinal` to reconstruct the checkpoint sequence.

## Frontend

### Model Tab

- New "Start Speed Run" button next to existing "Start Practice" button
- New "Stop Speed Run" button (shown when speed run active)
- Practice buttons hidden when speed run active, and vice versa
- Allocator weight slider and tuning panel hidden during speed run (segments are always sequential)

### Header Chip

- New CSS class `speed-run` for mode chip
- Label: "Speed Run — {level description}"
- Stop button in header works for speed run

### Practice Card

Reused during speed run with contextual adjustments:
- Current segment shows the level being played
- Recent attempts show cold recordings as they happen
- Session stats show progress through the game

## Not In Scope

- Hot attempt recording (future: ad hoc mid-segment waypoints)
- Model/estimator integration for speed run scheduling (segments are always sequential)
- Looping (session ends after last level)

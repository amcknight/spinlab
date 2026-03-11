# Step 4 — Practice Loop MVP Design

**Date:** 2026-03-11
**Status:** Approved
**Scope:** Practice loop MVP — Lua practice mode, Python orchestrator, DB setup, glue

---

## Overview

Step 4 wires together all prior steps into a working practice loop. The player runs
`python -m spinlab.orchestrator`, which loads a manifest into SQLite, connects to the
Lua TCP server, and drives a practice session using the SM-2 scheduler. Lua handles
in-emulator state loading, auto-retry on death, overlay display, and rating input.
Python handles scheduling, DB persistence, and session management.

Steps 0–3 are complete. The SM-2 scheduler, DB layer, and data models were written
ahead of schedule and require no changes.

---

## Architecture

Four interacting parts:

```
Python orchestrator  ←TCP (localhost:15482)→  Lua (Mesen2)
       │                                            │
       ▼                                            ▼
  SQLite DB                                  In-emulator overlay
  (attempts,                                 + controller input
   schedule,
   sessions)
```

Python is the loop driver. Lua is the executor. Python sends one command per attempt;
Lua sends one result per attempt. No polling.

---

## IPC Protocol Extensions

Extends the existing newline-delimited JSON TCP protocol.

### New commands (Python → Lua)

```
practice_load:<json>\n
```

JSON payload (matches `SplitCommand.to_dict()`):
```json
{
  "id": "smw_cod:5:0:normal",
  "state_path": "/path/to/states/smw_cod_5_0.mss",
  "goal": "normal",
  "description": "",
  "reference_time_ms": 12000
}
```

```
practice_stop\n
```

Exits practice mode, returns Lua to passive recording. Lua responds with `ok\n` before
transitioning back to IDLE.

### New events (Lua → Python, pushed)

```json
{"event": "attempt_result", "split_id": "smw_cod:5:0:normal", "completed": true, "time_ms": 11234, "goal": "normal", "rating": "good"}
{"event": "attempt_result", "split_id": "smw_cod:5:0:normal", "completed": false, "time_ms": 5400, "goal": "normal", "rating": "again"}
```

`completed=false` means the player aborted (start+select). Rating is always present
(set by L+D-pad input; no timeout — waits indefinitely). `time_ms` is always tracked
(time from state load to exit trigger), even for aborted attempts — useful for future
analysis. Rating string values are the `Rating` enum strings: `"again"`, `"hard"`,
`"good"`, `"easy"`. Python must coerce with `Rating(result["rating"])`.

---

## Lua Practice Mode

### State Machine

```
IDLE (passive mode, default)
  │  receive practice_load
  ▼
LOADING → set pending_load → cpuExec fires → state loaded
  │  (next startFrame)
  ▼
PLAYING
  ├── overlay: "Level X | Goal: Y | 0:08.4 | ref: 0:12.0"
  ├── death detected (player_anim == 9, prev != 9) → set pending_load (same state) → stay PLAYING (auto-retry)
  │     NOTE: death is checked BEFORE exit_mode — a simultaneous death+exit_mode edge
  │     case is treated as death (auto-retry), not abort.
  ├── abort detected (exit_mode != 0, goal_type()="abort", no death this frame)
  │     → go to RATING, completed=false
  └── clear detected (exit_mode != 0, goal_type() != "abort", no death this frame)
        → go to RATING, completed=true
  ▼
RATING
  ├── emu.pause() called
  ├── overlay: rating prompt (see Overlay section)
  ├── wait indefinitely for L+D-pad input
  └── L+← again | L+↓ hard | L+→ good | L+↑ easy
  ▼
DONE → emu.resume() → send attempt_result JSON → back to IDLE
       (Python immediately sends next practice_load)
```

### Passive Recording During Practice

Passive JSONL logging is **suspended** while in practice mode. Practice attempt data
is stored via the Python orchestrator into the `attempts` table with `source='practice'`.
Note: practice timing data will be valuable for future analysis — the `attempts` table
captures it.

### Overlay

**PLAYING state** (top-left, 1-frame duration called every startFrame):
```
SpinLab [PRACTICE] Lv5 goal:normal 0:08.4 ref:0:12.0
```

**RATING state** (shown while paused):
```
Line 1: "Clear! 0:11.2 (ref 0:12.0)"   -- or "Abort 0:05.4" if completed=false
Line 2: "L+<again  L+v hard  L+>good  L+^easy"
```

**Known risk:** `emu.drawString` has exhibited vertical/overlapping rendering with
longer strings in prior development sessions. The implementation plan must include a
verification step to confirm rendering behavior before building the full overlay, and
may need to use multiple short `drawString` calls or `emu.drawRectangle` for background
if needed.

---

## Python Orchestrator

### Entry Point

```
python -m spinlab.orchestrator
```

### Startup Sequence

1. Read `config.yaml`:
   - `config["game"]["id"]` — game_id
   - `config["network"]["host"]` / `config["network"]["port"]` — TCP connection
   - `config["scheduler"]["base_interval_minutes"]` — SM-2 base interval
   - `config["data"]["dir"]` — data directory root
2. Find latest manifest in `data/captures/*_manifest.yaml` (most recent by filename)
3. Open/create SQLite DB at `data/spinlab.db`
4. Upsert game record, upsert all splits from manifest, ensure schedule entries exist
5. TCP connect to Lua (retry loop with 0.5s backoff, up to ~30s)
6. Send `ping`, verify `pong` response
7. Create session record in DB

### Main Loop

```python
while True:
    cmd = scheduler.pick_next()       # Optional[SplitCommand]
    if cmd is None:
        print("No splits available — exiting.")
        break
    if cmd.state_path and not os.path.exists(cmd.state_path):
        log warning, continue  # skip missing state files
    send("practice_load:" + json.dumps(cmd.to_dict()))
    result = recv_until_attempt_result()  # blocking, no timeout; returns dict
    rating = Rating(result["rating"])
    log_attempt(result, rating)
    scheduler.process_rating(result["split_id"], rating)
```

Note: `recv_until_attempt_result()` must buffer partial TCP reads and return only when
a complete `\n`-terminated line is received with `"event": "attempt_result"`. Other
pushed lines (e.g., `ok:queued`) must be discarded or logged.

### Shutdown

Ctrl+C → send `practice_stop` → `end_session()` in DB → exit.

### Error Handling

- TCP disconnect during a session: log error, attempt reconnect once, then exit cleanly.
- No splits in DB after manifest load: exit with helpful message.
- Missing state file: Python checks `os.path.exists(cmd.state_path)` before sending
  `practice_load`; skips the split and picks next if absent.

---

## Files Changed / Created

| File | Change |
|------|--------|
| `lua/spinlab.lua` | Add practice mode state machine, new TCP commands, overlay, controller input, emu.pause/resume |
| `python/spinlab/orchestrator.py` | New file — full orchestrator implementation |
| `python/spinlab/__init__.py` | No change needed — `-m spinlab.orchestrator` works without export |
| `pyproject.toml` | Add `spinlab.orchestrator` as a script entry point (optional) |

No changes to `db.py`, `scheduler.py`, `models.py`, `capture.py` — they're already correct.

---

## Testing

- Manual: run orchestrator against live Mesen2 instance, verify full loop
- Verify: state loads immediately after rating (no perceptible lag)
- Verify: death triggers auto-reload (not end of attempt)
- Verify: abort (start+select) sends `completed=false`
- Verify: DB contains attempt records and updated schedule after session
- Overlay: verify text renders horizontally; adjust if vertical rendering bug manifests

No new automated tests required for this step (orchestrator is integration-only; all
unit-testable logic — scheduler, DB, models — is already tested).

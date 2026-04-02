# Cold-Fill Phase Design

## Problem

During a reference run, cold save states (respawn-from-death states) are only captured for checkpoints where the player happens to die. If the player plays clean through a checkpoint, only a hot variant exists. Today's workaround is a per-segment "fill-gap" button — tedious when multiple gaps exist.

Cold starts are the realistic practice scenario: when grinding a segment, you always start from a death/respawn state. Hot starts require holding buttons with momentum, which is awkward and unreliable. Cold should be the default practice variant.

## Solution

Add a **cold-fill phase** that runs automatically after a reference run. The system queues all segments missing cold variants, loads each hot state in sequence, and waits for the player to die and respawn. On respawn, it captures the cold state and advances to the next segment.

### What's NOT in scope

- **Auto-death via memory pokes.** Spiked and shelved — retry patch behavior varies across romhacks and is not robust. Player dies manually.
- **Hot state improvements** (pause-and-show-held-buttons). Future capability.
- **QoL controls during cold-fill** (skip segment, pause, reorder). Deferred — design separately when the core flow is proven.

## Design

### Data Model

No schema changes. The existing `segment_variants` table already supports hot/cold variants per segment. Cold-fill populates the missing cold rows.

New transient state on `CaptureController`:

```python
cold_fill_queue: list[str]        # segment IDs still needing cold variants
cold_fill_current: str | None     # segment currently being filled
cold_fill_total: int              # original queue length (for progress)
```

### Mode

New `Mode.COLD_FILL` in SessionManager, alongside existing IDLE, REFERENCE, REPLAY, PRACTICE, FILL_GAP.

### Flow

```
Reference run ends (draft saved by user)
  -> Query: segments in this capture run where cold variant is missing
  -> If none missing: -> IDLE
  -> If gaps found:
      -> Enter COLD_FILL mode
      -> cold_fill_queue = [seg_1, seg_2, ..., seg_N]
      -> cold_fill_total = N
      -> Load hot state for seg_1, send cold_fill_load to Lua
      -> Dashboard shows "Die to capture cold start (1/N) - L105 cp1 > cp2"
      -> Player dies -> respawns -> Lua captures cold state -> sends spawn event
      -> Python stores cold variant (is_default=True), pops queue
      -> If queue not empty: load next hot state
      -> If queue empty: -> IDLE
```

### Lua: Cold-Fill Mode (~25 lines)

New dedicated state machine, separate from `detect_transitions` and `handle_practice`. Activated by a `cold_fill_load` command from Python.

```
Command received: cold_fill_load {state_path, segment_id}
  -> Load save state (deferred via pending_loads)
  -> Set cold_fill.active = true
  -> cold_fill.state = WAITING_FOR_DEATH

Per-frame (only when cold_fill.active):
  WAITING_FOR_DEATH:
    -> Watch for player_anim == 9 (death animation start)
    -> On death: cold_fill.state = WAITING_FOR_SPAWN

  WAITING_FOR_SPAWN:
    -> Watch for level_start 0->1 (respawn)
    -> On spawn: capture save state, send event:
       {event: "spawn", is_cold_cp: true, state_captured: true,
        state_path: "<path>", segment_id: "<id>"}
    -> cold_fill.active = false
```

This is isolated from existing detection logic. The `on_start_frame` function checks `cold_fill.active` before `detect_transitions` / `handle_practice` and short-circuits when active.

The `cold_fill_load` command is handled in `handle_json_message` alongside existing commands.

### Python: CaptureController

New methods on `CaptureController`:

- `start_cold_fill(capture_run_id, tcp, db)` — queries segments missing cold variants, builds queue, loads first hot state, sends `cold_fill_load` to Lua.
- `handle_cold_fill_spawn(event, db) -> bool` — stores cold variant, advances queue, loads next hot state. Returns True when queue is empty (done).

### Python: SessionManager

- After `save_draft()` completes, check for cold-fill candidates. If any exist, call `start_cold_fill()` and set mode to `COLD_FILL`.
- Route spawn events in `COLD_FILL` mode to `handle_cold_fill_spawn()`. When it returns True, set mode to IDLE.

### Dashboard / SSE

State broadcast gains cold-fill progress:

```json
{
  "mode": "cold_fill",
  "cold_fill": {
    "current": 2,
    "total": 5,
    "segment_label": "L105 cp1 > cp2"
  }
}
```

Frontend displays this as a progress indicator in the existing manage view. No new pages.

### Practice Mode Default

Cold variants are stored with `is_default=True`. The existing practice flow loads the default variant, so practice automatically uses cold starts once cold-fill completes.

Hot variants remain stored with `is_default=False` for future use.

### Existing Fill-Gap

Kept as-is. It serves a different use case: filling a single segment's cold variant on demand, outside of the cold-fill phase. No changes needed.

### Error Handling

- If Lua disconnects during cold-fill: mode returns to IDLE, remaining queue is lost. User can re-trigger cold-fill from the dashboard (future QoL).
- If a segment's hot variant is missing (shouldn't happen, but): skip it, log a warning.

## Files Changed

| File | Change |
|------|--------|
| `python/spinlab/models.py` | Add `Mode.COLD_FILL` |
| `python/spinlab/capture_controller.py` | Add cold-fill queue state, `start_cold_fill()`, `handle_cold_fill_spawn()` |
| `python/spinlab/session_manager.py` | Wire cold-fill after draft save, route spawn events in COLD_FILL mode |
| `lua/spinlab.lua` | Add `cold_fill_load` command handler, cold-fill state machine (~25 lines) |
| `python/spinlab/static/manage.js` | Display cold-fill progress |
| `python/spinlab/static/style.css` | Minor styling for cold-fill status |
| `python/spinlab/dashboard.py` | No new endpoints needed (SSE state already broadcast) |

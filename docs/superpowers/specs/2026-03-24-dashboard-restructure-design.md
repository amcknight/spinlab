# Dashboard Restructure: Replay UI, Reference Lifecycle, Layout Overhaul

**Date:** 2026-03-24
**Status:** Draft

## Problem

The dashboard's Live tab is unergonomic — it crams disconnected state, idle controls, reference progress, and practice view into one page. There's no UI for replay (`.spinrec` playback) or for checking whether recordings exist. Reference runs auto-save with generated names, making it easy to accumulate junk data.

## Goals

1. Add replay UI: see which references have `.spinrec` files, trigger playback, create new references from recordings.
2. Improve reference lifecycle: captures are "drafts" until explicitly named and saved.
3. Restructure layout: remove Live tab, move status to header, merge practice into Model tab, merge reference controls into Manage tab.
4. Widen the dashboard from 320px to 428px.

## Non-Goals

- Category/route selection (future — the game selector is designed to accommodate it).
- Spinrec file management as a separate tab (may come later).
- Changes to the Lua script, TCP protocol, or scheduler.

---

## Layout

### Width

428px (up from 320px). Mesen2's rendering leaves ~108px of additional horizontal space.

### Header Bar (always visible)

```
┌──────────────────────────────────────────────┐
│ SpinLab   [Hack Name ▾]        ● Idle        │
└──────────────────────────────────────────────┘
│  Model  │  Manage  │
```

**Game selector** (left):
- Displays current game name. Clickable — opens a popover with a filterable ROM list.
- Clicking a ROM launches Mesen via the existing `/api/emulator/launch` endpoint.
- On first launch (no localStorage), shows "No game". On subsequent launches, localStorage stores `lastGameId` / `lastGameName` and displays it immediately, even before TCP connects.
- Once TCP connects and `rom_info` fires, updates to whatever's actually loaded.
- Future: category/route selector slots in below the ROM list in this popover.

**Mode chip** (right):
- `○ Disconnected` — hollow dot, dim. Tabs still functional; action buttons disabled.
- `● Idle` — dim dot.
- `● Recording — 4 segments` — red dot. Small `✕` stop button inline.
- `● Practicing — L105 ent→goal` — green dot. Small `✕` stop button inline.
- `● Replaying — 62%` — blue dot. Small `✕` stop button inline.

The inline stop button allows stopping any active mode from any tab.

### Tabs

Two tabs: **Model** and **Manage**. The Live tab is removed entirely.

---

## Model Tab

The primary view — performance data and the active practice session.

### When idle / recording / replaying

Just the model table as it exists today: segment list with Avg, Trend, Range, Value, Runs, Best columns. Estimator select in the table header. The wider layout gives columns more breathing room.

### When practicing

A practice card renders **above** the model table:

```
┌──────────────────────────────────────────────┐
│ L105 entrance → goal               Attempt 3 │
│ ↓ 0.42 s/run (high confidence)               │
├──────────────────────────────────────────────┤
│ UP NEXT                                      │
│  L106 entrance → goal                        │
│  L103 cp.1 → goal                            │
├──────────────────────────────────────────────┤
│ RECENT                                       │
│  4.2s  L105 ent→goal                         │
│  ✗     L103 cp.1→goal                        │
├──────────────────────────────────────────────┤
│ 12/15 cleared │ 8:42                         │
├──────────────────────────────────────────────┤
│ Allocator: [Greedy ▾]                        │
└──────────────────────────────────────────────┘

── Model State ────────────────────────────────
│ Segment │ Avg │ Trend │ ... │
```

- The practice card collapses away when practice stops.
- Allocator select lives in the practice card (only relevant during practice).
- Estimator select stays in the model table header.

---

## Manage Tab

Owns reference lifecycle, replay, and segment management. Three sections:

### 1. References Section

```
┌──────────────────────────────────────────────┐
│ REFERENCES                                   │
│ [Active ref dropdown ▾]  [✎] [✕]            │
│                                              │
│ [▶ Start Reference Run]   [▶ Replay]        │
└──────────────────────────────────────────────┘
```

- **Dropdown:** Shows saved (non-draft) references. Selecting one activates it. **Disabled** when recording or replaying.
- **Rename (✎) / Delete (✕):** Operate on selected reference. Delete requires confirmation.
- **Start Reference Run:** Begins live capture. Header chip switches to "Recording — 0 segments". Button disables during capture.
- **Replay button:** Enabled only when the selected reference has a `.spinrec` file. Triggers replay of that recording, which creates a new draft reference from the replayed segments. Future: speed options via dropdown or split button.

### 2. Save/Discard Prompt (inline, appears after capture or replay finishes)

```
┌──────────────────────────────────────────────┐
│ ✓ Captured 12 segments                       │
│ [Name this run: ___________________]         │
│              [Save]  [Discard]               │
└──────────────────────────────────────────────┘
```

- **Save:** Names the capture run, promotes it to a saved reference, sets it as the active reference in the dropdown.
- **Discard:** Requires confirmation ("Are you sure?"). Deletes the draft capture run and all its segments from the DB.
- **Blocking:** This prompt blocks the references section until resolved. You cannot start another reference, switch the active one, or trigger a replay while a draft is pending.

### 3. Segments Table

Same as today but wider. Columns: Name (editable inline), Level, Segment (start→end), Save State status, Delete button. Shows segments for the currently active (saved) reference.

### 4. Data Section

Same as today: "Clear All Data" button with confirmation.

---

## Backend Changes

### New Endpoints

**`GET /api/references/{ref_id}/spinrec`**
- Checks if a `.spinrec` file exists for the given reference.
- Returns `{ "exists": true, "path": "data/.../ref_xxx.spinrec" }` or `{ "exists": false }`.
- Used by the frontend to show/hide the Replay button.

**`POST /api/references/draft/save`**
- Body: `{ "name": "My Run Name" }`
- Renames the draft capture run, marks it as saved (no longer draft), sets it as active.
- Returns `{ "status": "ok", "ref_id": "...", "name": "..." }`.

**`POST /api/references/draft/discard`**
- Deletes the draft capture run and all associated segments.
- Returns `{ "status": "ok" }`.

### Modified Behavior

**`start_reference`:**
- No longer calls `db.set_active_capture_run()` immediately.
- The draft capture run is excluded from `list_capture_runs` (or marked with a flag so the frontend can filter it out).

**`get_state()` additions:**
- New `draft` field when a capture has just finished (recording stopped or replay completed):
  ```json
  {
    "draft": {
      "run_id": "live_abc123",
      "segments_captured": 12
    }
  }
  ```
- This field is present from the moment capture stops until the user saves or discards. The session manager holds this state.

**`list_capture_runs` (DB):**
- Either: add a `draft` boolean column to `capture_runs` and filter in the query.
- Or: the session manager filters drafts out in the endpoint handler.

### Replay Flow (unchanged from existing backend)

`POST /api/replay/start` with `{ "path": "...", "speed": 0 }` works as-is. The only addition is that the resulting capture run also goes through the draft→save/discard flow instead of auto-saving.

---

## Frontend File Changes

| File | Change |
|------|--------|
| `index.html` | Restructure: remove Live tab section, add header status bar markup, two tabs only. Width to 428px. |
| `style.css` | Width 428px. Header status chip styles. Practice card styles. Save/discard prompt styles. Game selector popover styles. Remove live-mode styles. |
| `app.js` | Rewire for two tabs. Header status chip updates from SSE. Game selector popover logic. Stop button wiring. Remove live-mode dispatch. localStorage for last game. |
| `live.js` | **Delete entirely.** Logic distributed to new `header.js` and into `model.js`. |
| `header.js` | **New file.** Game selector popover (ROM list, filter, launch). Mode chip rendering. Stop button. |
| `model.js` | Gains practice card rendering (current segment, queue, recent, stats, allocator select). Existing model table unchanged. |
| `manage.js` | Gains: start reference button, replay button, spinrec existence check, save/discard prompt, dropdown locking during capture. Existing segment table and data reset unchanged. |
| `api.js` | No changes (postJSON/fetchJSON/connectSSE sufficient). |
| `format.js` | No changes. |

---

## Testing

- **Unit tests:** Draft lifecycle (save, discard) in test_session_manager or new test file. Spinrec existence check endpoint.
- **Playwright E2E:** Header status chip transitions. Save/discard flow. Replay trigger from Manage. Practice card appears/disappears in Model tab. Game selector popover.
- **Manual:** Verify 428px width works with Mesen2 side-by-side. Verify localStorage game persistence across page reloads.

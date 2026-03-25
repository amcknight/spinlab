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
- `.spinrec` / `.mss` file cleanup on discard is out of scope — files stay on disk, only DB records are deleted. File management can be added later.

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
- Displays current game name. Clickable — opens a popover with a filterable ROM list. Popover closes on ROM selection, click outside, or Escape key.
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
- `● Draft — 12 segments` — yellow dot. No stop button (resolved via Manage tab save/discard).

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

- The practice card is **persistent DOM** (hidden/shown via `display: none`), not dynamically created/destroyed. This avoids re-binding event listeners on every SSE update. Content is updated in place.
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
- **Discard:** Requires confirmation ("Are you sure?"). Hard-deletes the draft capture run row and all its segment rows from the DB (not soft-delete — these were never "real"). `.spinrec` / `.mss` files on disk are left alone.
- **Blocking:** This prompt blocks the references section until resolved. You cannot start another reference, switch the active one, or trigger a replay while a draft is pending.

**Header chip during draft-pending state:** Shows `● Draft — 12 segments` (yellow dot). This is visible from any tab so the user knows there's an unresolved draft.

### 3. Segments Table

Same as today but wider. Columns: Name (editable inline), Level, Segment (start→end), Save State status, Delete button. Shows segments for the currently active (saved) reference.

### 4. Data Section

Same as today: "Clear All Data" button with confirmation.

---

## Backend Changes

### New Endpoints

**`GET /api/references/{ref_id}/spinrec`**
- Checks if a `.spinrec` file exists for the given reference.
- The path is derived by convention: `data/{game_id}/rec/{ref_id}.spinrec` (this is already how `start_reference` names files).
- Returns `{ "exists": true, "path": "data/.../ref_xxx.spinrec" }` or `{ "exists": false }`.
- Used by the frontend to show/hide the Replay button.

**`POST /api/references/draft/save`**
- Body: `{ "name": "My Run Name" }`
- Renames the draft capture run, sets `draft=0`, sets it as active.
- Clears `draft_run_id` and `draft_segments_count` from session manager.
- Returns `{ "status": "ok", "ref_id": "...", "name": "..." }`.

**`POST /api/references/draft/discard`**
- Hard-deletes the draft capture run and all associated data via new `hard_delete_capture_run()` DB method. Cascade order: delete `segment_variants`, `model_state`, and `attempts` rows for the draft's segments, then the segment rows, then the capture_run row.
- Clears `draft_run_id` and `draft_segments_count` from session manager.
- Returns `{ "status": "ok" }`.

### Draft Lifecycle (SessionManager)

The session manager gains two new fields: `draft_run_id: str | None` and `draft_segments_count: int`.

**Entering draft state — `stop_reference()`:**
1. Send TCP `reference_stop` command.
2. Copy `ref_capture_run_id` → `draft_run_id` and `ref_segments_count` → `draft_segments_count`.
3. Call `_clear_ref_state()` (resets recording state, sets mode to idle).
4. Notify SSE.

**Entering draft state — `replay_finished` event:**
1. Copy `ref_capture_run_id` → `draft_run_id` and `ref_segments_count` → `draft_segments_count`.
2. Call `_clear_ref_state()`.
3. Notify SSE.

**Entering draft state — `replay_error` event:**
- If `ref_segments_count > 0`: enter draft state (same as replay_finished — partial replay still has usable segments).
- If `ref_segments_count == 0`: auto-discard — hard-delete the capture run, call `_clear_ref_state()`.

This decouples "tell Lua to stop" from "draft is pending." The copy-then-clear ordering is critical.

**Exiting draft state:** On save or discard (see endpoints above), clear `draft_run_id` / `draft_segments_count`.

**Draft blocks all mode transitions:** While `draft_run_id` is set, `start_reference`, `start_replay`, AND `start_practice` all refuse to start (return `{ "status": "draft_pending" }`, HTTP 409). The user must save or discard first.

**Draft persistence across restarts:** Add a `draft` integer column (default 1) to the `capture_runs` table. New runs are created with `draft=1`. The save endpoint sets `draft=0`. On startup, `SessionManager.__init__` can check for any `draft=1` rows for the current game and restore `draft_run_id` if found. `list_capture_runs` filters to `draft=0` only.

### Modified Behavior

**`start_reference` / `start_replay`:**
- No longer call `db.set_active_capture_run()` immediately. The draft stays non-active until saved.
- `start_replay()` also defers `set_active_capture_run()` — same draft flow as live reference.
- Both refuse to start if `draft_run_id` is set (draft pending blocks new captures).

**`get_state()` additions:**
- New `draft` field when `draft_run_id` is set:
  ```json
  {
    "draft": {
      "run_id": "live_abc123",
      "segments_captured": 12
    }
  }
  ```
- Mode is `"idle"` during draft-pending (not a new mode — the header chip handles the visual via the draft field).

**`list_capture_runs` (DB):**
- Add `draft` integer column to `capture_runs` with `DEFAULT 0` (so existing rows remain visible). New capture runs are inserted with `draft=1` explicitly. The save endpoint sets `draft=0`. The listing query filters `WHERE draft = 0`.
- Include a `has_spinrec` boolean in the listing response. The backend checks file existence for each reference by convention path (`data/{game_id}/rec/{ref_id}.spinrec`) during listing. This avoids N+1 requests from the frontend.

### Replay Flow

`POST /api/replay/start` no longer takes a raw path from the frontend. Instead, the frontend sends `{ "ref_id": "..." }` where `ref_id` is the **source reference** whose `.spinrec` will be played back. The backend derives the path as `data/{game_id}/rec/{ref_id}.spinrec`. The replay creates a *new* draft capture run (with its own `replay_xxx` id) — the source ref_id is only used to locate the recording file. The replay then goes through the same draft→save/discard flow as live reference capture.

**Startup draft recovery:** On `SessionManager.__init__`, check for any `draft=1` rows in `capture_runs` for the current game. If exactly one exists, restore `draft_run_id` and query segment count. If multiple exist (crash during rapid captures), keep the most recent, hard-delete the rest.

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

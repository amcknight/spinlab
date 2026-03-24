# Dashboard-Driven Mode Control

## Problem

Two competing control planes (AHK hotkeys + dashboard UI) create confusion. Reference capture has no explicit boundaries — it starts silently on TCP connect and never ends. Stale in-memory state persists after data clears (sections counter bug).

## Design

### Three Explicit Modes

All mode transitions happen from the dashboard UI. The Lua script's passive memory watching runs regardless of mode — the dashboard decides whether to act on events.

| Mode | Entry | Exit | Behavior |
|------|-------|------|----------|
| **Idle** | Default when emulator connected | Start Reference or Start Practice | Dashboard ignores Lua events. Normal play. |
| **Reference** | Click "Start Reference Run" | Click "Stop" or Start Practice | Dashboard captures level_entrance/exit events, pairs into splits, saves to DB. Shows live sections counter. |
| **Practice** | Click "Start Practice" | Click "Stop Practice" | Dashboard sends practice_load to Lua, tracks attempts, updates Kalman model. |

**Key change**: Idle is the default connected state. Reference capture requires an explicit start action.

**Mode is tracked by an explicit flag** (`_mode = ["idle"]`), not derived from TCP connection state. `_current_mode()` reads this flag rather than inferring from `tcp.is_connected` / `practice.is_running`.

### Mode Transition Details

- **Reference → Practice**: Implicitly stops the reference capture first — clears `_ref_pending`, finalizes the capture run, resets sections counter. Then starts practice.
- **Reference → Idle**: Clears reference state (same cleanup as above).
- **Practice → Idle**: Sends `practice_stop` to Lua, clears practice session.
- **TCP disconnect (any mode)**: Resets mode to idle, clears all in-flight state.

### New API Endpoints

| Endpoint | Effect |
|----------|--------|
| `POST /api/reference/start` | Sets mode to "reference", creates capture run, begins accepting events. Returns `{run_id, run_name}`. |
| `POST /api/reference/stop` | Sets mode to "idle", clears pending state, finalizes capture run. |
| `POST /api/emulator/launch` | Launches Mesen2 via subprocess (reads `config.yaml` for emulator path). Returns `{status: "ok"}`. |

Existing endpoints (`POST /api/practice/start`, `POST /api/practice/stop`) remain but gain the implicit reference-cleanup behavior on start.

### AHK Simplification

**Keep:**
- **ctrl+alt+W** — Launch Mesen2 + Dashboard (one-shot "start everything"). If Mesen is already running, no-op on Mesen. If dashboard is already running, no-op on dashboard.
- **ctrl+alt+X** — Kill everything

**Remove:**
- **ctrl+alt+C** — Replaced by live dashboard capture
- **ctrl+alt+D** — Merged into ctrl+alt+W

### Bug Fix

`POST /api/reset` must also reset in-memory state:

- Stop active practice session if running (same cleanup as `practice_stop`)
- `_ref_splits_count[0] = 0`
- `_ref_capture_run_id[0] = None`
- `_ref_pending` dict cleared
- `_mode[0] = "idle"`

### CLI Cleanup

**Remove:**
- `spinlab capture` — Live capture in dashboard replaces batch JSONL processing
- `spinlab practice` — Dashboard replaces terminal orchestrator

**Keep:**
- `spinlab dashboard` — Primary entry point
- `spinlab lua-cmd` — Debugging utility
- `spinlab stats` — Stub (stays for future use)

### Dashboard UI Changes

**Live tab (disconnected):**
- Shows "Waiting for emulator..."
- "Launch Emulator" button (calls `POST /api/emulator/launch`)

**Live tab (idle — connected, no active mode):**
- Shows emulator connection status (connected)
- "Start Reference Run" button
- "Start Practice" button (if splits exist)

**Live tab (reference state):**
- Shows "Reference Run" header with live sections counter
- "Stop Reference Run" button
- Events stream in and pair into splits in real-time

**Live tab (practice state):**
- Current split, up-next queue, recent attempts (unchanged)
- "Stop Practice" button

### What Doesn't Change

- Lua script: passive memory watching, TCP server, event format, practice mode commands
- Kalman estimator, greedy allocator, pluggable scheduler interface
- DB schema (splits, attempts, sessions, model_state tables)
- Dashboard: Sessions tab, Model tab, Config tab
- Save state file format and deterministic split IDs

## Files Changed

| File | Change |
|------|--------|
| `scripts/spinlab.ahk` | Merge D into W, remove C hotkey. Idempotent: no-op if already running. |
| `python/spinlab/dashboard.py` | Add explicit mode flag + state machine, new reference/emulator endpoints, fix reset bug, gate event capture on reference mode |
| `python/spinlab/static/app.js` | Update Live tab with mode-aware buttons (Launch Emulator, Start/Stop Reference, Start/Stop Practice) |
| `python/spinlab/cli.py` | Remove capture and practice subcommands, keep dashboard/lua-cmd/stats. Update dashboard subcommand imports from `spinlab.orchestrator` to `spinlab.manifest`. |
| `python/spinlab/capture.py` | Delete (live capture replaces batch) |
| `python/spinlab/orchestrator.py` | Extract `seed_db_from_manifest`, `find_latest_manifest`, `load_manifest` to `python/spinlab/manifest.py`, then delete orchestrator |
| `python/spinlab/manifest.py` | New file: relocated manifest utilities from orchestrator |

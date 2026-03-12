# SpinLab Dashboard — Design Spec

## Goal

A local web app that runs alongside Mesen2 during practice sessions, providing live stats and model transparency on stream (like LiveSplit), plus a management view for curating reference runs and configuring sections between sessions.

## Architecture

**Single Python process.** FastAPI serves a JSON API and a single HTML page. No separate frontend build step, no JS framework — vanilla HTML/CSS/JS.

**Data flow:**
- Dashboard reads the same SQLite DB the orchestrator writes to. No new IPC.
- Live updates via polling: the page fetches `GET /api/state` every ~1 second.
- Management actions (enable/disable, delete, rename) are POST/DELETE requests that modify the DB directly.
- The orchestrator does not need to know the dashboard exists — they share the DB file.

**Launch:** `spinlab dashboard` starts the server on `http://localhost:15483` and opens the browser. Ctrl+Alt+W (launch Mesen) also starts the dashboard automatically if it isn't already running. The dashboard stays running across reference/practice mode switches — it adapts its display to whatever mode is active.

**Dependencies:** `fastapi`, `uvicorn`. Added to `pyproject.toml` under `[project.dependencies]`. HTML/CSS/JS served as static files from `python/spinlab/static/`.

**Security:** None. Localhost-only, single user, no auth.

## UI Layout

Narrow column, ~320px wide (phone-sized). Dark theme. Designed to sit beside Mesen2 on screen and be captured by OBS (window capture or Browser Source).

Two views toggled by tabs at the top: **Live** and **Manage**.

---

### Live View

Default view during practice. Everything auto-updates every ~1s.

**Header:**
- "SpinLab" title + session timer + time saved this session (sum of reference_time - actual_time across completed splits, e.g., "-4.2s" in green or "+1.3s" in red)

**Current split:**
- Goal label (e.g., "Exit: Normal")
- Difficulty indicator (color-coded)
- Attempt count for this split this session

**Model insight** (for the current split — deferred until Kalman model is implemented):
- Drift indicator: arrow + rate (e.g., "↓ 1.2s/run" green, "→ flat" gray, "↑ 0.5s/run" red)
- Why picked: one-line reason ("Highest marginal return", "Exploring (low confidence)", "Building data (3 runs)")
- Confidence: text label (uncertain / moderate / confident) derived from P_dd
- **PoC placeholder:** shows difficulty tier from SM-2 ease factor instead

**Up next:**

- 2-3 upcoming splits in a compact list (goal + difficulty color)
- Marginal return score added when Kalman model lands

**Recent results:**
- Last ~8 attempts as compact rows: goal, time, rating (color-coded: green=easy/good, yellow=hard, red=again)

**Session stats footer:**
- "12/15 cleared | 23min"

**When in reference mode (no practice session):** Shows "Reference Run" header with a count of sections captured so far from the passive log (e.g., "4 sections captured"). Gives quick feedback that the recorder is working.

**When idle (no Mesen running):** Shows "No active session" with last session summary.

---

### Manage View

For between sessions. Interactive — clicking/toggling makes POST requests to the API.

**Sections tab:**
- All captured splits grouped by level number
- Each row: goal, reference time, model state (μ, drift, runs, marginal return), enabled/disabled toggle
- Bulk controls: "Enable all Level 44", "Disable all"
- Per-section actions (expand on click): delete, rename, reset model/schedule
- Granularity toggle: practice by level vs. practice by room

**Reference runs tab:**
- List of captured manifests by date
- Expand to see all sections in that capture
- Delete bad entries (wrong level, died weirdly)
- Toggle which capture is the "active" reference for each split

---

## API Endpoints

### Read-only (Live view polling)

| Endpoint | Method | Returns |
|----------|--------|---------|
| `/api/state` | GET | Current session state: active split, queue, recent results, session stats, model insight for current split |
| `/api/splits` | GET | All splits with schedule/model state, grouped by level |
| `/api/sessions` | GET | Session history (last N sessions) |

### Management (Manage view actions)

| Endpoint | Method | Action |
|----------|--------|--------|
| `/api/splits/{id}/toggle` | POST | Enable/disable a split |
| `/api/splits/{id}/rename` | POST | Set display name |
| `/api/splits/{id}/reset` | POST | Reset model/schedule to defaults |
| `/api/splits/{id}` | DELETE | Soft-delete a split |
| `/api/splits/bulk` | POST | Bulk enable/disable by level |
| `/api/captures` | GET | List all manifest files |
| `/api/captures/{filename}/entries` | GET | Sections in a specific capture |
| `/api/captures/{filename}/entries/{id}` | DELETE | Remove a section from a capture |

### Dashboard lifecycle

| Endpoint | Method | Action |
|----------|--------|--------|
| `/` | GET | Serves the HTML page |
| `/static/*` | GET | CSS/JS files |

## File Map

| File | Purpose |
|------|---------|
| `python/spinlab/dashboard.py` | FastAPI app, all API endpoints, server startup |
| `python/spinlab/static/index.html` | Single-page HTML (both views) |
| `python/spinlab/static/style.css` | Dark theme, narrow layout, LiveSplit-inspired |
| `python/spinlab/static/app.js` | Polling logic, tab switching, management actions |
| `python/spinlab/cli.py` | Add `dashboard` subcommand (accepts `--config`, matching `practice`) |
| `python/spinlab/scheduler.py` | Add `peek_next_n(n)` method for queue preview |
| `pyproject.toml` | Add `fastapi`, `uvicorn` to dependencies |
| `scripts/spinlab.ahk` | Ctrl+Alt+W also launches dashboard; remove separate Ctrl+Alt+D |

## DB Query Additions

New read-only methods needed on `Database`:

- `get_all_splits_with_schedule(game_id)` — joins splits + schedule for the manage view
- `get_recent_attempts(game_id, limit)` — last N attempts joined with splits (includes goal, description)
- `get_session_history(game_id, limit)` — recent sessions
- `get_current_session(game_id)` — active session (ended_at IS NULL)
- `get_split_attempt_count(split_id, session_id)` — attempts on a specific split in current session

## Orchestrator Integration

The dashboard needs to know what the orchestrator is currently doing (which split is active, what's queued).

**Approach: Shared state file.** The orchestrator writes a JSON file on each split change. The dashboard reads it via polling. Simple, no coupling.

**State file:** `data/orchestrator_state.json`

```json
{
  "session_id": "a1b2c3...",
  "started_at": "2026-03-12T15:30:00Z",
  "current_split_id": "smw_cod:44:8:normal",
  "queue": ["smw_cod:56:1:normal", "smw_cod:58:0:key"],
  "updated_at": "2026-03-12T15:32:14Z"
}
```

- `queue` contains the next 2-3 split IDs. Requires a new `Scheduler.peek_next_n(n)` method.
- Written atomically: orchestrator writes to `.tmp` then renames to `.json` (prevents partial reads on Windows).
- Deleted (or `session_id` set to null) when the session ends.
- Dashboard treats missing/stale file as "no active session."

**Future upgrade:** WebSocket or SSE bridge for sub-second updates if polling feels sluggish.

## PoC Scope

For the first implementation, build:
1. FastAPI skeleton with `/api/state` and `/api/splits` endpoints
2. HTML page with Live view only (no Manage view yet)
3. Orchestrator writes state file
4. `spinlab dashboard` subcommand + AHK hotkey
5. Dark theme CSS, 320px wide

Manage view, reference run curation, and model insight come in subsequent iterations.

## Future Additions (not in PoC)

- Improvement graphs (Chart.js) in a third tab or expanded split view
- Kalman model state visualization (drift over time, confidence bands)
- Auto-name sections from ROM data
- Custom start/end conditions for sections
- Re-run allocation (practice N more runs on split X)

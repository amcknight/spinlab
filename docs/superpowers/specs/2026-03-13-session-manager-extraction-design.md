# Session Manager Extraction & Polish

## Problem

The dashboard's `create_app()` is a 500-line god function. All mutable state lives in closure-scoped mutable containers (`_mode[0]`, `_scheduler[0]`, etc.), making it untestable and hard to reason about. Two async loops compete for the same TCP event queue — the dispatch loop yields with a sleep hack during practice mode, causing events (like model updates) to be delayed or lost. Practice sessions do their own state-file validation with a retry loop that belongs in the scheduler. The JS frontend polls on a tight 500ms interval instead of receiving push updates.

These are all symptoms of one root cause: no proper state owner or event routing.

## Design

### 1. SessionManager Class

New file: `python/spinlab/session_manager.py`

Owns all mutable session state that currently lives in `create_app()` closures.

**Constructor params:** `db`, `tcp`, `rom_dir`, `default_category` (from config — needed for ROM discovery and reference capture).

**State:**
- `mode: str` — "idle", "reference", or "practice"
- `game_id: str | None`, `game_name: str | None` — current game context
- `scheduler: Scheduler | None` — lazy-init when game switches
- `practice_session: PracticeSession | None` — created on start, destroyed on stop
- `practice_task: asyncio.Task | None` — the running practice coroutine
- Reference capture state: `ref_pending: dict`, `ref_splits_count: int`, `ref_capture_run_id: str | None`
- `tcp: TcpManager` — reference, not owned (lifecycle managed by app)
- `db: Database` — reference
- `rom_dir: Path`, `default_category: str` — config values for game discovery
- `_sse_subscribers: list[asyncio.Queue]` — SSE push targets

**Key methods:**

```python
async def route_event(self, event: dict) -> None
    """Single entry point for all TCP events. Routes by type."""

async def start_reference(self, run_name: str | None = None) -> None
async def stop_reference(self) -> dict  # returns capture summary

async def start_practice(self) -> None
async def stop_practice(self) -> None

async def switch_game(self, game_id: str, game_name: str) -> None
def get_state(self) -> dict  # full snapshot for API/SSE

# SSE
def subscribe_sse(self) -> asyncio.Queue
def unsubscribe_sse(self, queue: asyncio.Queue) -> None
async def _notify_sse(self) -> None  # pushes get_state() to all subscribers

# Lifecycle
async def shutdown(self) -> None
```

**`route_event()` logic:**

```
rom_info        → always handled (game discovery via checksum)
game_context    → always handled (switch_game if different)
level_entrance  → if mode == "reference": buffer in ref_pending
level_exit      → if mode == "reference": pair with pending entrance, upsert split
attempt_result  → if mode == "practice": deliver to practice_session
```

No mode-checking sleep. No queue contention. One reader, one router.

**Practice session integration:** Currently `run_one()` polls the TCP queue for `attempt_result`. Instead:
- PracticeSession gets a `receive_result(event)` method and an internal `asyncio.Event`
- `run_one()` sends the load command, then `await`s the event
- `route_event()` calls `practice_session.receive_result(event)` which sets the event
- No queue access from practice at all

### 2. Event Loop Simplification

The two competing background tasks (`_reconnect_loop` + `_event_dispatch_loop`) merge into one clean loop:

```python
async def _event_loop(session: SessionManager, tcp: TcpManager):
    while True:
        if not tcp.is_connected:
            await tcp.connect()
            if not tcp.is_connected:
                await asyncio.sleep(2)
                continue
        event = await tcp.recv_event(timeout=1.0)
        if event:
            await session.route_event(event)
```

Reconnection and event dispatch in one loop. No mode branching. SessionManager decides what to do with each event.

### 3. SSE Push Updates

**Server side:**
- `GET /api/events` → `StreamingResponse` with `text/event-stream` content type
- On connect, creates a subscriber queue via `session.subscribe_sse()`
- Yields `data: {json}\n\n` lines as state snapshots arrive
- On disconnect, calls `session.unsubscribe_sse(queue)`
- `SessionManager._notify_sse()` is called after every state mutation (mode change, attempt result processed, reference split captured, game switch)

**Client side:**
- `EventSource('/api/events')` in `api.js`
- On message, parse JSON and call the appropriate tab renderer
- Fallback poll at 5s, but only activates when `EventSource.readyState === EventSource.CLOSED` (permanent failure). During transient reconnects, `EventSource` handles retry automatically — no duplicate updates from both SSE and poll firing simultaneously.
- SSE replaces the 500ms poll as the primary data source

### 4. Scheduler State File Filter

`Scheduler._load_splits_with_model()` currently returns all active splits regardless of state file existence.

**Change:** Add post-query filter:

```python
splits = [s for s in splits if s.state_path and os.path.exists(s.state_path)]
```

DB-level `WHERE state_path IS NOT NULL` handles the NULL case. File existence must be Python-side (SQLite can't stat files). This runs once per `pick_next()` call — O(N) on split count, acceptable.

**Practice simplification:** `run_one()` drops:
- The 50-iteration retry loop
- The `_skipped` set
- The `os.path.exists()` check

Becomes: `picked = self.scheduler.pick_next()` — if it returns something, it's valid.

### 5. JavaScript Restructure

Split `app.js` (334 lines) into ES native modules. `index.html` changes `<script src="app.js">` to `<script type="module" src="app.js">`. Module scripts are deferred by default, so no `DOMContentLoaded` wrapper needed. Inter-module imports use relative paths with `.js` extensions (e.g., `import { formatTime } from './format.js'`).

| File | Responsibility |
|------|---------------|
| `app.js` | Entry point: connects SSE, initializes tabs, imports modules |
| `api.js` | `fetchJSON(url, opts)` wrapper with error toasts, `connectSSE()` setup |
| `live.js` | Live tab rendering: `renderDisconnected()`, `renderIdle(data)`, `renderReference(data)`, `renderPractice(data)` |
| `model.js` | Model tab: split table, allocator/estimator dropdowns, model state display |
| `manage.js` | Manage tab: split editing, reference list, inline edits, import/export |
| `format.js` | `formatTime(ms)`, `formatDrift(drift, drift_info)`, `elapsedStr(isoDate)` |

**`updateLive()` breakup:** The current 91-line function becomes a dispatcher:
```javascript
function updateLive(data) {
    if (!data.connected) return renderDisconnected();
    switch (data.mode) {
        case 'idle': return renderIdle(data);
        case 'reference': return renderReference(data);
        case 'practice': return renderPractice(data);
    }
}
```

Each renderer is 15-20 lines.

**UX fixes:**
- "No game loaded" banner on Model/Manage tabs when `game_id` is null (check in `model.js` and `manage.js` render functions)
- "Start Practice" button disabled with title tooltip when no splits exist
- Favicon: inline SVG data URI in `index.html` `<head>`
- `catch` blocks on fetches show a brief toast/flash message instead of silent swallow
- `elapsedStr()` returns "0:00" for any non-finite input (NaN:NaN hardening)

### 6. Lua Cleanup

**Practice state consolidation.** Replace 7 globals with one table:

```lua
local practice = {
    active = false,
    state = PSTATE_IDLE,
    split = nil,
    start_ms = 0,
    elapsed_ms = 0,
    completed = false,
    result_start_ms = 0,
}

local function practice_reset()
    practice.active = false
    practice.state = PSTATE_IDLE
    practice.split = nil
    practice.start_ms = 0
    practice.elapsed_ms = 0
    practice.completed = false
    practice.result_start_ms = 0
end
```

**Split `detect_transitions()` (71 lines)** into:
- `detect_transitions(prev, curr)` — pure detection, returns event type + event data, no side effects. Handles three cases: entrance, death, and exit.
- `on_level_entrance(event_data)` — logs to JSONL, sends TCP event, queues state save
- `on_death()` — sets `died_flag`, resets `level_start_frame` (currently inlined in `detect_transitions`)
- `on_level_exit(event_data)` — logs to JSONL, sends TCP event

**`draw_practice_overlay()` dedup:** Extract helper for timer-color + goal-label rendering shared between PLAYING and RESULT states.

**game_id gating:** When reference mode receives a level entrance and `game_id` is nil, send an error event over TCP: `{"type": "error", "message": "No game context — save state skipped"}`. Dashboard surfaces this in the reference UI as a warning banner. Once `game_context` arrives, subsequent entrances work normally. The skipped entrance is acceptable (user re-enters the level).

### 7. AHK & Lifecycle

**Shutdown endpoint:** `POST /api/shutdown` on SessionManager:
1. Stop practice if running (sends `practice_stop` to Lua)
2. Stop reference if running (clears capture state)
3. Close TCP connection
4. Signal uvicorn to exit. On Windows, `signal.SIGINT` is unreliable for non-console processes — use uvicorn's `Server.should_exit = True` if the server instance is accessible, or fall back to `signal.CTRL_C_EVENT`. The AHK fallback (taskkill) covers the case where the HTTP call itself fails.

**AHK changes to `scripts/spinlab.ahk`:**
- `Ctrl+Alt+X`: Call `curl -X POST http://localhost:15483/api/shutdown` first. Fall back to taskkill if HTTP fails (dashboard already dead). Then kill Mesen if running.
- `Ctrl+Alt+W`: Remove the `ProcessExist("Mesen.exe")` / `launch.bat` block. Only starts the dashboard. Mesen launches from the dashboard's "Launch Emulator" button.

**CLAUDE.md updates:**
- Add `scripts/spinlab.ahk` to the architecture overview with hotkey descriptions
- Fix the worktree pip install note (pyproject.toml is at repo root; `pip install -e .` is correct)

### 8. Kalman Cleanup

Replace the ~5 instances of 14-parameter `KalmanState(...)` constructors (in `kalman.py`) where an existing state is being modified with `dataclasses.replace(state, mu=..., P_mm=...)`. Constructors that create initial/default states stay as-is. Eliminates repeated boilerplate where only 2-3 fields change.

### 9. Testing

The refactoring creates clean seams for new tests:

| What | How |
|------|-----|
| SessionManager state transitions | Unit: construct with mock TCP + in-memory DB, call start/stop methods, assert mode and state |
| Event routing | Unit: feed typed events into `route_event()`, assert correct handler called, correct state changes |
| Event routing during practice | Unit: feed `attempt_result` during practice mode, assert it reaches practice session's `receive_result()` |
| Scheduler state file filter | Unit: create splits with/without state files on disk, assert `pick_next()` only returns valid ones |
| Practice without retry loop | Simplify existing tests: no fake state file dance, scheduler handles it |
| SSE delivery | Integration: connect to `/api/events`, trigger state change on SessionManager, assert event received |
| Shutdown endpoint | Integration: `POST /api/shutdown`, assert clean teardown |
| Reference with nil game_id | Unit on SessionManager: route a `level_entrance` with no game context, assert error event emitted |

## What's Not Changing

- **Database schema** — no migrations needed
- **TCP protocol** — same JSON messages, same port
- **Lua TCP server** — same socket setup, just cleaner internal state
- **Scheduler algorithm** — Kalman math untouched, just adds a filter
- **Config.yaml format** — no changes

# SpinLab Session Manager Design

Merges dashboard + orchestrator into a single always-on Python process that manages reference capture, practice orchestration, and the web UI. Replaces the current two-process architecture (dashboard reads state file written by orchestrator) with a unified session manager that owns the Lua TCP connection.

## Motivation

The current architecture has the dashboard and orchestrator as separate processes communicating indirectly via a state file and shared DB. This causes:
- Allocator changes in the dashboard don't take effect until the orchestrator re-reads the DB
- Up Next queue is stale (only updates after each attempt)
- No live view during reference captures (batch JSONL → YAML → DB pipeline)
- Two processes to manage, coordinate, and kill

Merging them eliminates all of these problems.

## Architecture

### Single process: the session manager

The session manager is a FastAPI app that:
1. Serves the web dashboard UI
2. Owns the single TCP connection to Lua
3. Runs reference capture (receives transition events, pairs into splits, writes to DB)
4. Runs practice orchestration (sends practice_load commands, receives attempt_result, updates model)

### Mode detection

Three modes, mutually exclusive:

- **Idle** — No TCP connection to Lua (Mesen not running). Dashboard shows split library, model state, reference management.
- **Reference** — TCP connected, no practice session active. Receives passive transition events from Lua, pairs entrance/exit in memory, writes completed splits to DB. Dashboard shows splits appearing live.
- **Practice** — TCP connected, practice session started (via dashboard button or AHK hotkey). Runs the orchestrator loop as an async background task. Dashboard shows live session stats, current split, queue.

The session manager attempts TCP connection to Lua on a timer (every 2-3 seconds). When connected, it defaults to reference mode. Practice mode is explicitly started/stopped via API endpoint.

**TCP single-client constraint:** Lua's TCP server accepts exactly one client (`server:listen(1)`). If the standalone orchestrator (or another client) is already connected, the dashboard's connection will be rejected. The TCP manager handles this gracefully: connection refused → stay in Idle mode, retry on timer. During the Phase 3 transition period, the dashboard logs a warning if connection is refused (likely means the old orchestrator is running). After Phase 3, the standalone orchestrator is deprecated and this is no longer an issue. If Mesen restarts mid-practice, the TCP disconnect ends the practice session cleanly — the user must explicitly restart practice from the dashboard.

### File layout after merge

```
python/spinlab/
├── dashboard.py          # FastAPI app, API endpoints, mode management
├── tcp_manager.py        # NEW: async TCP client for Lua (shared by ref + practice)
├── practice.py           # NEW: practice loop logic (extracted from orchestrator.py)
├── capture.py            # Batch import for old JSONL logs (backwards compat)
├── scheduler.py          # Unchanged
├── db.py                 # Add references table, ordinal column, split edit queries
├── orchestrator.py       # DEPRECATED: standalone entry point kept for CLI fallback
```

### TCP manager

`tcp_manager.py` provides an async wrapper around the Lua TCP socket:

- `connect(host, port)` — non-blocking connect with retry
- `disconnect()` — clean shutdown
- `send(msg)` — send newline-delimited message
- `recv_events()` — async generator yielding parsed JSON events
- `is_connected` — connection state property

Both reference capture and practice orchestration use the same TCP manager instance. Only one can be active at a time (reference capture pauses when practice starts, resumes when it stops — matching current Lua behavior where passive logging suspends during practice mode).

### Practice loop

Extracted from `orchestrator.py` into `practice.py` as an async function:

```python
async def run_practice_loop(
    tcp: TcpManager,
    db: Database,
    scheduler: Scheduler,
    game_id: str,
    on_attempt: Callable,  # callback for dashboard state updates
) -> None:
```

Runs as a background `asyncio.Task` inside the FastAPI app. The dashboard starts/stops it via API endpoints (`POST /api/practice/start`, `POST /api/practice/stop`).

Key difference from current orchestrator: no state file. The dashboard reads current split, queue, and session stats directly from in-memory state in the practice loop.

### Reference capture (live)

When in reference mode, the TCP manager receives transition events from Lua and the dashboard pairs them into splits in memory (same logic as `capture.py:pair_events()` but incremental). Completed splits are written to the DB immediately with a `reference_id` FK.

The dashboard polls `/api/state` and shows new splits appearing in real-time during a reference run.

### State flow (no more state file)

```
Dashboard (FastAPI)
  ├── TcpManager (async TCP client)
  │     ├── Reference mode: receives transition events → pairs → DB
  │     └── Practice mode: sends practice_load, receives attempt_result → DB
  ├── Scheduler (in-memory, allocator changes take effect instantly)
  ├── DB (SQLite, shared state)
  └── Web UI (polls /api/state every 1s)
```

## Database Changes

### New table: `capture_runs`

Groups splits by capture run. One capture run is active at a time per game. (Named `capture_runs` to avoid the SQL reserved word `references`.)

```sql
CREATE TABLE IF NOT EXISTS capture_runs (
  id TEXT PRIMARY KEY,
  game_id TEXT NOT NULL REFERENCES games(id),
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  active INTEGER DEFAULT 0
);
```

### splits table additions

```sql
ALTER TABLE splits ADD COLUMN reference_id TEXT REFERENCES capture_runs(id);
ALTER TABLE splits ADD COLUMN ordinal INTEGER;
```

- `reference_id` — which capture run produced this split
- `ordinal` — position in capture sequence (1-based). Used by Round Robin allocator for ordering and by the dashboard for display order.

### Split edit queries

New DB methods for Phase 2:

- `update_split(split_id, description=, goal=, active=)` — partial update
- `delete_split(split_id)` — soft delete: sets `active=0`. Preserves attempts and model state for historical data. Hard delete only via `reset_all_data()`.
- `get_splits_by_reference(reference_id)` — all splits in a capture run

### Reference CRUD

- `create_reference(id, game_id, name)` — new reference run
- `list_references(game_id)` — all references for a game
- `set_active_reference(reference_id)` — deactivate others, activate this one
- `delete_capture_run(reference_id)` — soft-delete: deactivate all splits in the run, remove the capture_run record. Attempts are preserved.
- `rename_capture_run(reference_id, name)` — update name

### Migration path

Existing splits (no `reference_id`) get migrated: create a capture_run from each manifest file, assign splits to it. The most recent capture_run is set as active. Ordinals are assigned from the manifest entry order (enumerate the splits list). If the manifest file is unavailable, ordinals are assigned from the existing `level_number, room_id` sort order.

## API Changes

### New endpoints

**Practice control:**
- `POST /api/practice/start` — start practice session (connects to Lua, begins orchestrator loop)
- `POST /api/practice/stop` — stop practice session (sends practice_stop to Lua, ends session)

**Reference management:**
- `GET /api/references` — list all references for current game
- `POST /api/references` — create new reference (auto-generated during live capture)
- `PATCH /api/references/{id}` — rename
- `DELETE /api/references/{id}` — delete reference and its splits
- `POST /api/references/{id}/activate` — set as active reference

**Split editing:**
- `PATCH /api/splits/{id}` — update description, goal, active status
- `DELETE /api/splits/{id}` — remove split

**Manifest import (backwards compat):**
- `POST /api/import-manifest` — import a YAML manifest file as a new reference

### Modified endpoints

- `GET /api/state` — no longer reads state file. Returns mode, current split, queue, recent attempts, session stats directly from in-memory state. Includes `tcp_connected` boolean.

## Frontend Changes

### Phase 1: Model tab improvements

**Column header renames:**

| Current | New | Tooltip |
|---------|-----|---------|
| Split | Split | Level section being practiced |
| μ (s) | Avg | Expected completion time in seconds |
| Drift | Trend | How your time changes per run (negative = improving) |
| 95% CI | Range | 95% confidence interval for the trend |
| m_i | Value | Practice value: how much time you save per run here |
| Runs | Runs | Completed practice attempts |
| Gold | Best | Your fastest completion |

Tooltips shown on hover via `title` attribute on `<th>` elements.

### Phase 2: Manage tab — split editor

The Manage tab gets a split list view:

- Table of all splits in the active reference, ordered by ordinal
- Columns: Name (editable), Level, Goal (dropdown: normal/key/orb), Ref Time, Active (toggle)
- Delete button per row (with confirm)
- Reference selector dropdown at top (switch active reference, rename, delete)

### Phase 3: Live capture view

When in reference mode, the Live tab shows:
- "Reference Run" header with section count
- List of splits appearing in real-time as they're captured
- Each split shows: level, room, goal, reference time, with inline name editing
- "Finalize" button to save the reference and switch to it

### Overlay change (Phase 1)

The Lua overlay currently shows `elapsed / reference_time`. Change to show `elapsed / expected_time` where expected_time = μ from the Kalman model. Fall back to reference_time when no model state exists (first run of a split).

Implementation: add `expected_time_ms` field to `SplitCommand` dataclass. The practice loop computes it from `picked.estimator_state.mu * 1000` when available, falling back to `picked.reference_time_ms`. The Lua `parse_practice_split()` function reads `expected_time_ms` and uses it for the overlay comparison instead of `reference_time_ms`.

### Lua TCP event forwarding (Phase 3)

Currently, the Lua script writes transition events (level_entrance, level_exit) to a JSONL file via `log_jsonl()` but does NOT send them over the TCP socket. For live reference capture, the Lua script must be modified to also forward these events over TCP when a client is connected and practice mode is not active.

Changes to `spinlab.lua`:

- In `detect_transitions()`, when logging a `level_entrance` or `level_exit` event, also send the JSON line over the TCP socket if `client` is connected and `practice_mode` is false.
- The JSON format is identical to what's written to the JSONL file (same fields: event, level, room, goal, elapsed_ms, state_path, timestamp_ms).
- Continue writing to JSONL as well (backwards compat for batch import).

## Round Robin ordering

The Round Robin allocator currently iterates splits in DB query order (`level_number, room_id`). Change to sort by `ordinal` column, which records the order splits were captured during the reference run.

This matches the player's mental model: practice splits in the order you encounter them in a run.

`_load_splits_with_model()` in `scheduler.py` already returns splits in DB order. Add `ordinal` to the ORDER BY clause.

## Phased Implementation

### Phase 1 — Quick wins (no architecture changes)

All changes work within the current two-process architecture:

1. Lua overlay: send `expected_time_ms` in practice_load, use in overlay (fall back to reference_time_ms)
2. Model tab: rename column headers, add tooltips
3. Dashboard: compute Up Next queue server-side from scheduler's `peek_next_n()` (bypass stale state file). Note: Round Robin queue preview may differ from orchestrator's since `_index` state isn't shared between processes — this is acceptable until Phase 3 unifies them.
4. DB migration: add `ordinal` column to splits, populate from manifest position
5. Round Robin: sort by ordinal
6. Commit #6-13 bug fixes from earlier session

### Phase 2 — Reference editor + split management

Builds the Manage tab and introduces the references concept:

1. DB: add `references` table, `reference_id` FK on splits
2. Migration: create references from existing manifests, assign splits
3. API: split PATCH/DELETE, reference CRUD endpoints
4. Manage tab UI: split list with inline editing, reference selector
5. Import endpoint for YAML manifests (backwards compat)

### Phase 3 — Unified session manager

The big merge — dashboard becomes the single always-on process:

1. `tcp_manager.py`: async TCP client with auto-reconnect
2. Live reference capture: dashboard receives events from Lua, writes splits to DB in real-time
3. Practice loop as async background task inside dashboard (extract from orchestrator.py)
4. Remove state file (dashboard has current split + queue in memory)
5. Practice start/stop from dashboard UI
6. Auto-start on login (AHK startup script or Windows Task Scheduler)
7. Deprecate standalone orchestrator.py entry point

## Testing

- **Phase 1**: existing tests cover scheduler, allocators, capture. Add test for ordinal ordering in Round Robin.
- **Phase 2**: add tests for split PATCH/DELETE DB operations, reference CRUD, migration from manifests.
- **Phase 3**: add tests for TCP manager (mock socket), practice loop (mock TCP), reference capture pairing. Integration test: start practice via API, verify state updates without state file.

## What this does NOT cover

- Checkpoint-based splits (starting from mid-level checkpoints)
- 480px width / responsive layout
- Multi-game support (schema supports it, UI doesn't)
- Video recording integration

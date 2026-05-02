# SpinLab Architecture

## System Overview

SpinLab turns SNES-romhack speedrun practice into a spaced-repetition loop. The Lua script runs inside Mesen2 and operates in five modes; the Python dashboard is the orchestrator.

- **Idle** (default): Lua passively watches memory and emits transition events. Zero side effects beyond reads.
- **Reference**: Records controller inputs to a `.spinrec` file and saves states at level entrances, checkpoints, and cold spawns. Each transition pairs into a database segment.
- **Replay**: Loads a `.spinrec` plus its companion frame-0 save state, then replays controller inputs via `emu.setInput()` at any speed. Existing transition detection fires naturally; events are tagged `source: "replay"`.
- **Practice**: Python orchestrator picks a segment, sends `practice_load` to Lua, Lua loads the state and shows an overlay. On completion or death, Lua emits `attempt_result` and the orchestrator picks the next segment.
- **Fill-gap / Cold-fill**: Loads a "hot" save state so the player can die and capture the missing "cold" (post-respawn) variant. Cold-fill is the batched form for many segments at once.

## Components

- **Lua script** (`lua/spinlab.lua`) — Always-on. Owns transition detection, save-state I/O, the practice overlay, `.spinrec` recording/replay, and the TCP server (port 15482).
- **Dashboard** (`python/spinlab/dashboard.py`) — FastAPI app on port 15483. SSE (`/api/events`) is the primary update mechanism; `/api/state` is the polling fallback.
- **SessionManager** (`python/spinlab/session_manager.py`) — Central state owner: mode, game context, scheduler. Single `route_event()` entry point dispatches Lua events to delegated controllers. Pushes state snapshots to SSE subscribers.
- **ReferenceController** (`python/spinlab/capture/reference.py`) — Reference + replay capture, multi-session lifecycle (start / stop / resume / finalize / save-and-finish / discard). Owns the `paused_run_id` / `recorder.capture_run_id` invariant.
- **SegmentRecorder** (`python/spinlab/capture/recorder.py`) — Pairs incoming transition events into segments and writes them to the DB along with timing rows (`recorded_segment_times`).
- **ColdFillController** (`python/spinlab/capture/cold_fill.py`) — Batched cold-variant capture across many segments.
- **Scheduler** (`python/spinlab/scheduler.py`) — Wires estimators and the allocator. All registered estimators run on every attempt; the active estimator's output feeds the allocator.
- **PracticeSession** (`python/spinlab/practice.py`) — Async loop: pick segment → load state → wait for `attempt_result` → log → pick next.
- **Database** (`python/spinlab/db/`) — SQLite via mixin-composed repositories. All consumers `from spinlab.db import Database`.
- **TcpManager** (`python/spinlab/tcp_manager.py`) — Async TCP client. Single reader coroutine dispatches events into an `asyncio.Queue`.
- **Frontend** (`frontend/src/`) — TypeScript + Vite. Built output goes to `python/spinlab/static/` (git-ignored). Chart.js for segment history. API response types in `types.ts` must stay in sync with Python response models. See `CLAUDE.md` for dev/build/test commands.

## Reference Run State Machine

A reference run can span multiple sessions (e.g. a 50-hour run played across many days). The state model lives entirely in `ReferenceController`:

- **IDLE** — no run loaded. Both `paused_run_id` and `recorder.capture_run_id` are `None`.
- **RECORDING** — a capture session is active; `recorder.capture_run_id` is set; `paused_run_id` is `None`.
- **PAUSED** — a `draft=1` capture run exists with no active session; `paused_run_id` is set; recorder is cleared.

`paused_run_id` and `recorder.capture_run_id` are a mutually exclusive pair. The only methods that mutate them are `_enter_recording`, `_enter_paused`, and `_enter_idle`, each of which asserts the invariant after the transition. Callers elsewhere read but never write.

**Lifecycle:**

1. `start_reference` (IDLE → RECORDING) — creates a `capture_run` with `draft=1` and a first `capture_session` row, sends `reference_start` to Lua with the spinrec path.
2. `stop_reference` (RECORDING → PAUSED) — non-destructive. Ends the capture session, leaves the run draft. Multiple stop-resume cycles produce multiple `capture_sessions` rows under the same run.
3. `resume_reference` (PAUSED → RECORDING) — opens a new capture session under the existing paused run.
4. `finalize_run` (PAUSED → IDLE) — drains `recorded_segment_times` into seed `attempts`, promotes draft to active, rebuilds estimator state.
5. `save_and_finish_run` (RECORDING → IDLE) — combined stop + finalize, atomic via explicit `BEGIN IMMEDIATE`. Either every step succeeds or every state is left exactly as it was.
6. `discard_paused_run` (PAUSED → IDLE) — hard-deletes the run and all dependent rows (CASCADE).

**Recovery on startup**: `recover_paused_capture_run` checks for orphaned `draft=1` runs and restores them as paused so the user can resume after a crash. A partial unique index (`idx_one_paused_run_per_game` on `capture_runs(game_id) WHERE draft=1 AND id NOT LIKE 'replay_%'`) prevents two paused runs for the same game.

## Three "Session" Concepts

The word "session" refers to three different things — distinct in code, easy to conflate in conversation.

- **Capture session** (`capture_sessions` table) — one continuous recording window inside a multi-session reference run. A `capture_run` has 1..N capture sessions; each has its own `.spinrec` file and its own `ordinal`.
- **Practice session** (`sessions` table) — one practice loop instance from start to stop. Tracks attempt counts and completion rates for that loop.
- **`attempts.parent_id`** — polymorphic foreign key. References a practice session id when `source='practice'`, and a `capture_run` id when `source='reference'` (seeded attempts from a finalized reference run). The shared column reflects "the parent context this attempt belongs to," not a specific table.

## IPC: TCP Socket

Mesen2 has LuaSocket compiled in. The Lua script runs a TCP server; Python connects as client. Messages are newline-delimited JSON. The single source of truth for the IPC contract is `python/spinlab/protocol.py` — every event and command is a typed dataclass.

**Python → Lua commands:** `game_context`, `reference_start`, `reference_stop`, `replay`, `replay_stop`, `practice_load`, `practice_stop`, `speed_run_load`, `speed_run_stop`, `fill_gap_load`, `cold_fill_load`, `set_conditions`, `set_invalidate_combo`, `reset`.

**Lua → Python events:** `rom_info`, `game_context`, `level_entrance`, `checkpoint`, `death`, `spawn`, `level_exit`, `attempt_result`, `attempt_invalidated`, `rec_saved`, `replay_started`, `replay_progress`, `replay_finished`, `replay_error`, `speed_run_checkpoint`, `speed_run_death`, `speed_run_complete`.

`SPEED_UNCAPPED = 0` in the Python API means "run as fast as possible." Mesen's `emu.setSpeed(0)` means "paused" — the Lua side must never pass `SPEED_UNCAPPED` directly to `setSpeed`.

## Database Schema

SQLite (`{data.dir}/spinlab.db`). WAL mode, foreign keys on. Schema lives in `python/spinlab/db/core.py`.

**Core tables:**

- **`games`** — auto-discovered from ROM checksums (truncated SHA-256, 16 hex chars).
- **`waypoints`** — `(game, level, endpoint_type, ordinal, conditions_json)`. Save states attach here. Two waypoints at the same geographic endpoint with different conditions are distinct waypoints.
- **`segments`** — edges between waypoints. Deterministic IDs from `(game_id, level_number, start_type, start_ordinal, end_type, end_ordinal)`. Carry `is_primary` (per-geography flag), `active`, `reference_id` (creating capture_run), and `capture_session_id` (creating session).
- **`waypoint_save_states`** — variant per waypoint: `cold` (post-respawn) or `hot` (at-checkpoint).
- **`attempts`** — every practice attempt and every seeded reference attempt. `parent_id` is polymorphic (see "Three session concepts"). `clean_tail_ms` records time from the last death to completion.
- **`capture_runs`** — a reference recording (live or replay). `draft=1` while pending save, `active=1` for the currently selected reference per game.
- **`capture_sessions`** — one window of recording inside a multi-session run. CASCADE-deletes from `capture_runs`. `(capture_run_id, ordinal)` is unique.
- **`recorded_segment_times`** — per-session timing rows captured during a reference run. Drained into seed `attempts` at finalize. CASCADE-deletes from `capture_sessions`.
- **`model_state`** — `(segment_id, estimator)` PK. One serialized state + last `ModelOutput` per segment per estimator.
- **`sessions`** — practice session metadata (start, end, attempt counts).
- **`allocator_config`** — persisted active allocator and estimator names.

**Schema migration policy:** `db/core.py:_init_schema` drops any table whose columns drift from `_expected_columns()`. This is fine while the DB is recreatable. Once a table holds data the user would not want to lose, switch to a forward-only migration log instead — see `docs/BACKLOG.md` for the trigger.

## Scheduler: Estimator + Allocator Pipeline

The scheduler has two pluggable layers, both via decorator-based registries.

**Estimators** (`python/spinlab/estimators/`) track per-segment performance and produce a `ModelOutput` prediction. All registered estimators run on every attempt; only the active estimator's output feeds the allocator.

Registered names:
- `kalman` — Kalman filter on `[mu, d]` state with full covariance (default).
- `rolling_mean` — rolling-window mean, model-free baseline.
- `exp_decay` — `time(n) = A * exp(-rate * n) + asymptote` via scipy `curve_fit`.

`ModelOutput` is nested: `total: Estimate` (wall-clock time, including deaths) and `clean: Estimate` (clean tail). Each `Estimate` has nullable fields. `None` means "model cannot compute this with available data" — never a silent fallback. See `docs/model-improvements-spec.md`.

**Allocators** (`python/spinlab/allocators/`) pick the next segment from a list of `SegmentWithModel`.

Registered names: `greedy` (highest expected improvement, default), `round_robin`, `random`, `least_played`, `mix` (weighted blend of others).

State is persisted per-segment per-estimator in `model_state`. Active estimator and allocator names live in `allocator_config` and can be switched at runtime via `POST /api/estimator` / `POST /api/allocator`.

Segment history (`GET /api/segments/{id}/history`) is computed on demand: the route replays all attempts through every estimator to produce per-attempt curves. There is no cached history — it always reflects current estimator parameters.

## Save States and `.spinrec`

- **Save states are files.** Mesen2's `emu.saveSavestate()` returns a binary blob; SpinLab writes it to disk under `{script_data_dir}/states/{game_id}/`. Loading must happen inside a `startFrame` or `cpuExec` callback.
- **Cold/hot variants.** Checkpoint endpoints have a "hot" (captured at the checkpoint frame) and a "cold" (captured on first respawn). Practice loads cold by default; fill-gap mode exists to capture missing colds.
- **`.spinrec`** is a 32-byte header plus one `uint16` per frame (SNES joypad bitmask). Reader/writer in `python/spinlab/spinrec.py`; Lua serializer is inline in `lua/spinlab.lua`. Every reference recording produces a `.spinrec` next to its frame-0 `.mss`.
- **Replay injects inputs, not save states.** It loads the frame-0 state once and then feeds recorded inputs via `emu.setInput()`. `detect_transitions()` fires naturally and produces the same segment events as a live run, tagged `source: "replay"`.

## Logging

Dashboard logs to `{data_dir}/spinlab.log` (rotating, 1 MB max, 3 backups). Configured on `spinlab dashboard` startup. Routes log `logger.warning()` before returning HTTP 4xx responses for observability. All `logger.info` / `warning` / `exception` calls land in this file.

Integration test failures append a diagnostic block to the pytest report: `/api/state` snapshot, DB row counts, Mesen process status, and the last 30 lines from the `spinlab` logger ring buffer. Implemented in `tests/integration/conftest.py`.

## Test Layers

- **Unit** (`tests/unit/`) — fast, mocked dependencies. ~23 s.
- **Slow** (`@pytest.mark.slow`) — practice-loop and TCP-wait timing. ~4 s.
- **Emulator** (`@pytest.mark.emulator`) — headless Mesen + Lua + poke scenarios over real TCP. Includes smoke tests (full FastAPI + DB stack) and replay-fixture tests. ~6 s.
- **Frontend** (`@pytest.mark.frontend`) — static-asset and Vitest tests. Requires a built frontend.

See `CLAUDE.md` for the canonical commands.

## Emulator

- **Primary: Mesen2** — LuaSocket built in, async save state API, `emu.isKeyPressed()`, headless `--testRunner` mode.
- **Potential fallback: SNES9X-rr** — would require file-based IPC instead of TCP. Not a current priority; Mesen2 works.

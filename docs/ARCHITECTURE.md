# SpinLab Architecture

## System Overview

SpinLab turns SNES-romhack speedrun practice into a spaced-repetition loop. The Python dashboard is the single orchestrator. There is one backend: RetroArch + snes9x_libretro, connected via the libretro Network Command Interface (NCI) on UDP port 55355.

```
Vite (5173)  ──proxies /api──▶  FastAPI (15483)  ◀──NCI/UDP──▶  RetroArch (55355)
                                ┌──────────────────────┐
                                │  SessionManager      │
                                │  ReferenceController │
                                │  PracticeSession     │
                                │  Scheduler           │
                                │  SQLite DB           │
                                └──────────────────────┘
                                        │
                                RetroArchOrchestrator
                                        │
                          ┌─────────────┼─────────────┐
                       NCIClient    Poller          StateIO
                      (transport)  (60Hz WRAM)   (save/load)
                                       │
                               TransitionDetector
                               (predicates.py)
```

## Components

**NCI layer (`retroarch/nci.py`)** — `NCIClient` sends UDP datagrams to RetroArch and reads replies. Supports `VERSION`, `GET_STATUS`, `READ_CORE_RAM`, `SAVE_STATE`, `LOAD_STATE_SLOT`, `RESET`, `PAUSE_TOGGLE`, `RECORD_REPLAY`, `HALT_REPLAY`, `PLAY_REPLAY`. Reconnect-on-failure with log-spam suppression.

**Poller (`retroarch/poller.py`)** — reads SMW WRAM at 60 Hz via `READ_CORE_RAM`, hands snapshots to `TransitionDetector` and `ColdFillSpawnDetector`, emits typed event objects (`LevelEntrance`, `Checkpoint`, `Death`, `Spawn`, `LevelExit`). Calls `resync_after_state_load()` on both detectors after every state load to prevent phantom edges.

**TransitionDetector (`retroarch/predicates.py`)** — stateful SMW memory predicate engine. Detects level entrances, checkpoint hits, deaths (sprite-hit and exit-mode), and level exits (goal vs. abort). Address map in `retroarch/addresses.py` — must stay in sync with `tests/integration/addresses.py`.

**StateIO (`retroarch/state_io.py`)** — filesystem-shuffle approach to RA's slot-keyed save state API. Save: snapshot directory → `SAVE_STATE` → poll for new/changed file → move to SpinLab-keyed path. Load: copy SpinLab file to reserved slot 9999 → `LOAD_STATE_SLOT 9999`. Retries saves (3x) and moves (5x) to handle Windows file-lock delays. Details and known issues in `docs/retroarch-migration/slot-management.md`.

**MovieRecorder / MoviePlayer (`retroarch/movie.py`)** — BSV movie record/playback via NCI `RECORD_REPLAY` / `HALT_REPLAY` / `PLAY_REPLAY`. Same filesystem-shuffle pattern as StateIO for file discovery. Phase E option (a) shipped; see [Phase E state](#phase-e-state) below.

**RetroArchOrchestrator (`retroarch/orchestrator.py`)** — wires NCIClient + Poller + StateIO + MovieRecorder/Player into a coherent session lifecycle. Auto-detects `game_basename` from RA's `GET_STATUS` reply. Provides `_on_reference_start/stop`, `_on_practice_load`, `_on_replay`, `_on_cold_fill_load`, etc.

**SessionManager (`python/spinlab/session_manager.py`)** — central state owner. One `route_event()` entry point dispatches typed events to `ReferenceController`, `PracticeSession`, `ColdFillController`, or `SpeedRunTiming`. Pushes state snapshots to SSE subscribers after each event.

**ReferenceController (`capture/reference.py`)** — multi-session reference run lifecycle. States: IDLE → RECORDING → PAUSED → IDLE. `paused_run_id` and `recorder.capture_run_id` are a mutually exclusive pair maintained by `_enter_recording`, `_enter_paused`, `_enter_idle`. Recovery on startup: `recover_paused_capture_run` restores orphaned `draft=1` runs after a crash.

**SegmentRecorder (`capture/recorder.py`)** — pairs incoming transition events into segments and writes them to the DB along with `recorded_segment_times` rows.

**ColdFillController (`capture/cold_fill.py`)** — batched cold-variant capture. Loads a hot CP state, watches for death-then-respawn via `ColdFillSpawnDetector`, captures the post-respawn frame as the cold variant.

**Scheduler (`python/spinlab/scheduler.py`)** — wires estimators and the allocator. All registered estimators update on every attempt; only the active estimator's output feeds the allocator.

**PracticeSession (`python/spinlab/practice.py`)** — async loop: pick segment → load state → wait for `attempt_result` → log → pick next. Reload-on-death triggers on `Death` or `LevelExit(goal='abort')` while `PracticeTiming.is_armed`.

**Dashboard (`python/spinlab/dashboard.py`)** — FastAPI app on port 15483. SSE (`/api/events`) is the primary update mechanism; `/api/state` is the polling fallback. Routes in `routes/`.

**Frontend (`frontend/src/`)** — TypeScript + Vite. Built output goes to `python/spinlab/static/` (git-ignored). `types.ts` must stay in sync with Python response models.

## Data Flow: Reference Run

1. User clicks **Start Reference** → `POST /api/reference/start`.
2. `ReferenceController._enter_recording` creates a `capture_run` (draft=1) and a `capture_session` row, calls `orchestrator.start_reference`.
3. `RetroArchOrchestrator` starts `MovieRecorder` (BSV recording) and arms the poller.
4. Poller reads WRAM at 60Hz; `TransitionDetector` emits events.
5. On each event (entrance, checkpoint, etc.) `SegmentRecorder` pairs events into segments and calls `StateIO.save` — saves a `.mss` state file to a SpinLab-keyed path.
6. User clicks **Save & Finish** → `POST /api/reference/save_and_finish`.
7. `ReferenceController.save_and_finish_run` drains `recorded_segment_times` into seed `attempts`, promotes the draft to active, rebuilds estimator state.

Note: SAVE_STATE during BSV recording corrupts the recording (see Phase E state below). State files are written correctly; the `.replay` file from the same run is truncated.

## Data Flow: Practice Attempt

1. `PracticeSession` calls `allocator.next_segment()` → segment ID.
2. `orchestrator._on_practice_load` calls `StateIO.load(segment_state_path)` → RA loads the state.
3. Poller continues at 60Hz; `TransitionDetector` emits events.
4. On `Death` or `LevelExit(goal='abort')`: `PracticeSession.handle_death` → reload the same state.
5. On `LevelExit(goal='normal')`: attempt complete → log timing → `estimator.update` → `model_state` row updated → pick next segment.

## Reference Run State Machine

- **IDLE** — no run loaded. `paused_run_id` and `recorder.capture_run_id` are both `None`.
- **RECORDING** — active capture session. `recorder.capture_run_id` is set; `paused_run_id` is `None`.
- **PAUSED** — a `draft=1` run exists, no active session. `paused_run_id` is set; recorder is cleared.

Transitions: `start_reference` (IDLE→RECORDING), `stop_reference` (RECORDING→PAUSED), `resume_reference` (PAUSED→RECORDING), `finalize_run` (PAUSED→IDLE), `save_and_finish_run` (RECORDING→IDLE), `discard_paused_run` (PAUSED→IDLE).

A partial unique index (`idx_one_paused_run_per_game`) prevents two paused runs for the same game simultaneously.

## Phase E State

Phase E option (a) shipped 2026-05-08: `MovieRecorder` integrates into the reference flow and produces `.replay` (BSV) files alongside state files. Isolated playback via `MoviePlayer` is deterministic. However, **SAVE_STATE during BSV recording is broken in RA 1.22.2** — the recording terminates at the first SAVE_STATE call. Reference `.replay` files are therefore truncated and not suitable for replay-driven segment capture. Phase E option (b) (full replay → segment capture parity) is not yet implemented. See `docs/retroarch-migration/status.md` and `docs/retroarch-migration/slot-management.md` for the full picture.

## Three "Session" Concepts

- **Capture session** (`capture_sessions` table) — one continuous recording window inside a multi-session reference run. A `capture_run` has 1..N capture sessions.
- **Practice session** (`sessions` table) — one practice loop instance from start to stop.
- **`attempts.parent_id`** — polymorphic foreign key: a practice session id when `source='practice'`, a `capture_run` id when `source='reference'`.

## Database Schema

SQLite (`{data.dir}/spinlab.db`). WAL mode, foreign keys on. Schema in `python/spinlab/db/core.py`.

Core tables: `games`, `waypoints`, `segments`, `waypoint_save_states`, `attempts`, `capture_runs`, `capture_sessions`, `recorded_segment_times`, `model_state`, `sessions`, `allocator_config`.

Schema migration policy: `_init_schema` drops any table whose columns drift from `_expected_columns()`. Fine while the DB is recreatable. Switch to a forward-only migration log before the DB holds data users would not want to lose (see `docs/BACKLOG.md`).

## Scheduler: Estimator + Allocator Pipeline

**Estimators** (`estimators/`) track per-segment performance and produce `ModelOutput`. All registered estimators run on every attempt; only the active estimator's output feeds the allocator. `ModelOutput` fields are nullable — `None` means "not enough data", never a silent fallback.

Registered: `kalman` (Kalman filter on `[mu, d]` state), `rolling_mean`, `exp_decay`.

**Allocators** (`allocators/`) pick the next segment from a list of `SegmentWithModel`.

Registered: `greedy` (highest expected improvement), `round_robin`, `random`, `least_played`, `mix`.

State persisted per-segment per-estimator in `model_state`. Active names live in `allocator_config`, switchable at runtime via `POST /api/estimator` / `POST /api/allocator`.

## Save States

- **Save states are files.** SpinLab triggers RA to write via NCI `SAVE_STATE`, discovers the output file by mtime diff, and moves it to a SpinLab-keyed path under `spinlab_state_dir`.
- **Cold/hot variants.** Checkpoint endpoints have a "hot" (captured at the checkpoint frame) and a "cold" (captured on first respawn after death). Practice loads cold by default; cold-fill mode exists to capture missing colds.
- **State slot 9999** is reserved for load operations. SpinLab copies its keyed file to that slot path and fires `LOAD_STATE_SLOT 9999`. The reserved slot file is cleaned up on connect and after every load.

## Logging

Dashboard logs to `{data_dir}/spinlab.log` (rotating, 1 MB max, 3 backups). Routes log `logger.warning()` before returning HTTP 4xx responses for observability.

Integration test failures append a diagnostic block to the pytest report: `/api/state` snapshot, DB row counts, RA process status, and the last 30 lines from the `spinlab` logger ring buffer. Implemented in `tests/integration/conftest.py`.

## Test Layers

- **Unit** (`tests/unit/`) — fast, mocked dependencies.
- **Slow** (`@pytest.mark.slow`) — practice-loop and timing waits.
- **Emulator** (`@pytest.mark.emulator`) — headless RA + poke harness + smoke tests. Requires RA installed.
- **Frontend** (`@pytest.mark.frontend`) — static-asset and Vitest tests. Requires a built frontend.

See `CLAUDE.md` for canonical commands.

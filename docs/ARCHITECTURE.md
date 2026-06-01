# SpinLab Architecture

## System Overview

SpinLab turns SNES-romhack speedrun practice into a spaced-repetition loop. The Python dashboard is the single orchestrator. There is one backend: RetroArch + snes9x_libretro, connected via the libretro Network Command Interface (NCI) on UDP port 55355.

```
Vite (5173)  ──proxies /api──▶  FastAPI (15483)  ◀──NCI/UDP──▶  RetroArch (55355)
                                ┌──────────────────────┐
                                │  SessionManager      │
                                │  ReferenceController │
                                │  PracticeSession     │
                                │  HyperPlaySession    │
                                │  Scheduler           │
                                │  SQLite DB           │
                                └──────────────────────┘
                                        │
                                RetroArchOrchestrator
                                        │
                       ┌────────────────┼─────────────────┐
                    RAClient         Poller         MovieController
                  (NCI + save/      (60Hz WRAM)    (BSV record/play)
                   load + slot)          │
                                 TransitionDetector
                              ColdFillSpawnDetector
```

## Components

**NCIClient (`retroarch/nci.py`)** — sends UDP datagrams to RetroArch and reads replies. Supports `VERSION`, `GET_STATUS`, `GET_CONFIG_PARAM`, `READ_CORE_RAM`, `WRITE_CORE_RAM`, `SAVE_STATE`, `LOAD_STATE_SLOT`, `RESET`, `PAUSE_TOGGLE`, `FAST_FORWARD`, `FRAMEADVANCE`, `RECORD_REPLAY`, `HALT_REPLAY`, `PLAY_REPLAY`, slot ±, `QUIT`. Used by `RAClient` (transport) and `Poller` (RAM reads).

**RAClient (`retroarch/raclient.py`)** — the RA-mechanics layer. Wraps `NCIClient` and owns the filesystem-shuffle around RA's slot-keyed savestate API:
- **save_state:** snapshot mtimes of `<basename>.state*` → `SAVE_STATE` → poll for new/advanced file → move to a SpinLab-keyed path. 3 retries on the save + 5 on the move (Windows file-lock delays).
- **load_state:** copy the SpinLab file into the reserved slot (default 9999) → `LOAD_STATE_SLOT 9999` → bump a monotonic `state_version` so the poller resyncs before running detection on the next snapshot.
- Holds `RAMovieIO` for the movie controller, auto-detects `game_basename` from `GET_STATUS`, cleans up the reserved-slot file on connect and after each load.

**Poller (`retroarch/poller.py`)** — async loop that reads SMW WRAM at 60 Hz via `NCIClient.read_ram`, feeds `MemorySnapshot`s to `TransitionDetector` and `ColdFillSpawnDetector`, and emits typed events through a callback. Watches `RAClient.state_version` once per tick: when it changes, the next snapshot is treated as a fresh prev and detection is skipped (eliminates phantom edges after state loads).

**TransitionDetector (`retroarch/detector.py` + predicates in `predicates.py`)** — stateful SMW memory-predicate engine. Detects level entrances, checkpoint hits, deaths (sprite-hit and exit-mode), and level exits (goal vs. abort). Address map in `retroarch/addresses.py` — kept in sync with `tests/integration/addresses.py` (the integration map imports from the production module).

**ColdFillSpawnDetector (`retroarch/cold_fill_detector.py`)** — separate detector that activates per-segment for cold-fill capture: watches for death-then-respawn after loading a hot CP state, and emits the post-respawn frame so it can be saved as the cold variant.

**MovieController (`retroarch/movies.py`) + RAMovieIO (`retroarch/movie_io.py`)** — BSV movie record/playback. `RAMovieIO` is the low-level wrapper (handles `RECORD_REPLAY` / `HALT_REPLAY` / `PLAY_REPLAY`, the filesystem shuffle for `.replay` files, replay-slot resolution from RA's log, and a WRAM-advance verification heuristic on playback). `MovieController` owns the cross-call state (`_active_recording`, `_active_playback`, `_fast_forwarding`) and emits `ReplayStarted` / `ReplayFinished` / `ReplayError` events into the orchestrator's queue. Disabled entirely if no movie dir resolves at build time.

**RetroArchOrchestrator (`retroarch/orchestrator.py`, built by `retroarch/wiring.py`)** — implements the `EmuBackend` protocol. Owns `RAClient + Poller + MovieController + ConditionRegistry + timing modules`. Translates typed `protocol.*Cmd`s into RAClient calls; publishes typed `protocol.*Event`s into an `asyncio.Queue` consumed by `SessionManager.route_event`. Runs a 20 Hz tick loop alongside the poller for timing deadlines. Stays thin: dispatch + tick + event routing.

**EmuBackend (`emu_backend.py`)** — `Protocol` defining the surface the rest of the codebase depends on (`connect`, `disconnect`, `send_command`, `recv_event`, `save_state`, `load_state`). Everything above `RetroArchOrchestrator` is backend-agnostic.

**SessionManager (`session_manager.py`)** — central state owner. One `route_event()` entry dispatches each typed event to one of the controllers (`ReferenceController`, `PracticeSession`, `HyperPlaySession`, `ColdFillController`, `FillGapController`) or to its own internal handlers. Pushes state snapshots to SSE subscribers after each event.

**ReferenceController (`capture/reference.py`)** — multi-session reference-run lifecycle. States: IDLE → RECORDING → PAUSED → IDLE. `paused_run_id` and `recorder.capture_run_id` are a mutually exclusive pair, mutated only through `_enter_recording` / `_enter_paused` / `_enter_idle`, which assert the invariant after each transition. Also drives replay-as-capture (replay a saved `.replay` and re-emit segment captures). Recovery on game switch: `recover_paused_run` rehydrates any orphaned `draft=1` live run.

**SegmentRecorder (`capture/recorder.py`)** — pairs incoming transition events into segments. Deaths and the closing survived event are buffered in memory; at `_close_segment` the full event list is flushed into `attempts` atomically with the segment upsert — one row per died/survived event, all keyed to the just-computed `segment_id`.

**ColdFillController (`capture/cold_fill.py`)** — batched cold-variant capture. Walks a queue of segments missing cold states; for each, loads the hot CP state, waits for death-then-respawn via `ColdFillSpawnDetector`, captures the post-respawn frame.

**FillGapController (`capture/fill_gap.py`)** — single-shot cousin of `ColdFillController`. User picks one specific segment; the controller loads its hot variant, waits for the next `SpawnEvent` with a state_path, returns to IDLE.

**PracticeSession (`practice.py`)** — async loop: pick segment → load state → wait for `AttemptResultEvent` → log → pick next. Reload-on-death triggers on `Death` or `LevelExit(goal='abort')` while an attempt is in flight (`_current_state_path` is the armed flag).

**HyperPlaySession (`hyper_play.py`)** — full-run mode that walks a `LevelPlan` end-to-end, recording per-level attempts against the active reference run.

**Scheduler (`scheduler.py`)** — wires the single sampler (`EmSuiteSamplerEstimator`) and the allocator. Sampler output feeds the allocator; state persists per-segment in `model_state`. The top-level allocator is always a `MixAllocator` built from per-allocator weights persisted in `allocator_config`.

**Dashboard (`dashboard.py`)** — FastAPI app on port 15483. SSE (`/api/events`) is the primary update mechanism; `/api/state` is the polling fallback. Routes are split across `routes/` modules (`reference.py`, `practice.py`, `hyper_play.py`, `model.py`, `segments.py`, `attempts.py`, `system.py`).

**Frontend (`frontend/src/`)** — TypeScript + Vite. Built output goes to `python/spinlab/static/` (git-ignored). Types are codegen'd from FastAPI's OpenAPI schema (`scripts/dump_openapi.py` → `frontend/openapi.json` → `frontend/src/api-types.ts`); source of truth is `python/spinlab/api_schemas.py`.

## Data Flow: Reference Run

1. User clicks **Start Reference** → `POST /api/reference/start`.
2. `ReferenceController._enter_recording` creates a `capture_runs` row (status=draft, kind=live) and a `capture_sessions` row, sends `ReferenceStartCmd` to the orchestrator.
3. `RetroArchOrchestrator` arms the poller and tells `MovieController` to start BSV recording.
4. Poller reads WRAM at 60 Hz; `TransitionDetector` emits typed events.
5. On each event (entrance, checkpoint, exit) `SegmentRecorder` pairs events into segments and calls `RAClient.save_state` via the backend — writes a `.state` file to a SpinLab-keyed path under `spinlab_state_dir`.
6. User clicks **Save & Finish** → `POST /api/reference/save_and_finish`.
7. `ReferenceController` calls `atomic_save_and_finish_run`: ends the capture session, promotes the draft to `saved`/`active` in one transaction. Attempt rows were already written per-segment as they closed — no drain step at finalize.

## Data Flow: Practice Attempt

1. `PracticeSession` calls `scheduler.pick_next()` → `SegmentWithModel`.
2. `PracticeLoadCmd` flows to the orchestrator → `RAClient.load_state(segment_state_path)` → RA loads the state, `state_version` bumps.
3. Poller's next tick sees the version change, resyncs detectors, then resumes at 60 Hz.
4. On `Death` or `LevelExit(goal='abort')` while armed: `PracticeSession.handle_death` → reload the same state.
5. On `LevelExit(goal='normal')`: attempt complete → `Scheduler.record_attempt` updates the sampler, persists model_state, logs the attempt → pick next segment.

## Reference Run State Machine

- **IDLE** — no run loaded. `paused_run_id` and `recorder.capture_run_id` are both `None`.
- **RECORDING** — active capture session. `recorder.capture_run_id` is set; `paused_run_id` is `None`.
- **PAUSED** — a `draft=1` run exists, no active session. `paused_run_id` is set; recorder is cleared.

Transitions: `start_reference` (IDLE→RECORDING), `stop_reference` (RECORDING→PAUSED), `resume_reference` (PAUSED→RECORDING), `finalize_run` (PAUSED→IDLE), `save_and_finish_run` (RECORDING→IDLE), `discard_paused_run` (PAUSED→IDLE).

A partial unique index (`idx_one_live_draft_per_game`) prevents two live drafts for the same game simultaneously. Replay-kind drafts are intentionally not unique-constrained (ephemeral, never recovered on restart).

## Three "Session" Concepts

- **Capture session** (`capture_sessions` table) — one continuous recording window inside a multi-session reference run. A `capture_run` has 1..N capture sessions.
- **Practice session** (`sessions` table) — one practice or speed-run loop instance from start to stop.
- **`attempts.session_id` / `attempts.capture_run_id`** — typed nullable FKs; exactly one is set per row (`CHECK` constraint). Practice and speed-run attempts use `session_id`; reference-seeded attempts use `capture_run_id`. The `source` enum (`practice` | `hyper_play` | `reference` | `replay`) discriminates within each category.

## Replay-as-capture

`MovieController` produces deterministic BSV `.replay` files alongside the `.state` files during a reference run. Replaying one of those files re-emits the same transition events through the production poller, so `ReferenceController.start_replay` can capture segments from a stored run instead of a live one. End-to-end coverage lives in `tests/integration/test_replay_fixture.py`. `ReplayStartedEvent.frame_count` populates `state["replay"]["total"]` — the only replay-progress signal exposed to the dashboard (per-frame progress isn't observable under RA).

## Database Schema

SQLite (`{data.dir}/spinlab.db`). WAL mode, foreign keys on. Schema is declared in numbered migration files under `python/spinlab/db/migrations/`; the runner in that package's `__init__.py` applies any unapplied file at `Database()` construction and records the result in `schema_migrations`.

Core tables: `games`, `waypoints`, `segments`, `waypoint_save_states`, `attempts`, `capture_runs`, `capture_sessions`, `model_state`, `sessions`, `allocator_config`, `schema_migrations`.

Schema-change workflow: create a new `NNNN_name.sql` file. Never edit an existing migration — that's how environments diverge.

Transactional model: the SQLite connection runs in autocommit mode. Single-statement mixin methods commit on the spot; multi-statement work composes via `with db.transaction():`. The context manager uses `BEGIN IMMEDIATE` at the top level and `SAVEPOINT` when nested, so methods that wrap their own body in `self.transaction()` (e.g. `hard_delete_capture_run` in `db/capture_runs.py`) still join an outer caller transaction cleanly — and top-level callers like `atomic_save_and_finish_run` in `capture/finalizer.py` use `db.transaction()` directly. See `db/core.py` module docstring for the full rationale.

## Scheduler: Sampler + Allocator Pipeline

**Sampler** (`estimators/em_suite_sampler.py`) — the sole model. `EmSuiteSamplerEstimator` tracks per-segment EMA suites + recency-weighted draw pools (`success_time_pool` / `death_time_pool`) and produces a `ModelOutput` whose `total.expected_ms` is a closed-form geometric mean over the suite. `ModelOutput` fields are nullable — `None` means "not enough data" (gate: ≥2 successes AND ≥2 deaths), never a silent fallback. State persisted per-segment in `model_state`. The `Estimator` / `EstimatorState` ABCs in `estimators/__init__.py` are retained as the ingestion seam for a future value-of-practice allocator (Spec #3); there is no registry / name-keyed factory.

**Allocators** (`allocators/`) pick the next segment from a list of `SegmentWithModel`. The scheduler always wraps them in a `MixAllocator` built from weights in `allocator_config` — including a sub-allocator means giving it a positive weight.

Registered: `greedy` (highest expected improvement), `round_robin`, `random`, `least_played`, `mix`.

Per-allocator weights live in `allocator_config`, switchable at runtime via `POST /api/allocator-weights`.

## Save States

- **Save states are files.** SpinLab triggers RA via NCI `SAVE_STATE`, discovers the output file by mtime diff, and moves it to a SpinLab-keyed `.state` path under `spinlab_state_dir`.
- **Cold/hot variants.** Checkpoint endpoints have a "hot" (captured at the checkpoint frame) and a "cold" (captured on first respawn after death). Practice loads cold by default; `ColdFillController` (batched) and `FillGapController` (single-shot) exist to capture missing colds.
- **Reserved slot (default 9999)** is used for load operations. SpinLab copies its keyed file to that slot path and fires `LOAD_STATE_SLOT 9999`. The reserved slot file is cleaned up on connect and after every load.
- Implementation lives in `RAClient.save_state` / `RAClient.load_state`; see `docs/retroarch-migration/slot-management.md` for the design and known edge cases.

## Logging

Dashboard logs to `{data_dir}/spinlab.log` (rotating, 1 MB max, 3 backups). Routes log `logger.warning()` before returning HTTP 4xx responses for observability.

Integration test failures append a diagnostic block to the pytest report: `/api/state` snapshot, DB row counts, RA process status, and the last 30 lines from the `spinlab` logger ring buffer. Implemented in `tests/integration/conftest.py`.

## Test Layers

- **Unit** (`tests/unit/`) — fast, mocked dependencies. The default workhorse.
- **Integration** (`tests/integration/`) — split by marker:
  - `@pytest.mark.emulator` for tests that drive a real RetroArch via the RA poke harness (`RAHarness` / `RAPokeEngine`) plus the replay fixture.
  - Frontend smoke test (`test_frontend_smoke.py`) runs under the default suite; requires a built `frontend/` and Playwright Chromium.
- **Frontend** (`frontend/`) — Vitest + happy-dom for pure logic and API-contract tests, run via `cd frontend && npm test`.

See `CLAUDE.md` for canonical commands.

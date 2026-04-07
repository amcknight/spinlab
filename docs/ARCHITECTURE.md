# SpinLab Architecture

## Components

1. **Lua Script** (`lua/spinlab.lua`): Runs inside Mesen2. Always-on. Three modes:
   - **Passive mode** (default): Watches memory addresses, logs section completions. When a reference run is active, also records controller inputs every frame into a `.spinrec` binary file.
   - **Replay mode**: Loads a `.spinrec` + companion `.mss` save state, injects recorded inputs via `emu.setInput()`. Segment events fire through `detect_transitions()` tagged with `source: "replay"`.
   - **Practice mode**: Loads save states on command, shows overlay, reads controller for L+D-pad ratings, reports results back.

2. **Dashboard** (`python/spinlab/dashboard.py`): FastAPI web app. Run with `spinlab dashboard`. Ports configurable via `config.yaml`.

3. **SessionManager** (`python/spinlab/session_manager.py`): Coordinator that owns mode and game context. Delegates to:
   - `CaptureController` — reference/replay/fill-gap/draft orchestration
   - `SSEBroadcaster` — subscriber queue management
   - `PracticeSession` — practice loop lifecycle

4. **Database** (`python/spinlab/db/`): SQLite via mixin-composed repositories. All consumers import `from spinlab.db import Database`.

5. **Frontend** (`frontend/src/`): TypeScript modules built with Vite. Output goes to `python/spinlab/static/` (git-ignored). SSE (`/api/events`) as primary update mechanism with polling fallback. API response types in `types.ts` must stay in sync with Python response models. See `CLAUDE.md` for dev/build/test commands.

## IPC: TCP Socket

Mesen2 has LuaSocket compiled in. The Lua script runs a TCP server; Python connects as client. Messages are newline-delimited JSON.

**Python → Lua commands:** `reference_start`, `reference_stop`, `replay`, `replay_stop`, `practice_load:<json>`, `practice_stop`, `fill_gap_load`.

**Lua → Python events:** `rom_info`, `game_context`, `level_entrance`, `checkpoint`, `death`, `spawn`, `level_exit`, `attempt_result`, `rec_saved`, `replay_started`, `replay_progress`, `replay_finished`, `replay_error`.

## Key Design Decisions

- **Save states are files.** Mesen2's `saveSavestate()` returns binary; written to disk. Loading must happen inside a `startFrame` or `cpuExec` callback.
- **Games auto-discovered from ROM checksums.** Truncated SHA-256 (16 hex chars) as game ID.
- **Segment IDs are deterministic** from (game_id, level_number, start_type, start_ordinal, end_type, end_ordinal).
- **Cold/hot start variants.** Checkpoint segments have "hot" (captured at checkpoint hit) and "cold" (captured on first respawn) save states.
- **`.spinrec` binary format.** 32-byte header + one uint16/frame (SNES joypad bitmask). Python reader/writer in `spinrec.py`; Lua inline.
- **Replay injects inputs, not save states.** Loads frame-0 state once, feeds recorded inputs via `emu.setInput()`. Existing `detect_transitions()` fires naturally.
- **Estimators are pluggable** via registry decorator. All estimators run on every attempt; the allocator reads the selected one.

## Emulator

**Primary: Mesen2** — LuaSocket built in, async save state API, `emu.isKeyPressed()`, headless test runner mode.

**Potential fallback: SNES9X-rr** — would require file-based IPC instead of TCP.

## Test Layers

1. **Unit tests** (`tests/`): Fast, mocked dependencies. ~23s. Run after any code change.
2. **Poke tests** (`tests/integration/test_*.py`): Headless Mesen + Lua + poke scenarios over real TCP. Test level transitions, segment detection, save state capture.
3. **Smoke tests** (`tests/integration/test_smoke.py`): Headless Mesen + real dashboard (FastAPI + Uvicorn + DB) in a background thread. Test that the assembled system works: endpoints return 200, game loads after TCP connect, reference start is accepted.

All integration tests use `@pytest.mark.emulator` and skip when Mesen is not available.

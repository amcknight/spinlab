# SpinLab — CLAUDE.md

## What This Is

SpinLab is a spaced-repetition practice system for SNES romhack speedrunning. It automatically saves states at segment boundaries (level entrances, checkpoints, goals) during reference runs, then serves them back in an intelligent practice loop (like Anki for segments). The player rates difficulty via controller input, and a scheduler determines what to practice next.

## Project Owner Context

Andrew is a software engineer and SNES romhack speedrunner. He has an existing LiveSplit autosplitter plugin called **kaizosplits** (C#) which contains memory address maps and transition detection logic for SMW romhacks. The kaizosplits code is in `reference/` for extracting memory addresses and detection patterns — it is NOT a dependency. He is experienced with Mesen2 Lua scripting (used it for RNG analysis with Mesen2 + Lua on a fish generator section).

## Architecture Overview

Four components in one repo:

```
spinlab/
├── CLAUDE.md              # This file
├── docs/
│   └── DESIGN.md          # Full architecture, IPC, schema, scheduler, build order
├── lua/
│   └── spinlab.lua        # The Mesen2 Lua script (always-on, three modes)
├── python/
│   └── spinlab/
│       ├── __init__.py
│       ├── cli.py              # Entry point: dashboard, replay, lua-cmd subcommands
│       ├── dashboard.py        # FastAPI web app, HTTP routes, event loop
│       ├── session_manager.py  # Thin coordinator: mode, game context, event routing
│       ├── capture_controller.py # Reference/replay/fill-gap/draft orchestration
│       ├── reference_capture.py  # Stateful segment builder during capture
│       ├── draft_manager.py    # Draft save/discard lifecycle
│       ├── practice.py         # Async practice session loop
│       ├── scheduler.py        # Wires estimators + allocators together
│       ├── sse.py              # SSE broadcaster (subscriber queue management)
│       ├── tcp_manager.py      # Async TCP client for Lua socket
│       ├── db/                 # SQLite interface (mixin-composed package)
│       │   ├── core.py         # Schema, connection, games, reset
│       │   ├── segments.py     # Segment CRUD + variants
│       │   ├── attempts.py     # Attempt logging + stats
│       │   ├── sessions.py     # Practice session lifecycle
│       │   ├── model_state.py  # Estimator state persistence, golds
│       │   └── capture_runs.py # Reference run CRUD, draft lifecycle
│       ├── models.py           # Data classes / types
│       ├── romid.py            # ROM checksum + game name extraction
│       ├── manifest.py         # Legacy YAML manifest import
│       ├── spinrec.py          # .spinrec binary format reader/writer
│       ├── estimators/         # Kalman, Model A (rolling), Model B (exp decay)
│       ├── allocators/         # Greedy, round-robin, random
│       └── static/
│           ├── app.js          # Entry point (ES module), wires tabs + SSE
│           ├── api.js          # fetchJSON, postJSON, connectSSE
│           ├── header.js       # Game selector, mode chip
│           ├── model.js        # Model tab (all estimators side-by-side)
│           ├── manage.js       # Reference/segment management tab
│           └── format.js       # segmentName, formatTime, elapsedStr
├── reference/              # kaizosplits source for memory address extraction
├── scripts/
│   ├── launch.sh           # Launches Mesen2 with Lua script auto-loaded
│   └── spinlab.ahk         # AHK hotkeys: Ctrl+Alt+W (start), Ctrl+Alt+X (stop)
└── config.yaml             # User config: ROM dir, emulator path, ports, scheduler
```

### Component Roles

1. **Lua Script** (`lua/spinlab.lua`): Runs inside Mesen2. Always-on. Three modes:
   - **Passive mode** (default): Watches memory addresses, logs all section completions with timestamps. Silent data collection during real runs. When a reference run is active, also records controller inputs every frame into a `.spinrec` binary file via `inputPolled` callback.
   - **Replay mode** (toggled via TCP `replay` command): Loads a `.spinrec` + companion `.mss` save state, injects recorded inputs via `emu.setInput()` at configurable speed. Segment events fire through `detect_transitions()` tagged with `source: "replay"`. Reports `replay_progress` and `replay_finished` events.
   - **Practice mode** (toggled via TCP connection from orchestrator): Loads save states on command, shows overlay (segment name, end condition, timer, rating prompt), reads controller for L+D-pad ratings, reports results back.

2. **Practice Session** (`python/spinlab/practice.py`): Async loop that picks next segment from scheduler, sends load commands to Lua via TCP, receives completion results, updates DB.

3. **Python CLI** (`python/spinlab/cli.py`): Entry point with `dashboard`, `replay`, and `lua-cmd` subcommands. Reads ports from `config.yaml`.

4. **Launch Script** (`scripts/launch.sh`): Starts Mesen2 with the ROM and Lua script. Takes care of paths so the user never manually loads scripts.

5. **Dashboard** (`python/spinlab/dashboard.py`): FastAPI web app. Run with `spinlab dashboard`. Ports configurable via `config.yaml` (`network.port` for TCP, `network.dashboard_port` for HTTP). Endpoints are thin wrappers delegating to `SessionManager`.

6. **SessionManager** (`python/spinlab/session_manager.py`): Thin coordinator that owns mode and game context. Delegates to focused controllers:
   - `CaptureController` — reference/replay/fill-gap/draft orchestration
   - `SSEBroadcaster` — subscriber queue management
   - `PracticeSession` — practice loop lifecycle
   Single `route_event()` entry point for all TCP events.

7. **Database** (`python/spinlab/db/`): SQLite interface split into focused repository modules (segments, attempts, sessions, model_state, capture_runs) composed via mixins into a single `Database` class. All consumers import `from spinlab.db import Database`.

8. **Frontend** (`python/spinlab/static/`): Vanilla JS ES modules. `app.js` is the entry point, imports from `api.js` (SSE + fetch), `header.js` (game selector + mode chip), `model.js` (all estimators side-by-side), `manage.js` (reference management), `format.js` (shared formatters). Uses SSE (`/api/events`) as primary update mechanism with polling fallback.

### IPC: TCP Socket (via Mesen2's built-in LuaSocket)

Mesen2 has LuaSocket compiled in. The Lua script runs a lightweight TCP server. The Python orchestrator connects as a client. Messages are newline-delimited JSON.

See `docs/DESIGN.md` § IPC Contract for the full message spec. Recording/replay adds: `reference_start` (with `.spinrec` path), `reference_stop`, `replay` (with path + speed), `replay_stop`. Lua emits: `rec_saved`, `replay_started`, `replay_progress`, `replay_finished`, `replay_error`.

### Emulator Choice

**Primary: Mesen2** — has LuaSocket built in, async save state API, `emu.isKeyPressed()`, headless test runner mode, `emu.getScriptDataFolder()`.

**Fallback: SNES9X-rr** — simpler API, Andrew is more familiar. Would require file-based IPC instead of TCP. The Lua API surface is small enough to abstract if we need to support both later.

### Database

SQLite. Single file. Schema in `docs/DESIGN.md` § Database Schema.

## Key Design Decisions

- **Save states are binary blobs written to files.** Mesen2's `saveSavestate()` returns a binary string; we write it to disk via `io.open`. To load, we read the file and call `loadSavestate(data)`. This must happen inside a `startFrame` or `cpuExec` callback.
- **Games are auto-discovered from ROM checksums.** Lua sends the ROM filename over TCP on connect. Python computes a truncated SHA-256 (16 hex chars) as the game ID. No manual game configuration needed — just open any ROM in Mesen2 and SpinLab tracks it automatically. Save states are organized in per-game subdirectories.
- **Segment IDs are deterministic from game state**, not sequence-based. Derived from (game_id, level_number, start_type, start_ordinal, end_type, end_ordinal). This means the same section always gets the same ID regardless of run order, enabling reference run diffing.
- **The Lua script does NOT poll files.** It either: (a) in passive mode, just watches memory on frame callbacks with zero overhead, or (b) in practice mode, listens on a TCP socket which LuaSocket handles efficiently with non-blocking receives.
- **Controller input for ratings uses L + D-pad** combo to avoid interfering with gameplay. Only checked during the post-completion "liminal" state.
- **Segments support cold/hot start variants.** A checkpoint segment has a "hot" save state (captured at the moment the checkpoint is hit) and a "cold" save state (captured on first respawn after death). The fill-gap flow lets users capture missing cold states.
- **Input recording uses `.spinrec` binary format.** 32-byte header (magic `SREC`, version uint16, game_id 16 bytes ASCII, frame_count uint32, 6 bytes reserved) followed by one uint16 per frame (SNES joypad bitmask). Recorded via `inputPolled` callback during reference runs. Companion `.mss` save state captured at frame 0. Python reader/writer in `spinrec.py`; Lua reader/writer inline in `spinlab.lua`.
- **Replay injects inputs, not save states.** Replay loads the frame-0 `.mss` save state once, then feeds recorded inputs via `emu.setInput()` every frame. The existing `detect_transitions()` pipeline fires naturally, so segment events are created without special-casing. Events are tagged with `source: "replay"` for downstream filtering.

## Build Order

See `docs/DESIGN.md` § Build Order for the full plan. Summary:

0. Launch harness (shell script)
1. **Proof of concept**: Lua loads a manually-created save state from a TCP command (validates core mechanic)
2. Passive recorder (memory watching + JSONL logging)
3. Reference capture (add state saving to passive recorder)
4. Practice loop MVP (orchestrator + Lua practice mode + round-robin)
5. SM-2 scheduling (replace round-robin)
6. Polish (TUI, reference diffing, strat resets)

Step 1 is prioritized early because programmatic save state invocation is the critical risk.

## Coding Guidelines

- Python 3.11+. Type hints everywhere. `dataclasses` for models.
- Lua: keep it readable, liberal comments. Memory addresses in a clearly separated config section at top of script.
- YAML for manifests and config (Andrew's preference).
- Tests for scheduler logic and DB operations. Lua testing is manual via Mesen2.
- The kaizosplits C# code in `reference/` is read-only reference material for memory addresses and detection logic — never import or compile it.

## Memory Address Reference

The kaizosplits code contains SMW memory address definitions. Key addresses to port:
- Level number / sublevel
- Room/screen transitions
- Goal type (normal exit, secret exit, key, etc.)
- Checkpoint flags
- Player state (alive, dead, transitioning)
- Overworld position

Extract these from the kaizosplits source and define them in a config section in the Lua script. All SMW romhacks share the same memory layout.

## Testing Approach

Red-Green TDD. After tests pass, remove trivial/scaffolding tests and clean up — keep only tests that document behavior or catch regressions.

### Unit tests (fast, no Mesen2)
`pytest tests/` — runs all unit tests (~30s). In-memory SQLite, mocked TCP, FastAPI TestClient.

### Integration tests (Mesen2 headless)
`pytest -m integration` — runs Lua+Python integration tests via Mesen2's `--testRunner` headless mode (~7 min). Each test launches Mesen2 with `lua/poke_engine.lua`, which `dofile`s `spinlab.lua` and injects SNES memory writes from `.poke` scenario files. See `tests/integration/README.md` for full details.

**Key headless-mode gotchas:**
- `emu.isKeyPressed()` crashes in `--testRunner` — guarded with `pcall` in `spinlab.lua`
- ROM actively overwrites memory every frame — the poke engine holds values persistently
- TCP requires `tcp-nodelay` to avoid Nagle buffering at max emulation speed
- Port 15482 TIME_WAIT on Windows needs ~3s cooldown between test runs

**Address maps are defined in three places** (must stay in sync):
- `lua/spinlab.lua` lines 43-53 (source of truth)
- `lua/poke_engine.lua` ADDR_MAP
- `tests/integration/addresses.py` ADDR_MAP

## Worktrees

Worktrees live in `.worktrees/{name}/` with branch `worktree/{name}`.

**Detection:** `git rev-parse --show-toplevel` — if it contains `.worktrees/`, you're in a worktree.

**Resource policy:**

- **Main checkout (not in a worktree):** Full access to dashboard, TCP ports, emulator, Playwright tests. No need to ask.
- **In a worktree:** Editing code and running unit tests is always OK (tests use in-memory DBs). Binding ports (dashboard, TCP), running Playwright, or launching the emulator requires **asking the user first** — another session may be using those resources.

**Pip editable installs:** `pip install -e` is path-bound. The worktree shares the same virtualenv but the editable install still resolves to whichever checkout was last installed. This is fine for unit tests (they use `sys.path` manipulation), but if imports fail in a worktree, re-run `pip install -e .` from the worktree root.

**Cleanup lifecycle:**

1. When a worktree branch is merged or abandoned, remove the worktree: `git worktree remove .worktrees/{name}`
2. If the branch was merged, delete it: `git branch -d worktree/{name}`
3. Prune stale worktree references: `git worktree prune`

## Superpowers Visual Companion (Windows)

The brainstorming visual companion server must be launched with `--foreground` and `run_in_background: true` on Windows — background mode dies immediately. The `.superpowers/` directory is gitignored.


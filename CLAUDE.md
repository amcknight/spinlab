# SpinLab — CLAUDE.md

## What This Is

SpinLab is a spaced-repetition practice system for SNES romhack speedrunning. It automatically saves states at split points during reference runs, then serves them back in an intelligent practice loop (like Anki for splits). The player rates difficulty via controller input, and a scheduler determines what to practice next.

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
│   └── spinlab.lua        # The Mesen2 Lua script (always-on, two modes)
├── python/
│   └── spinlab/
│       ├── __init__.py
│       ├── orchestrator.py # Practice session manager, talks to Lua via TCP
│       ├── scheduler.py    # SM-2 adapted scheduling algorithm
│       ├── db.py           # SQLite interface
│       ├── capture.py      # Post-processes reference run data into manifest
│       ├── cli.py          # TUI for stats, session management, strat resets
│       └── models.py       # Data classes / types
├── reference/              # kaizosplits source for memory address extraction
├── scripts/
│   └── launch.sh           # Launches Mesen2 with Lua script auto-loaded
└── config.yaml             # User config: ROM dir, emulator path, scheduler settings
```

### Component Roles

1. **Lua Script** (`lua/spinlab.lua`): Runs inside Mesen2. Always-on. Two modes:
   - **Passive mode** (default): Watches memory addresses, logs all section completions with timestamps. Silent data collection during real runs.
   - **Practice mode** (toggled via TCP connection from orchestrator): Loads save states on command, shows overlay (split name, goal, timer, rating prompt), reads controller for L+D-pad ratings, reports results back.

2. **Python Orchestrator** (`python/spinlab/orchestrator.py`): Manages practice sessions. Connects to Lua via TCP socket, picks next split from scheduler, sends load commands, receives completion results, updates DB.

3. **Python CLI/TUI** (`python/spinlab/cli.py`): Session management, stats display, strat resets, reference run processing. Uses `rich` or `textual`.

4. **Launch Script** (`scripts/launch.sh`): Starts Mesen2 with the ROM and Lua script. Takes care of paths so the user never manually loads scripts.

5. **Dashboard** (`python/spinlab/dashboard.py`): FastAPI web app on `http://localhost:15483`. Run with `spinlab dashboard`. Lua TCP server is on port `15482`.

### IPC: TCP Socket (via Mesen2's built-in LuaSocket)

Mesen2 has LuaSocket compiled in. The Lua script runs a lightweight TCP server. The Python orchestrator connects as a client. Messages are newline-delimited JSON.

See `docs/DESIGN.md` § IPC Contract for the full message spec.

### Emulator Choice

**Primary: Mesen2** — has LuaSocket built in, async save state API, `emu.isKeyPressed()`, headless test runner mode, `emu.getScriptDataFolder()`.

**Fallback: SNES9X-rr** — simpler API, Andrew is more familiar. Would require file-based IPC instead of TCP. The Lua API surface is small enough to abstract if we need to support both later.

### Database

SQLite. Single file. Schema in `docs/DESIGN.md` § Database Schema.

## Key Design Decisions

- **Save states are binary blobs written to files.** Mesen2's `saveSavestate()` returns a binary string; we write it to disk via `io.open`. To load, we read the file and call `loadSavestate(data)`. This must happen inside a `startFrame` or `cpuExec` callback.
- **Games are auto-discovered from ROM checksums.** Lua sends the ROM filename over TCP on connect. Python computes a truncated SHA-256 (16 hex chars) as the game ID. No manual game configuration needed — just open any ROM in Mesen2 and SpinLab tracks it automatically. Save states are organized in per-game subdirectories.
- **Split IDs are deterministic from game state**, not sequence-based. Derived from (game_id, level_number, room_id, goal_type). This means the same section always gets the same ID regardless of run order, enabling reference run diffing.
- **The Lua script does NOT poll files.** It either: (a) in passive mode, just watches memory on frame callbacks with zero overhead, or (b) in practice mode, listens on a TCP socket which LuaSocket handles efficiently with non-blocking receives.
- **Controller input for ratings uses L + D-pad** combo to avoid interfering with gameplay. Only checked during the post-completion "liminal" state.
- **Identical starts with different goals** are handled by having the same save state file but different split entries with a `goal` field displayed on overlay.

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

Red-Green TDD. After tests pass, remove trivial/scaffolding tests and clean up — keep only tests that document behavior or catch regressions. For Lua, use Mesen2's `--testRunner` headless mode where possible; otherwise manual testing in-emulator.

## Worktrees

Worktrees live in `.worktrees/{name}/` with branch `worktree/{name}`.

**Detection:** `git rev-parse --show-toplevel` — if it contains `.worktrees/`, you're in a worktree.

**Resource policy:**

- **Main checkout (not in a worktree):** Full access to dashboard, TCP ports, emulator, Playwright tests. No need to ask.
- **In a worktree:** Editing code and running unit tests is always OK (tests use in-memory DBs). Binding ports (dashboard, TCP), running Playwright, or launching the emulator requires **asking the user first** — another session may be using those resources.

**Pip editable installs:** `pip install -e` is path-bound. The worktree shares the same virtualenv but the editable install still resolves to whichever checkout was last installed. This is fine for unit tests (they use `sys.path` manipulation), but if imports fail in a worktree, re-run `pip install -e python/` from the worktree root.

**Cleanup lifecycle:**

1. When a worktree branch is merged or abandoned, remove the worktree: `git worktree remove .worktrees/{name}`
2. If the branch was merged, delete it: `git branch -d worktree/{name}`
3. Prune stale worktree references: `git worktree prune`

## Superpowers Visual Companion (Windows)

The brainstorming visual companion server must be launched with `--foreground` and `run_in_background: true` on Windows — background mode dies immediately. The `.superpowers/` directory is gitignored.


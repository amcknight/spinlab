# SpinLab

Spaced-repetition practice for SNES romhack speedrunning. Records save states at split points during reference runs, then serves them back in a scheduled practice loop using a Kalman filter to estimate performance and a value-of-information allocator to pick what you need most. Rate difficulty with your controller after each attempt.

Input recording captures every frame's controller state during reference runs into `.spinrec` binary files. Replay mode feeds those inputs back at any emulation speed to regenerate reference data without human input.

## Requirements

- [Mesen2](https://www.mesen.ca/) (has LuaSocket built in)
- Python 3.11+

## Setup

```bash
pip install -e ./python          # installs spinlab CLI + dependencies
cp config.example.yaml config.yaml
# Edit config.yaml: set emulator.path, rom.path, script_data_dir
```

## Quick Start

### 1. Launch Mesen with SpinLab

```bash
./scripts/launch.sh                  # load ROM from Mesen UI
./scripts/launch.sh path/to/rom.sfc  # or pass ROM directly
```

On Windows: run `scripts\launch.bat` instead.

### 2. Start the dashboard

```bash
spinlab dashboard
```

Open `http://localhost:5173`. The dashboard spawns a Vite dev server (frontend) and a FastAPI backend (default port 15483); the frontend proxies `/api` calls to the backend, and the backend connects to Mesen's Lua TCP server (default port 15482).

### 3. Record a reference run

Click **Start Reference**, play through the run, and click **Stop Reference** when done. The Lua script records every transition, saves `.mss` state files at level entrances/checkpoints/cold spawns, and captures controller inputs into a `.spinrec` file.

A reference run can span multiple sessions: stopping leaves the run paused; **Resume** opens a new capture session under the same run, then **Save & Finish** finalizes it as your active reference. Use this when a long run gets played across multiple sittings.

### 4. Practice

The dashboard's **Practice** tab loads save states for segments from your active reference and tracks each attempt. Each completion updates the per-segment estimator (mean, uncertainty, drift). The greedy allocator picks whichever segment has the highest expected improvement — where another attempt is likely to teach the most.

## Dashboard

The web dashboard is the primary interface. Tabs:

- **Practice** — Start/stop sessions, see the current segment and up-next queue, live attempt tracking.
- **Manage** — Reference runs, capture sessions, segment list, paused-run resume/save/discard.
- **Sessions** — Historical practice-session list with attempt counts and completion rates.
- **Model** — Per-segment estimator state (mean time, uncertainty, marginal return, drift since last attempt) for every registered estimator side-by-side.
- **Config** — Swap allocator or estimator on the fly.

## CLI Commands

| Command | Description |
|---------|-------------|
| `spinlab dashboard` | Start the web dashboard (primary interface) |
| `spinlab replay <path>` | Replay a `.spinrec` file to regenerate a reference run |
| `spinlab lua-cmd <cmds>` | Send raw commands to the Lua TCP server |
| `spinlab db reset` | Delete and recreate the SQLite database |
| `spinlab stats` | Stub — prints a placeholder message |

## Config Reference

See [config.example.yaml](config.example.yaml) for the full template.

| Key | Description |
|-----|-------------|
| `emulator.path` | Absolute path to `Mesen.exe` |
| `emulator.lua_script` | Path to `lua/spinlab.lua` (relative to project root) |
| `emulator.script_data_dir` | Where Lua writes state files and logs |
| `rom.dir` | Directory containing ROM files (`.sfc`/`.smc`) |
| `game.category` | Default category for auto-discovered games (e.g. `any%`) |
| `network.port` | TCP port for Lua ↔ Python IPC (default `15482`, must match `TCP_PORT` in Lua) |
| `network.dashboard_port` | Dashboard HTTP port (default `15483`) |
| `network.host` | Bind host (default `127.0.0.1`) |
| `scheduler.estimator` | Active estimator: `kalman`, `rolling_mean`, or `exp_decay` |
| `scheduler.allocator` | Active allocator: `greedy`, `round_robin`, `random`, `least_played`, or `mix` |
| `data.dir` | Where the SQLite DB lives |

## How It Works

```
Vite (5173)  ──proxies /api──▶  FastAPI (15483)  ◀──TCP──▶  Mesen2 + Lua (15482)
                                ┌──────────────────────┐    ┌────────────────────┐
                                │  session manager     │    │  spinlab.lua       │
                                │  reference + replay  │    │  - transition log  │
                                │  practice loop       │    │  - .spinrec I/O    │
                                │  scheduler (Kalman / │    │  - state load/save │
                                │   rolling / decay)   │    │  - practice overlay│
                                │  SQLite DB           │    │  - input replay    │
                                └──────────────────────┘    └────────────────────┘
```

The Lua script runs inside Mesen2 and switches between five modes (idle, reference, replay, practice, fill-gap) on dashboard commands:
- **Idle** (default): watches SNES memory addresses each frame and emits transition events.
- **Reference**: idle + records controller inputs into a `.spinrec` and saves states at level entrances, checkpoints, and cold spawns.
- **Replay**: loads a `.spinrec` plus its frame-0 `.mss`, injects recorded inputs via `emu.setInput()`, and lets the existing detection pipeline produce segment events tagged `source: "replay"`.
- **Practice**: loads save states on command, detects completion/death, draws the overlay, reads the controller for ratings, auto-advances after a configurable delay.
- **Fill-gap / Cold-fill**: loads a "hot" state so the player can die and capture the missing "cold" variant.

For the full architecture — components, multi-session reference state machine, IPC contract, database schema — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Project Layout

```
lua/spinlab.lua              # Mesen2 Lua script (idle + practice + replay + speed-run)
lua/poke_engine.lua          # Memory-poke engine for integration tests
python/spinlab/              # CLI, dashboard, scheduler, DB
  cli.py                     # Entry point (dashboard, replay, lua-cmd, db)
  dashboard.py               # FastAPI app, route registration
  session_manager.py         # Mode coordinator, event routing, SSE
  capture/                   # Reference/replay/cold-fill orchestration
    reference.py             #   ReferenceController (multi-session lifecycle)
    recorder.py              #   SegmentRecorder (event pairing → DB)
    cold_fill.py             #   ColdFillController (batch cold-variant capture)
  practice.py                # Async practice session loop
  speed_run.py               # Full-run speed-run mode
  scheduler.py               # Wires estimators + allocators together
  estimators/                # kalman, rolling_mean, exp_decay
  allocators/                # greedy, round_robin, random, least_played, mix
  db/                        # SQLite interface (mixin-composed package)
  tcp_manager.py             # Async TCP client for Lua socket
  protocol.py                # Typed dataclasses for every IPC message
  sse.py                     # SSE broadcaster
  spinrec.py                 # .spinrec binary format reader/writer
  routes/                    # FastAPI route modules
  state_builder.py           # Builds the snapshot served by /api/state and SSE
  vite.py                    # Spawns/manages the Vite dev server subprocess
frontend/                    # TypeScript + Vite frontend (built into static/)
scripts/launch.sh            # Launch harness (mac/linux)
scripts/launch.bat           # Launch harness (Windows)
scripts/spinlab.ahk          # Windows hotkeys (Ctrl+Alt+W/X)
config.yaml                  # Your local config (gitignored)
docs/ARCHITECTURE.md         # Components, state machine, DB schema, IPC contract
docs/GLOSSARY.md             # Domain terms
docs/BACKLOG.md              # Open follow-ups and ideas
docs/model-improvements-spec.md  # Estimator design and roadmap
```

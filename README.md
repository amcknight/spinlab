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

### 2. Record a reference run

Open the dashboard and click **Start Reference**, then play. The Lua script records all level transitions, saves `.mss` state files at each split point, and captures controller inputs into a `.spinrec` file. Click **Stop Reference** when done.

### 3. Process the reference run

```bash
spinlab capture
```

This parses the passive log into a manifest YAML with split IDs, state file paths, and reference completion times.

### 4. Practice

```bash
spinlab dashboard
```

Open `http://localhost:15483`. The dashboard connects to Mesen's Lua TCP server (port 15482), loads save states, and manages your practice session. Each attempt's completion time automatically updates the Kalman filter's per-split performance estimate (mean time and uncertainty). The greedy allocator then picks whichever split has the highest marginal return — the one where another attempt would reduce your overall uncertainty the most.

## Dashboard

The web dashboard (`spinlab dashboard`) is the primary interface. Four tabs:

- **Practice** — Start/stop sessions, see the current split and up-next queue, live attempt tracking
- **Sessions** — Historical session list with attempt counts and completion rates
- **Model** — Per-split estimator state (mean time, uncertainty, marginal return, drift since last attempt)
- **Config** — Swap allocator (greedy / round-robin / random) or estimator on the fly

## CLI Commands

| Command | Description |
|---------|-------------|
| `spinlab dashboard` | Start the web dashboard (primary interface) |
| `spinlab replay <path>` | Replay a `.spinrec` file to regenerate a reference run |
| `spinlab lua-cmd <cmds>` | Send raw commands to the Lua TCP server |
| `spinlab stats` | Show practice statistics (coming soon) |

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
| `scheduler.estimator` | Active estimator: `kalman`, `model_a`, or `model_b` |
| `scheduler.allocator` | Active allocator: `greedy`, `round_robin`, or `random` |
| `data.dir` | Where the SQLite DB lives |

## How It Works

```
Mesen2 + Lua (port 15482)          Python (port 15483)
┌─────────────────────┐            ┌──────────────────────┐
│  spinlab.lua        │◄──TCP────►│  FastAPI dashboard    │
│  - passive recorder │            │  - session manager    │
│  - input recording  │            │  - Kalman estimator   │
│  - replay mode      │            │  - greedy allocator   │
│  - practice mode    │            │  - SQLite DB          │
│  - overlay drawing  │            │  - .spinrec format    │
└─────────────────────┘            └──────────────────────┘
```

The Lua script runs inside Mesen2 and operates in three modes:
- **Passive mode** (default): Watches SNES memory addresses on each frame, logs level transitions, saves state files. When a reference run is active, also records controller inputs every frame into a `.spinrec` binary file.
- **Replay mode** (activated by dashboard or CLI): Loads a `.spinrec` + companion `.mss` save state, then injects the recorded inputs via `emu.setInput()` at any emulation speed. Segment events fire naturally through the existing detection pipeline, tagged with `source: "replay"`.
- **Practice mode** (activated by dashboard): Loads save states on command, detects completion/death, draws an overlay with split name and timer, reads controller for ratings, auto-advances after a configurable delay.

## Project Layout

```
lua/spinlab.lua              # Mesen2 Lua script (passive + practice + replay modes)
python/spinlab/              # CLI, dashboard, scheduler, DB
  cli.py                     # Entry point (dashboard, replay, lua-cmd)
  dashboard.py               # FastAPI web app + HTTP routes
  session_manager.py         # Mode coordinator, event routing
  capture_controller.py      # Reference/replay/fill-gap orchestration
  practice.py                # Async practice session loop
  scheduler.py               # Wires estimators + allocators together
  estimators/                # Kalman, Model A (rolling), Model B (exp decay)
  allocators/                # Greedy, round-robin, random
  db/                        # SQLite interface (mixin-composed package)
  tcp_manager.py             # Async TCP client for Lua socket
  sse.py                     # SSE broadcaster
  spinrec.py                 # .spinrec binary format reader/writer
  static/                    # Dashboard frontend (HTML/JS/CSS)
scripts/launch.sh            # Launch harness
scripts/spinlab.ahk          # Windows hotkeys (Ctrl+Alt+W/X)
config.yaml                  # Your local config (gitignored)
docs/DESIGN.md               # Full architecture, IPC spec, DB schema
```

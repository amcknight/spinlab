# SpinLab

Spaced-repetition practice for SNES romhack speedrunning. Records save states at split points during reference runs, then serves them back in a scheduled practice loop using a Kalman filter to estimate performance and a value-of-information allocator to pick what you need most. Rate difficulty with your controller after each attempt.

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

Just play. The Lua script runs in passive mode by default — it silently watches memory addresses, logs level transitions to JSONL, and saves a `.mss` state file at each split point. No action needed.

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
| `spinlab capture` | Process passive log into a split manifest |
| `spinlab practice` | Start a practice session via terminal (legacy) |
| `spinlab lua-cmd <cmds>` | Send raw commands to the Lua TCP server |
| `spinlab stats` | Show practice statistics (coming soon) |

## Config Reference

See [config.example.yaml](config.example.yaml) for the full template.

| Key | Description |
|-----|-------------|
| `emulator.path` | Absolute path to `Mesen.exe` |
| `emulator.script_data_dir` | Where Lua writes state files and logs |
| `rom.path` | ROM path (optional — leave empty to load from Mesen UI) |
| `game.id` | Game identifier used in DB and manifests |
| `game.name` | Display name for the game |
| `network.port` | TCP port for Lua ↔ Python IPC (default `15482`) |
| `data.dir` | Where the SQLite DB and manifests live |

## How It Works

```
Mesen2 + Lua (port 15482)          Python (port 15483)
┌─────────────────────┐            ┌──────────────────────┐
│  spinlab.lua        │◄──TCP────►│  FastAPI dashboard    │
│  - passive recorder │            │  - session manager    │
│  - practice mode    │            │  - Kalman estimator   │
│  - overlay drawing  │            │  - greedy allocator   │
│  - controller input │            │  - SQLite DB          │
└─────────────────────┘            └──────────────────────┘
```

The Lua script runs inside Mesen2 and operates in two modes:
- **Passive mode** (default): Watches SNES memory addresses on each frame, logs level transitions, saves state files. Zero overhead during normal play.
- **Practice mode** (activated by dashboard): Loads save states on command, detects completion/death, draws an overlay with split name and timer, reads controller for ratings, auto-advances after a configurable delay.

## Project Layout

```
lua/spinlab.lua              # Mesen2 Lua script (passive + practice modes)
python/spinlab/              # CLI, dashboard, scheduler, DB
  dashboard.py               # FastAPI web app + TCP client
  scheduler.py               # Wires estimator + allocator together
  estimators/kalman.py       # Kalman filter performance model
  allocators/greedy.py       # VoI-based split selection
  db.py                      # SQLite interface
  capture.py                 # Passive log → manifest processor
  cli.py                     # Entry point
  static/                    # Dashboard frontend (HTML/JS/CSS)
scripts/launch.sh|bat        # Launch harness
config.yaml                  # Your local config (gitignored)
docs/DESIGN.md               # Full architecture, IPC spec, DB schema
```

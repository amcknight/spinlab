# SpinLab

Spaced-repetition practice for SNES romhack speedrunning. Records save states at split points during reference runs, then serves them back in a Kalman-filter-scheduled practice loop.

## Requirements

- [Mesen2](https://www.mesen.ca/) (LuaSocket built in)
- Python 3.11+
- `pip install pyyaml rich fastapi uvicorn`

## Setup

```bash
cp config.example.yaml config.yaml
# Edit config.yaml: set emulator.path and rom.path
```

## Usage

### Quick Start (Windows, with AHK)

1. Run `scripts\spinlab.ahk` (requires AutoHotkey v2.0)
2. Press **Ctrl+Alt+W** — launches Mesen + dashboard
3. Open `http://localhost:15483` — the dashboard shows mode buttons
4. Click **Start Reference Run** to capture splits, or **Start Practice** to begin practicing
5. Press **Ctrl+Alt+X** to kill everything

### Manual Launch

**Launch Mesen with SpinLab loaded:**
```bash
./scripts/launch.sh                  # load ROM from Mesen UI
./scripts/launch.sh path/to/rom.sfc  # or pass ROM directly
```

On Windows: run `scripts\launch.bat` instead.

**Start the dashboard:**
```bash
spinlab dashboard
```

The dashboard at `http://localhost:15483` manages all modes:
- **Reference mode**: captures section completions and save states as you play
- **Practice mode**: loads save states in order, tracks your times against reference

### CLI Commands

| Command                  | Description                                    |
| ------------------------ | ---------------------------------------------- |
| `spinlab dashboard`      | Start the web dashboard (main entry point)     |
| `spinlab stats`          | Show practice statistics (coming soon)         |
| `spinlab lua-cmd <cmds>` | Send raw commands to the Lua TCP server        |

## Config reference

| Key                      | Description                                    |
| ------------------------ | ---------------------------------------------- |
| `emulator.path`          | Absolute path to `Mesen.exe`                   |
| `emulator.lua_script`    | Path to SpinLab Lua script                     |
| `rom.path`               | ROM path (optional — can load from Mesen UI)   |
| `game.id`                | Game identifier used in DB and manifests       |
| `network.port`           | TCP port for Lua ↔ Python IPC (default `15482`)|
| `scheduler.estimator`    | `kalman` (default)                             |
| `scheduler.allocator`    | `greedy` (default), `random`, or `round_robin` |
| `data.dir`               | Where save states and DB are stored            |

## Project layout

```
lua/spinlab.lua          # Mesen2 Lua script (passive recorder + practice mode)
python/spinlab/          # Dashboard, scheduler, DB, CLI
scripts/launch.sh|bat    # Launch harness
scripts/spinlab.ahk      # AHK hotkeys: Ctrl+Alt+W (launch), Ctrl+Alt+X (kill)
config.yaml              # Your local config (gitignored)
config.example.yaml      # Template
docs/DESIGN.md           # Full architecture and IPC spec
```

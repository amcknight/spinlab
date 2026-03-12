# SpinLab

Spaced-repetition practice for SNES romhack speedrunning. Records save states at split points during reference runs, then serves them back in an SM-2 scheduled loop. Rate difficulty with L + D-pad after each attempt.

## Requirements

- [Mesen2](https://www.mesen.ca/) (LuaSocket built in)
- Python 3.11+
- `pip install pyyaml rich`

## Setup

```bash
cp config.example.yaml config.yaml
# Edit config.yaml: set emulator.path and rom.path
```

## Usage

**Launch Mesen with SpinLab loaded:**
```bash
./scripts/launch.sh                  # load ROM from Mesen UI
./scripts/launch.sh path/to/rom.sfc  # or pass ROM directly
```

On Windows: run `scripts\launch.bat` instead.

**Record a reference run:**
Just play. In passive mode the Lua script silently logs section completions and saves states at splits. No action needed.

**Start a practice session:**
```bash
cd python && python -m spinlab.orchestrator
```

The scheduler picks the next split, loads its save state in Mesen, and prompts you to rate it:
- `L + ←` — hard
- `L + ↓` — okay
- `L + →` — easy

Ratings feed SM-2 to determine when you see each split again.

**TUI / stats:**
```bash
python -m spinlab.cli
```

## Config reference

| Key | Description |
|-----|-------------|
| `emulator.path` | Absolute path to `Mesen.exe` |
| `rom.path` | ROM path (optional — can load from Mesen UI) |
| `game.id` | Game identifier used in DB and manifests |
| `network.port` | TCP port for Lua ↔ Python IPC (default `15482`) |
| `scheduler.algorithm` | `sm2` (default) |
| `data.dir` | Where save states and DB are stored |

## Project layout

```
lua/spinlab.lua          # Mesen2 Lua script (passive recorder + practice mode)
python/spinlab/          # Orchestrator, scheduler, DB, CLI
scripts/launch.sh|bat    # Launch harness
config.yaml              # Your local config (gitignored)
config.example.yaml      # Template
DESIGN.md                # Full architecture and IPC spec
```

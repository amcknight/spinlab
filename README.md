# SpinLab

Spaced-repetition practice for SNES romhack speedrunning. Records save states at split points during reference runs, then serves them back in a scheduled practice loop using a Kalman filter to estimate performance and a value-of-information allocator to pick what you need most. Rate difficulty with your controller after each attempt.

SpinLab records `.replay` (BSV format) files alongside save states during reference runs. Replay mode feeds those inputs back to regenerate reference data. See [Phase E state](#phase-e-state--movie-replay) below for the current limits of this path.

## Requirements

- [RetroArch](https://retroarch.com) + snes9x_libretro core (installed via RA's Online Updater)
- Python 3.11+

## RetroArch Setup

In `retroarch.cfg` (typically `<RetroArch dir>/retroarch.cfg`):

```
network_cmd_enable = "true"
network_cmd_port = "55355"
cheevos_hardcore_mode_enable = "false"
run_ahead_secondary_instance = "true"
replay_max_keep = "99"
log_to_file = "true"
log_to_file_timestamp = "true"
log_verbosity = "true"
```

The first two enable the Network Command Interface SpinLab uses. **`cheevos_hardcore_mode_enable` is non-obvious but required:** when set to `"true"`, RetroArch silently drops NCI hotkey-style commands (SAVE_STATE, LOAD_STATE_SLOT, PAUSE_TOGGLE, MENU_TOGGLE) — even when `cheevos_enable = "false"`. The hardcore flag is checked independently. Manual gamepad save state continues to work, so this fails silently and is genuinely confusing if you don't know to look. SpinLab needs hardcore-mode off to drive its automated saves/loads.

**`run_ahead_secondary_instance = "true"`** is required if you use runahead (recommended). With single-instance runahead, RetroArch corrupts state buffers on save/load. Force secondary-instance runahead and state I/O works reliably.

**`replay_max_keep = "99"`** (default `"0"`) is required for movie recording. With `0`, RetroArch silently refuses to create new `.replay` files when any already exist for the loaded game — `RECORD_REPLAY` no-ops without an error reply, and `MovieRecorder.stop` reports "no new file appeared". Set to `99` (or any large value) to allow multiple recordings per game.

**`log_to_file = "true"`** (with the two companion keys) is strongly recommended, not just for debugging. Several RA failure modes are silent over NCI — RA shows an in-app popup ("Failed to load movie file", "savestate failed", etc.) but the NCI command appears to succeed. SpinLab's only window into those failures is RA's log file. With these on, logs land in `<RetroArch dir>/logs/retroarch__YYYY_MM_DD__HH_MM_SS.log`. When something seems off, `tail -f` the most recent file in that dir while exercising the dashboard.

Restart RetroArch fully after editing — the cfg is read once at startup. SpinLab talks to RetroArch over UDP using the libretro Network Command Interface (NCI). The default port `55355` matches RA's default; SpinLab's `network.nci_port` config will track it.

Run-ahead is fully compatible with all NCI commands SpinLab uses; enable it for low input latency. Verified with `run_ahead_enabled = "true"`, `run_ahead_frames = "2"` or `"3"`, and `run_ahead_secondary_instance = "true"` (see above).

Quick sanity check from PowerShell with RA running:

```powershell
$udp = New-Object System.Net.Sockets.UdpClient
$udp.Connect("127.0.0.1", 55355)
$bytes = [Text.Encoding]::ASCII.GetBytes("VERSION")
$udp.Send($bytes, $bytes.Length) | Out-Null
$udp.Client.ReceiveTimeout = 2000
$ep = New-Object System.Net.IPEndPoint([Net.IPAddress]::Any, 0)
[Text.Encoding]::ASCII.GetString($udp.Receive([ref]$ep))
$udp.Close()
```

Should print RA's version string. If it times out, NCI isn't reachable — re-check the cfg edits and that RA was restarted, not just reloaded.

A standalone validation spike lives at [`scripts/spike_retroarch.py`](scripts/spike_retroarch.py) — runs five tests (NCI handshake, memory read, sustained 60Hz polling, runahead coexistence, savestate round-trip) and reports pass/fail per step.

### Upgrading from pre-Phase-G

Phase G dropped the Mesen backend and the `spinrec_path` column from the `capture_sessions` table. If you have an existing SpinLab database from before this change, run:

```bash
spinlab db reset --config config.yaml
```

This deletes and recreates the database with the new schema. Saved practice attempts will be lost; recapture as needed.

## Setup

```bash
pip install -e ./python          # installs spinlab CLI + dependencies
cp config.example.yaml config.yaml
# Edit config.yaml — see Config Reference below
```

Key fields to set in `config.yaml`:

```yaml
emulator:
  retroarch_path: "C:/RetroArch-Win64/retroarch.exe"
  savestate_dir: "C:/RetroArch-Win64/saves/states"
  spinlab_state_dir: "data/spinlab_states"
  ra_core_subdir: "snes9x"        # subdir RA uses under savestate_dir for movie files

rom:
  dir: "C:/path/to/your/romhacks"
```

See [config.example.yaml](config.example.yaml) for the full template with all options.

## Quick Start

### 1. Open RetroArch and load your ROM

Launch RetroArch normally (with the cfg edits above applied). Load your SNES ROM/hack. SpinLab will connect to the already-running RA instance when the dashboard starts, or can launch RA for you via the "Launch Emulator" button in the dashboard.

### 2. Start the dashboard

```bash
spinlab dashboard
```

Open `http://localhost:5173`. The dashboard spawns a Vite dev server (frontend) and a FastAPI backend (default port 15483); the frontend proxies `/api` calls to the backend. The backend connects to RetroArch over NCI (UDP port 55355) and polls SNES WRAM at 60 Hz to detect transitions.

### 3. Record a reference run

Click **Start Reference**, play through the run, and click **Stop Reference** when done. SpinLab saves `.mss` state files at level entrances, checkpoints, and cold spawns. A `.replay` file (BSV input recording) is also written alongside the states. See [Phase E state](#phase-e-state--movie-replay) for the current limits of BSV recording.

A reference run can span multiple sessions: stopping leaves the run paused; **Resume** opens a new capture session under the same run, then **Save & Finish** finalizes it as your active reference. Use this when a long run gets played across multiple sittings.

### 4. Practice

The dashboard's **Practice** tab loads save states for segments from your active reference and tracks each attempt. Each completion updates the per-segment estimator (mean, uncertainty, drift). The greedy allocator picks whichever segment has the highest expected improvement — where another attempt is likely to teach the most.

### Phase E state — movie replay

SpinLab writes `.replay` (BSV) files during reference runs (Phase E option a, shipped 2026-05-08). Isolated movie record and playback work. However, **SAVE_STATE during BSV recording corrupts the recording** — a hard limitation in RA 1.22.2 with snes9x_libretro and bsnes_libretro. When SpinLab's reference flow fires SAVE_STATE on segment events while recording is active, the recording is silently truncated at the first save. `.replay` files produced during reference runs are therefore partial and not usable for replay-driven segment capture. The known workaround paths (decouple recording from saves, multi-segment recording, core swap, RA patch) are documented in [`docs/retroarch-migration/slot-management.md`](docs/retroarch-migration/slot-management.md). Full replay-driven segment capture (Phase E option b) is not yet implemented.

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
| `spinlab db reset` | Delete and recreate the SQLite database |
| `spinlab stats` | Stub — prints a placeholder message |

## Config Reference

See [config.example.yaml](config.example.yaml) for the full template.

| Key | Description |
|-----|-------------|
| `emulator.retroarch_path` | Absolute path to `retroarch.exe` |
| `emulator.savestate_dir` | Where RA writes save state files (e.g. `<RA dir>/saves/states`) |
| `emulator.spinlab_state_dir` | Where SpinLab stores its keyed state files (relative or absolute) |
| `emulator.ra_core_subdir` | Core name subdir RA uses under `savestate_dir` for movie files (e.g. `snes9x`) |
| `emulator.ra_movie_dir` | Override movie file directory if RA puts movies elsewhere |
| `emulator.ra_core_path` | Path to the libretro core `.dll` (used when launching RA from dashboard) |
| `rom.dir` | Directory containing ROM files (`.sfc`/`.smc`) |
| `game.category` | Default category for auto-discovered games (e.g. `any%`) |
| `network.nci_port` | RetroArch NCI UDP port (default `55355`, must match `network_cmd_port` in RA cfg) |
| `network.dashboard_port` | Dashboard HTTP port (default `15483`) |
| `network.host` | Bind host (default `127.0.0.1`) |
| `scheduler.estimator` | Active estimator: `kalman`, `rolling_mean`, or `exp_decay` |
| `scheduler.allocator` | Active allocator: `greedy`, `round_robin`, `random`, `least_played`, or `mix` |
| `data.dir` | Where the SQLite DB lives |

## How It Works

```
Vite (5173)  ──proxies /api──▶  FastAPI (15483)  ◀──NCI/UDP──▶  RetroArch (55355)
                                ┌──────────────────────┐
                                │  session manager     │
                                │  reference + replay  │
                                │  practice loop       │
                                │  scheduler (Kalman / │
                                │   rolling / decay)   │
                                │  SQLite DB           │
                                └──────────────────────┘
```

SpinLab owns all transition detection in Python. A `Poller` reads SMW WRAM at 60 Hz via NCI `READ_CORE_RAM`, drives a `TransitionDetector`, and emits typed events (`LevelEntrance`, `Checkpoint`, `Death`, `Spawn`, `LevelExit`). The dashboard backend orchestrates reference capture, cold-fill, and practice via these events. State I/O uses NCI `SAVE_STATE` / `LOAD_STATE_SLOT` with a filesystem shuffle to associate slot files with logical segment IDs.

For the full architecture — components, multi-session reference state machine, database schema — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Project Layout

```
python/spinlab/              # CLI, dashboard, scheduler, DB
  cli.py                     # Entry point (dashboard, db)
  dashboard.py               # FastAPI app, route registration
  session_manager.py         # Mode coordinator, event routing, SSE
  capture/                   # Reference/cold-fill orchestration
    reference.py             #   ReferenceController (multi-session lifecycle)
    recorder.py              #   SegmentRecorder (event pairing → DB)
    cold_fill.py             #   ColdFillController (batch cold-variant capture)
  practice.py                # Async practice session loop
  speed_run.py               # Full-run speed-run mode
  scheduler.py               # Wires estimators + allocators together
  estimators/                # kalman, rolling_mean, exp_decay
  allocators/                # greedy, round_robin, random, least_played, mix
  db/                        # SQLite interface (mixin-composed package)
  protocol.py                # Typed dataclasses for every IPC message
  sse.py                     # SSE broadcaster
  retroarch/                 # RetroArch backend
    nci.py                   #   NCIClient — UDP NCI transport
    poller.py                #   Poller — 60Hz WRAM reader + event emitter
    state_io.py              #   StateIO — save/load via filesystem shuffle
    movie.py                 #   MovieRecorder + MoviePlayer (BSV .replay)
    orchestrator.py          #   RetroArchOrchestrator — wires it all together
    predicates.py            #   TransitionDetector + cold-fill spawn detector
    addresses.py             #   SMW WRAM address map
  routes/                    # FastAPI route modules
  state_builder.py           # Builds the snapshot served by /api/state and SSE
  vite.py                    # Spawns/manages the Vite dev server subprocess
frontend/                    # TypeScript + Vite frontend (built into static/)
scripts/spinlab.ahk          # Windows hotkeys (Ctrl+Alt+W/X)
scripts/spike_retroarch.py   # NCI validation spike (5 tests, pass/fail)
config.yaml                  # Your local config (gitignored)
docs/ARCHITECTURE.md         # Components, state machine, DB schema
docs/GLOSSARY.md             # Domain terms
docs/BACKLOG.md              # Open follow-ups and ideas
docs/retroarch-migration/    # Migration history and Phase E state
```

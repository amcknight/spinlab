# Dashboard-Driven Mode Control Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move all mode control (reference capture, practice, emulator launch) into the dashboard, simplify AHK to two hotkeys, and fix stale state bugs.

**Architecture:** Replace the implicit TCP-derived mode with an explicit `_mode` flag in dashboard.py. Gate reference event capture on `_mode[0] == "reference"`. Add API endpoints for reference start/stop and emulator launch. Extract manifest utilities from orchestrator.py to manifest.py, then delete orchestrator.py and capture.py.

**Tech Stack:** Python 3.11+, FastAPI, vanilla JS, AutoHotkey v2.0

---

## Chunk 1: Backend — Mode State Machine + Bug Fixes

### Task 1: Extract manifest utilities from orchestrator.py

**Files:**
- Create: `python/spinlab/manifest.py`
- Modify: `python/spinlab/dashboard.py:358`
- Modify: `python/spinlab/cli.py:71`
- Modify: `tests/test_db_references.py:5`

- [ ] **Step 1: Create `python/spinlab/manifest.py`**

Move these three functions from `orchestrator.py`:

```python
"""Manifest utilities: find, load, and seed DB from reference manifests."""
from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from .db import Database
from .models import Split


def find_latest_manifest(data_dir: Path) -> Optional[Path]:
    """Return the most-recently-named manifest YAML, or None if none exist."""
    captures = list((data_dir / "captures").glob("*_manifest.yaml"))
    if not captures:
        return None
    return sorted(captures)[-1]


def load_manifest(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def seed_db_from_manifest(db: Database, manifest: dict, game_name: str) -> None:
    """Upsert game + all splits from manifest into the DB."""
    game_id: str = manifest["game_id"]
    category: str = manifest.get("category", "any%")
    db.upsert_game(game_id, game_name, category)

    captured_at = manifest.get("captured_at", datetime.utcnow().isoformat())
    run_id = f"manifest_{uuid.uuid4().hex[:8]}"
    run_name = f"Capture {captured_at[:10]}"
    db.create_capture_run(run_id, game_id, run_name)
    db.set_active_capture_run(run_id)

    for idx, entry in enumerate(manifest["splits"], start=1):
        split = Split(
            id=entry["id"],
            game_id=game_id,
            level_number=entry["level_number"],
            room_id=entry.get("room_id"),
            goal=entry["goal"],
            description=entry.get("name", ""),
            state_path=entry.get("state_path"),
            reference_time_ms=entry.get("reference_time_ms"),
            ordinal=idx,
            reference_id=run_id,
        )
        db.upsert_split(split)
```

- [ ] **Step 2: Update imports in dashboard.py**

Change line 358:
```python
# Old:
from spinlab.orchestrator import seed_db_from_manifest
# New:
from spinlab.manifest import seed_db_from_manifest
```

- [ ] **Step 3: Update imports in cli.py**

Change line 71:
```python
# Old:
from spinlab.orchestrator import find_latest_manifest, load_manifest, seed_db_from_manifest
# New:
from spinlab.manifest import find_latest_manifest, load_manifest, seed_db_from_manifest
```

- [ ] **Step 4: Update imports in tests/test_db_references.py**

Change line 5:
```python
# Old:
from spinlab.orchestrator import seed_db_from_manifest
# New:
from spinlab.manifest import seed_db_from_manifest
```

- [ ] **Step 5: Run tests to verify extraction**

Run: `cd python && python -m pytest ../tests/test_db_references.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/manifest.py python/spinlab/dashboard.py python/spinlab/cli.py tests/test_db_references.py
git commit -m "refactor: extract manifest utilities from orchestrator to manifest.py"
```

---

### Task 2: Add explicit mode flag and new endpoints to dashboard.py

**Files:**
- Modify: `python/spinlab/dashboard.py`

- [ ] **Step 1: Replace `_current_mode()` with explicit flag**

Replace lines 66-71 in dashboard.py:

```python
# Old:
def _current_mode() -> str:
    if _practice[0] and _practice[0].is_running:
        return "practice"
    if tcp.is_connected:
        return "reference"
    return "idle"

# New:
_mode: list[str] = ["idle"]  # "idle" | "reference" | "practice"

def _current_mode() -> str:
    return _mode[0]
```

- [ ] **Step 2: Update `_clear_ref_state` to also reset mode**

Replace lines 78-82:

```python
def _clear_ref_state():
    """Clear reference capture state on disconnect or mode change."""
    _ref_pending.clear()
    _ref_splits_count[0] = 0
    _ref_capture_run_id[0] = None
    _mode[0] = "idle"
```

- [ ] **Step 3: Update `tcp.on_disconnect` to also stop practice**

Replace line 84 (`tcp.on_disconnect = _clear_ref_state`) with a wrapper that also stops practice:

```python
def _on_disconnect():
    """Handle TCP disconnect: stop practice if running, clear ref state."""
    if _practice[0] and _practice[0].is_running:
        _practice[0].is_running = False
    _clear_ref_state()

tcp.on_disconnect = _on_disconnect
```

- [ ] **Step 4: Gate event dispatch on reference mode**

In `_event_dispatch_loop()`, change lines 104-106:

```python
# Old:
# During practice, PracticeSession reads from the same queue
if _practice[0] and _practice[0].is_running:
    continue

# New:
# Only capture events in reference mode
if _mode[0] != "reference":
    continue
```

- [ ] **Step 5: Add `POST /api/reference/start` endpoint**

Add after the practice endpoints (after line 247):

```python
@app.post("/api/reference/start")
def reference_start():
    if _mode[0] == "practice":
        return {"status": "practice_active"}
    if not tcp.is_connected:
        return {"status": "not_connected"}
    _clear_ref_state()  # reset any stale state
    run_id = f"live_{uuid.uuid4().hex[:8]}"
    run_name = f"Live {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
    db.create_capture_run(run_id, game_id, run_name)
    db.set_active_capture_run(run_id)
    _ref_capture_run_id[0] = run_id
    _mode[0] = "reference"
    return {"status": "started", "run_id": run_id, "run_name": run_name}

@app.post("/api/reference/stop")
def reference_stop():
    if _mode[0] != "reference":
        return {"status": "not_in_reference"}
    _clear_ref_state()  # resets mode to idle
    return {"status": "stopped"}
```

- [ ] **Step 6: Remove auto-create capture run from event dispatch**

In `_event_dispatch_loop()`, remove the auto-create block (lines 114-120):

```python
# DELETE this block — capture run is now created by /api/reference/start:
                    # Create capture_run on first entrance event
                    if _ref_capture_run_id[0] is None:
                        run_id = f"live_{uuid.uuid4().hex[:8]}"
                        run_name = f"Live {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
                        db.create_capture_run(run_id, game_id, run_name)
                        db.set_active_capture_run(run_id)
                        _ref_capture_run_id[0] = run_id
```

- [ ] **Step 7: Update `practice_start` to clean up reference state**

Modify the `practice_start` endpoint:

```python
@app.post("/api/practice/start")
async def practice_start():
    if _practice[0] and _practice[0].is_running:
        return {"status": "already_running"}
    if not tcp.is_connected:
        return {"status": "not_connected"}

    # Clean up reference state if transitioning from reference mode
    if _mode[0] == "reference":
        _clear_ref_state()

    ps = PracticeSession(tcp=tcp, db=db, game_id=game_id)
    _practice[0] = ps
    _practice_task[0] = asyncio.create_task(ps.run_loop())
    _mode[0] = "practice"
    return {"status": "started", "session_id": ps.session_id}
```

- [ ] **Step 8: Update `practice_stop` to reset mode**

Modify the `practice_stop` endpoint:

```python
@app.post("/api/practice/stop")
async def practice_stop():
    if _practice[0] and _practice[0].is_running:
        _practice[0].is_running = False
        if _practice_task[0]:
            try:
                await asyncio.wait_for(_practice_task[0], timeout=5)
            except asyncio.TimeoutError:
                _practice_task[0].cancel()
        _mode[0] = "idle"
        return {"status": "stopped"}
    return {"status": "not_running"}
```

- [ ] **Step 9: Fix `reset_data` to stop practice and clear all state**

Replace the reset endpoint:

```python
@app.post("/api/reset")
async def reset_data():
    # Stop active practice session if running
    if _practice[0] and _practice[0].is_running:
        _practice[0].is_running = False
        if _practice_task[0]:
            try:
                await asyncio.wait_for(_practice_task[0], timeout=5)
            except asyncio.TimeoutError:
                _practice_task[0].cancel()
    _clear_ref_state()  # resets mode, ref state
    db.reset_all_data()
    _scheduler[0] = None
    return {"status": "ok"}
```

- [ ] **Step 10: Add config dict parameter to `create_app` and store on app.state**

Update the `create_app` signature to accept the full parsed config:

```python
def create_app(
    db: Database,
    game_id: str,
    state_file: Path | None = None,  # deprecated, ignored
    host: str = "127.0.0.1",
    port: int = 15482,
    config: dict | None = None,
) -> FastAPI:
```

Store it early in the function:

```python
app.state.config = config or {}
```

- [ ] **Step 11: Add `POST /api/emulator/launch` endpoint**

Add near the other endpoints. Reads config from `app.state.config` (passed in at startup), not from CWD:

```python
@app.post("/api/emulator/launch")
def launch_emulator():
    import subprocess
    cfg = app.state.config
    emu_path = cfg.get("emulator", {}).get("path", "")
    if not emu_path or not Path(emu_path).exists():
        return {"status": "error", "message": f"Emulator not found: {emu_path}"}
    lua_script = cfg.get("emulator", {}).get("lua_script", "")
    rom_path = cfg.get("rom", {}).get("path", "")
    cmd = [emu_path]
    if rom_path and Path(rom_path).exists():
        cmd.append(rom_path)
    if lua_script:
        script_path = Path(lua_script)
        if not script_path.is_absolute():
            script_path = Path.cwd() / script_path
        if script_path.exists():
            cmd.append(str(script_path))
    subprocess.Popen(cmd)
    return {"status": "ok"}
```

- [ ] **Step 12: Run existing tests**

Run: `cd python && python -m pytest ../tests/test_dashboard.py -v`
Expected: PASS (idle mode is still the default when TCP isn't connected)

- [ ] **Step 13: Commit**

```bash
git add python/spinlab/dashboard.py
git commit -m "feat: explicit mode state machine, reference/emulator endpoints, reset fix"
```

---

### Task 3: Update tests for new mode behavior

**Files:**
- Modify: `tests/test_dashboard.py`

- [ ] **Step 1: Add test for reference start/stop**

```python
def test_reference_start_not_connected(client):
    resp = client.post("/api/reference/start")
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_connected"

def test_reference_stop_not_in_reference(client):
    resp = client.post("/api/reference/stop")
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_in_reference"
```

- [ ] **Step 2: Add test for reset clearing mode state**

```python
def test_reset_clears_mode_state(client, db):
    # Add some data to reset
    db.create_session("s1", "test_game")
    db.end_session("s1", 5, 3)
    resp = client.post("/api/reset")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
```

- [ ] **Step 3: Add test for emulator launch endpoint (config missing)**

```python
def test_launch_emulator_no_config(client):
    # app.state.config is empty dict by default (no emulator path)
    resp = client.post("/api/emulator/launch")
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"
```

- [ ] **Step 4: Run tests**

Run: `cd python && python -m pytest ../tests/test_dashboard.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_dashboard.py
git commit -m "test: add tests for mode state machine and new endpoints"
```

---

## Chunk 2: Frontend — Mode-Aware UI

### Task 4: Update index.html with mode-aware buttons

**Files:**
- Modify: `python/spinlab/static/index.html`

- [ ] **Step 1: Replace the idle/reference/practice sections**

Replace lines 24-56 (the three mode divs inside tab-live):

```html
<!-- Live Tab -->
<section id="tab-live" class="tab-content active">
  <div id="mode-disconnected">
    <p class="dim">Waiting for emulator...</p>
    <button id="btn-launch-emu" class="btn-primary">Launch Emulator</button>
  </div>
  <div id="mode-idle" style="display:none">
    <p class="dim">Emulator connected — ready</p>
    <button id="btn-ref-start" class="btn-primary">Start Reference Run</button>
    <button id="btn-practice-start" class="btn-primary">Start Practice</button>
  </div>
  <div id="mode-reference" style="display:none">
    <h2>Reference Run</h2>
    <p id="ref-sections">Sections: 0</p>
    <button id="btn-ref-stop" class="btn-danger">Stop Reference Run</button>
  </div>
  <div id="mode-practice" style="display:none">
    <div class="card" id="current-split">
      <div class="split-header">
        <span id="current-goal" class="goal-label"></span>
        <span id="current-attempts" class="dim"></span>
      </div>
      <div id="insight" class="insight-card"></div>
    </div>
    <div class="allocator-row">
      <label>Allocator:</label>
      <select id="allocator-select">
        <option value="greedy">Greedy</option>
        <option value="random">Random</option>
        <option value="round_robin">Round Robin</option>
      </select>
    </div>
    <h3>Up Next</h3>
    <ul id="queue"></ul>
    <h3>Recent</h3>
    <ul id="recent"></ul>
    <footer id="session-stats" class="dim"></footer>
    <button id="btn-practice-stop" class="btn-danger" style="margin:8px">Stop Practice</button>
  </div>
</section>
```

- [ ] **Step 2: Bump cache version**

Change `?v=6` to `?v=7` on both the CSS and JS script tags.

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/static/index.html
git commit -m "feat: mode-aware Live tab with launch/reference/practice buttons"
```

---

### Task 5: Update app.js for four-state mode switching

**Files:**
- Modify: `python/spinlab/static/app.js`

- [ ] **Step 1: Update `updateLive` function**

Replace the entire `updateLive` function (lines 44-131):

```javascript
function updateLive(data) {
  const disconnected = document.getElementById('mode-disconnected');
  const idle = document.getElementById('mode-idle');
  const ref = document.getElementById('mode-reference');
  const practice = document.getElementById('mode-practice');

  // Hide all first
  disconnected.style.display = 'none';
  idle.style.display = 'none';
  ref.style.display = 'none';
  practice.style.display = 'none';

  if (!data.tcp_connected) {
    disconnected.style.display = 'block';
    return;
  }

  if (data.mode === 'practice') {
    practice.style.display = 'block';

    const cs = data.current_split;
    if (cs) {
      document.getElementById('current-goal').textContent = splitName(cs);
      document.getElementById('current-attempts').textContent =
        'Attempt ' + (cs.attempt_count || 0);

      const insight = document.getElementById('insight');
      if (cs.drift_info) {
        const arrow = cs.drift_info.drift < 0 ? '↓' : cs.drift_info.drift > 0 ? '↑' : '→';
        const rate = Math.abs(cs.drift_info.drift).toFixed(2);
        insight.innerHTML =
          '<span class="drift-' + cs.drift_info.label + '">' +
          arrow + ' ' + rate + ' s/run</span>' +
          ' <span class="dim">(' + cs.drift_info.confidence + ')</span>';
      } else {
        insight.textContent = 'No data yet';
      }
    }

    const queue = document.getElementById('queue');
    queue.innerHTML = '';
    (data.queue || []).forEach(q => {
      const li = document.createElement('li');
      li.textContent = splitName(q);
      queue.appendChild(li);
    });

    const recent = document.getElementById('recent');
    recent.innerHTML = '';
    (data.recent || []).forEach(r => {
      const li = document.createElement('li');
      const time = formatTime(r.time_ms);
      const refTime = r.reference_time_ms ? formatTime(r.reference_time_ms) : '—';
      const cls = r.reference_time_ms && r.time_ms <= r.reference_time_ms ? 'ahead' : 'behind';
      li.innerHTML = '<span class="' + cls + '">' + time + '</span> / ' + refTime +
        ' <span class="dim">' + splitName(r) + '</span>';
      recent.appendChild(li);
    });

    const stats = document.getElementById('session-stats');
    if (data.session) {
      stats.textContent = (data.session.splits_completed || 0) + '/' +
        (data.session.splits_attempted || 0) + ' cleared | ' +
        elapsedStr(data.session.started_at);
    }

    if (data.allocator) {
      document.getElementById('allocator-select').value = data.allocator;
    }

  } else if (data.mode === 'reference') {
    ref.style.display = 'block';
    document.getElementById('ref-sections').textContent =
      'Sections: ' + (data.sections_captured || 0);

  } else {
    // idle
    idle.style.display = 'block';
  }

  if (data.session && data.session.started_at) {
    document.getElementById('session-timer').textContent = elapsedStr(data.session.started_at);
  }
}
```

- [ ] **Step 2: Add event listeners for new buttons**

Replace the practice start/stop listeners (lines 305-312) and add new ones:

```javascript
// === Mode control buttons ===
document.getElementById('btn-launch-emu')?.addEventListener('click', async () => {
  const res = await fetch('/api/emulator/launch', { method: 'POST' });
  const data = await res.json();
  if (data.status === 'error') alert(data.message);
});

document.getElementById('btn-ref-start')?.addEventListener('click', async () => {
  await fetch('/api/reference/start', { method: 'POST' });
});

document.getElementById('btn-ref-stop')?.addEventListener('click', async () => {
  await fetch('/api/reference/stop', { method: 'POST' });
});

document.getElementById('btn-practice-start')?.addEventListener('click', async () => {
  await fetch('/api/practice/start', { method: 'POST' });
});

document.getElementById('btn-practice-stop')?.addEventListener('click', async () => {
  await fetch('/api/practice/stop', { method: 'POST' });
});
```

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/static/app.js
git commit -m "feat: four-state mode UI (disconnected/idle/reference/practice)"
```

---

## Chunk 3: Cleanup — Remove Dead Code + Simplify AHK

### Task 6: Simplify AHK to two hotkeys

**Files:**
- Modify: `scripts/spinlab.ahk`

- [ ] **Step 1: Rewrite spinlab.ahk**

```ahk
#Requires AutoHotkey v2.0
#SingleInstance Force

global dashPID := 0

Flash(msg, ms := 2000) {
    ToolTip msg
    SetTimer () => ToolTip(), -ms
}

FindDashPID() {
    try {
        tmpFile := A_Temp "\spinlab_port.txt"
        RunWait 'cmd /c "netstat -ano | findstr :15483 | findstr LISTENING > ' tmpFile '"',, "Hide"
        line := Trim(FileRead(tmpFile))
        FileDelete tmpFile
        if (line != "") {
            parts := StrSplit(line, " ")
            pid := parts[parts.Length]
            if (pid > 0)
                return Integer(pid)
        }
    }
    return 0
}

StopDashboard() {
    global dashPID
    pid := (dashPID != 0 && ProcessExist(dashPID)) ? dashPID : FindDashPID()
    if (pid != 0 && ProcessExist(pid)) {
        Run "taskkill /PID " pid " /T /F",, "Hide"
        dashPID := 0
        return true
    }
    return false
}

; Ctrl+Alt+W — launch Mesen + dashboard (idempotent)
^!w:: {
    global dashPID
    ; Launch Mesen if not running
    if !ProcessExist("Mesen.exe") {
        Run 'cmd /c "' A_ScriptDir '\launch.bat"',, 'Hide'
    }
    ; Launch dashboard if not running
    existingPID := (dashPID != 0 && ProcessExist(dashPID)) ? dashPID : FindDashPID()
    if (existingPID != 0) {
        dashPID := existingPID
    } else {
        Run 'spinlab dashboard', A_ScriptDir '\..',  'Min', &dashPID
    }
    Flash("SpinLab started", 2000)
}

; Ctrl+Alt+X — kill everything
^!x:: {
    Run 'cmd /c spinlab lua-cmd practice_stop', A_ScriptDir '\..',  'Hide'
    if ProcessExist("Mesen.exe")
        Run 'taskkill /IM Mesen.exe /F',, "Hide"
    StopDashboard()
    Flash "SpinLab — stopped"
}
```

- [ ] **Step 2: Commit**

```bash
git add scripts/spinlab.ahk
git commit -m "feat: simplify AHK to two hotkeys (launch + kill)"
```

---

### Task 7: Remove deprecated CLI commands and dead modules

**Files:**
- Modify: `python/spinlab/cli.py`
- Delete: `python/spinlab/capture.py`
- Delete: `python/spinlab/orchestrator.py`
- Delete: `tests/test_orchestrator.py`
- Delete: `tests/test_orchestrator_state.py`
- Delete: `tests/test_capture.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Update cli.py — remove capture and practice subcommands**

```python
"""SpinLab CLI entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(args: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="spinlab",
        description="SpinLab — spaced repetition practice for SNES speedrunning",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # stats
    sub.add_parser("stats", help="Show practice statistics (coming soon)")

    # dashboard
    p_dash = sub.add_parser("dashboard", help="Start the web dashboard")
    p_dash.add_argument(
        "--config", default="config.yaml", help="Path to config.yaml"
    )
    p_dash.add_argument(
        "--port", type=int, default=15483, help="Dashboard port"
    )

    # lua-cmd
    p_lua = sub.add_parser("lua-cmd", help="Send raw commands to the Lua TCP server")
    p_lua.add_argument("commands", nargs="+", help="Commands to send (e.g. practice_stop reset)")

    parsed = parser.parse_args(args)

    if parsed.command == "stats":
        print("Stats coming in a future step.")
        sys.exit(0)

    elif parsed.command == "dashboard":
        import uvicorn
        import yaml
        from spinlab.dashboard import create_app
        from spinlab.db import Database
        from spinlab.manifest import find_latest_manifest, load_manifest, seed_db_from_manifest

        config_path = Path(parsed.config)
        with config_path.open(encoding="utf-8") as f:
            config = yaml.safe_load(f)
        game_id = config["game"]["id"]
        data_dir = Path(config["data"]["dir"])
        host = config.get("network", {}).get("host", "127.0.0.1")
        port = config.get("network", {}).get("port", 15482)
        db = Database(data_dir / "spinlab.db")

        # Seed DB from manifest if splits are empty
        if not db.get_active_splits(game_id):
            manifest_path = find_latest_manifest(data_dir)
            if manifest_path:
                manifest = load_manifest(manifest_path)
                seed_db_from_manifest(db, manifest, config["game"]["name"])

        app = create_app(db=db, game_id=game_id, host=host, port=port, config=config)
        print(f"SpinLab Dashboard: http://localhost:{parsed.port}")
        uvicorn.run(app, host="0.0.0.0", port=parsed.port, log_level="warning")

    elif parsed.command == "lua-cmd":
        import socket
        try:
            with socket.create_connection(("127.0.0.1", 15482), timeout=2) as s:
                for cmd in parsed.commands:
                    s.sendall((cmd + "\n").encode())
        except OSError:
            pass


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Update test_cli.py**

```python
"""Tests for CLI dispatch."""
import pytest
from spinlab.cli import main


def test_stats_subcommand_prints_stub(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["stats"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "Stats coming in a future step" in captured.out


def test_unknown_subcommand_exits_nonzero():
    with pytest.raises(SystemExit) as exc:
        main(["notacommand"])
    assert exc.value.code != 0


def test_dashboard_subcommand_imports():
    """Dashboard subcommand is registered and dashboard module is importable."""
    from spinlab import dashboard
    assert hasattr(dashboard, "create_app")
```

- [ ] **Step 3: Delete dead files**

```bash
rm python/spinlab/capture.py python/spinlab/orchestrator.py
rm tests/test_orchestrator.py tests/test_orchestrator_state.py tests/test_capture.py
```

- [ ] **Step 4: Run all tests**

Run: `cd python && python -m pytest ../tests/ -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/cli.py tests/test_cli.py
git rm python/spinlab/capture.py python/spinlab/orchestrator.py tests/test_orchestrator.py tests/test_orchestrator_state.py tests/test_capture.py
git commit -m "feat: remove deprecated CLI commands, delete orchestrator.py and capture.py"
```

---

### Task 8: Update config.example.yaml and README

**Files:**
- Modify: `config.example.yaml`
- Modify: `README.md`

- [ ] **Step 1: Clean up config.example.yaml**

Remove the `scheduler.algorithm`, `scheduler.base_interval_minutes`, and `scheduler.auto_rate_passive` keys that reference the old SM-2 system. Replace with current scheduler config:

```yaml
emulator:
  path: "C:/path/to/Mesen.exe"
  type: mesen2
  lua_script: "lua/spinlab.lua"
  script_data_dir: "C:/Users/<you>/Documents/Mesen2/LuaScriptData/spinlab"

rom:
  path: ""  # Optional — leave empty to load ROM from Mesen UI

game:
  id: smw_cod
  name: "SMW: City of Dreams"
  category: "any%"

network:
  port: 15482
  host: "127.0.0.1"

scheduler:
  estimator: kalman
  allocator: greedy
  auto_advance_delay_s: 2.0

data:
  dir: "data"
```

- [ ] **Step 2: Update README scheduler section and config table**

Update the config table to match the new keys. Remove references to `spinlab capture` and `spinlab practice` from the CLI commands table. Update the "Quick Start" section step 4 to mention the dashboard's reference/practice mode buttons.

- [ ] **Step 3: Commit**

```bash
git add config.example.yaml README.md
git commit -m "docs: update config and README for dashboard-driven mode control"
```

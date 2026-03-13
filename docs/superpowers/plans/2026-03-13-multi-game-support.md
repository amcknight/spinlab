# Multi-Game Support Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-discover games from ROM files so Andrew can open any of ~300 SMW romhacks and have SpinLab track it without configuration.

**Architecture:** Lua sends the ROM filename over TCP. Python computes a SHA-256 checksum of the ROM file (truncated to 16 hex chars) as the game ID. Dashboard switches to mutable game context that changes dynamically when the emulator loads a different ROM. DB schema is already multi-game — we just need to wire up the dynamic switching.

**Tech Stack:** Python 3.11+, FastAPI, Lua (Mesen2), vanilla JS

**Prerequisite:** The `worktree/dashboard-mode-control` branch is merged. This plan assumes the explicit `_mode` flag, `manifest.py`, and updated endpoints are in place.

**Key codebase patterns to follow:**
- All mutable state in `dashboard.py` lives **inside** the `create_app` closure (not at module level): `_scheduler`, `_practice`, `_mode`, etc.
- State is exposed for testing via `app.state` (e.g., `app.state._mode`, `app.state.tcp`).
- Test fixtures create the app inline in each test file (no shared `conftest.py`).
- Lua uses `to_json(table)` for JSON serialization and `ensure_dir(path)` for directory creation.

---

## Chunk 1: Backend — Game Identity & DB Changes

### Task 1: Add ROM checksum utility

**Files:**
- Create: `python/spinlab/romid.py`
- Create: `tests/test_romid.py`

- [ ] **Step 1: Write failing test for checksum computation**

```python
"""Tests for ROM identity utilities."""
from pathlib import Path

from spinlab.romid import rom_checksum, game_name_from_filename


def test_rom_checksum_deterministic(tmp_path):
    rom = tmp_path / "test.sfc"
    rom.write_bytes(b"\x00" * 1024)
    c1 = rom_checksum(rom)
    c2 = rom_checksum(rom)
    assert c1 == c2
    assert len(c1) == 16
    assert all(ch in "0123456789abcdef" for ch in c1)


def test_rom_checksum_differs_for_different_content(tmp_path):
    rom_a = tmp_path / "a.sfc"
    rom_b = tmp_path / "b.sfc"
    rom_a.write_bytes(b"\x00" * 1024)
    rom_b.write_bytes(b"\xff" * 1024)
    assert rom_checksum(rom_a) != rom_checksum(rom_b)


def test_game_name_from_filename():
    assert game_name_from_filename("City of Dreams.sfc") == "City of Dreams"
    assert game_name_from_filename("My Hack v1.2.smc") == "My Hack v1.2"
    assert game_name_from_filename("noext") == "noext"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && python -m pytest ../tests/test_romid.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement romid.py**

```python
"""ROM identity utilities: checksum and name extraction."""
from __future__ import annotations

import hashlib
from pathlib import Path


def rom_checksum(rom_path: Path) -> str:
    """Compute truncated SHA-256 of a ROM file. Returns 16 hex chars."""
    h = hashlib.sha256(rom_path.read_bytes())
    return h.hexdigest()[:16]


def game_name_from_filename(filename: str) -> str:
    """Extract display name from ROM filename (strip extension)."""
    p = Path(filename)
    if p.suffix.lower() in (".sfc", ".smc", ".fig", ".swc"):
        return p.stem
    return filename
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && python -m pytest ../tests/test_romid.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/romid.py tests/test_romid.py
git commit -m "feat: add ROM checksum and name utilities"
```

---

### Task 2: Update `upsert_game` to preserve display names

**Files:**
- Modify: `python/spinlab/db.py:145-151`
- Modify: `tests/test_db_references.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_db_references.py`:

```python
def test_upsert_game_preserves_existing_name(tmp_db):
    """upsert_game should not overwrite name if game already exists."""
    tmp_db.upsert_game("g1", "Original Name", "any%")
    tmp_db.upsert_game("g1", "New Name", "any%")
    row = tmp_db.conn.execute("SELECT name FROM games WHERE id = ?", ("g1",)).fetchone()
    assert row[0] == "Original Name"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest ../tests/test_db_references.py::test_upsert_game_preserves_existing_name -v`
Expected: FAIL (currently overwrites with "New Name")

- [ ] **Step 3: Update upsert_game in db.py**

Replace lines 145-151 in `python/spinlab/db.py`:

```python
def upsert_game(self, game_id: str, name: str, category: str) -> None:
    now = datetime.utcnow().isoformat()
    self.conn.execute(
        "INSERT INTO games (id, name, category, created_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(id) DO NOTHING",
        (game_id, name, category, now),
    )
    self.conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && python -m pytest ../tests/test_db_references.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db.py tests/test_db_references.py
git commit -m "fix: upsert_game preserves existing display name"
```

---

### Task 3: Add game-scoped reset to db.py

**Files:**
- Modify: `python/spinlab/db.py` (add after `reset_all_data` at line ~400)
- Modify: `tests/test_db_dashboard.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_db_dashboard.py`. Note: use `db.log_attempt(Attempt(...))` — the DB API takes an `Attempt` dataclass, not positional args.

```python
from spinlab.models import Split, Attempt

def test_reset_game_data_scoped(tmp_db):
    """reset_game_data should only delete data for the specified game."""
    tmp_db.upsert_game("g1", "Game 1", "any%")
    tmp_db.upsert_game("g2", "Game 2", "any%")
    s1 = Split(id="g1:1:1:normal", game_id="g1", level_number=1, room_id=1, goal="normal")
    s2 = Split(id="g2:1:1:normal", game_id="g2", level_number=1, room_id=1, goal="normal")
    tmp_db.upsert_split(s1)
    tmp_db.upsert_split(s2)
    tmp_db.create_session("s1", "g1")
    tmp_db.create_session("s2", "g2")
    tmp_db.log_attempt(Attempt(split_id="g1:1:1:normal", time_ms=5000, completed=True, session_id="s1"))
    tmp_db.log_attempt(Attempt(split_id="g2:1:1:normal", time_ms=6000, completed=True, session_id="s2"))

    tmp_db.reset_game_data("g1")

    # g1 data gone
    assert tmp_db.get_recent_attempts("g1") == []
    assert tmp_db.get_session_history("g1") == []
    # g2 data intact
    assert len(tmp_db.get_recent_attempts("g2")) == 1
    assert len(tmp_db.get_session_history("g2")) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest ../tests/test_db_dashboard.py::test_reset_game_data_scoped -v`
Expected: FAIL (method doesn't exist)

- [ ] **Step 3: Add reset_game_data method**

Add after `reset_all_data` in `python/spinlab/db.py`:

```python
def reset_game_data(self, game_id: str) -> None:
    """Delete attempts, sessions, model state for a specific game.

    Keeps splits, games, and global allocator_config intact.
    """
    self.conn.execute(
        "DELETE FROM attempts WHERE split_id IN"
        " (SELECT id FROM splits WHERE game_id = ?)",
        (game_id,),
    )
    self.conn.execute(
        "DELETE FROM model_state WHERE split_id IN"
        " (SELECT id FROM splits WHERE game_id = ?)",
        (game_id,),
    )
    self.conn.execute("DELETE FROM sessions WHERE game_id = ?", (game_id,))
    self.conn.execute("DELETE FROM transitions WHERE game_id = ?", (game_id,))
    self.conn.commit()
```

Note: `allocator_config` is global (not per-game) — do NOT delete it here.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && python -m pytest ../tests/test_db_dashboard.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db.py tests/test_db_dashboard.py
git commit -m "feat: add game-scoped reset_game_data method"
```

---

## Chunk 2: Dashboard — Dynamic Game Context

### Task 4: Replace fixed game_id with mutable state in dashboard.py

**Files:**
- Modify: `python/spinlab/dashboard.py`
- Modify: `tests/test_dashboard.py`
- Modify: `tests/test_dashboard_integration.py`
- Modify: `tests/test_dashboard_references.py`

**Important context:** All mutable state in `dashboard.py` is defined inside the `create_app` closure (lines 57-69), not at module level. The `_switch_game` function must also be defined inside the closure since it accesses `_practice`, `_mode`, `_scheduler`, etc. Tests access internal state via `app.state`.

- [ ] **Step 1: Update create_app signature**

Change `create_app` signature in `python/spinlab/dashboard.py` — remove `game_id` parameter, add `rom_dir` and `default_category`:

```python
def create_app(
    db: Database,
    rom_dir: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 15482,
    config: dict | None = None,
    default_category: str = "any%",
) -> FastAPI:
```

Remove the game-row-creation block (lines 35-39) — games are now auto-created on ROM detection.

- [ ] **Step 2: Add mutable game state containers**

Add to the shared state section (after line 63, near `_mode`):

```python
_game_id: list[str | None] = [None]
_game_name: list[str | None] = [None]
```

Expose for testing:

```python
app.state._game_id = _game_id
app.state._game_name = _game_name
```

- [ ] **Step 3: Add _require_game helper and _switch_game function**

Both inside the `create_app` closure:

```python
def _require_game() -> str:
    """Return current game_id or raise HTTPException."""
    gid = _game_id[0]
    if gid is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail="No game loaded")
    return gid

def _switch_game(new_game_id: str, display_name: str, category: str) -> None:
    """Switch active game context. Stops any active session first."""
    if _game_id[0] == new_game_id:
        return  # same game, no-op

    # Stop practice if running (set flag; task will self-terminate on next loop)
    if _practice[0] and _practice[0].is_running:
        _practice[0].is_running = False

    # Clear reference state
    _clear_ref_state()

    # Create game in DB if new (preserves existing name)
    db.upsert_game(new_game_id, display_name, category)

    # Switch context
    _game_id[0] = new_game_id
    _game_name[0] = display_name
    _scheduler[0] = None  # force re-creation for new game
    _mode[0] = "idle"
```

Note: `_switch_game` is synchronous — it cannot `await` the practice task. Setting `is_running = False` causes the practice loop to exit on its next iteration. The existing `done_callback` on the task (line 265-266) will reset mode to idle.

Expose for testing:

```python
app.state._switch_game = _switch_game
```

- [ ] **Step 4: Replace all `game_id` closure references**

These are the 15 locations in `dashboard.py` that reference the old `game_id` closure variable. Each must be replaced:

| Line | Old | New |
|------|-----|-----|
| 73 | `Scheduler(db, game_id)` | `Scheduler(db, _require_game())` |
| 138 | `Split.make_id(game_id, ...)` | `Split.make_id(_require_game(), ...)` |
| 142 | `game_id=game_id` | `game_id=_require_game()` |
| 192 | `db.get_all_splits_with_model(game_id)` | `db.get_all_splits_with_model(gid)` (use local var) |
| 211 | `db.get_all_splits_with_model(game_id)` | `db.get_all_splits_with_model(gid)` |
| 215 | `db.get_recent_attempts(game_id, ...)` | `db.get_recent_attempts(gid, ...)` |
| 238 | `db.create_capture_run(run_id, game_id, ...)` | `db.create_capture_run(run_id, _require_game(), ...)` |
| 262 | `PracticeSession(tcp=tcp, db=db, game_id=game_id)` | `PracticeSession(tcp=tcp, db=db, game_id=_require_game())` |
| 347 | `db.get_all_splits_with_model(game_id)` | `db.get_all_splits_with_model(_require_game())` |
| 352 | `db.get_session_history(game_id)` | `db.get_session_history(_require_game())` |
| 359 | `db.list_capture_runs(game_id)` | `db.list_capture_runs(_require_game())` |
| 365 | `db.create_capture_run(run_id, game_id, ...)` | `db.create_capture_run(run_id, _require_game(), ...)` |
| 433 | `manifest.get("game_id", game_id)` | `manifest.get("game_id", _game_id[0] or "unknown")` |

- [ ] **Step 5: Update /api/state to handle no-game gracefully**

The `/api/state` endpoint must NOT call `_get_scheduler()` when no game is loaded. Return a minimal response instead:

```python
@app.get("/api/state")
def api_state():
    mode = _current_mode()

    # No game loaded yet — return minimal state
    if _game_id[0] is None:
        return {
            "mode": mode,
            "tcp_connected": tcp.is_connected,
            "game_id": None,
            "game_name": None,
            "current_split": None,
            "queue": [],
            "recent": [],
            "session": None,
            "sections_captured": 0,
            "allocator": None,
            "estimator": None,
        }

    gid = _game_id[0]
    sched = _get_scheduler()
    # ... rest of existing logic, using gid instead of game_id ...

    return {
        # ... existing fields ...
        "game_id": _game_id[0],
        "game_name": _game_name[0],
    }
```

- [ ] **Step 6: Update /api/reset to use game-scoped reset**

```python
@app.post("/api/reset")
async def reset_data():
    if _practice[0] and _practice[0].is_running:
        _practice[0].is_running = False
        if _practice_task[0]:
            try:
                await asyncio.wait_for(_practice_task[0], timeout=5)
            except asyncio.TimeoutError:
                _practice_task[0].cancel()
    _clear_ref_state()
    gid = _game_id[0]
    if gid:
        db.reset_game_data(gid)
    _scheduler[0] = None
    return {"status": "ok"}
```

- [ ] **Step 7: Update test fixtures in all dashboard test files**

The `client` fixture in each test file passes `game_id="test_game"` to `create_app`. Update to use the new signature and set game context via `app.state`:

In `tests/test_dashboard.py`, update the `client` fixture:

```python
@pytest.fixture
def client(db, tmp_path):
    from spinlab.dashboard import create_app
    app = create_app(db=db, host="127.0.0.1", port=59999)
    # Set game context for tests (simulates ROM detection)
    app.state._game_id[0] = "test_game"
    app.state._game_name[0] = "Test Game"
    return TestClient(app)
```

Apply the same pattern to fixtures in `tests/test_dashboard_integration.py` and `tests/test_dashboard_references.py`.

Add a separate no-game test with its own fixture:

```python
@pytest.fixture
def client_no_game(db, tmp_path):
    from spinlab.dashboard import create_app
    app = create_app(db=db, host="127.0.0.1", port=59999)
    return TestClient(app)


def test_api_state_no_game_loaded(client_no_game):
    """When no ROM has been detected, state should report no game."""
    resp = client_no_game.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()
    assert data["game_id"] is None
    assert data["game_name"] is None
    assert data["allocator"] is None
```

- [ ] **Step 8: Run all dashboard tests**

Run: `cd python && python -m pytest ../tests/test_dashboard.py ../tests/test_dashboard_integration.py ../tests/test_dashboard_references.py -v`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add python/spinlab/dashboard.py tests/test_dashboard.py tests/test_dashboard_integration.py tests/test_dashboard_references.py
git commit -m "feat: replace fixed game_id with mutable game context"
```

---

### Task 5: Handle rom_info events and game switching

**Files:**
- Modify: `python/spinlab/dashboard.py` (event dispatch loop)
- Modify: `tests/test_dashboard.py`

- [ ] **Step 1: Write tests for game switching**

Add to `tests/test_dashboard.py`:

```python
def test_switch_game_sets_context(client, db):
    """Switching game updates game_id and game_name."""
    app = client.app
    app.state._switch_game("new_checksum", "New Game", "any%")
    resp = client.get("/api/state")
    data = resp.json()
    assert data["game_id"] == "new_checksum"
    assert data["game_name"] == "New Game"


def test_switch_game_same_id_is_noop(client, db):
    """Switching to the same game should be a no-op."""
    app = client.app
    app.state._switch_game("test_game", "Test Game", "any%")
    # mode should still be whatever it was (not reset to idle)
    resp = client.get("/api/state")
    assert resp.json()["mode"] == "idle"


def test_switch_game_resets_scheduler(client, db):
    """Switching game should invalidate cached scheduler."""
    app = client.app
    # Access scheduler to cache it
    client.get("/api/state")
    assert app.state._scheduler[0] is not None
    # Switch game
    app.state._switch_game("other_game", "Other Game", "any%")
    assert app.state._scheduler[0] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && python -m pytest ../tests/test_dashboard.py -v -k "switch_game"`
Expected: FAIL

- [ ] **Step 3: Add rom_info handling to event dispatch loop**

In `_event_dispatch_loop()` in `python/spinlab/dashboard.py`, add before the reference-mode check (before the `if _mode[0] != "reference":` line):

```python
# Handle rom_info: auto-discover game from ROM filename
if event.get("event") == "rom_info":
    filename = event.get("filename", "")
    if rom_dir and filename:
        rom_path = rom_dir / filename
        if rom_path.exists():
            from spinlab.romid import rom_checksum, game_name_from_filename
            checksum = rom_checksum(rom_path)
            name = game_name_from_filename(filename)
            _switch_game(checksum, name, default_category)
            # Send game_context back to Lua
            tcp.send(json.dumps({
                "event": "game_context",
                "game_id": checksum,
                "game_name": name,
            }))
        else:
            # Fallback: use filename as game ID when ROM not in rom_dir
            name = game_name_from_filename(filename)
            fallback_id = f"file_{name.lower().replace(' ', '_')}"
            _switch_game(fallback_id, name, default_category)
            tcp.send(json.dumps({
                "event": "game_context",
                "game_id": fallback_id,
                "game_name": name,
            }))
            logger.warning("ROM not found in rom_dir: %s — using filename as ID", filename)
    continue
```

- [ ] **Step 4: Run all tests**

Run: `cd python && python -m pytest ../tests/test_dashboard.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/dashboard.py tests/test_dashboard.py
git commit -m "feat: handle rom_info events, auto-discover games from ROM files"
```

---

## Chunk 3: Lua — ROM Filename Reporting

### Task 6: Send rom_info on connect and handle game_context response

**Files:**
- Modify: `lua/spinlab.lua`

Note: Lua changes are manually tested in Mesen2, not via automated tests.

- [ ] **Step 1: Replace hardcoded GAME_ID with mutable variable**

Replace line 25 in `lua/spinlab.lua`:

```lua
-- Old:
local GAME_ID    = "smw_cod"   -- TODO: read from config.yaml in Step 6

-- New:
local game_id    = nil  -- set dynamically from dashboard via game_context
```

- [ ] **Step 2: Add ROM filename detection**

Add after the config section, before TCP setup:

```lua
-- Get ROM filename from Mesen
local function get_rom_filename()
    local info = emu.getRomInfo()
    if info and info.fileName then
        return info.fileName
    end
    -- Fallback: try other API
    local name = emu.getRomName and emu.getRomName() or "unknown"
    return name .. ".sfc"
end
```

Note: The exact Mesen2 API needs verification. `emu.getRomInfo()` is the most likely candidate. If it doesn't exist, check Mesen2 docs for the correct function.

- [ ] **Step 3: Send rom_info on TCP connect**

In the TCP connection handling code, after a successful client connection, use the existing `to_json` function (line 231) for safe JSON serialization:

```lua
local rom_fname = get_rom_filename()
client:send(to_json({event = "rom_info", filename = rom_fname}) .. "\n")
```

- [ ] **Step 4: Handle game_context response**

In the TCP message parsing section, add handling for `game_context`. Use the existing `ensure_dir` function (line 88) for directory creation:

```lua
if decoded.event == "game_context" then
    game_id = decoded.game_id
    -- Create per-game state subdirectory
    ensure_dir(STATE_DIR .. "/" .. game_id)
    emu.log("[spinlab] Game context: " .. (decoded.game_name or game_id))
end
```

- [ ] **Step 5: Update state file naming to use subdirectory**

Find the state file naming code (currently line ~296 after merge). Replace:

```lua
-- Old:
local state_fname = GAME_ID .. "_" .. curr.level_num .. "_" .. curr.room_num .. ".mss"
local state_path  = STATE_DIR .. "/" .. state_fname

-- New:
if not game_id then
    emu.log("[spinlab] No game context yet, skipping state save")
    return
end
local state_fname = curr.level_num .. "_" .. curr.room_num .. ".mss"
local state_path  = STATE_DIR .. "/" .. game_id .. "/" .. state_fname
```

- [ ] **Step 6: Test manually in Mesen2**

1. Start dashboard: `spinlab dashboard`
2. Open Mesen2 with a ROM from `C:\Users\thedo\Dropbox\SNES\SMW Hacks`
3. Verify dashboard header shows the game name
4. Verify state files are saved to `states/<checksum>/` subdirectory
5. Switch ROMs in Mesen2 — verify dashboard switches game context

- [ ] **Step 7: Commit**

```bash
git add lua/spinlab.lua
git commit -m "feat: send rom_info on connect, use dynamic game_id for state files"
```

---

## Chunk 4: Frontend, Config & CLI Cleanup

### Task 7: Add game name to dashboard header

**Files:**
- Modify: `python/spinlab/static/index.html`
- Modify: `python/spinlab/static/app.js`

- [ ] **Step 1: Update header in index.html**

Replace the header section in `python/spinlab/static/index.html`:

```html
<header>
  <div>
    <h1>SpinLab<span id="game-name" class="dim" style="margin-left:12px;font-size:0.6em"></span></h1>
  </div>
  <span id="session-timer" class="dim"></span>
</header>
```

- [ ] **Step 2: Update app.js to populate game name**

In `python/spinlab/static/app.js`, in the main polling function that processes `/api/state` response data (near the top, before mode-specific handling), add:

```javascript
const gameName = document.getElementById('game-name');
if (data.game_name) {
  gameName.textContent = data.game_name;
} else {
  gameName.textContent = '';
}
```

- [ ] **Step 3: Bump cache version on CSS/JS tags**

Increment `?v=` on both the CSS and JS script tags in `index.html`.

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/static/index.html python/spinlab/static/app.js
git commit -m "feat: show current game name in dashboard header"
```

---

### Task 8: Update config and CLI

**Files:**
- Modify: `config.example.yaml`
- Modify: `python/spinlab/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Update config.example.yaml**

```yaml
emulator:
  path: "C:/path/to/Mesen.exe"
  type: mesen2
  lua_script: "lua/spinlab.lua"
  script_data_dir: "C:/Users/<you>/Documents/Mesen2/LuaScriptData/spinlab"

rom:
  dir: "C:/path/to/your/romhacks"  # directory containing ROM files (.sfc/.smc)

game:
  category: "any%"  # default category for auto-discovered games

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

Note: `game.id` and `game.name` are removed — games are auto-discovered from ROMs. `rom.path` (single file) is replaced by `rom.dir` (directory).

- [ ] **Step 2: Update CLI dashboard subcommand**

Replace the dashboard handler in `python/spinlab/cli.py`. Key changes:
- No longer reads `game_id` or `game_name` from config
- Passes `rom_dir` and `default_category` to `create_app`
- Removes manifest seeding on startup (games are auto-discovered)

```python
elif parsed.command == "dashboard":
    import uvicorn
    import yaml
    from spinlab.dashboard import create_app
    from spinlab.db import Database

    config_path = Path(parsed.config)
    with config_path.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)
    data_dir = Path(config["data"]["dir"])
    host = config.get("network", {}).get("host", "127.0.0.1")
    port = config.get("network", {}).get("port", 15482)
    rom_dir_str = config.get("rom", {}).get("dir", "")
    rom_dir = Path(rom_dir_str) if rom_dir_str else None
    default_category = config.get("game", {}).get("category", "any%")
    db = Database(data_dir / "spinlab.db")

    app = create_app(
        db=db,
        rom_dir=rom_dir,
        host=host,
        port=port,
        config=config,
        default_category=default_category,
    )
    print(f"SpinLab Dashboard: http://localhost:{parsed.port}")
    uvicorn.run(app, host="0.0.0.0", port=parsed.port, log_level="warning")
```

- [ ] **Step 3: Update test_cli.py**

Ensure the dashboard import test still works:

```python
def test_dashboard_subcommand_imports():
    """Dashboard subcommand is registered and dashboard module is importable."""
    from spinlab import dashboard
    assert hasattr(dashboard, "create_app")
```

- [ ] **Step 4: Run all tests**

Run: `cd python && python -m pytest ../tests/ -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add config.example.yaml python/spinlab/cli.py tests/test_cli.py
git commit -m "feat: update config and CLI for auto-discovered games"
```

---

### Task 9: Clean up old game references and update docs

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update CLAUDE.md architecture section**

Update the component list to remove references to hardcoded game config:
- Remove `config.yaml # User config: ROM path, emulator path, game-specific settings` and replace with note about auto-discovery
- Update the Architecture Overview to mention that games are auto-discovered from ROM checksums
- Remove `game.id` and `game.name` from any config references

- [ ] **Step 2: Run full test suite**

Run: `cd python && python -m pytest ../tests/ -v`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for multi-game auto-discovery"
```

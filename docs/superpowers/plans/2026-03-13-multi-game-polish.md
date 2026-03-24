# Multi-Game Polish & Cleanup Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix cross-platform bugs, clean up stale artifacts from pre-multi-game era, add Playwright smoke tests with two-game switching, and start fresh with an empty DB.

**Architecture:** Quick fixes first (Lua mkdir, Python utcnow, config), then remove stale files/code, then add Playwright tests that exercise the dashboard with two dummy ROM files to verify game auto-discovery and switching work end-to-end.

**Tech Stack:** Python 3.11+, FastAPI, Lua (Mesen2), Playwright MCP, vanilla JS

**Prerequisite:** The `worktree/multi-game` branch is merged to main. All 121 tests pass.

**Key codebase patterns to follow:**
- All mutable state in `dashboard.py` lives inside the `create_app` closure
- State exposed for testing via `app.state`
- Test fixtures create the app inline in each test file (no shared conftest.py)
- Lua uses `to_json(table)` for JSON serialization

**User involvement required:**
- Task 5 (Mesen2 API verification) requires Andrew to test in Mesen2 and report back
- Task 8 (Playwright smoke tests) requires dashboard to be running — launch with `spinlab dashboard`

---

## Chunk 1: Quick Fixes & Housekeeping

### Task 1: Fix `ensure_dir` for Windows

**Files:**
- Modify: `lua/spinlab.lua:88-91`

The current `ensure_dir` uses `mkdir -p` which is Unix-only. Andrew is on Windows.

- [ ] **Step 1: Fix ensure_dir to be cross-platform**

Replace lines 88-91 in `lua/spinlab.lua`:

```lua
local function ensure_dir(path)
  if package.config:sub(1, 1) == "\\" then
    -- Windows: mkdir creates parent dirs by default, 2>NUL suppresses "already exists"
    os.execute('mkdir "' .. path:gsub("/", "\\") .. '" 2>NUL')
  else
    os.execute('mkdir -p "' .. path .. '" 2>/dev/null')
  end
end
```

Note: `package.config:sub(1,1)` returns the directory separator (`\` on Windows, `/` on Unix). This is the standard Lua idiom for OS detection.

- [ ] **Step 2: Commit**

```bash
git add lua/spinlab.lua
git commit -m "fix: cross-platform ensure_dir (mkdir -p → Windows mkdir)"
```

---

### Task 2: Fix `datetime.utcnow()` deprecation warnings

**Files:**
- Modify: `python/spinlab/db.py` (3 occurrences)
- Modify: `python/spinlab/practice.py` (1 occurrence)
- Modify: `python/spinlab/manifest.py` (1 occurrence)

These produce 795 DeprecationWarnings in tests. Replace `datetime.utcnow()` with `datetime.now(datetime.UTC)`.

- [ ] **Step 1: Fix all occurrences**

In `python/spinlab/db.py`, find and replace all `datetime.utcnow()` with `datetime.now(datetime.UTC)`.

In `python/spinlab/practice.py`, same replacement.

In `python/spinlab/manifest.py`, same replacement.

- [ ] **Step 2: Run tests to verify no warnings and all pass**

Run: `cd python && python -m pytest ../tests/ -v -W error::DeprecationWarning 2>&1 | tail -5`
Expected: ALL PASS with zero DeprecationWarnings (the `-W error` flag turns warnings into errors)

Note: If there are third-party deprecation warnings we can't fix, use `-W error::DeprecationWarning -W ignore::DeprecationWarning:starlette` or similar to filter.

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/db.py python/spinlab/practice.py python/spinlab/manifest.py
git commit -m "fix: replace deprecated datetime.utcnow() with datetime.now(UTC)"
```

---

### Task 3: Update config.yaml and clean up stale data

**Files:**
- Modify: `config.yaml`
- Delete: `data/spinlab.db`, `data/spinlab.db-shm`, `data/spinlab.db-wal`
- Delete: `data/orchestrator_state.json`
- Delete: `data/captures/*.yaml` (old manifests with hardcoded `smw_cod` game ID)

**Important:** This task requires user confirmation before deleting data. The old DB has `smw_cod` practice data from before multi-game support — it's incompatible with the new checksum-based game IDs and should be discarded.

- [ ] **Step 1: Update config.yaml**

```yaml
emulator:
  path: "C:/Apps/Mesen/Mesen 2.1.1/Mesen.exe"
  type: mesen2
  lua_script: "lua/spinlab.lua"
  script_data_dir: "C:/Users/thedo/Documents/Mesen2/LuaScriptData/spinlab"

rom:
  dir: "C:/Users/thedo/Dropbox/SNES/SMW Hacks"

game:
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

Key changes: `rom.path` → `rom.dir` (whole directory), `game.id` and `game.name` removed (auto-discovered now).

- [ ] **Step 2: Delete stale data files**

```bash
rm -f data/spinlab.db data/spinlab.db-shm data/spinlab.db-wal
rm -f data/orchestrator_state.json
rm -f data/captures/*.yaml
```

The DB will be auto-recreated on next `spinlab dashboard` launch. Captures are from the old `smw_cod` game ID and won't match checksum-based IDs.

- [ ] **Step 3: Remove `state_file` parameter from create_app**

In `python/spinlab/dashboard.py`, remove the deprecated `state_file` parameter:

```python
# Old:
def create_app(
    db: Database,
    rom_dir: Path | None = None,
    state_file: Path | None = None,  # deprecated, ignored
    ...

# New:
def create_app(
    db: Database,
    rom_dir: Path | None = None,
    ...
```

Also update `tests/test_dashboard_references.py` if it still passes `state_file`:

```python
# Remove state_file=state_file from create_app call
```

- [ ] **Step 4: Run tests**

Run: `cd python && python -m pytest ../tests/ -v --tb=short`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add config.yaml python/spinlab/dashboard.py tests/test_dashboard_references.py
git commit -m "chore: update config for multi-game, remove stale data and deprecated state_file"
```

Note: Don't `git add` the deleted data files — they're in `.gitignore`.

---

### Task 4: Update CLAUDE.md with worktree pip note

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add pip editable install note to Worktrees section**

After the "Resource policy" subsection in CLAUDE.md, add:

```markdown
**Pip editable installs:** `pip install -e` is path-bound. The worktree shares the same virtualenv but the editable install still resolves to whichever checkout was last installed. This is fine for unit tests (they use `sys.path` manipulation), but if imports fail in a worktree, re-run `pip install -e python/` from the worktree root.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add worktree pip editable install note"
```

---

## Chunk 2: JSONL Cleanup & Mesen2 API

### Task 5: Verify Mesen2 ROM info API (USER ACTION REQUIRED)

**Files:** None (manual verification)

The Lua script uses `emu.getRomInfo()` to get the ROM filename. This API call is a best guess — it needs to be verified in Mesen2.

- [ ] **Step 1: Andrew tests in Mesen2**

Open Mesen2's Script Window, load the spinlab script, and run this in the console or add it temporarily:

```lua
local info = emu.getRomInfo()
emu.log(type(info))
if info then
  for k, v in pairs(info) do
    emu.log(k .. " = " .. tostring(v))
  end
end
```

Report back:
- Does `emu.getRomInfo()` exist?
- What fields does it return?
- What's the field name for the ROM filename?

If `emu.getRomInfo()` doesn't exist, try:
- `emu.getRomName()`
- `emu.getState()` and inspect its fields
- Check Mesen2's Lua API docs: Help → Scripting API Reference

- [ ] **Step 2: Fix get_rom_filename() if needed**

Based on Andrew's findings, update the `get_rom_filename()` function in `lua/spinlab.lua` to use the correct API.

- [ ] **Step 3: Commit if changed**

```bash
git add lua/spinlab.lua
git commit -m "fix: use correct Mesen2 API for ROM filename"
```

---

### Task 6: Make JSONL logging optional

**Files:**
- Modify: `lua/spinlab.lua`

The JSONL passive log (`passive_log.jsonl`) is now redundant — all transition events are forwarded over TCP and captured in the DB via live reference mode. Rather than removing it (useful for debugging), make it opt-in.

- [ ] **Step 1: Add JSONL toggle to config section**

In the CONFIG section of `lua/spinlab.lua`, add:

```lua
local JSONL_LOGGING = false  -- set true to enable passive_log.jsonl (debugging)
```

- [ ] **Step 2: Guard log_jsonl calls**

Wrap the two `log_jsonl()` calls in `detect_transitions()` with the toggle:

```lua
if JSONL_LOGGING then
  log_jsonl(event_data)
end
```

There are two calls — one in the level entrance block and one in the level exit block. Wrap both.

- [ ] **Step 3: Commit**

```bash
git add lua/spinlab.lua
git commit -m "feat: make JSONL passive logging opt-in (disabled by default)"
```

---

## Chunk 3: Test Fixtures & Playwright Smoke Tests

### Task 7: Create two-game test fixtures

**Files:**
- Create: `tests/fixtures/game_a.sfc` (binary, 1024 bytes of 0x00)
- Create: `tests/fixtures/game_b.sfc` (binary, 1024 bytes of 0xFF)
- Create: `tests/test_multi_game.py`

These are dummy ROM files — just byte blobs with different content to produce different checksums. They exercise the multi-game switching flow without needing real ROMs.

- [ ] **Step 1: Create fixture files**

```python
# Run this once to create fixtures:
from pathlib import Path
fixtures = Path("tests/fixtures")
fixtures.mkdir(exist_ok=True)
(fixtures / "game_a.sfc").write_bytes(b"\x00" * 1024)
(fixtures / "game_b.sfc").write_bytes(b"\xff" * 1024)
```

Or via bash:
```bash
mkdir -p tests/fixtures
python -c "from pathlib import Path; Path('tests/fixtures/game_a.sfc').write_bytes(b'\\x00'*1024); Path('tests/fixtures/game_b.sfc').write_bytes(b'\\xff'*1024)"
```

- [ ] **Step 2: Write multi-game integration tests**

Create `tests/test_multi_game.py`:

```python
"""Integration tests for multi-game support: switching, isolation, reset scoping."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spinlab.db import Database
from spinlab.models import Split, Attempt
from spinlab.romid import rom_checksum, game_name_from_filename

FIXTURES = Path(__file__).parent / "fixtures"


# ── ROM identity ──────────────────────────────────────────────────────────

def test_two_fixtures_have_different_checksums():
    """Fixture ROMs must produce distinct game IDs."""
    c_a = rom_checksum(FIXTURES / "game_a.sfc")
    c_b = rom_checksum(FIXTURES / "game_b.sfc")
    assert c_a != c_b
    assert len(c_a) == 16
    assert len(c_b) == 16


def test_game_name_strips_extension():
    assert game_name_from_filename("game_a.sfc") == "game_a"
    assert game_name_from_filename("game_b.sfc") == "game_b"


# ── Dashboard game switching ─────────────────────────────────────────────

@pytest.fixture
def app_with_rom_dir(tmp_path):
    """Dashboard app with rom_dir pointing to test fixtures."""
    from spinlab.dashboard import create_app

    db = Database(tmp_path / "test.db")
    app = create_app(db=db, rom_dir=FIXTURES, host="127.0.0.1", port=59999)
    return app, db


def test_switch_game_creates_db_record(app_with_rom_dir):
    """Switching to a new game auto-creates a game row in the DB."""
    app, db = app_with_rom_dir
    checksum = rom_checksum(FIXTURES / "game_a.sfc")
    app.state._switch_game(checksum, "game_a", "any%")
    row = db.conn.execute("SELECT name FROM games WHERE id = ?", (checksum,)).fetchone()
    assert row is not None
    assert row[0] == "game_a"


def test_switch_between_two_games(app_with_rom_dir):
    """Switching games updates state and preserves both games in DB."""
    app, db = app_with_rom_dir
    c_a = rom_checksum(FIXTURES / "game_a.sfc")
    c_b = rom_checksum(FIXTURES / "game_b.sfc")

    app.state._switch_game(c_a, "game_a", "any%")
    assert app.state._game_id[0] == c_a
    assert app.state._game_name[0] == "game_a"

    app.state._switch_game(c_b, "game_b", "any%")
    assert app.state._game_id[0] == c_b
    assert app.state._game_name[0] == "game_b"

    # Both games exist in DB
    games = db.conn.execute("SELECT id FROM games").fetchall()
    game_ids = {g[0] for g in games}
    assert c_a in game_ids
    assert c_b in game_ids


def test_switch_game_invalidates_scheduler(app_with_rom_dir):
    """Switching games nulls the cached scheduler so it's rebuilt for the new game."""
    app, db = app_with_rom_dir
    c_a = rom_checksum(FIXTURES / "game_a.sfc")
    c_b = rom_checksum(FIXTURES / "game_b.sfc")

    app.state._switch_game(c_a, "game_a", "any%")
    client = TestClient(app)
    # Access state to trigger scheduler creation
    client.get("/api/state")
    assert app.state._scheduler[0] is not None

    # Switch to game B — scheduler should be invalidated
    app.state._switch_game(c_b, "game_b", "any%")
    assert app.state._scheduler[0] is None


def test_api_state_shows_game_info(app_with_rom_dir):
    """The /api/state endpoint includes game_id and game_name."""
    app, db = app_with_rom_dir
    checksum = rom_checksum(FIXTURES / "game_a.sfc")
    app.state._switch_game(checksum, "game_a", "any%")
    client = TestClient(app)

    data = client.get("/api/state").json()
    assert data["game_id"] == checksum
    assert data["game_name"] == "game_a"


# ── Data isolation ────────────────────────────────────────────────────────

def test_reset_is_game_scoped(app_with_rom_dir):
    """Reset only clears data for the active game, not all games."""
    app, db = app_with_rom_dir
    c_a = rom_checksum(FIXTURES / "game_a.sfc")
    c_b = rom_checksum(FIXTURES / "game_b.sfc")

    # Set up game A with data
    app.state._switch_game(c_a, "game_a", "any%")
    s_a = Split(id=f"{c_a}:1:0:normal", game_id=c_a, level_number=1, room_id=0, goal="normal")
    db.upsert_split(s_a)
    db.create_session("sa", c_a)
    db.log_attempt(Attempt(split_id=s_a.id, time_ms=5000, completed=True, session_id="sa"))

    # Set up game B with data
    app.state._switch_game(c_b, "game_b", "any%")
    s_b = Split(id=f"{c_b}:1:0:normal", game_id=c_b, level_number=1, room_id=0, goal="normal")
    db.upsert_split(s_b)
    db.create_session("sb", c_b)
    db.log_attempt(Attempt(split_id=s_b.id, time_ms=6000, completed=True, session_id="sb"))

    # Reset game B (active game)
    client = TestClient(app)
    resp = client.post("/api/reset")
    assert resp.json()["status"] == "ok"

    # Game B data gone
    assert db.get_recent_attempts(c_b) == []
    # Game A data intact
    assert len(db.get_recent_attempts(c_a)) == 1


def test_splits_are_game_scoped(app_with_rom_dir):
    """The /api/splits endpoint returns only splits for the active game."""
    app, db = app_with_rom_dir
    c_a = rom_checksum(FIXTURES / "game_a.sfc")
    c_b = rom_checksum(FIXTURES / "game_b.sfc")

    # Splits for game A
    app.state._switch_game(c_a, "game_a", "any%")
    db.upsert_split(Split(id=f"{c_a}:1:0:normal", game_id=c_a, level_number=1, room_id=0, goal="normal"))
    db.upsert_split(Split(id=f"{c_a}:2:0:normal", game_id=c_a, level_number=2, room_id=0, goal="normal"))

    # Splits for game B
    app.state._switch_game(c_b, "game_b", "any%")
    db.upsert_split(Split(id=f"{c_b}:1:0:key", game_id=c_b, level_number=1, room_id=0, goal="key"))

    client = TestClient(app)

    # While game B is active, should see only 1 split
    data = client.get("/api/splits").json()
    assert len(data["splits"]) == 1
    assert data["splits"][0]["id"] == f"{c_b}:1:0:key"

    # Switch to game A, should see 2 splits
    app.state._switch_game(c_a, "game_a", "any%")
    data = client.get("/api/splits").json()
    assert len(data["splits"]) == 2
```

- [ ] **Step 3: Run tests**

Run: `cd python && python -m pytest ../tests/test_multi_game.py -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/game_a.sfc tests/fixtures/game_b.sfc tests/test_multi_game.py
git commit -m "test: multi-game integration tests with two-game fixtures"
```

---

### Task 8: Playwright smoke tests (USER ACTION REQUIRED)

**Files:**
- Create: `tests/playwright/test_dashboard_smoke.py`

**Prerequisites:**
1. Dashboard must be running: `spinlab dashboard` (from main checkout)
2. Playwright MCP server must be available (already configured in `.playwright-mcp/`)
3. No emulator connection needed — tests exercise the dashboard UI only

**Important:** These tests interact with the live dashboard on `http://localhost:15483`. They do NOT use TestClient — they use a real browser via Playwright MCP. Run from main checkout only (not a worktree).

- [ ] **Step 1: Verify dashboard is running**

Ask the user to start: `spinlab dashboard`

- [ ] **Step 2: Smoke test — dashboard loads and shows tabs**

Using Playwright MCP:

1. Navigate to `http://localhost:15483`
2. Verify: page title contains "SpinLab"
3. Verify: three tabs visible (Live, Model, Manage)
4. Verify: header shows "SpinLab"
5. Verify: mode shows "idle"
6. Take screenshot for visual confirmation

- [ ] **Step 3: Smoke test — no-game state is handled gracefully**

With no emulator connected:

1. Navigate to `http://localhost:15483`
2. Verify: game name area is empty (no game loaded)
3. Verify: connection status shows disconnected
4. Verify: no errors in console
5. Click each tab — verify no crashes

- [ ] **Step 4: Smoke test — tab switching works**

1. Click "Model" tab
2. Verify: model content area is visible
3. Click "Manage" tab
4. Verify: manage content area is visible (references section)
5. Click "Live" tab
6. Verify: back to live view

- [ ] **Step 5: Document findings**

Record any visual issues, layout bugs, or console errors found during smoke testing. These become follow-up tasks.

- [ ] **Step 6: Commit test file if created**

```bash
git add tests/playwright/
git commit -m "test: Playwright smoke tests for dashboard"
```

Note: Playwright tests are manual-run only (require live dashboard). They are NOT part of `pytest` CI — they live in a separate directory.

---

## Summary of User Actions Required

| Task | What Andrew needs to do |
|------|------------------------|
| Task 3 | Confirm it's OK to delete old DB and manifests (or back them up first) |
| Task 5 | Test `emu.getRomInfo()` in Mesen2 and report the correct API |
| Task 8 | Run `spinlab dashboard` and help with Playwright smoke tests |

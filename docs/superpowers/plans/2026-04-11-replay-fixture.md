# Replay Fixture Integration Test — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Check in a two-level TAS recording (`.spinrec` + `.mss`) as a test fixture and write a full-stack integration test that replays it through headless Mesen to verify the complete capture pipeline.

**Architecture:** The fixture files (recorded from Love Yourself) are copied into the test's temp data directory. A dedicated Mesen process loads the Love Yourself ROM, a dashboard server connects to it, and the test triggers replay via the HTTP API. Assertions verify segments, save states, and attempts were captured correctly.

**Tech Stack:** pytest + pytest-asyncio, headless Mesen2, FastAPI TestClient (via requests), SQLite

**Spec:** `docs/superpowers/specs/2026-04-11-replay-fixture-design.md`

---

### Task 1: Commit bug fixes from design session

The brainstorming session produced three bug fixes that are already applied to the working tree but not yet committed together. Commit them before starting new work.

**Files:**
- Already modified: `python/spinlab/vite.py` (IPv6 port check)
- Already modified: `python/spinlab/capture_controller.py` (absolute rec path)
- Already modified: `python/spinlab/dashboard.py` (global exception handler)

- [ ] **Step 1: Verify the three changes are present**

Run:
```bash
git diff python/spinlab/vite.py python/spinlab/capture_controller.py python/spinlab/dashboard.py
```

Expect to see:
- `vite.py`: `wait_for_port` loops over `("127.0.0.1", "::1")` instead of just `"127.0.0.1"`
- `capture_controller.py`: `.resolve()` appended to rec_path construction
- `dashboard.py`: `Request` and `JSONResponse` imported, `unhandled_exception_handler` added

- [ ] **Step 2: Run full test suite to verify no regressions**

Run:
```bash
pytest -m "not emulator" -x -q
```

Expected: all pass

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/vite.py python/spinlab/capture_controller.py python/spinlab/dashboard.py
git commit -m "fix: IPv6 vite port check, absolute rec path, global 500 logging"
```

---

### Task 2: Add fixture files and update .gitattributes

Copy the recorded fixture into the repo and mark binary formats in `.gitattributes`.

**Files:**
- Create: `tests/fixtures/love_yourself/two_level.spinrec`
- Create: `tests/fixtures/love_yourself/two_level.mss`
- Modify: `.gitattributes`

- [ ] **Step 1: Copy fixture files**

```bash
mkdir -p tests/fixtures/love_yourself
cp data/bd94dbb29012c7f5/rec/live_af4ecb2f.spinrec tests/fixtures/love_yourself/two_level.spinrec
cp data/bd94dbb29012c7f5/rec/live_af4ecb2f.mss tests/fixtures/love_yourself/two_level.mss
```

- [ ] **Step 2: Verify the fixture is valid**

```bash
python -c "
from spinlab.spinrec import read_spinrec
from pathlib import Path
data = Path('tests/fixtures/love_yourself/two_level.spinrec').read_bytes()
header, frames = read_spinrec(data)
print(f'Game ID: {header.game_id}')
print(f'Frames: {header.frame_count}')
assert header.frame_count > 0
assert header.game_id == 'bd94dbb29012c7f5'
print('OK')
"
```

Expected: prints game ID, frame count, and "OK"

- [ ] **Step 3: Add binary formats to .gitattributes**

Add these lines to `.gitattributes` in the binary assets section:

```
*.mss   binary
*.spinrec binary
```

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/love_yourself/ .gitattributes
git commit -m "test: add two-level Love Yourself replay fixture"
```

---

### Task 3: Add Love Yourself ROM discovery to integration conftest

The existing `_test_rom_path()` returns the first ROM alphabetically — not necessarily Love Yourself. Add a helper that specifically finds the Love Yourself ROM, used by the replay fixture test.

**Files:**
- Modify: `tests/integration/conftest.py`

- [ ] **Step 1: Add `_love_yourself_rom_path()` function**

Add after the existing `_test_rom_path()` function (after line 80):

```python
LOVE_YOURSELF_ROM_NAME = "Love Yourself.smc"
LOVE_YOURSELF_GAME_ID = "bd94dbb29012c7f5"

def _love_yourself_rom_path() -> str | None:
    """Find the Love Yourself ROM for replay fixture tests."""
    env_rom = os.environ.get("SPINLAB_REPLAY_ROM")
    if env_rom:
        return env_rom
    config = _load_config()
    rom_dir = config.get("rom", {}).get("dir")
    if rom_dir:
        rom_path = Path(rom_dir) / LOVE_YOURSELF_ROM_NAME
        if rom_path.exists():
            return str(rom_path)
    return None


_love_yourself_rom = _love_yourself_rom_path()

skip_no_love_yourself = pytest.mark.skipif(
    not _love_yourself_rom or not Path(_love_yourself_rom).exists(),
    reason=f"Love Yourself ROM not found (SPINLAB_REPLAY_ROM or '{LOVE_YOURSELF_ROM_NAME}' in rom.dir)",
)
```

- [ ] **Step 2: Verify it resolves on your machine**

```bash
python -c "
import sys; sys.path.insert(0, 'tests')
from integration.conftest import _love_yourself_rom_path
print(_love_yourself_rom_path())
"
```

Expected: prints the path to `Love Yourself.smc`

- [ ] **Step 3: Commit**

```bash
git add tests/integration/conftest.py
git commit -m "test: add Love Yourself ROM discovery for replay fixture"
```

---

### Task 4: Add replay-specific Mesen + dashboard fixtures

The replay test needs its own Mesen process running Love Yourself (the existing smoke fixtures use whatever ROM `_test_rom_path()` returns). Add session-scoped fixtures to `conftest.py`.

**Files:**
- Modify: `tests/integration/conftest.py`

- [ ] **Step 1: Add `replay_mesen_process` fixture**

Add at the end of `conftest.py`:

```python
@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def replay_mesen_process():
    """Launch Mesen2 with spinlab.lua and Love Yourself ROM for replay tests."""
    if not _mesen or not _love_yourself_rom:
        pytest.skip("Mesen2 or Love Yourself ROM not configured")

    tcp_port = _free_port()
    spinlab_lua = LUA_DIR / "spinlab.lua"

    tmp_lua_dir = Path(tempfile.mkdtemp(prefix="spinlab_replay_lua_"))
    patched_lua = tmp_lua_dir / "spinlab.lua"
    original = spinlab_lua.read_text(encoding="utf-8")
    patched_lua.write_text(
        original.replace("local TCP_PORT   = 15482", f"local TCP_PORT   = {tcp_port}"),
        encoding="utf-8",
    )

    import shutil as _shutil
    addresses_src = LUA_DIR / "addresses.lua"
    if addresses_src.exists():
        _shutil.copy2(str(addresses_src), str(tmp_lua_dir / "addresses.lua"))

    cmd = [_mesen, "--testrunner", _love_yourself_rom, str(patched_lua)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    yield proc, tcp_port

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    _shutil.rmtree(str(tmp_lua_dir), ignore_errors=True)
```

- [ ] **Step 2: Add `replay_dashboard` fixture**

Add after `replay_mesen_process`:

```python
@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def replay_dashboard(replay_mesen_process):
    """Start a dashboard connected to the Love Yourself Mesen process.

    Yields (base_url, db, tmp_path) — tmp_path is the data dir for placing fixtures.
    """
    from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig, PracticeConfig
    from spinlab.dashboard import create_app
    from spinlab.db import Database

    _, tcp_port = replay_mesen_process

    tmp = tempfile.mkdtemp(prefix="spinlab_replay_")
    tmp_path = Path(tmp)

    db = Database(str(tmp_path / "spinlab.db"))
    dashboard_port = _free_port()

    rom_dir = Path(_love_yourself_rom).parent if _love_yourself_rom else None

    config = AppConfig(
        network=NetworkConfig(host="127.0.0.1", port=tcp_port, dashboard_port=dashboard_port),
        emulator=EmulatorConfig(),
        data_dir=tmp_path,
        rom_dir=rom_dir,
        practice=PracticeConfig(),
    )

    app = create_app(db=db, config=config)

    uvi_config = uvicorn.Config(app, host="127.0.0.1", port=dashboard_port, log_level="warning")
    server = uvicorn.Server(uvi_config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{dashboard_port}"
    for _ in range(40):
        try:
            resp = http_requests.get(f"{base_url}/api/state", timeout=1)
            if resp.status_code == 200:
                break
        except http_requests.ConnectionError:
            pass
        await asyncio.sleep(0.25)
    else:
        pytest.fail("Replay dashboard server did not start within 10 seconds")

    for _ in range(40):
        resp = http_requests.get(f"{base_url}/api/state", timeout=2)
        state = resp.json()
        if state.get("tcp_connected") and state.get("game_id"):
            break
        await asyncio.sleep(0.25)
    else:
        pytest.fail("Replay dashboard did not connect to Mesen within 10 seconds")

    yield base_url, db, tmp_path

    server.should_exit = True
    thread.join(timeout=5)
    db.close()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
```

- [ ] **Step 3: Commit**

```bash
git add tests/integration/conftest.py
git commit -m "test: add replay-specific Mesen + dashboard fixtures"
```

---

### Task 5: Write the replay fixture integration test

The main test: replay the two-level fixture through headless Mesen and assert the capture pipeline produced the expected segments, save states, and attempts.

**Files:**
- Create: `tests/integration/test_replay_fixture.py`

- [ ] **Step 1: Write the test**

Create `tests/integration/test_replay_fixture.py`:

```python
"""Full-stack replay fixture test: replay a recorded two-level run through
headless Mesen and verify the capture pipeline produces correct segments,
save states, and attempts.

Requires: Mesen2 + Love Yourself ROM (see conftest.py replay fixtures).
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
import requests

from tests.integration.conftest import LOVE_YOURSELF_GAME_ID, skip_no_love_yourself

pytestmark = [pytest.mark.emulator, skip_no_love_yourself]

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "love_yourself"
REPLAY_TIMEOUT_S = 120
POLL_INTERVAL_S = 0.5


def _api(base_url: str, method: str, path: str, **kwargs):
    """Helper for HTTP requests to the dashboard."""
    return getattr(requests, method)(base_url + path, timeout=5, **kwargs)


def _wait_for_idle(base_url: str, timeout: float = REPLAY_TIMEOUT_S) -> dict:
    """Poll /api/state until mode returns to idle or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = _api(base_url, "get", "/api/state")
        state = resp.json()
        if state["mode"] == "idle":
            return state
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(f"Replay did not finish within {timeout}s")


class TestReplayFixture:
    """Replay a two-level Love Yourself recording and verify capture output."""

    @pytest.fixture(autouse=True)
    def _setup(self, replay_dashboard):
        base_url, db, tmp_path = replay_dashboard
        self.base_url = base_url
        self.db = db
        self.tmp_path = tmp_path

        # Copy fixture files into the data dir where the replay API expects them
        game_rec_dir = tmp_path / LOVE_YOURSELF_GAME_ID / "rec"
        game_rec_dir.mkdir(parents=True, exist_ok=True)
        self.ref_id = "fixture_two_level"
        shutil.copy2(
            FIXTURE_DIR / "two_level.spinrec",
            game_rec_dir / f"{self.ref_id}.spinrec",
        )
        shutil.copy2(
            FIXTURE_DIR / "two_level.mss",
            game_rec_dir / f"{self.ref_id}.mss",
        )

    def test_replay_produces_segments(self):
        """Replay the fixture and verify segments were detected."""
        # Start replay (speed=0 = uncapped)
        resp = _api(self.base_url, "post", "/api/replay/start",
                     json={"ref_id": self.ref_id, "speed": 0})
        assert resp.status_code == 200, f"replay start failed: {resp.text}"

        # Wait for replay to finish (mode returns to idle)
        _wait_for_idle(self.base_url)

        # Verify segments were captured
        resp = _api(self.base_url, "get", "/api/segments")
        assert resp.status_code == 200
        segments = resp.json()["segments"]
        assert len(segments) >= 4, (
            f"Expected at least 4 segments (2 levels × 2 segments), got {len(segments)}: "
            f"{[s.get('id', s.get('description', '?')) for s in segments]}"
        )

    def test_replay_records_attempts(self):
        """Replay the fixture and verify attempts were recorded in the DB."""
        resp = _api(self.base_url, "post", "/api/replay/start",
                     json={"ref_id": self.ref_id, "speed": 0})
        assert resp.status_code == 200

        _wait_for_idle(self.base_url)

        # Check that attempts exist
        resp = _api(self.base_url, "get", "/api/state")
        state = resp.json()
        recent = state.get("recent", [])
        assert len(recent) > 0, "Expected at least one attempt recorded"
```

- [ ] **Step 2: Run the test**

```bash
pytest tests/integration/test_replay_fixture.py -v --timeout=180
```

Expected: both tests pass (or skip if Love Yourself ROM not available).

This is the moment of truth for headless replay speed — note how long it takes!

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_replay_fixture.py
git commit -m "test: add full-stack replay fixture integration test"
```

---

### Task 6: Update CLAUDE.md testing section

Add a note about the replay fixture test so future sessions know it exists and what it needs.

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add replay fixture entry to Testing section**

In the Testing section, after the "Smoke tests" entry, add:

```markdown
- **Replay fixture tests:** Included in `pytest -m emulator`. Replays a two-level Love Yourself recording through headless Mesen. Requires `Love Yourself.smc` in `rom.dir` (or set `SPINLAB_REPLAY_ROM`). See `docs/superpowers/specs/2026-04-11-replay-fixture-design.md` for recording instructions.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add replay fixture test entry to CLAUDE.md"
```

---

### Task 7: Run full test suite and verify

Final verification that everything works together and nothing regressed.

- [ ] **Step 1: Run the full non-emulator test suite**

```bash
pytest -m "not emulator" -x -q
```

Expected: all pass

- [ ] **Step 2: Run emulator tests (if Mesen available)**

```bash
pytest -m emulator -v --timeout=180
```

Expected: all pass (smoke tests + replay fixture tests)

- [ ] **Step 3: Note the replay speed**

Check the test output duration for `test_replay_produces_segments`. The fixture is 6255 frames (~104s at 60fps). With uncapped headless replay, it should be significantly faster than real-time.

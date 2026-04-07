# Full-Stack Smoke Tests + Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add full-stack integration tests that launch Mesen headless + a real dashboard, plus file-based logging, a `spinlab db reset` CLI command, and expanded TCP non-JSON handling.

**Architecture:** Extends existing poke test infrastructure with a `dashboard_server` session-scoped fixture that starts a real FastAPI + Uvicorn + DB in a background thread alongside the headless Mesen process. Unit tests cover stale-estimator fallback. Logging uses Python's `RotatingFileHandler` in `cli.py`. New CLI subcommand for DB reset.

**Tech Stack:** Python 3.11+, pytest/pytest-asyncio, FastAPI, Uvicorn, requests, SQLite, Python `logging` module.

---

### Task 1: TcpManager — Expand Known Non-JSON Handling

**Files:**
- Modify: `python/spinlab/tcp_manager.py:112-116`
- Test: `tests/test_tcp_manager.py` (new)

- [ ] **Step 1: Write failing tests for ok:/err: prefix handling**

```python
# tests/test_tcp_manager.py
"""Tests for TcpManager non-JSON line handling."""
import asyncio
import json
import logging
import pytest


@pytest.fixture
def tcp_pair():
    """Create a connected TcpManager talking to a local server."""
    from spinlab.tcp_manager import TcpManager

    server = None
    writer_ref = None

    async def _setup():
        nonlocal server, writer_ref

        async def handle_client(reader, writer):
            nonlocal writer_ref
            writer_ref = writer
            # Keep connection open until cancelled
            try:
                while True:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                writer.close()

        server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = TcpManager("127.0.0.1", port)
        connected = await client.connect(timeout=2.0)
        assert connected
        # Wait for server to accept
        for _ in range(20):
            if writer_ref is not None:
                break
            await asyncio.sleep(0.05)
        return client, writer_ref, server

    loop = asyncio.new_event_loop()
    client, writer, srv = loop.run_until_complete(_setup())
    yield client, writer, loop

    async def _teardown():
        await client.disconnect()
        srv.close()
        await srv.wait_closed()

    loop.run_until_complete(_teardown())
    loop.close()


def test_ok_prefix_no_warning(tcp_pair, caplog):
    """Lines starting with 'ok:' should be logged at DEBUG, not WARNING."""
    client, writer, loop = tcp_pair

    async def _test():
        writer.write(b"ok:queued\n")
        writer.write(b"ok:practice_loaded\n")
        await writer.drain()
        await asyncio.sleep(0.2)

    with caplog.at_level(logging.DEBUG, logger="spinlab.tcp_manager"):
        loop.run_until_complete(_test())

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, f"Got unexpected warnings: {[r.message for r in warnings]}"


def test_err_prefix_no_warning(tcp_pair, caplog):
    """Lines starting with 'err:' should be logged at DEBUG, not WARNING."""
    client, writer, loop = tcp_pair

    async def _test():
        writer.write(b"err:unknown_command\n")
        await writer.drain()
        await asyncio.sleep(0.2)

    with caplog.at_level(logging.DEBUG, logger="spinlab.tcp_manager"):
        loop.run_until_complete(_test())

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, f"Got unexpected warnings: {[r.message for r in warnings]}"


def test_unknown_non_json_warns(tcp_pair, caplog):
    """Truly unexpected non-JSON should still warn."""
    client, writer, loop = tcp_pair

    async def _test():
        writer.write(b"something_weird\n")
        await writer.drain()
        await asyncio.sleep(0.2)

    with caplog.at_level(logging.DEBUG, logger="spinlab.tcp_manager"):
        loop.run_until_complete(_test())

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "something_weird" in warnings[0].message


def test_json_events_still_queued(tcp_pair):
    """Valid JSON lines should still be parsed and queued."""
    client, writer, loop = tcp_pair

    async def _test():
        msg = json.dumps({"event": "rom_info", "game": "test"})
        writer.write((msg + "\n").encode())
        await writer.drain()
        event = await client.recv_event(timeout=2.0)
        assert event is not None
        assert event["event"] == "rom_info"

    loop.run_until_complete(_test())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_tcp_manager.py -v`
Expected: `test_ok_prefix_no_warning` FAILS (ok:practice_loaded triggers warning), `test_err_prefix_no_warning` FAILS (err: triggers warning). The other two should pass already.

- [ ] **Step 3: Implement ok:/err: prefix handling in _read_loop**

In `python/spinlab/tcp_manager.py`, replace the non-JSON handling block (lines 112-116):

```python
                except json.JSONDecodeError:
                    if text.startswith("ok:") or text.startswith("err:"):
                        logger.debug("TCP response: %s", text)
                    elif text in _KNOWN_NON_JSON:
                        logger.debug("TCP non-JSON (expected): %s", text)
                    else:
                        logger.warning("Unexpected non-JSON from Lua: %r", text)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_tcp_manager.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Run full fast suite to check for regressions**

Run: `pytest -m "not (emulator or slow or frontend)" -q`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add tests/test_tcp_manager.py python/spinlab/tcp_manager.py
git commit -m "feat(tcp): handle ok:/err: prefixes as expected non-JSON responses"
```

---

### Task 2: Stale Estimator Fallback — Unit Test

**Files:**
- Test: `tests/test_scheduler_fallback.py` (new)

The fix is already implemented in `python/spinlab/scheduler.py:46-50`. This task adds the regression test.

- [ ] **Step 1: Write test for stale estimator fallback**

```python
# tests/test_scheduler_fallback.py
"""Regression test: stale estimator name in DB should not crash Scheduler."""
import pytest
from spinlab.db import Database
from spinlab.scheduler import Scheduler


def test_stale_estimator_falls_back_to_default(tmp_path):
    """Scheduler with a bogus saved estimator name falls back to 'kalman'."""
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Test Game", "any%")
    db.save_allocator_config("estimator", "bogus_name_that_does_not_exist")

    scheduler = Scheduler(db, "g1")

    assert scheduler.estimator.name == "kalman"


def test_valid_saved_estimator_is_used(tmp_path):
    """Scheduler with a valid saved estimator name should use it."""
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Test Game", "any%")
    db.save_allocator_config("estimator", "rolling_mean")

    scheduler = Scheduler(db, "g1")

    assert scheduler.estimator.name == "rolling_mean"


def test_no_saved_estimator_uses_default(tmp_path):
    """Scheduler with no saved estimator uses the constructor default."""
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Test Game", "any%")

    scheduler = Scheduler(db, "g1")

    assert scheduler.estimator.name == "kalman"
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `pytest tests/test_scheduler_fallback.py -v`
Expected: All 3 PASS (the fix is already in `scheduler.py`).

- [ ] **Step 3: Commit**

```bash
git add tests/test_scheduler_fallback.py
git commit -m "test: add regression tests for stale estimator fallback"
```

---

### Task 3: File-Based Logging

**Files:**
- Modify: `python/spinlab/cli.py:1-7` (imports) and `cli.py:69-83` (dashboard command)
- Test: `tests/test_cli_logging.py` (new)

- [ ] **Step 1: Write failing test for log file creation**

```python
# tests/test_cli_logging.py
"""Tests for file-based logging setup."""
import logging
from pathlib import Path

from spinlab.cli import _setup_file_logging


def test_setup_file_logging_creates_log_file(tmp_path):
    """_setup_file_logging creates a rotating log file in data_dir."""
    _setup_file_logging(tmp_path)

    log_path = tmp_path / "spinlab.log"
    # Write a log message through the root logger
    logger = logging.getLogger("spinlab.test_logging")
    logger.info("test message from logging test")

    # Force flush by finding our handler
    for handler in logging.root.handlers:
        handler.flush()

    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "test message from logging test" in content


def test_setup_file_logging_handler_is_rotating(tmp_path):
    """The file handler should be a RotatingFileHandler."""
    from logging.handlers import RotatingFileHandler

    _setup_file_logging(tmp_path)

    rotating_handlers = [
        h for h in logging.root.handlers
        if isinstance(h, RotatingFileHandler)
        and str(tmp_path) in str(h.baseFilename)
    ]
    assert len(rotating_handlers) == 1
    assert rotating_handlers[0].maxBytes == 1_000_000
    assert rotating_handlers[0].backupCount == 3


def teardown_function():
    """Clean up any handlers we added to root logger."""
    from logging.handlers import RotatingFileHandler
    for h in logging.root.handlers[:]:
        if isinstance(h, RotatingFileHandler):
            logging.root.removeHandler(h)
            h.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli_logging.py -v`
Expected: FAIL — `_setup_file_logging` does not exist yet.

- [ ] **Step 3: Implement _setup_file_logging in cli.py**

Add the import at the top of `python/spinlab/cli.py`:

```python
import logging
from logging.handlers import RotatingFileHandler
```

Add the function after `_write_lua_dir_breadcrumb`:

```python
def _setup_file_logging(data_dir: Path) -> None:
    """Configure rotating file log in data_dir/spinlab.log."""
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "spinlab.log"
    handler = RotatingFileHandler(
        str(log_path), maxBytes=1_000_000, backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s — %(message)s"
    ))
    handler.setLevel(logging.INFO)
    logging.root.addHandler(handler)
    logging.root.setLevel(min(logging.root.level or logging.INFO, logging.INFO))
```

Call it in the dashboard command, right after `config = AppConfig.from_yaml(...)`:

```python
    elif parsed.command == "dashboard":
        import uvicorn
        from spinlab.config import AppConfig
        from spinlab.dashboard import create_app
        from spinlab.db import Database

        config = AppConfig.from_yaml(Path(parsed.config))
        _setup_file_logging(config.data_dir)
        dashboard_port = parsed.port or config.network.dashboard_port
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli_logging.py -v`
Expected: All 2 tests PASS.

- [ ] **Step 5: Run fast suite**

Run: `pytest -m "not (emulator or slow or frontend)" -q`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/cli.py tests/test_cli_logging.py
git commit -m "feat: add rotating file-based logging to dashboard startup"
```

---

### Task 4: `spinlab db reset` CLI Command

**Files:**
- Modify: `python/spinlab/cli.py` (add subcommand)
- Test: `tests/test_cli_db_reset.py` (new)

- [ ] **Step 1: Write failing test for db reset**

```python
# tests/test_cli_db_reset.py
"""Tests for the 'spinlab db reset' CLI command."""
from pathlib import Path

import yaml
import pytest

from spinlab.cli import main


def _write_config(tmp_path: Path) -> Path:
    """Write a minimal config.yaml pointing data.dir at tmp_path/data."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = {
        "data": {"dir": str(data_dir)},
        "network": {"port": 15482, "dashboard_port": 15483},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(config), encoding="utf-8")
    return config_path


def test_db_reset_creates_fresh_db(tmp_path):
    """'spinlab db reset' deletes existing DB and creates a fresh one."""
    config_path = _write_config(tmp_path)
    data_dir = tmp_path / "data"

    # Create a DB with some data
    from spinlab.db import Database
    db = Database(str(data_dir / "spinlab.db"))
    db.upsert_game("g1", "Test", "any%")
    db.close()
    assert (data_dir / "spinlab.db").exists()

    # Run reset
    main(["db", "reset", "--config", str(config_path)])

    # DB should exist but be empty (fresh schema, no games)
    db2 = Database(str(data_dir / "spinlab.db"))
    rows = db2.conn.execute("SELECT * FROM games").fetchall()
    assert len(rows) == 0
    db2.close()


def test_db_reset_no_existing_db(tmp_path):
    """'spinlab db reset' with no existing DB still creates a fresh one."""
    config_path = _write_config(tmp_path)
    data_dir = tmp_path / "data"
    assert not (data_dir / "spinlab.db").exists()

    main(["db", "reset", "--config", str(config_path)])

    assert (data_dir / "spinlab.db").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli_db_reset.py -v`
Expected: FAIL — `db reset` subcommand does not exist.

- [ ] **Step 3: Implement db reset subcommand**

In `python/spinlab/cli.py`, add the `db` subparser after the `lua-cmd` parser definition:

```python
    # db
    p_db = sub.add_parser("db", help="Database management commands")
    db_sub = p_db.add_subparsers(dest="db_command", required=True)
    p_db_reset = db_sub.add_parser("reset", help="Delete and recreate the database")
    p_db_reset.add_argument("--config", default="config.yaml", help="Path to config.yaml")
```

Add the handler after the `lua-cmd` handler:

```python
    elif parsed.command == "db":
        if parsed.db_command == "reset":
            from spinlab.config import AppConfig
            from spinlab.db import Database

            config = AppConfig.from_yaml(Path(parsed.config))
            db_path = config.data_dir / "spinlab.db"
            if db_path.exists():
                db_path.unlink()
            # Also remove WAL and SHM files if present
            for suffix in (".db-wal", ".db-shm"):
                wal = config.data_dir / f"spinlab{suffix}"
                if wal.exists():
                    wal.unlink()
            Database(str(db_path))
            print(f"Database reset: {db_path}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli_db_reset.py -v`
Expected: All 2 tests PASS.

- [ ] **Step 5: Run fast suite**

Run: `pytest -m "not (emulator or slow or frontend)" -q`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/cli.py tests/test_cli_db_reset.py
git commit -m "feat: add 'spinlab db reset' CLI command for clean DB slate"
```

---

### Task 5: Full-Stack Smoke Test Infrastructure

**Files:**
- Modify: `tests/integration/conftest.py` (add `dashboard_server`, `dashboard_url`, `api` fixtures)

This task builds the test fixtures. The actual test scenarios are in Task 6.

- [ ] **Step 1: Write the dashboard_server fixture**

Add these imports to the top of `tests/integration/conftest.py`:

```python
import threading
import requests as http_requests
import socket
import uvicorn
```

Add these fixtures after the existing `tcp_client` fixture:

```python
def _free_port() -> int:
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def dashboard_server(mesen_process):
    """Start a real FastAPI dashboard with DB, connecting to the same Mesen TCP server.

    Yields (base_url, db) tuple. The dashboard's event loop handles rom_info
    and sends game_context — no manual TCP handshake needed for smoke tests.
    """
    from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig, PracticeConfig
    from spinlab.dashboard import create_app
    from spinlab.db import Database
    import tempfile

    tmp = tempfile.mkdtemp(prefix="spinlab_smoke_")
    tmp_path = Path(tmp)

    db = Database(str(tmp_path / "spinlab.db"))
    port = _free_port()
    tcp_port = _tcp_port()

    config = AppConfig(
        network=NetworkConfig(host="127.0.0.1", port=tcp_port, dashboard_port=port),
        emulator=EmulatorConfig(),
        data_dir=tmp_path,
        rom_dir=None,
        practice=PracticeConfig(),
    )

    app = create_app(db=db, config=config)

    uvi_config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(uvi_config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for uvicorn to be ready
    base_url = f"http://127.0.0.1:{port}"
    for _ in range(40):
        try:
            resp = http_requests.get(f"{base_url}/api/state", timeout=1)
            if resp.status_code == 200:
                break
        except http_requests.ConnectionError:
            pass
        await asyncio.sleep(0.25)
    else:
        pytest.fail("Dashboard server did not start within 10 seconds")

    # Wait for TCP to connect and game to load (event loop handles rom_info)
    for _ in range(40):
        resp = http_requests.get(f"{base_url}/api/state", timeout=2)
        state = resp.json()
        if state.get("tcp_connected") and state.get("game_id"):
            break
        await asyncio.sleep(0.25)

    yield base_url, db

    # Teardown
    server.should_exit = True
    thread.join(timeout=5)
    db.close()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def dashboard_url(dashboard_server) -> str:
    """Convenience alias — just the base URL string."""
    base_url, _ = dashboard_server
    return base_url


@pytest.fixture
def api(dashboard_url):
    """Function-scoped requests.Session pre-configured with base URL."""
    class _ApiSession:
        def __init__(self, base_url: str):
            self._base = base_url
            self._session = http_requests.Session()

        def get(self, path: str, **kwargs):
            return self._session.get(self._base + path, **kwargs)

        def post(self, path: str, **kwargs):
            return self._session.post(self._base + path, **kwargs)

    return _ApiSession(dashboard_url)
```

- [ ] **Step 2: Verify fixtures load without syntax errors**

Run: `pytest tests/integration/conftest.py --collect-only -q 2>&1 | head -5`
Expected: No import errors. (Tests won't be collected from conftest — that's fine.)

- [ ] **Step 3: Commit**

```bash
git add tests/integration/conftest.py
git commit -m "feat(tests): add dashboard_server fixture for full-stack smoke tests"
```

---

### Task 6: Full-Stack Smoke Test Scenarios

**Files:**
- Create: `tests/integration/test_smoke.py`

**Note:** These tests require Mesen2 and a test ROM, so they use the existing `@pytest.mark.emulator` marker and skip automatically when Mesen is unavailable.

- [ ] **Step 1: Write all smoke test scenarios**

```python
# tests/integration/test_smoke.py
"""Full-stack smoke tests: Mesen headless + dashboard + DB.

These tests verify the assembled system works end-to-end.
They require Mesen2 and a test ROM (same as poke tests).
"""
import pytest

pytestmark = pytest.mark.emulator


class TestNoGameEndpoints:
    """Before Mesen connects, all GET endpoints should return 200 with empty data."""

    @pytest.fixture(autouse=True)
    def _setup(self, dashboard_url):
        """Just ensures dashboard_server fixture is active."""

    def test_state_returns_200(self, api):
        resp = api.get("/api/state")
        assert resp.status_code == 200

    def test_segments_returns_200(self, api):
        resp = api.get("/api/segments")
        assert resp.status_code == 200
        assert resp.json()["segments"] == [] or isinstance(resp.json()["segments"], list)

    def test_references_returns_200(self, api):
        resp = api.get("/api/references")
        assert resp.status_code == 200

    def test_sessions_returns_200(self, api):
        resp = api.get("/api/sessions")
        assert resp.status_code == 200

    def test_estimator_params_returns_200(self, api):
        resp = api.get("/api/estimator-params")
        assert resp.status_code == 200

    def test_model_returns_200(self, api):
        resp = api.get("/api/model")
        assert resp.status_code == 200


class TestGameLoadsAfterConnect:
    """After Mesen starts, the dashboard should show a connected game."""

    def test_tcp_connected(self, api):
        resp = api.get("/api/state")
        state = resp.json()
        assert state["tcp_connected"] is True

    def test_game_id_populated(self, api):
        resp = api.get("/api/state")
        state = resp.json()
        assert state["game_id"] is not None

    def test_game_name_populated(self, api):
        resp = api.get("/api/state")
        state = resp.json()
        assert state.get("game_name") is not None
        assert len(state["game_name"]) > 0

    def test_segments_returns_200(self, api):
        resp = api.get("/api/segments")
        assert resp.status_code == 200

    def test_references_returns_200(self, api):
        resp = api.get("/api/references")
        assert resp.status_code == 200

    def test_model_returns_estimator_info(self, api):
        resp = api.get("/api/model")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("estimator") is not None
        assert isinstance(data.get("estimators"), list)
        assert len(data["estimators"]) > 0


class TestReferenceStartAfterConnect:
    """After game loads, reference start should be accepted (not 409 'No game loaded')."""

    def test_reference_start_returns_200(self, api):
        resp = api.post("/api/reference/start")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") is not None
        # Stop it to clean up
        api.post("/api/reference/stop")
```

- [ ] **Step 2: Run smoke tests (requires Mesen)**

Run: `pytest tests/integration/test_smoke.py -v`
Expected: All tests PASS (or skip if Mesen not available).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_smoke.py
git commit -m "test: add full-stack smoke tests for dashboard + Mesen integration"
```

---

### Task 7: Documentation Updates

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/ARCHITECTURE.md`

- [ ] **Step 1: Update CLAUDE.md**

In the **Testing** section, after the "Emulator tests" line, add:

```markdown
- **Smoke tests:** Included in `pytest -m emulator`. Full-stack: Mesen headless + dashboard + DB. See `tests/integration/test_smoke.py`.
```

In the **Testing** section, after the "Everything" line, add a note about `spinlab db reset`:

```markdown
- **DB reset:** `spinlab db reset [--config config.yaml]` — deletes and recreates the database. Useful after schema changes during development.
```

After the **Frontend** section, add:

```markdown
## Logging

Dashboard logs to `{data_dir}/spinlab.log` (rotating, 1 MB max, 3 backups). Configured automatically on `spinlab dashboard` startup. All `logger.info()` / `logger.warning()` / `logger.exception()` calls go to this file.
```

- [ ] **Step 2: Update docs/ARCHITECTURE.md**

Add a section at the end of the file:

```markdown
## Test Layers

1. **Unit tests** (`tests/`): Fast, mocked dependencies. ~23s. Run after any code change.
2. **Poke tests** (`tests/integration/test_*.py`): Headless Mesen + Lua + poke scenarios over real TCP. Test level transitions, segment detection, save state capture.
3. **Smoke tests** (`tests/integration/test_smoke.py`): Headless Mesen + real dashboard (FastAPI + Uvicorn + DB) in a background thread. Test that the assembled system works: endpoints return 200, game loads after TCP connect, reference start is accepted.

All integration tests use `@pytest.mark.emulator` and skip when Mesen is not available.
```

- [ ] **Step 3: Verify documentation looks correct**

Run: `head -5 CLAUDE.md && echo "---" && tail -10 docs/ARCHITECTURE.md`
Expected: New sections visible.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/ARCHITECTURE.md
git commit -m "docs: add smoke test, db reset, and logging documentation"
```

---

## Summary of Changes

| Task | What | Files |
|------|------|-------|
| 1 | TCP ok:/err: prefix handling | `tcp_manager.py`, `test_tcp_manager.py` |
| 2 | Stale estimator fallback regression test | `test_scheduler_fallback.py` |
| 3 | Rotating file-based logging | `cli.py`, `test_cli_logging.py` |
| 4 | `spinlab db reset` CLI command | `cli.py`, `test_cli_db_reset.py` |
| 5 | Dashboard server fixture | `tests/integration/conftest.py` |
| 6 | Smoke test scenarios | `tests/integration/test_smoke.py` |
| 7 | Documentation updates | `CLAUDE.md`, `docs/ARCHITECTURE.md` |

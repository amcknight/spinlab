"""Pytest fixtures for Mesen2 headless integration tests.

Two independent Mesen processes run in the same session:
  - Poke tests use mesen_process (poke_engine.lua) + tcp_client
  - Smoke tests use smoke_mesen_process (spinlab.lua) + dashboard_server

Fixtures:
    mesen_process      — session-scoped: Mesen2 with poke_engine.lua for poke tests
    tcp_client         — session-scoped: TCP connection for poke tests
    run_scenario       — function-scoped: sends poke scenario, collects events
    smoke_mesen_process — session-scoped: Mesen2 with spinlab.lua on a free port
    dashboard_server   — session-scoped: real FastAPI dashboard connected to smoke Mesen
    dashboard_url      — session-scoped: convenience alias for the dashboard base URL
    api                — function-scoped: requests session pre-configured with dashboard URL

Diagnostics:
    On emulator/integration test failure, a diagnostic block is appended to the
    pytest longrepr showing dashboard state, DB row counts, and Mesen process
    status. Controlled by the ``pytest_runtest_makereport`` hook below.
"""
from __future__ import annotations

import asyncio

import json
import logging
import os
import socket
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio
import requests as http_requests
import uvicorn
import yaml

from spinlab.tcp_manager import TcpManager
from tests.integration.poke_parser import parse_poke_file

# Resolve project paths
PROJECT_ROOT = Path(__file__).resolve().parents[2]
LUA_DIR = PROJECT_ROOT / "lua"
SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"

# Test game context
TEST_GAME_ID = "integration_test_"
TEST_GAME_NAME = "Integration Test ROM"


def _load_config() -> dict:
    """Load config.yaml from project root."""
    config_path = PROJECT_ROOT / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f)
    return {}


def _mesen_path() -> str | None:
    """Resolve Mesen2 executable path from env var or config."""
    env_path = os.environ.get("MESEN_PATH")
    if env_path:
        return env_path
    config = _load_config()
    return config.get("emulator", {}).get("path")


def _test_rom_path() -> str | None:
    """Resolve a ROM path for testing."""
    env_rom = os.environ.get("SPINLAB_TEST_ROM")
    if env_rom:
        return env_rom
    config = _load_config()
    rom_dir = config.get("rom", {}).get("dir")
    if rom_dir:
        # Use first .sfc/.smc/.emc file found
        rom_path = Path(rom_dir)
        for ext in ("*.sfc", "*.smc", "*.emc"):
            roms = list(rom_path.glob(ext))
            if roms:
                return str(roms[0])
    return None


def _tcp_port() -> int:
    """Resolve TCP port from config or default."""
    config = _load_config()
    return config.get("network", {}).get("port", 15482)


# Skip all integration tests if Mesen2 not available
_mesen = _mesen_path()
_rom = _test_rom_path()

pytestmark = pytest.mark.emulator

skip_no_mesen = pytest.mark.skipif(
    not _mesen or not Path(_mesen).exists(),
    reason=f"Mesen2 not found (MESEN_PATH or config.yaml emulator.path): {_mesen}",
)
skip_no_rom = pytest.mark.skipif(
    not _rom or not Path(_rom).exists(),
    reason=f"Test ROM not found (SPINLAB_TEST_ROM or config.yaml rom.dir): {_rom}",
)

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


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def mesen_process():
    """Launch Mesen2 in --testrunner mode with poke_engine.lua (once per session)."""
    if not _mesen or not _rom:
        pytest.skip("Mesen2 or test ROM not configured")

    poke_engine = str(LUA_DIR / "poke_engine.lua")
    cmd = [_mesen, "--testrunner", _rom, poke_engine]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    yield proc

    _hard_kill(proc)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def tcp_client(mesen_process) -> AsyncGenerator[TcpManager, None]:
    """Connect TcpManager to the Lua TCP server with retry (once per session)."""
    port = _tcp_port()
    client = TcpManager("127.0.0.1", port)

    # Retry connection — Mesen2 may need time to start TCP server
    connected = False
    for attempt in range(20):
        connected = await client.connect(timeout=2.0)
        if connected:
            break
        await asyncio.sleep(0.5)

    if not connected:
        pytest.fail("Could not connect to Lua TCP server after 20 attempts")

    # Wait for rom_info event
    rom_event = await client.recv_event(timeout=5.0)
    assert rom_event is not None, "Did not receive rom_info from Lua"
    assert rom_event.get("event") == "rom_info"

    # Send game_context
    await client.send(json.dumps({
        "event": "game_context",
        "game_id": TEST_GAME_ID,
        "game_name": TEST_GAME_NAME,
    }))

    yield client

    # Send quit to cleanly stop the emulator
    try:
        await client.send(json.dumps({"event": "quit"}))
    except (ConnectionError, OSError):
        pass
    await client.disconnect()


@pytest.fixture
def run_scenario(tcp_client):
    """Send a poke scenario and collect events until scenario_done sentinel."""

    async def _run(scenario_name: str, timeout: float = 30.0) -> list[dict]:
        """Send a poke scenario and collect events until scenario_done.

        Args:
            scenario_name: filename in tests/integration/scenarios/
            timeout: max seconds to wait for scenario completion

        Returns:
            Ordered list of event dicts (scenario_done sentinel excluded).
        """
        scenario_path = SCENARIO_DIR / scenario_name
        if not scenario_path.exists():
            pytest.fail(f"Scenario file not found: {scenario_path}")

        scenario = parse_poke_file(str(scenario_path))
        await tcp_client.send(json.dumps(scenario))

        # Collect events until scenario_done sentinel
        events: list[dict] = []
        try:
            while True:
                event = await tcp_client.recv_event(timeout=timeout)
                if event is None:
                    pytest.fail(f"Timeout waiting for scenario_done ({scenario_name})")
                if event.get("event") == "scenario_done":
                    break
                events.append(event)
        except (ConnectionError, OSError):
            pytest.fail(f"Connection lost during scenario ({scenario_name})")

        return events

    return _run


def _free_port() -> int:
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _hard_kill(proc: subprocess.Popen) -> None:
    """Best-effort kill that survives Mesen processes that ignore terminate().

    On Windows, ``Popen.terminate()`` and ``Popen.kill()`` both call
    ``TerminateProcess()`` — there is no real escalation between them.  Use
    ``taskkill /F /T`` so the whole tree dies and we don't leak children.
    Every wait gets a timeout so a wedged Mesen can't hang the pytest
    finalizer.
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def pytest_sessionstart(session: pytest.Session) -> None:
    """Sweep orphan Mesen processes before any fixture spawns a new one.

    Pytest fixture finalizers don't run if pytest is interrupted (Ctrl+C,
    crash, OOM), so on Windows every interrupted run leaks a Mesen.exe child.
    The next session inherits the orphan on the fixed port (15482) and every
    test fails with "Did not receive rom_info from Lua".  Killing leftovers up
    front breaks that cycle.

    Set ``SPINLAB_NO_MESEN_SWEEP=1`` to skip — useful if you're running pytest
    while a real Mesen window is open for unrelated work.
    """
    if os.environ.get("SPINLAB_NO_MESEN_SWEEP"):
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/IM", "Mesen.exe", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def smoke_mesen_process():
    """Launch a separate Mesen2 with spinlab.lua on a free TCP port for smoke tests.

    Unlike mesen_process (which runs poke_engine.lua for poke tests), this runs
    the real spinlab.lua script so the dashboard's event loop can connect naturally.
    Returns (process, tcp_port) tuple.
    """
    if not _mesen or not _rom:
        pytest.skip("Mesen2 or test ROM not configured")

    tcp_port = _free_port()
    spinlab_lua = LUA_DIR / "spinlab.lua"

    # spinlab.lua hardcodes TCP_PORT as a local. Create a temp copy with the port
    # patched so the smoke test Mesen doesn't collide with the poke test Mesen.
    tmp_lua_dir = Path(tempfile.mkdtemp(prefix="spinlab_lua_"))
    patched_lua = tmp_lua_dir / "spinlab.lua"
    original = spinlab_lua.read_text(encoding="utf-8")
    patched_lua.write_text(
        original.replace("local TCP_PORT   = 15482", f"local TCP_PORT   = {tcp_port}"),
        encoding="utf-8",
    )

    # Copy shared Lua modules to the temp dir so dofile resolves them
    import shutil as _shutil
    for lua_module in ("addresses.lua", "json.lua", "overlay.lua", "spinrec.lua"):
        src = LUA_DIR / lua_module
        if src.exists():
            _shutil.copy2(str(src), str(tmp_lua_dir / lua_module))

    cmd = [_mesen, "--testrunner", _rom, str(patched_lua)]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    yield proc, tcp_port

    _hard_kill(proc)
    _shutil.rmtree(str(tmp_lua_dir), ignore_errors=True)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def dashboard_server(smoke_mesen_process):
    """Start a real FastAPI dashboard with DB, connecting to its own Mesen process.

    Yields (base_url, db) tuple. The dashboard's event loop handles rom_info
    and sends game_context — no manual TCP handshake needed for smoke tests.
    """
    from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig, PracticeConfig
    from spinlab.dashboard import create_app
    from spinlab.db import Database

    _, tcp_port = smoke_mesen_process

    tmp = tempfile.mkdtemp(prefix="spinlab_smoke_")
    tmp_path = Path(tmp)

    db = Database(str(tmp_path / "spinlab.db"))
    dashboard_port = _free_port()

    # rom_dir is needed so _handle_rom_info can compute game_id from ROM checksum
    rom_dir = Path(_rom).parent if _rom else None

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

    # Wait for uvicorn to be ready
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
        pytest.fail("Dashboard server did not start within 10 seconds")

    # Wait for TCP to connect and game to load (event loop handles rom_info)
    for _ in range(40):
        resp = http_requests.get(f"{base_url}/api/state", timeout=2)
        state = resp.json()
        if state.get("tcp_connected") and state.get("game_id"):
            break
        await asyncio.sleep(0.25)
    else:
        pytest.fail("Dashboard did not connect to Mesen within 10 seconds")

    yield base_url, db

    # Teardown
    server.should_exit = True
    thread.join(timeout=5)
    db.close()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def dashboard_url(dashboard_server) -> str:
    """Convenience alias — just the base URL string."""
    base_url, _db = dashboard_server
    return base_url


# -- Seeded-game fixture for frontend contract smoke tests -------------------
FAKE_GAME_NAME = "FakeGame"


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def fake_dashboard_server():
    """Start a FastAPI dashboard with a FakeTcpManager — no Mesen required.

    Mirrors the real ``dashboard_server`` fixture but swaps ``session.tcp`` for
    the in-process FakeTcpManager (see tests/conftest.py) so tests can exercise
    the dashboard's HTTP API and SessionManager without booting an emulator.

    The dashboard's background event_loop still runs and keeps trying to open
    a TCP connection on the configured port — nothing listens there, so each
    attempt fails fast and the loop sleeps. The session's ``tcp`` reference is
    the fake, which ``SystemState`` reads for ``tcp_connected``.

    Yields (base_url, db, session).
    """
    from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig, PracticeConfig
    from spinlab.dashboard import create_app
    from spinlab.db import Database
    from tests.conftest import FakeTcpManager

    tmp = tempfile.mkdtemp(prefix="spinlab_fake_")
    tmp_path = Path(tmp)

    db = Database(str(tmp_path / "spinlab.db"))
    dashboard_port = _free_port()
    # Port is unused — pick a free one so the real event_loop's connect()
    # attempts fail with connection-refused rather than colliding with a
    # running service.
    fake_tcp_port = _free_port()

    config = AppConfig(
        network=NetworkConfig(host="127.0.0.1", port=fake_tcp_port, dashboard_port=dashboard_port),
        emulator=EmulatorConfig(),
        data_dir=tmp_path,
        rom_dir=None,
        practice=PracticeConfig(),
    )

    app = create_app(db=db, config=config)
    # Swap TCP for the fake *before* starting uvicorn so the lifespan-started
    # event_loop's real-TCP retries don't matter: state reads session.tcp.
    fake_tcp = FakeTcpManager(connected=True)
    app.state.session.tcp = fake_tcp
    app.state.session.capture.tcp = fake_tcp
    app.state.session.cold_fill.tcp = fake_tcp

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
        pytest.fail("Fake dashboard server did not start within 10 seconds")

    yield base_url, db, app.state.session

    server.should_exit = True
    thread.join(timeout=5)
    db.close()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def fake_game_loaded(fake_dashboard_server):
    """Seed a minimal game + segments + reference + attempts + session, then
    drive the real ``switch_game`` path so SystemState reports a loaded game.

    Uses ``fake_dashboard_server`` (no Mesen) so frontend contract tests have
    stable data on every tab without the emulator marker.

    Session-scoped to match ``fake_dashboard_server``'s session loop — seeding
    is one-time and ``switch_game`` must run on the session loop so any
    asyncio primitives it creates stay bound to it.

    Yields the seeded game_id.
    """
    from tests.factories import seed_basic_game
    _base_url, db, session = fake_dashboard_server
    game_id = seed_basic_game(db)
    # switch_game is the real code path used by _handle_rom_info /
    # _handle_game_context; tcp_connected is already True via FakeTcpManager.
    await session.switch_game(game_id, FAKE_GAME_NAME)
    yield game_id


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
    for lua_module in ("addresses.lua", "json.lua", "overlay.lua", "spinrec.lua"):
        src = LUA_DIR / lua_module
        if src.exists():
            _shutil.copy2(str(src), str(tmp_lua_dir / lua_module))

    cmd = [_mesen, "--testrunner", _love_yourself_rom, str(patched_lua)]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    yield proc, tcp_port

    _hard_kill(proc)
    _shutil.rmtree(str(tmp_lua_dir), ignore_errors=True)


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
    import shutil as _shutil_cleanup
    _shutil_cleanup.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Diagnostic dump on integration test failure
# ---------------------------------------------------------------------------

# Collect event log lines from the spinlab logger during the entire session.
# The handler is installed once at import time; the ring buffer is read by
# the failure hook to include recent events in the pytest report.

_EVENT_LOG_CAPACITY = 200


class _RingHandler(logging.Handler):
    """Fixed-capacity ring buffer logging handler."""

    def __init__(self, capacity: int = _EVENT_LOG_CAPACITY):
        super().__init__()
        self._buf: list[str] = []
        self._capacity = capacity

    def emit(self, record: logging.LogRecord) -> None:
        line = self.format(record)
        self._buf.append(line)
        if len(self._buf) > self._capacity:
            self._buf = self._buf[-self._capacity:]

    def recent(self, n: int = 30) -> list[str]:
        return self._buf[-n:]


_ring = _RingHandler()
_ring.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
logging.getLogger("spinlab").addHandler(_ring)


def _collect_diagnostics(item: pytest.Item) -> str:
    """Best-effort snapshot of integration test state at failure time."""
    parts: list[str] = []

    # --- Dashboard API state ---
    for fixture_name in ("dashboard_server", "replay_dashboard"):
        fixture_val = item.funcargs.get(fixture_name)
        if fixture_val is None:
            continue
        if fixture_name == "dashboard_server":
            base_url, db = fixture_val
        else:
            base_url, db, _ = fixture_val
        try:
            state = http_requests.get(f"{base_url}/api/state", timeout=2).json()
            parts.append(f"  /api/state: {json.dumps(state, indent=2)}")
        except Exception as exc:
            parts.append(f"  /api/state: <unavailable: {exc}>")

        # DB row counts
        try:
            seg_count = db.conn.execute(
                "SELECT COUNT(*) FROM segments WHERE active = 1"
            ).fetchone()[0]
            ref_count = db.conn.execute(
                "SELECT COUNT(*) FROM capture_runs"
            ).fetchone()[0]
            draft_count = db.conn.execute(
                "SELECT COUNT(*) FROM capture_runs WHERE draft = 1"
            ).fetchone()[0]
            parts.append(f"  DB: {seg_count} active segments, {ref_count} capture_runs ({draft_count} drafts)")
        except Exception as exc:
            parts.append(f"  DB: <unavailable: {exc}>")
        break

    # --- Mesen process status ---
    for proc_name in ("smoke_mesen_process", "replay_mesen_process"):
        proc_val = item.funcargs.get(proc_name)
        if proc_val is None:
            continue
        proc, tcp_port = proc_val
        status = "running" if proc.poll() is None else f"exited ({proc.returncode})"
        parts.append(f"  Mesen ({proc_name}): {status}, TCP port {tcp_port}")

    # --- Recent event log ---
    recent = _ring.recent(30)
    if recent:
        parts.append(f"  Recent log ({len(recent)} lines):")
        for line in recent:
            parts.append(f"    {line}")

    if not parts:
        return ""
    return "\n--- SpinLab Integration Diagnostics ---\n" + "\n".join(parts)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Append diagnostic state to the report when an integration test fails."""
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.failed:
        return
    # Only for tests in the integration directory
    if "integration" not in str(item.fspath):
        return
    diag = _collect_diagnostics(item)
    if diag:
        # Append to the longrepr so it shows in terminal output
        if hasattr(report, "longreprtext"):
            report.longreprtext += diag
        report.sections.append(("SpinLab Diagnostics", diag))

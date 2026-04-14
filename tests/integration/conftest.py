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
"""
from __future__ import annotations

import asyncio

import json
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

    # Teardown: kill if still running
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


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

    # Copy addresses.lua to the temp dir so dofile resolves it
    import shutil as _shutil
    addresses_src = LUA_DIR / "addresses.lua"
    if addresses_src.exists():
        _shutil.copy2(str(addresses_src), str(tmp_lua_dir / "addresses.lua"))

    cmd = [_mesen, "--testrunner", _rom, str(patched_lua)]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    yield proc, tcp_port

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

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

    yield base_url, db, app.state.session

    # Teardown
    server.should_exit = True
    thread.join(timeout=5)
    db.close()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def dashboard_url(dashboard_server) -> str:
    """Convenience alias — just the base URL string."""
    base_url, _db, _session = dashboard_server
    return base_url


# -- Seeded-game fixture for frontend contract smoke tests -------------------
#
# FakeGame identity for the fake_game_loaded fixture. Uses a fixed game_id so
# seeded rows remain idempotent across repeated session-scoped invocations.
FAKE_GAME_ID = "fake_game_frontend_smoke"
FAKE_GAME_NAME = "FakeGame"


@pytest.fixture
def fake_game_loaded(dashboard_server):
    """Seed a minimal game + segments + reference + attempts + session, then
    force the SessionManager's SystemState to reflect a loaded game.

    Bypasses the TCP/Lua boundary (which is exercised by test_smoke.py) so
    frontend contract tests have stable data on every tab without booting
    a recording emulator.

    Yields the seeded game_id.
    """
    from tests.factories import seed_basic_game
    _base_url, db, session = dashboard_server
    game_id = seed_basic_game(db)
    # Force SystemState to look "game loaded". tcp_connected is derived from
    # session.tcp.is_connected; the live dashboard_server has a real Mesen
    # connection so this is already True. We only need to point the session
    # at the seeded game (the real code path is switch_game, called by
    # _handle_rom_info / _handle_game_context when Lua reports a ROM).
    import asyncio as _asyncio
    _asyncio.run(session.switch_game(game_id, FAKE_GAME_NAME))
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
    addresses_src = LUA_DIR / "addresses.lua"
    if addresses_src.exists():
        _shutil.copy2(str(addresses_src), str(tmp_lua_dir / "addresses.lua"))

    cmd = [_mesen, "--testrunner", _love_yourself_rom, str(patched_lua)]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    yield proc, tcp_port

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

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
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

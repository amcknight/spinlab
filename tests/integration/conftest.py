"""Pytest fixtures for Mesen2 headless integration tests.

Fixtures:
    mesen_process    — session-scoped: one Mesen2 launch per pytest session
    tcp_client       — session-scoped: persistent TCP connection across all tests
    run_scenario     — function-scoped: sends scenario, collects events until scenario_done
    dashboard_server — session-scoped: real FastAPI dashboard with DB for smoke tests
    dashboard_url    — session-scoped: convenience alias for the dashboard base URL
    api              — function-scoped: requests session pre-configured with dashboard URL
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
async def dashboard_server(mesen_process):
    """Start a real FastAPI dashboard with DB, connecting to the same Mesen TCP server.

    Yields (base_url, db) tuple. The dashboard's event loop handles rom_info
    and sends game_context — no manual TCP handshake needed for smoke tests.
    """
    from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig, PracticeConfig
    from spinlab.dashboard import create_app
    from spinlab.db import Database

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

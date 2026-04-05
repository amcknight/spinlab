"""Pytest fixtures for Mesen2 headless integration tests.

Fixtures:
    mesen_process  — session-scoped: one Mesen2 launch per pytest session
    tcp_client     — session-scoped: persistent TCP connection across all tests
    run_scenario   — function-scoped: sends scenario, collects events until scenario_done
"""
from __future__ import annotations

import asyncio

import json
import os
import subprocess
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio
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

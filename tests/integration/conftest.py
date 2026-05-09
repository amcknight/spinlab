"""Pytest fixtures for RetroArch headless integration tests.

Fixtures:
    run_scenario       — function-scoped: sends poke scenario via RA harness (RA path)
    fake_dashboard_server — session-scoped: FastAPI dashboard with FakeTcpManager, no emulator
    fake_game_loaded   — session-scoped: seeds a game into fake_dashboard_server
    replay_ra_dashboard — session-scoped: dashboard wired to a Love Yourself RA session

Diagnostics:
    On emulator/integration test failure, a diagnostic block is appended to the
    pytest longrepr showing dashboard state, DB row counts, and recent log lines.
    Controlled by the ``pytest_runtest_makereport`` hook below.
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

import pytest
import pytest_asyncio
import requests as http_requests
import uvicorn
import yaml
from tests.integration.poke_parser import parse_poke_file

# Resolve project paths
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"

pytestmark = pytest.mark.emulator

LOVE_YOURSELF_ROM_NAME = "Love Yourself.smc"
LOVE_YOURSELF_GAME_ID = "bd94dbb29012c7f5"


def _load_config() -> dict:
    """Load config.yaml from project root."""
    config_path = PROJECT_ROOT / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f)
    return {}


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


def _free_port() -> int:
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _hard_kill(proc: subprocess.Popen) -> None:
    """Best-effort kill for subprocess processes.

    On Windows, ``Popen.terminate()`` and ``Popen.kill()`` both call
    ``TerminateProcess()`` — there is no real escalation between them.  Use
    ``taskkill /F /T`` so the whole tree dies and we don't leak children.
    Every wait gets a timeout so a wedged process can't hang the pytest
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
    from tests.conftest import FakeTcpManager

    from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig, PracticeConfig
    from spinlab.dashboard import create_app
    from spinlab.db import Database

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


# ---------------------------------------------------------------------------
# RetroArch poke harness
# ---------------------------------------------------------------------------


def _ra_paths() -> tuple[Path | None, Path | None, Path | None]:
    """Resolve (retroarch_exe, ra_core_path, rom_path) from env/config."""
    config = _load_config()
    emu = config.get("emulator", {})
    retroarch_exe = emu.get("retroarch_path")
    ra_core_path = emu.get("ra_core_path")
    rom_path = _test_rom_path()
    return (
        Path(retroarch_exe) if retroarch_exe else None,
        Path(ra_core_path) if ra_core_path else None,
        Path(rom_path) if rom_path else None,
    )


def _ra_paths_love_yourself() -> tuple[Path | None, Path | None, Path | None]:
    """Resolve (retroarch_exe, ra_core_path, love_yourself_rom_path) from env/config."""
    config = _load_config()
    emu = config.get("emulator", {})
    retroarch_exe = emu.get("retroarch_path")
    ra_core_path = emu.get("ra_core_path")
    rom_path = _love_yourself_rom_path()
    return (
        Path(retroarch_exe) if retroarch_exe else None,
        Path(ra_core_path) if ra_core_path else None,
        Path(rom_path) if rom_path else None,
    )


@pytest.fixture(scope="session")
def ra_harness():
    """Launch one RetroArch process per pytest session for poke-driven tests."""
    from tests.integration.ra_harness import RAHarness, RAHarnessLaunchError

    retroarch_exe, ra_core_path, rom_path = _ra_paths()
    missing = [
        label for label, p in
        [("retroarch_path", retroarch_exe), ("ra_core_path", ra_core_path), ("rom", rom_path)]
        if p is None or not p.exists()
    ]
    if missing:
        pytest.skip(
            f"ra_harness requires: {', '.join(missing)} "
            "(retroarch_path/ra_core_path in config.yaml emulator section; "
            "rom from SPINLAB_TEST_ROM env or rom.dir in config.yaml)"
        )

    try:
        harness = RAHarness.launch(rom_path=rom_path, core_path=ra_core_path, retroarch_exe=retroarch_exe)
    except RAHarnessLaunchError as exc:
        pytest.skip(f"ra_harness launch failed: {exc}")

    try:
        yield harness
    finally:
        harness.teardown()


@pytest.fixture(scope="session")
def ra_harness_love_yourself():
    """Launch one RetroArch process per pytest session using the Love Yourself ROM.

    Same shape as ``ra_harness`` but uses ``_love_yourself_rom_path()`` instead
    of the default test ROM.  Skips if Love Yourself is not available, keeping
    the Phase E movie smoke test aligned with ``test_replay_fixture.py`` which
    also gates on this ROM.
    """
    from tests.integration.ra_harness import RAHarness, RAHarnessLaunchError

    retroarch_exe, ra_core_path, rom_path = _ra_paths_love_yourself()
    missing = [
        label for label, p in
        [("retroarch_path", retroarch_exe), ("ra_core_path", ra_core_path), ("rom", rom_path)]
        if p is None or not p.exists()
    ]
    if missing:
        pytest.skip(
            f"ra_harness_love_yourself requires: {', '.join(missing)} "
            "(retroarch_path/ra_core_path in config.yaml emulator section; "
            f"Love Yourself ROM from SPINLAB_REPLAY_ROM env or '{LOVE_YOURSELF_ROM_NAME}' in rom.dir)"
        )

    try:
        harness = RAHarness.launch(rom_path=rom_path, core_path=ra_core_path, retroarch_exe=retroarch_exe)
    except RAHarnessLaunchError as exc:
        pytest.skip(f"ra_harness_love_yourself launch failed: {exc}")

    try:
        yield harness
    finally:
        harness.teardown()


@pytest.fixture
def run_scenario(ra_harness):
    """Send a poke scenario through the RA harness and collect events."""

    async def _run(scenario_name: str, timeout: float = 30.0) -> list[dict]:
        scenario_path = SCENARIO_DIR / scenario_name
        if not scenario_path.exists():
            pytest.fail(f"Scenario file not found: {scenario_path}")
        scenario = parse_poke_file(str(scenario_path))
        return await asyncio.wait_for(
            asyncio.to_thread(ra_harness.engine.run_scenario, scenario),
            timeout=timeout,
        )

    return _run


@pytest.fixture(scope="session")
def replay_ra_dashboard(ra_harness_love_yourself):
    """Start a dashboard pointed at the Love Yourself RA session for replay tests.

    Mirrors ``replay_dashboard`` but uses the RA backend (build_orchestrator)
    instead of Mesen+TCP.  The RA process is already up (ra_harness_love_yourself);
    this fixture wires the dashboard to it via NCI at the configured port.

    Phase E PLAY_REPLAY requires RA to be in PLAYING (not PAUSED) state.
    The harness leaves RA paused; we unpause it here so the orchestrator's
    _on_replay → MoviePlayer.play → play_replay() works correctly.

    Yields (base_url, db, tmp_path) — tmp_path is the data dir where the
    test should stage its fixture files.
    """
    from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig, PracticeConfig
    from spinlab.dashboard import create_app
    from spinlab.db import Database

    config_raw = _load_config()
    emu_raw = config_raw.get("emulator", {})

    # Resolve savestate_dir from config — required by build_orchestrator.
    savestate_dir_str = emu_raw.get("savestate_dir")
    ra_core_subdir = emu_raw.get("ra_core_subdir") or "Snes9x"

    if not savestate_dir_str:
        pytest.skip("replay_ra_dashboard: emulator.savestate_dir not configured")

    savestate_dir = Path(savestate_dir_str)

    tmp = tempfile.mkdtemp(prefix="spinlab_ra_replay_")
    tmp_path = Path(tmp)
    spinlab_state_dir = tmp_path / "spinlab_states"
    spinlab_state_dir.mkdir(parents=True, exist_ok=True)

    db = Database(str(tmp_path / "spinlab.db"))
    dashboard_port = _free_port()

    rom_dir = Path(_love_yourself_rom).parent if _love_yourself_rom else None

    config = AppConfig(
        network=NetworkConfig(
            host="127.0.0.1",
            port=15482,  # unused — RA backend uses NCI, not TCP
            dashboard_port=dashboard_port,
            nci_port=config_raw.get("network", {}).get("nci_port", 55355),
        ),
        emulator=EmulatorConfig(
            backend="retroarch",
            savestate_dir=savestate_dir,
            spinlab_state_dir=spinlab_state_dir,
            ra_core_subdir=ra_core_subdir,
        ),
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

    import time as _time

    # Wait for uvicorn to come up.
    for _ in range(40):
        try:
            resp = http_requests.get(f"{base_url}/api/state", timeout=1)
            if resp.status_code == 200:
                break
        except http_requests.ConnectionError:
            pass
        _time.sleep(0.25)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        db.close()
        import shutil as _s
        _s.rmtree(tmp, ignore_errors=True)
        pytest.fail("replay_ra_dashboard: uvicorn did not start within 10 seconds")

    # Wait for the orchestrator to connect to RA and receive rom_info so the
    # dashboard has a game_id (required before /api/replay/start will resolve).
    for _ in range(40):
        resp = http_requests.get(f"{base_url}/api/state", timeout=2)
        state = resp.json()
        if state.get("tcp_connected") and state.get("game_id"):
            break
        _time.sleep(0.25)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        db.close()
        import shutil as _s
        _s.rmtree(tmp, ignore_errors=True)
        pytest.fail("replay_ra_dashboard: orchestrator did not connect to RA within 10 seconds")

    # PLAY_REPLAY requires RA to be in PLAYING state. The harness left RA paused;
    # unpause it now so the orchestrator's _on_replay → MoviePlayer.play_replay()
    # actually starts playback. Use the harness's NCI client directly.
    harness = ra_harness_love_yourself
    try:
        status = harness.client.get_status()
        if status.state == "PAUSED":
            harness.client.pause_toggle()
            _time.sleep(0.3)  # allow RA to settle into PLAYING before the test POSTs replay/start
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "replay_ra_dashboard: could not unpause RA before yield: %s", exc
        )

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
    for fixture_name in ("dashboard_server", "replay_ra_dashboard"):
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
        # `longreprtext` is a read-only property in current pytest, so the
        # diagnostic block has to ride along on `sections` instead.  pytest
        # renders sections in the terminal report after the traceback.
        report.sections.append(("SpinLab Diagnostics", diag))

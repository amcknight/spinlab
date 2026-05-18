"""Pytest fixtures for RetroArch headless integration tests.

Fixtures:
    run_scenario       — function-scoped: sends poke scenario via RA harness (RA path)
    fake_dashboard_server — session-scoped: FastAPI dashboard with FakeEmuBackend, no emulator
    fake_game_loaded   — session-scoped: seeds a game into fake_dashboard_server
    replay_ra_dashboard — session-scoped: dashboard wired to a Love Yourself RA session

Diagnostics:
    On emulator/integration test failure, a diagnostic block is appended to the
    pytest longrepr showing dashboard state, DB row counts, and recent log lines.
    Controlled by the ``pytest_runtest_makereport`` hook below.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
import pytest_asyncio
import requests as http_requests
import uvicorn
from tests.integration._diagnostics import (
    collect_diagnostics,
    collect_launch_failure_diagnostics,
    format_dashboard_startup_failure,
    format_pause_toggle_failure,
    install_log_handler,
    ring,
)
from tests.integration._rom_paths import (
    CLEAN_SMW_ROM_NAME,  # noqa: F401 — re-exported for downstream consumers
    INTEGRATION_STATES_DIR,  # noqa: F401 — re-exported for downstream consumers
    LOVE_YOURSELF_GAME_ID,  # noqa: F401 — re-exported; used by test_replay_fixture
    LOVE_YOURSELF_ROM_NAME,  # noqa: F401 — re-exported for downstream consumers
    PROJECT_ROOT,  # noqa: F401 — re-exported for downstream consumers
    ROM_REGISTRY,
    TOOTHPASTE_ROM_NAME,  # noqa: F401 — re-exported for downstream consumers
    load_config,
    resolve_ra_paths,
    resolve_rom_path,
    state_path_for,
)
from tests.integration.poke_parser import parse_poke_file
from tests.integration.ra_harness import RAHarness, RAHarnessLaunchError

install_log_handler()

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"

pytestmark = pytest.mark.emulator


def _free_port() -> int:
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _free_udp_port() -> int:
    """Find a free UDP port.

    Small TOCTOU window between the bind here releasing and RetroArch binding
    to the same port — acceptable because the harness's NCI ping retries cover
    transient failures, and the loopback UDP port space is otherwise quiet on
    a test host.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# -- Seeded-game fixture for frontend contract smoke tests -------------------
FAKE_GAME_NAME = "FakeGame"


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def fake_dashboard_server():
    """Start a FastAPI dashboard with a FakeEmuBackend — no live emulator required.

    Mirrors the real ``dashboard_server`` fixture but swaps ``session.emu`` for
    the in-process FakeEmuBackend (see tests/conftest.py) so tests can exercise
    the dashboard's HTTP API and SessionManager without booting an emulator.

    The dashboard's background event_loop still runs and keeps trying to open
    a backend connection on the configured port — nothing listens there, so each
    attempt fails fast and the loop sleeps. The session's ``emu`` reference is
    the fake, which ``SystemState`` reads for ``emu_connected``.

    Yields (base_url, db, session).
    """
    from tests.conftest import FakeEmuBackend

    from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig
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
        emulator=EmulatorConfig(
            savestate_dir=tmp_path / "ra",
            spinlab_state_dir=tmp_path / "sl",
        ),
        data_dir=tmp_path,
        rom_dir=None,
    )

    app = create_app(db=db, config=config)
    # Swap the backend for the fake *before* starting uvicorn so the lifespan-started
    # event_loop's real-backend retries don't matter: state reads session.emu.
    fake_emu = FakeEmuBackend(connected=True)
    app.state.session.emu = fake_emu
    app.state.session.capture.emu = fake_emu
    app.state.session.cold_fill.emu = fake_emu

    uvi_config = uvicorn.Config(app, host="127.0.0.1", port=dashboard_port, log_level="warning")
    server = uvicorn.Server(uvi_config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{dashboard_port}"
    last_error = _wait_for_dashboard_state(base_url, check=_status_200)
    if last_error is not None:
        pytest.fail(format_dashboard_startup_failure(
            port=dashboard_port,
            attempts=40,
            interval_s=0.25,
            last_error=last_error,
        ))

    yield base_url, db, app.state.session

    server.should_exit = True
    thread.join(timeout=5)
    db.close()
    shutil.rmtree(tmp, ignore_errors=True)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def fake_game_loaded(fake_dashboard_server):
    """Seed a minimal game + segments + reference + attempts + session, then
    drive the real ``switch_game`` path so SystemState reports a loaded game.

    Uses ``fake_dashboard_server`` (no live emulator) so frontend contract tests have
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
    # _handle_game_context; emu_connected is already True via FakeEmuBackend.
    await session.switch_game(game_id, FAKE_GAME_NAME)
    yield game_id


# ---------------------------------------------------------------------------
# RetroArch poke harness
# ---------------------------------------------------------------------------


class _HarnessFactory:
    """Session-scoped cache mapping rom_key -> RAHarness.

    Separated from the pytest fixture so unit tests can drive the cache and
    teardown logic without a real fixture lifecycle.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, bool], RAHarness] = {}

    def __call__(self, rom_key: str, use_fresh_state: bool = True) -> RAHarness:
        """Return (or create + cache) a harness for ``rom_key``.

        ``use_fresh_state=True`` (the default) wires a per-launch isolated
        savestate_directory with the fresh-boot state from
        tests/integration/states/ pre-staged at FRESH_BOOT_STATE_SLOT, and
        causes RAPokeEngine to LOAD_STATE_SLOT it before each scenario.
        Required by the poke-transition tests to keep SPC700/CPU state from
        leaking between scenarios.

        ``use_fresh_state=False`` is for fixtures whose RA process must talk
        to the user's actual savestate_directory — currently just the replay
        fixture (its .replay file lives in the user's savestate dir).
        """
        cache_key = (rom_key, use_fresh_state)
        if cache_key in self._cache:
            return self._cache[cache_key]
        retroarch_exe, ra_core_path, rom_path = resolve_ra_paths(rom_key)
        fresh_state_path = state_path_for(ROM_REGISTRY[rom_key]) if use_fresh_state else None
        try:
            harness = RAHarness.launch(
                rom_path=rom_path,
                core_path=ra_core_path,
                retroarch_exe=retroarch_exe,
                nci_port=_free_udp_port(),
                fresh_state_path=fresh_state_path,
            )
        except RAHarnessLaunchError as exc:
            # CLAUDE.md: launch failure is a FAILURE, not a skip. RAHarnessLaunchError
            # subclasses RuntimeError so the hard-fail rule still holds; re-raise the
            # typed exception so the diagnostic hook can read its structured fields
            # (pid, port, stage, log_path). Annotate args with rom_key so the test
            # report still names the harness that failed.
            exc.args = (f"ra_harness launch failed for rom_key={rom_key!r}: {exc.args[0]}",)
            raise
        self._cache[cache_key] = harness
        return harness

    def teardown_all(self) -> None:
        while self._cache:
            cache_key, harness = self._cache.popitem()
            try:
                harness.teardown()
            except Exception:
                # Best-effort: surface in the log, don't mask the original test failure.
                logging.getLogger(__name__).exception("ra_harness teardown failed for %r", cache_key)


def _harness_factory_impl() -> _HarnessFactory:
    """Factory constructor surface used by both the pytest fixture and unit tests."""
    return _HarnessFactory()


@pytest.fixture(scope="session")
def ra_harness_factory():
    """Session-scoped factory: factory(rom_key) -> RAHarness, cached per rom_key.

    Hard-fails (RuntimeError) on any missing infrastructure — no pytest.skip.
    See ROM_REGISTRY for the available rom_keys.
    """
    factory = _harness_factory_impl()
    yield factory
    factory.teardown_all()


@pytest.fixture(scope="session")
def ra_harness_vanilla_smw(ra_harness_factory):
    """Session-scoped RAHarness pinned to vanilla SMW (_clean.smc).

    Used by the practice smoke and harness isolation tests. NOT currently
    used by poke transition tests — the committed _clean.state savestate
    lands on the title screen (the no-input free-run settle can't reach
    an in-game frame without controller input injection, which NCI doesn't
    expose). Once make_fresh_boot_state.py learns to drive controller input
    to reach an in-game frame, run_scenario can switch to this harness and
    test vanilla SMW level numbering directly.
    """
    return ra_harness_factory("vanilla_smw")


@pytest.fixture(scope="session")
def ra_harness_love_yourself(ra_harness_factory):
    """Session-scoped RAHarness for Love Yourself.smc, with per-scenario fresh-boot
    reset. Backs ``run_scenario`` for poke transition tests.

    The fresh-boot savestate is in-game (Love Yourself's intro lands the
    player in a level on its own; no controller input needed), so SPC700/CPU
    state can't leak between scenarios. This is the harness that resolved
    project_transition_state_leak.
    """
    return ra_harness_factory("love_yourself")


@pytest.fixture(scope="session")
def ra_harness_love_yourself_no_reset(ra_harness_factory):
    """Session-scoped RAHarness for Love Yourself.smc, WITHOUT fresh-boot reset.

    The only consumer is the replay fixture, which needs RA's
    ``savestate_directory`` to be the user's actual savestate dir so RA can
    find the staged ``.replay`` files. The fresh-state harness override
    isolates ``savestate_directory`` to a tmp dir, which would hide the
    .replay files from RA. Distinct cache key from ``ra_harness_love_yourself``,
    so a second RA process is launched for this purpose.
    """
    return ra_harness_factory("love_yourself", use_fresh_state=False)


@pytest.fixture
def run_scenario(ra_harness_love_yourself):
    """Send a poke scenario through the Love Yourself RA harness and collect events.

    Pinned to Love Yourself because that's the ROM whose committed fresh-boot
    savestate lands the player in a level (vanilla SMW's lands on the title
    screen — see ra_harness_vanilla_smw). The detector tests are ROM-agnostic
    in practice: they assert on transitions in tracked ADDR_MAP bytes, which
    the engine zeros after the savestate load — so absolute level numbers
    don't matter, only that the ROM isn't actively overwriting our pokes.
    """

    async def _run(scenario_name: str, timeout: float = 30.0) -> list:
        scenario_path = SCENARIO_DIR / scenario_name
        if not scenario_path.exists():
            pytest.fail(f"Scenario file not found: {scenario_path}")
        scenario = parse_poke_file(str(scenario_path))
        return await asyncio.wait_for(
            asyncio.to_thread(ra_harness_love_yourself.engine.run_scenario, scenario),
            timeout=timeout,
        )

    return _run


@pytest.fixture(scope="session")
def replay_ra_dashboard(ra_harness_love_yourself_no_reset):
    """Start a dashboard pointed at the Love Yourself RA session for replay tests.

    Mirrors ``replay_dashboard`` but uses the RA backend (build_orchestrator)
    instead of the legacy Mesen+TCP backend.  The RA process is already up (ra_harness_love_yourself);
    this fixture wires the dashboard to it via NCI at the configured port.

    Phase E PLAY_REPLAY requires RA to be in PLAYING (not PAUSED) state.
    The harness leaves RA paused; we unpause it here so the orchestrator's
    _on_replay → MoviePlayer.play → play_replay() works correctly.

    Yields (base_url, db, tmp_path) — tmp_path is the data dir where the
    test should stage its fixture files.
    """
    from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig
    from spinlab.dashboard import create_app
    from spinlab.db import Database

    config_raw = load_config()
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

    rom_dir = resolve_rom_path("love_yourself").parent

    config = AppConfig(
        network=NetworkConfig(
            host="127.0.0.1",
            port=15482,  # unused — RA backend uses NCI, not TCP
            dashboard_port=dashboard_port,
            # Talk to the harness's RA, not whatever the user's config.yaml
            # advertises. The harness picks a free port per session.
            nci_port=ra_harness_love_yourself_no_reset.client.port,
        ),
        emulator=EmulatorConfig(
            savestate_dir=savestate_dir,
            spinlab_state_dir=spinlab_state_dir,
            ra_core_subdir=ra_core_subdir,
        ),
        data_dir=tmp_path,
        rom_dir=rom_dir,
    )

    app = create_app(db=db, config=config)

    uvi_config = uvicorn.Config(app, host="127.0.0.1", port=dashboard_port, log_level="warning")
    server = uvicorn.Server(uvi_config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{dashboard_port}"

    # Wait for uvicorn to come up.
    if _wait_for_dashboard_state(base_url, check=_status_200) is not None:
        _teardown_replay_dashboard(server=server, thread=thread, db=db, tmp=tmp)
        pytest.fail("replay_ra_dashboard: uvicorn did not start within 10 seconds")

    # Wait for the orchestrator to connect to RA and receive rom_info so the
    # dashboard has a game_id (required before /api/replay/start will resolve).
    def _orchestrator_ready(resp: http_requests.Response) -> tuple[bool, Exception | None]:
        state = resp.json()
        if state.get("emu_connected") and state.get("game_id"):
            return True, None
        return False, RuntimeError(
            f"emu_connected={state.get('emu_connected')!r} "
            f"game_id={state.get('game_id')!r}"
        )

    last_state_error = _wait_for_dashboard_state(
        base_url, check=_orchestrator_ready, timeout_s=2.0
    )
    if last_state_error is not None:
        _teardown_replay_dashboard(server=server, thread=thread, db=db, tmp=tmp)
        pytest.fail(format_dashboard_startup_failure(
            port=dashboard_port,
            attempts=40,
            interval_s=0.25,
            last_error=last_state_error,
            subject="replay_ra_dashboard orchestrator connection",
        ))

    # PLAY_REPLAY requires RA to be in PLAYING state. The harness left RA paused;
    # unpause it now so the orchestrator's _on_replay → MoviePlayer.play_replay()
    # actually starts playback. Use the harness's NCI client directly.
    harness = ra_harness_love_yourself_no_reset
    try:
        status = harness.client.get_status()
        if status.state == "PAUSED":
            harness.client.pause_toggle()
            time.sleep(0.3)  # allow RA to settle into PLAYING before the test POSTs replay/start
    except Exception as exc:
        # Tear down what we built before failing — preserves the no-yield
        # invariant for downstream cleanup hooks.
        _teardown_replay_dashboard(server=server, thread=thread, db=db, tmp=tmp)
        pytest.fail(format_pause_toggle_failure(harness, exc))

    yield base_url, db, tmp_path

    _teardown_replay_dashboard(server=server, thread=thread, db=db, tmp=tmp)


# ---------------------------------------------------------------------------
# Diagnostic dump on integration test failure
# ---------------------------------------------------------------------------

# Diagnostic capture machinery lives in _diagnostics.py. The ring handler is
# installed once at module load via install_log_handler() (called above in the
# imports block). The pytest hooks below delegate to the module functions.


def _wait_for_dashboard_state(
    base_url: str,
    *,
    check: Callable[[http_requests.Response], tuple[bool, Exception | None]],
    attempts: int = 40,
    interval_s: float = 0.25,
    timeout_s: float = 1.0,
) -> Exception | None:
    """Poll ``{base_url}/api/state`` until ``check(resp)`` returns ``(True, _)``.

    Returns ``None`` on success or the last captured Exception on timeout.
    Caller decides how to surface the failure (typically ``pytest.fail`` with a
    formatted message from ``format_dashboard_startup_failure``).

    Uses blocking ``time.sleep`` for use across sync and async fixtures alike:
    the dashboard's background event_loop runs in a separate uvicorn thread,
    and the fixture's own asyncio loop is single-tenant during setup, so the
    block doesn't starve anything.
    """
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            resp = http_requests.get(f"{base_url}/api/state", timeout=timeout_s)
            ok, err = check(resp)
            if ok:
                return None
            last_error = err
        except Exception as exc:
            last_error = exc
        time.sleep(interval_s)
    return last_error


def _status_200(resp: http_requests.Response) -> tuple[bool, Exception | None]:
    """Default dashboard-ready check: HTTP 200 from /api/state."""
    if resp.status_code == 200:
        return True, None
    return False, RuntimeError(f"status {resp.status_code}")


def _teardown_replay_dashboard(*, server, thread, db, tmp) -> None:
    """Tear down the uvicorn server, DB connection, and tmp dir created by
    replay_ra_dashboard. Used by all bail-out paths and the happy-path cleanup.
    """
    server.should_exit = True
    thread.join(timeout=5)
    db.close()
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Append diagnostic state to the report on integration test failure.

    Two paths:
      - ``report.when == "call"`` + test body failed: walks ``item.funcargs``
        via ``collect_diagnostics`` for dashboard and harness state.
      - ``report.when == "setup"`` + the failing exception is
        ``RAHarnessLaunchError``: renders the typed exception's structured
        fields and tails the preserved retroarch.log. The factory never made
        it into ``funcargs`` so there's nothing to walk; the exception is the
        only source of truth.

    Other setup-phase failures fall through to pytest's normal reporting.
    """
    outcome = yield
    report = outcome.get_result()
    if not report.failed:
        return
    # Only for tests in the integration directory
    if "integration" not in str(item.fspath):
        return

    if report.when == "call":
        diag = collect_diagnostics(item)
        if diag:
            # `longreprtext` is a read-only property in current pytest, so the
            # diagnostic block has to ride along on `sections` instead.  pytest
            # renders sections in the terminal report after the traceback.
            report.sections.append(("SpinLab Diagnostics", diag))
        return

    if report.when == "setup" and call.excinfo is not None:
        exc = call.excinfo.value
        if isinstance(exc, RAHarnessLaunchError):
            diag = collect_launch_failure_diagnostics(exc)
            if diag:
                report.sections.append(
                    ("SpinLab Launch-Failure Diagnostics", diag)
                )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    """Clear the diagnostic ring buffer at the start of each integration test
    so the failure diagnostic only shows logs from the current test, not stale
    lines bled in from earlier tests in the session-scoped harness."""
    if "integration" in str(item.fspath):
        ring.clear()
    yield

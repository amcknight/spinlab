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
import tempfile
import time
from pathlib import Path

import pytest
import pytest_asyncio
import requests as http_requests
from tests.integration._dashboard_harness import _free_port
from tests.integration._diagnostics import (
    collect_diagnostics,
    collect_launch_failure_diagnostics,
    format_pause_toggle_failure,
    install_log_handler,
    ring,
)
from tests.integration._harness_factory import harness_factory_impl
from tests.integration._rom_paths import (
    CLEAN_SMW_ROM_NAME,  # noqa: F401 — re-exported for downstream consumers
    INTEGRATION_STATES_DIR,  # noqa: F401 — re-exported for downstream consumers
    LOVE_YOURSELF_GAME_ID,  # noqa: F401 — re-exported; used by test_replay_fixture
    LOVE_YOURSELF_ROM_NAME,  # noqa: F401 — re-exported for downstream consumers
    PROJECT_ROOT,  # noqa: F401 — re-exported for downstream consumers
    TOOTHPASTE_ROM_NAME,  # noqa: F401 — re-exported for downstream consumers
    load_config,
    resolve_rom_path,
)
from tests.integration.poke_parser import parse_poke_file
from tests.integration.ra_harness import RAHarnessLaunchError

install_log_handler()

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"

pytestmark = pytest.mark.emulator


# -- Seeded-game fixture for frontend contract smoke tests -------------------
FAKE_GAME_NAME = "FakeGame"


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def fake_dashboard_server(tmp_path_factory):
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
    from tests.integration._dashboard_harness import DashboardHarness

    tmp_root = tmp_path_factory.mktemp("fake_dashboard")
    with DashboardHarness.fake(tmp_path_root=tmp_root) as ctx:
        yield ctx.base_url, ctx.db, ctx.session


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


# Accumulates (rom_key, use_fresh_state, exit_code) for every transparent RA
# relaunch this session, copied off the factory at teardown so
# pytest_terminal_summary can surface them after the factory is gone.
_RA_RECOVERIES: list[tuple[str, bool, int | None]] = []


@pytest.fixture(scope="session")
def ra_harness_factory():
    """Session-scoped factory: factory(rom_key) -> live RAHarness, cached per key.

    Hard-fails (RuntimeError) on any missing infrastructure — no pytest.skip.
    The factory health-gates cached harnesses and transparently relaunches a
    crashed RA (upstream snes9x ACCESS_VIOLATION); those relaunches are
    recorded and surfaced at session end. See ROM_REGISTRY for rom_keys.
    """
    factory = harness_factory_impl()
    yield factory
    _RA_RECOVERIES.extend(factory.recoveries)
    factory.teardown_all()


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


@pytest.fixture
def ra_harness_love_yourself_no_reset(ra_harness_factory):
    """RAHarness for Love Yourself.smc WITHOUT fresh-boot reset (distinct RA process).

    Consumers: the replay fixture (needs RA's ``savestate_directory`` to be the
    user's actual dir so RA finds the staged ``.replay`` files) and the harness-
    isolation test (needs a second distinct RA process).

    FUNCTION-scoped + released on teardown (not session-scoped) so this second
    RA process does NOT stay alive into the crash-prone transition phase. The
    snes9x ACCESS_VIOLATION (Mode 3) scales with concurrent RA count, so we keep
    that phase at one live RA. The factory relaunches on the next request.
    """
    yield ra_harness_factory("love_yourself", use_fresh_state=False)
    ra_harness_factory.release("love_yourself", use_fresh_state=False)


@pytest.fixture
def run_scenario(ra_harness_love_yourself, ra_harness_factory):
    """Send a poke scenario through the Love Yourself RA harness and collect events.

    Pinned to Love Yourself because that's the ROM whose committed fresh-boot
    savestate lands the player in a level. The detector tests are ROM-agnostic
    in practice: they assert on transitions in tracked ADDR_MAP bytes, which
    the engine zeros after the savestate load — so absolute level numbers
    don't matter, only that the ROM isn't actively overwriting our pokes.

    Depends on ``ra_harness_love_yourself`` so the RA launches at fixture-setup
    time (preserving launch-failure-at-setup diagnostics). Resolves the harness
    through ``ra_harness_factory`` at run time so the factory health-gate hands
    back a *live* harness — if the snes9x core crashed in a prior test (Mode 3 /
    0xC0000005), the factory transparently relaunches a fresh one, so the crash
    doesn't cascade across the remaining transition tests.
    """

    def _resolve_and_run(scenario) -> list:
        return ra_harness_factory("love_yourself").engine.run_scenario(scenario)

    async def _run(scenario_name: str, timeout: float = 30.0) -> list:
        scenario_path = SCENARIO_DIR / scenario_name
        if not scenario_path.exists():
            pytest.fail(f"Scenario file not found: {scenario_path}")
        scenario = parse_poke_file(str(scenario_path))
        start = time.monotonic()
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_resolve_and_run, scenario),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            pytest.fail(
                f"run_scenario({scenario_name!r}) timed out after "
                f"{elapsed:.1f}s (limit {timeout:.1f}s)"
            )

    return _run


@pytest.fixture
def replay_ra_dashboard(
    ra_harness_love_yourself_no_reset, ra_harness_factory, tmp_path_factory
):
    """Start a dashboard pointed at the Love Yourself RA session for replay tests.

    Mirrors ``replay_dashboard`` but uses the RA backend (build_orchestrator)
    instead of the legacy Mesen+TCP backend. Wires the dashboard to the no-reset
    harness via NCI at its configured port.

    Phase E PLAY_REPLAY requires RA to be in PLAYING (not PAUSED) state.
    The harness leaves RA paused; we unpause it here so the orchestrator's
    _on_replay → MoviePlayer.play → play_replay() works correctly.

    Releases the fresh-state workhorse harness (``love_yourself``) for the
    duration of the replay so this high-load test runs against ONE live RA.
    Measured crash rate: 1 RA ≈ 0.7%/scenario-load vs ~18-43% at 2-6 concurrent
    RA (snes9x ACCESS_VIOLATION scales steeply with concurrency). The next
    transition test relaunches the workhorse via the factory.

    Yields (base_url, db, tmp_path) — tmp_path is the data dir where the
    test should stage its fixture files.
    """
    ra_harness_factory.release("love_yourself", use_fresh_state=True)
    from tests.integration._dashboard_harness import DashboardHarness

    config_raw = load_config()
    emu_raw = config_raw.get("emulator", {})

    savestate_dir_str = emu_raw.get("savestate_dir")
    ra_core_subdir = emu_raw.get("ra_core_subdir") or "Snes9x"

    if not savestate_dir_str:
        pytest.skip("replay_ra_dashboard: emulator.savestate_dir not configured")

    savestate_dir = Path(savestate_dir_str)
    tmp_root = tmp_path_factory.mktemp("spinlab_ra_replay")
    tmp_path = Path(tempfile.mkdtemp(prefix="spinlab_ra_replay_", dir=tmp_root))
    spinlab_state_dir = tmp_path / "spinlab_states"
    spinlab_state_dir.mkdir(parents=True, exist_ok=True)

    dashboard_port = _free_port()
    rom_dir = resolve_rom_path("love_yourself").parent

    from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig
    config = AppConfig(
        network=NetworkConfig(
            host="127.0.0.1",
            port=15482,  # unused — RA backend uses NCI, not TCP
            dashboard_port=dashboard_port,
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

    harness_cm = DashboardHarness(
        config=config, fake_emu=False, tmp_path=tmp_path,
    )
    try:
        ctx = harness_cm.__enter__()
    except RuntimeError as exc:
        # DashboardHarness raises with the WaitOutcome.format_message() text;
        # surface it as a pytest.fail so the report shape matches the old path.
        pytest.fail(str(exc))

    try:
        # Wait for the orchestrator to connect to RA and receive rom_info so the
        # dashboard has a game_id (required before /api/replay/start will resolve).
        from tests.integration._wait_for import wait_for

        def _fetch_state():
            return http_requests.get(
                f"{ctx.base_url}/api/state", timeout=1.0,
            ).json()

        def _orchestrator_ready(state):
            if state.get("emu_connected") and state.get("game_id"):
                return True, ""
            return False, (
                f"emu_connected={state.get('emu_connected')!r} "
                f"game_id={state.get('game_id')!r}"
            )

        outcome = wait_for(
            name="orchestrator_ready",
            fetch=_fetch_state,
            predicate=_orchestrator_ready,
            timeout_s=10.0,
            interval_s=0.25,
        )
        if not outcome.succeeded:
            pytest.fail(outcome.format_message())

        # PLAY_REPLAY requires RA in PLAYING state. The harness left it paused.
        harness = ra_harness_love_yourself_no_reset
        try:
            status = harness.client.get_status()
            if status.state == "PAUSED":
                harness.client.pause_toggle()
                time.sleep(0.3)  # let RA settle into PLAYING before tests POST replay/start
        except Exception as exc:
            pytest.fail(format_pause_toggle_failure(harness, exc))

        yield ctx.base_url, ctx.db, ctx.tmp_path
    finally:
        harness_cm.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Diagnostic dump on integration test failure
# ---------------------------------------------------------------------------

# Diagnostic capture machinery lives in _diagnostics.py. The ring handler is
# installed once at module load via install_log_handler() (called above in the
# imports block). The pytest hooks below delegate to the module functions.


def _convert_auto_skipped_emulator_to_failure(item, call, report):
    """Per CLAUDE.md: an emulator test reporting SKIPPED without an explicit
    ``@pytest.mark.skipif`` is a failure, not a skip. Fixture launch failures,
    missing config, env-var misses must surface loudly so the next session
    doesn't inherit the silent-skip habit.
    """
    if not report.skipped:
        return
    if report.when not in ("setup", "call"):
        return
    if not any(m.name == "emulator" for m in item.iter_markers()):
        return
    if any(m.name == "skipif" for m in item.iter_markers()):
        return

    skip_reason = "<unknown>"
    if call.excinfo is not None:
        skip_reason = str(call.excinfo.value)
    report.outcome = "failed"
    report.longrepr = (
        "LOUD-FAIL: emulator test reported SKIPPED without "
        "@pytest.mark.skipif.\n"
        f"  Skip reason: {skip_reason}\n"
        "  Per CLAUDE.md ('skips count as failures'), this is a failure. "
        "Either fix the environment so the test runs, or add "
        "@pytest.mark.skipif with a reason already accepted."
    )


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

    Also converts auto-skipped emulator tests into failures
    (see ``_convert_auto_skipped_emulator_to_failure``).

    Other setup-phase failures fall through to pytest's normal reporting.
    """
    outcome = yield
    report = outcome.get_result()
    _convert_auto_skipped_emulator_to_failure(item, call, report)
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


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Surface RA crash recoveries prominently in the test summary.

    The snes9x core crashes intermittently with a Windows ACCESS_VIOLATION
    (0xC0000005) — an upstream bug. The harness recovers by relaunching so the
    crash doesn't cascade or redden the suite, but the crashes must NOT be
    swallowed silently (CLAUDE.md: "never silently move on"). This reports how
    many times RA crashed and was relaunched this session.
    """
    if not _RA_RECOVERIES:
        return
    terminalreporter.section("SpinLab RA crash recovery")
    terminalreporter.write_line(
        f"RA backend crashed and was relaunched {len(_RA_RECOVERIES)} time(s) "
        "this session (upstream snes9x ACCESS_VIOLATION; tests auto-recovered):"
    )
    for rom_key, use_fresh_state, exit_code in _RA_RECOVERIES:
        exit_hex = (
            f"0x{exit_code & 0xFFFFFFFF:X}" if exit_code is not None else "<unknown>"
        )
        terminalreporter.write_line(
            f"  - {rom_key} (fresh_state={use_fresh_state}) exit {exit_hex}"
        )


def pytest_sessionfinish(session, exitstatus):
    """Clean up `spinlab_ra_failed_*` tmpdirs left behind by RAHarness launch
    failures. `_cleanup_launch` creates one of these per failed launch so the
    preserved RA log survives the original tmp_dir's rmtree and the diagnostic
    hook can tail it; nothing else owns them after the session ends.
    """
    import shutil as _shutil

    failed_root = Path(tempfile.gettempdir())
    for orphan in failed_root.glob("spinlab_ra_failed_*"):
        _shutil.rmtree(orphan, ignore_errors=True)

"""RetroArchOrchestrator tests using FakeRAClient.

Consolidates what was previously split across two files (test_orchestrator.py
and test_retroarch_orchestrator.py, with three parallel _FakeNCI hierarchies).
Tests drive the orchestrator through its public command/event surface; RAClient-
internal mechanics (slot resolution, WRAM verify, mtime polling) are tested
separately at the RAClient level.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from tests.fakes.raclient import FakeMovieIO, FakePoller, FakeRAClient

from spinlab.condition_registry import ConditionRegistry
from spinlab.errors import BackendNotImplementedError
from spinlab.protocol import (
    ColdFillLoadCmd,
    ConditionSpec,
    FillGapLoadCmd,
    PracticeLoadCmd,
    PracticeStopCmd,
    ReferenceStartCmd,
    ReferenceStopCmd,
    ReplayCmd,
    ReplayErrorEvent,
    ReplayFinishedEvent,
    ReplayStartedEvent,
    ReplayStopCmd,
    ResetCmd,
    RomInfoEvent,
    SetConditionsCmd,
    HyperPlayLoadCmd,
    HyperPlayStopCmd,
)
from spinlab.retroarch.orchestrator import RetroArchOrchestrator
from spinlab.retroarch.raclient import NotReachableError
from spinlab.state_paths import StatePathResolver
from spinlab.timing import HyperPlayTiming, PracticeTiming


def _build_orchestrator(
    tmp_path: Path,
    *,
    enable_movies: bool = True,
    raclient: FakeRAClient | None = None,
    movie_io: FakeMovieIO | None = None,
) -> tuple[RetroArchOrchestrator, FakeRAClient, FakePoller, ConditionRegistry, FakeMovieIO]:
    raclient = raclient or FakeRAClient()
    movie_io = movie_io or FakeMovieIO()
    poller = FakePoller()
    conditions = ConditionRegistry()
    from spinlab.retroarch.movies import MovieController
    movies = MovieController(
        movie_io=movie_io,  # type: ignore[arg-type]  # duck-typed FakeMovieIO
        raclient=raclient,
        enable=enable_movies,
        on_event=lambda ev: None,  # rebound by orch.__init__
    )
    orch = RetroArchOrchestrator(
        raclient=raclient,
        poller=poller,
        conditions=conditions,
        practice_timing=PracticeTiming(),
        hyper_play_timing=HyperPlayTiming(),
        state_paths=StatePathResolver(tmp_path / "states"),
        movies=movies,
    )
    return orch, raclient, poller, conditions, movie_io


# ------------------------------------------------------------------
# Lifecycle
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_emits_rom_info_event(tmp_path):
    orch, raclient, _, _, _ = _build_orchestrator(tmp_path)
    ok = await orch.connect()
    assert ok is True
    assert orch.is_connected is True
    ev = await asyncio.wait_for(orch.events.get(), timeout=0.1)
    assert isinstance(ev, RomInfoEvent)
    assert ev.filename == "Test Game"
    await orch.disconnect()


@pytest.mark.asyncio
async def test_connect_returns_false_when_nci_fails(tmp_path):
    raclient = FakeRAClient(raise_on_connect=NotReachableError("simulated"))
    orch, _, _, _, _ = _build_orchestrator(tmp_path, raclient=raclient)
    ok = await orch.connect()
    assert ok is False
    assert orch.is_connected is False


@pytest.mark.asyncio
async def test_connect_returns_false_when_rom_not_yet_loaded(tmp_path):
    """GET_STATUS returns empty rom_filename when RA is reachable but the
    game isn't loaded yet (race: dashboard launches RA, NCI port opens
    before the core finishes loading the ROM). Orchestrator must not flip
    to connected — the dashboard's reconnect tick will retry."""
    raclient = FakeRAClient(rom_filename="")
    orch, _, _, _, _ = _build_orchestrator(tmp_path, raclient=raclient)
    ok = await orch.connect()
    assert ok is False
    assert orch.is_connected is False
    assert orch.events.empty(), "no RomInfoEvent emitted on empty rom_filename"


@pytest.mark.asyncio
async def test_connect_succeeds_on_retry_after_rom_loads(tmp_path):
    """Simulates the race: first connect() sees empty rom_filename and
    returns False; subsequent connect() once the ROM finishes loading
    succeeds normally."""
    raclient = FakeRAClient(rom_filename="")
    orch, _, _, _, _ = _build_orchestrator(tmp_path, raclient=raclient)

    assert await orch.connect() is False
    assert orch.is_connected is False

    # ROM finishes loading; retry succeeds.
    raclient.rom_filename = "Love Yourself"
    assert await orch.connect() is True
    assert orch.is_connected is True
    ev = await asyncio.wait_for(orch.events.get(), timeout=0.1)
    assert isinstance(ev, RomInfoEvent)
    assert ev.filename == "Love Yourself"
    await orch.disconnect()


@pytest.mark.asyncio
async def test_disconnect_stops_poller_and_closes_raclient(tmp_path):
    orch, raclient, poller, _, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    await orch.disconnect()
    assert orch.is_connected is False
    assert poller.is_stopped is True
    assert raclient.disconnect_calls == 1


# ------------------------------------------------------------------
# ROM-change re-detection (dashboard tracks whatever ROM RA has loaded)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rom_change_after_connect_emits_new_rom_info(tmp_path):
    """If RA loads a different ROM after the orchestrator is already
    connected (the user switches games in RA's Quick Menu, or relaunches),
    the periodic GET_STATUS check must re-emit RomInfoEvent so the dashboard
    switches games. Regression: backlog item B — the dashboard stayed stuck
    on the first ROM because RomInfoEvent was only ever emitted from connect()."""
    orch, raclient, _, _, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    first = await orch.events.get()
    assert isinstance(first, RomInfoEvent)
    assert first.filename == "Test Game"

    raclient.rom_filename = "Love Yourself"
    await orch._check_rom_change()

    ev = await asyncio.wait_for(orch.events.get(), timeout=0.1)
    assert isinstance(ev, RomInfoEvent)
    assert ev.filename == "Love Yourself"
    await orch.disconnect()


@pytest.mark.asyncio
async def test_rom_change_refreshes_raclient_basename(tmp_path):
    """On a detected ROM switch, the RAClient's cached game_basename must be
    refreshed too — movie record/replay staging builds paths from it, so a
    stale basename would stage a new game's movie under the old game's name
    (backlog D #2)."""
    orch, raclient, _, _, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    await orch.events.get()  # rom_info
    assert raclient.game_basename == "Test Game"

    raclient.rom_filename = "Love Yourself"
    await orch._check_rom_change()

    assert raclient.game_basename == "Love Yourself"
    await orch.disconnect()


@pytest.mark.asyncio
async def test_rom_check_emits_nothing_when_rom_unchanged(tmp_path):
    """The common case: ROM hasn't changed since last check → no event, so
    we don't churn switch_game (and its checksum + condition-registry reload)
    every 2s."""
    orch, raclient, _, _, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    await orch.events.get()  # rom_info

    await orch._check_rom_change()

    assert orch.events.empty()
    await orch.disconnect()


@pytest.mark.asyncio
async def test_rom_check_ignores_empty_rom(tmp_path):
    """GET_STATUS returns an empty game field while RA is between ROMs (Quick
    Menu open, core unloaded). Don't emit a RomInfoEvent with an empty
    filename — that would be a spurious switch toward 'no game'."""
    orch, raclient, _, _, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    await orch.events.get()  # rom_info

    raclient.rom_filename = ""
    await orch._check_rom_change()

    assert orch.events.empty()
    await orch.disconnect()


@pytest.mark.asyncio
async def test_rom_check_survives_get_status_failure(tmp_path):
    """RA died between checks → GET_STATUS raises. The background ROM check
    must swallow it (the poller's own reconnect path handles socket recovery)
    rather than crash the tick loop."""
    raclient = FakeRAClient()
    orch, _, _, _, _ = _build_orchestrator(tmp_path, raclient=raclient)
    await orch.connect()
    await orch.events.get()  # rom_info

    raclient.raise_on_get_status = NotReachableError("RA gone")
    await orch._check_rom_change()  # must not raise

    assert orch.events.empty()
    await orch.disconnect()


# ------------------------------------------------------------------
# Practice
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_practice_load_cmd_loads_state_and_arms_timing(tmp_path):
    orch, raclient, _, _, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    await orch.events.get()
    cmd = PracticeLoadCmd(
        id="seg-x", state_path="/p/seg.state", end_type="goal",
        expected_time_ms=5000, auto_advance_delay_ms=200, death_penalty_ms=3200,
    )
    await orch.send_command(cmd)
    assert raclient.load_state_calls == [Path("/p/seg.state")]
    assert orch._practice_timing.is_armed is True
    await orch.disconnect()


@pytest.mark.asyncio
async def test_practice_stop_cmd_disarms(tmp_path):
    orch, _, _, _, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    await orch.events.get()
    await orch.send_command(PracticeLoadCmd(
        id="seg-x", state_path="/p", end_type="goal",
        auto_advance_delay_ms=200, death_penalty_ms=3200,
    ))
    await orch.send_command(PracticeStopCmd())
    assert orch._practice_timing.is_armed is False
    await orch.disconnect()


# ------------------------------------------------------------------
# Cold-fill / fill-gap
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cold_fill_load_activates_poller(tmp_path):
    orch, raclient, poller, _, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    await orch.events.get()
    await orch.send_command(ColdFillLoadCmd(state_path="/p/cold.state", segment_id="seg-cold"))
    assert raclient.load_state_calls == [Path("/p/cold.state")]
    assert poller.cold_fill_activations == ["seg-cold"]
    await orch.disconnect()


@pytest.mark.asyncio
async def test_fill_gap_load_loads_state_no_cold_fill(tmp_path):
    orch, raclient, poller, _, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    await orch.events.get()
    await orch.send_command(FillGapLoadCmd(state_path="/p/gap.state"))
    assert raclient.load_state_calls == [Path("/p/gap.state")]
    assert poller.cold_fill_activations == []
    await orch.disconnect()


# ------------------------------------------------------------------
# EmuBackend save/load
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_state_resolves_segment_id_and_delegates(tmp_path):
    orch, raclient, _, _, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    await orch.events.get()
    await orch.save_state("seg_123")
    assert raclient.save_state_calls == [tmp_path / "states" / "seg_123.state"]
    await orch.disconnect()


@pytest.mark.asyncio
async def test_load_state_delegates_to_raclient(tmp_path):
    orch, raclient, _, _, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    await orch.events.get()
    await orch.load_state("/some/path/file.state")
    assert raclient.load_state_calls == [Path("/some/path/file.state")]
    # state_version bumps automatically inside RAClient (FakeRAClient mirrors).
    assert raclient.state_version == 1
    await orch.disconnect()


# ------------------------------------------------------------------
# Reset / set_conditions
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reset_cmd_calls_raclient_reset(tmp_path):
    orch, raclient, _, _, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    await orch.events.get()
    await orch.send_command(ResetCmd())
    assert raclient.reset_calls == 1  # RAClient.reset() handles the double-tap internally
    await orch.disconnect()


@pytest.mark.asyncio
async def test_set_conditions_cmd_updates_registry(tmp_path):
    orch, _, _, conditions, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    await orch.events.get()
    defs = [ConditionSpec(name="x", address=0x100, size=1)]
    await orch.send_command(SetConditionsCmd(definitions=defs))
    assert len(conditions.definitions) == 1
    assert conditions.definitions[0].name == "x"
    await orch.disconnect()


# ------------------------------------------------------------------
# Reference (movie recording)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reference_start_invokes_record_movie(tmp_path):
    orch, _, _, _, movie_io = _build_orchestrator(tmp_path)
    await orch.connect()
    await orch.events.get()
    await orch.send_command(ReferenceStartCmd(path="/tmp/seg.replay"))
    assert movie_io.record_movie_calls == [Path("/tmp/seg.replay")]
    assert orch._movies.is_recording is True
    await orch.disconnect()


@pytest.mark.asyncio
async def test_reference_stop_stops_active_recording(tmp_path):
    orch, raclient, _, _, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    await orch.events.get()
    await orch.send_command(ReferenceStartCmd(path="/tmp/seg.replay"))
    await orch.send_command(ReferenceStopCmd())
    assert orch._movies.is_recording is False
    await orch.disconnect()


@pytest.mark.asyncio
async def test_reference_start_no_movies_is_noop(tmp_path):
    orch, _, _, _, movie_io = _build_orchestrator(tmp_path, enable_movies=False)
    await orch.connect()
    await orch.events.get()
    await orch.send_command(ReferenceStartCmd(path="/tmp/seg.replay"))
    assert movie_io.record_movie_calls == []
    await orch.disconnect()


# ------------------------------------------------------------------
# Replay (movie playback)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replay_cmd_emits_replay_started(tmp_path):
    movie_io = FakeMovieIO(frame_count=900)
    orch, _, _, _, _ = _build_orchestrator(tmp_path, movie_io=movie_io)
    await orch.connect()
    await orch.events.get()  # rom_info
    await orch.send_command(ReplayCmd(path="/tmp/run.replay"))
    ev = await asyncio.wait_for(orch.events.get(), timeout=0.1)
    assert isinstance(ev, ReplayStartedEvent)
    assert Path(ev.path) == Path("/tmp/run.replay")
    assert ev.frame_count == 900
    await orch.disconnect()


@pytest.mark.asyncio
async def test_replay_cmd_emits_error_when_playback_refused(tmp_path):
    movie_io = FakeMovieIO(fail_play_movie=True)
    orch, _, _, _, _ = _build_orchestrator(tmp_path, movie_io=movie_io)
    await orch.connect()
    await orch.events.get()  # rom_info
    await orch.send_command(ReplayCmd(path="/tmp/run.replay"))
    ev = await asyncio.wait_for(orch.events.get(), timeout=0.1)
    assert isinstance(ev, ReplayErrorEvent)
    assert "fake refusal" in ev.message
    await orch.disconnect()


@pytest.mark.asyncio
async def test_replay_stop_emits_finished_and_clears_handle(tmp_path):
    orch, raclient, _, _, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    await orch.events.get()  # rom_info
    await orch.send_command(ReplayCmd(path="/tmp/run.replay"))
    await orch.events.get()  # ReplayStartedEvent
    await orch.send_command(ReplayStopCmd())
    ev = await asyncio.wait_for(orch.events.get(), timeout=0.1)
    assert isinstance(ev, ReplayFinishedEvent)
    assert orch._movies.is_playing is False
    await orch.disconnect()


@pytest.mark.asyncio
async def test_replay_cmd_rejected_when_movies_disabled(tmp_path):
    orch, _, _, _, _ = _build_orchestrator(tmp_path, enable_movies=False)
    await orch.connect()
    await orch.events.get()
    with pytest.raises(BackendNotImplementedError) as exc_info:
        await orch.send_command(ReplayCmd())
    assert exc_info.value.http_code == 501
    await orch.disconnect()


@pytest.mark.asyncio
async def test_replay_stop_is_noop_when_no_active_playback(tmp_path):
    orch, _, _, _, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    await orch.events.get()
    await orch.send_command(ReplayStopCmd())  # should not raise
    await orch.disconnect()


# ------------------------------------------------------------------
# Hyper play
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hyper_play_load_arms_and_stop_disarms(tmp_path):
    orch, raclient, _, _, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    await orch.events.get()
    await orch.send_command(HyperPlayLoadCmd(id="run-1", state_path="/p/sr.state"))
    assert orch._hyper_play_timing.is_armed is True
    assert raclient.load_state_calls == [Path("/p/sr.state")]
    await orch.send_command(HyperPlayStopCmd())
    assert orch._hyper_play_timing.is_armed is False
    await orch.disconnect()


@pytest.mark.asyncio
async def test_hyper_play_load_forwards_delay_kwargs(tmp_path):
    orch, _, _, _, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    await orch.events.get()
    await orch.send_command(HyperPlayLoadCmd(
        id="run-1", state_path="/p/sr.state",
        death_delay_ms=2000, auto_advance_delay_ms=2500,
    ))
    assert orch._hyper_play_timing._death_delay_ms == 2000
    assert orch._hyper_play_timing._auto_advance_delay_ms == 2500
    await orch.disconnect()


# ------------------------------------------------------------------
# Misc
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_command_logged_no_raise(tmp_path):
    orch, _, _, _, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    await orch.events.get()

    class WeirdCmd:
        pass

    await orch.send_command(WeirdCmd())  # should not raise
    await orch.disconnect()


@pytest.mark.asyncio
async def test_recv_event_returns_typed_event(tmp_path):
    orch, _, _, _, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    ev = await orch.recv_event(timeout=0.1)
    assert isinstance(ev, RomInfoEvent)
    await orch.disconnect()


@pytest.mark.asyncio
async def test_recv_event_timeout_returns_none(tmp_path):
    orch, _, _, _, _ = _build_orchestrator(tmp_path)
    await orch.connect()
    await orch.recv_event(timeout=0.1)  # drain rom_info
    ev = await orch.recv_event(timeout=0.05)
    assert ev is None
    await orch.disconnect()

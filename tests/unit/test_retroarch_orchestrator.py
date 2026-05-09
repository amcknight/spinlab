"""Unit tests: MovieRecorder and MoviePlayer wired into RetroArchOrchestrator.

Tests that _on_reference_start/_stop trigger movie recording and that
failures are non-fatal (recorder missing or throwing).
Also tests that _on_replay/_on_replay_stop drive MoviePlayer and emit
the correct protocol events.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from spinlab.protocol import ReferenceStartCmd, ReferenceStopCmd
from spinlab.retroarch.orchestrator import RetroArchOrchestrator


class FakeNCI:
    def __init__(self, advancing: bool = True) -> None:
        # Two consecutive read_ram calls return different bytes when
        # advancing=True (simulates RA running frames), same bytes when
        # False (simulates a stalled core, e.g. failed PLAY_REPLAY).
        self._advancing = advancing
        self._read_count = 0

    def get_status(self):
        return type("S", (), {"state": "PLAYING", "system": None, "game": None, "crc32": None})()

    def read_ram(self, addr: int, length: int) -> bytes:
        self._read_count += 1
        if self._advancing:
            return bytes([self._read_count]) * length
        return b"\x00" * length

    def replay_slot_minus(self) -> None:
        # No-op for tests — the orchestrator fires this many times before
        # PLAY_REPLAY to reset RA's runtime slot. Real NCIClient wraps a
        # UDP send; the fake just absorbs the call.
        pass

    def replay_slot_plus(self) -> None:
        pass


class FakeStateIO:
    game_basename: str | None = "Test Game"

    def update_game_basename(self, name): self.game_basename = name
    def resolve_event_path(self, ev): return None


class FakePoller:
    deps = type("D", (), {"on_event": lambda *a: None})()
    period_sec = 0.016

    async def run(self): pass

    async def stop(self): pass

    def mark_state_loaded(self): pass


class FakeMovieRecorder:
    def __init__(self):
        self.started_with: Path | None = None
        self.stopped: bool = False
        self._is_recording: bool = False

    def start(self, dest: Path) -> None:
        self.started_with = dest
        self._is_recording = True

    def stop(self) -> Path:
        self.stopped = True
        self._is_recording = False
        return self.started_with  # type: ignore[return-value]

    def is_recording(self) -> bool:
        return self._is_recording


def _build_orch(recorder=None, player=None):
    return RetroArchOrchestrator(
        client=FakeNCI(),
        state_io=FakeStateIO(),
        poller=FakePoller(),
        conditions=None,
        practice_timing=None,
        speed_run_timing=None,
        movie_recorder=recorder,
        movie_player=player,
    )


@pytest.fixture
def orchestrator_with_fake_recorder():
    rec = FakeMovieRecorder()
    return _build_orch(recorder=rec), rec


@pytest.fixture
def orchestrator_with_failing_recorder():
    class FailingRec:
        def start(self, dest):
            raise RuntimeError("simulated failure")

        def is_recording(self): return False

    return _build_orch(recorder=FailingRec()), None


@pytest.fixture
def orchestrator_without_recorder():
    return _build_orch()


@pytest.mark.asyncio
async def test_on_reference_start_triggers_recorder(orchestrator_with_fake_recorder):
    orch, fake_rec = orchestrator_with_fake_recorder
    spinrec = "/data/game/rec/refid.spinrec"
    await orch._on_reference_start(ReferenceStartCmd(path=spinrec))
    assert fake_rec.started_with == Path("/data/game/rec/refid.replay")


@pytest.mark.asyncio
async def test_on_reference_stop_triggers_recorder_stop(orchestrator_with_fake_recorder):
    orch, fake_rec = orchestrator_with_fake_recorder
    await orch._on_reference_start(ReferenceStartCmd(path="/x/y/z.spinrec"))
    await orch._on_reference_stop(ReferenceStopCmd())
    assert fake_rec.stopped


@pytest.mark.asyncio
async def test_on_reference_start_logs_warning_if_recorder_fails(
    orchestrator_with_failing_recorder, caplog,
):
    orch, _ = orchestrator_with_failing_recorder
    # Should NOT raise — failures are non-fatal.
    await orch._on_reference_start(ReferenceStartCmd(path="/x/y/z.spinrec"))
    # The exact warning text is implementation-defined; assert that a WARNING
    # log was emitted that mentions "movie recording" or similar.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "movie" in r.message.lower() or "recording" in r.message.lower()
        for r in warnings
    )


@pytest.mark.asyncio
async def test_on_reference_start_skips_when_recorder_is_none(
    orchestrator_without_recorder,
):
    # No-op path preserved for installs that don't have ra_movie_dir configured.
    orch = orchestrator_without_recorder
    await orch._on_reference_start(ReferenceStartCmd(path="/x/y/z.spinrec"))
    # No exception, no behavior change from current state.


# ---------------------------------------------------------------------------
# MoviePlayer tests
# ---------------------------------------------------------------------------

class FakeMoviePlayer:
    def __init__(self):
        self.played: Path | None = None
        self.played_basename: str | None = None
        self.played_slot: int | None = None
        self.stopped: bool = False
        self._is_playing: bool = False

    def play(self, src: Path, *, staged_basename: str | None = None,
             staged_slot: int = 0) -> None:
        self.played = src
        self.played_basename = staged_basename
        self.played_slot = staged_slot
        self._is_playing = True

    def stop(self) -> None:
        self.stopped = True
        self._is_playing = False

    def is_playing(self) -> bool:
        return self._is_playing


def _build_orch_with_nci(recorder=None, player=None, nci=None):
    return RetroArchOrchestrator(
        client=nci or FakeNCI(),
        state_io=FakeStateIO(),
        poller=FakePoller(),
        conditions=None,
        practice_timing=None,
        speed_run_timing=None,
        movie_recorder=recorder,
        movie_player=player,
    )


@pytest.fixture
def orchestrator_with_fake_player():
    p = FakeMoviePlayer()
    return _build_orch(player=p), p


@pytest.fixture
def orchestrator_with_stalled_nci():
    """Orchestrator whose NCI's read_ram returns the same bytes both times —
    simulates a failed PLAY_REPLAY where the core didn't advance frames."""
    p = FakeMoviePlayer()
    return _build_orch_with_nci(player=p, nci=FakeNCI(advancing=False)), p


@pytest.fixture
def orchestrator_without_player():
    return _build_orch()


@pytest.mark.asyncio
async def test_on_replay_translates_spinrec_path_to_replay(orchestrator_with_fake_player):
    """Dashboard resolves ref_id to a .spinrec path; orchestrator translates."""
    from spinlab.protocol import ReplayCmd
    orch, fake_player = orchestrator_with_fake_player
    await orch._on_replay(ReplayCmd(path="/data/game/rec/refid.spinrec", speed=0))
    assert fake_player.played == Path("/data/game/rec/refid.replay")
    # Orchestrator must pass game_basename so MoviePlayer stages with the
    # filename RA's PLAY_REPLAY actually looks for.
    assert fake_player.played_basename == "Test Game"
    # Slot 0 is the fallback when no RA log is configured (the test
    # orchestrator doesn't pass an ra_log_dir).
    assert fake_player.played_slot == 0


@pytest.mark.asyncio
async def test_on_replay_stages_at_slot_from_ra_log(tmp_path):
    """If RA's log dir is configured and contains 'Replay slot: N' lines,
    the orchestrator stages at the most recent slot found."""
    from spinlab.protocol import ReplayCmd
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "retroarch__2026_05_08__19_08_23.log").write_text(
        "[INFO] [Replay] Found last replay slot: #64\n"
        "[INFO] [Replay] Replay slot: 63\n"
        "[INFO] [Replay] Replay slot: 44\n",
        encoding="utf-8",
    )
    fake_player = FakeMoviePlayer()
    orch = RetroArchOrchestrator(
        client=FakeNCI(),
        state_io=FakeStateIO(),
        poller=FakePoller(),
        conditions=None,
        practice_timing=None,
        speed_run_timing=None,
        movie_recorder=None,
        movie_player=fake_player,
        ra_log_dir=log_dir,
    )
    await orch._on_replay(ReplayCmd(path="/x.spinrec", speed=0))
    # Should pick slot 44 — the most recent in the log file.
    assert fake_player.played_slot == 44


@pytest.mark.asyncio
async def test_on_replay_emits_error_event_when_playback_does_not_advance(
    orchestrator_with_stalled_nci,
):
    """When PLAY_REPLAY silently fails (e.g. ROM-checksum mismatch), memory
    doesn't advance. Orchestrator must surface this as ReplayErrorEvent
    instead of leaving the dashboard stuck in 'replaying'."""
    from spinlab.protocol import ReplayCmd
    orch, fake_player = orchestrator_with_stalled_nci
    await orch._on_replay(ReplayCmd(path="/x.spinrec", speed=0))
    ev = await asyncio.wait_for(orch.events.get(), timeout=1.0)
    assert ev["event"] == "replay_error"
    assert "RA refused to load" in ev["message"]
    # Player.stop must have been called to clean up the staged file.
    assert fake_player.stopped


@pytest.mark.asyncio
async def test_on_replay_emits_replay_started_event(orchestrator_with_fake_player):
    """Orchestrator must emit ReplayStartedEvent for the session manager to enter replay mode."""
    from spinlab.protocol import ReplayCmd
    orch, _ = orchestrator_with_fake_player
    await orch._on_replay(ReplayCmd(path="/x.spinrec", speed=0))
    # Pull the emitted event off the orchestrator's event queue
    ev = await asyncio.wait_for(orch.events.get(), timeout=1.0)
    assert ev["event"] == "replay_started"


@pytest.mark.asyncio
async def test_on_replay_reads_frame_count_from_sibling_json(
    orchestrator_with_fake_player, tmp_path,
):
    """Frame count for ReplayStartedEvent comes from <bsv_path>.json sibling if present."""
    import json
    from spinlab.protocol import ReplayCmd
    bsv_path = tmp_path / "fixture.replay"
    bsv_path.write_bytes(b"")
    meta_path = tmp_path / "fixture.json"
    meta_path.write_text(json.dumps({"frame_count": 1234}))
    spinrec_path = tmp_path / "fixture.spinrec"

    orch, _ = orchestrator_with_fake_player
    await orch._on_replay(ReplayCmd(path=str(spinrec_path), speed=0))
    ev = await asyncio.wait_for(orch.events.get(), timeout=1.0)
    assert ev["frame_count"] == 1234


@pytest.mark.asyncio
async def test_on_replay_stop_calls_player_stop_and_emits_finished(
    orchestrator_with_fake_player,
):
    """Stop calls MoviePlayer.stop() and emits ReplayFinishedEvent."""
    from spinlab.protocol import ReplayCmd, ReplayStopCmd
    orch, fake_player = orchestrator_with_fake_player
    await orch._on_replay(ReplayCmd(path="/x.spinrec", speed=0))
    await orch.events.get()  # consume the started event
    await orch._on_replay_stop(ReplayStopCmd())
    assert fake_player.stopped
    ev = await asyncio.wait_for(orch.events.get(), timeout=1.0)
    assert ev["event"] == "replay_finished"


@pytest.mark.asyncio
async def test_on_replay_without_player_raises_backend_not_implemented(
    orchestrator_without_player,
):
    """If no MoviePlayer is configured, replay still raises BackendNotImplementedError."""
    from spinlab.errors import BackendNotImplementedError
    from spinlab.protocol import ReplayCmd
    orch = orchestrator_without_player
    with pytest.raises(BackendNotImplementedError):
        await orch._on_replay(ReplayCmd(path="/x.spinrec", speed=0))

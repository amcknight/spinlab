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
    def get_status(self):
        return type("S", (), {"state": "PLAYING", "system": None, "game": None, "crc32": None})()


class FakeStateIO:
    def update_game_basename(self, name): pass
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
        self.stopped: bool = False
        self._is_playing: bool = False

    def play(self, src: Path) -> None:
        self.played = src
        self._is_playing = True

    def stop(self) -> None:
        self.stopped = True
        self._is_playing = False

    def is_playing(self) -> bool:
        return self._is_playing


@pytest.fixture
def orchestrator_with_fake_player():
    p = FakeMoviePlayer()
    return _build_orch(player=p), p


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

"""Unit tests: MovieRecorder wired into RetroArchOrchestrator.

Tests that _on_reference_start/_stop trigger movie recording and that
failures are non-fatal (recorder missing or throwing).
"""
from __future__ import annotations

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


def _build_orch(recorder):
    return RetroArchOrchestrator(
        client=FakeNCI(),
        state_io=FakeStateIO(),
        poller=FakePoller(),
        conditions=None,
        practice_timing=None,
        speed_run_timing=None,
        movie_recorder=recorder,
    )


@pytest.fixture
def orchestrator_with_fake_recorder():
    rec = FakeMovieRecorder()
    return _build_orch(rec), rec


@pytest.fixture
def orchestrator_with_failing_recorder():
    class FailingRec:
        def start(self, dest):
            raise RuntimeError("simulated failure")

        def is_recording(self): return False

    return _build_orch(FailingRec()), None


@pytest.fixture
def orchestrator_without_recorder():
    return _build_orch(None)


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

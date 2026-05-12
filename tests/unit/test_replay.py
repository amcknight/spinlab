"""Tests for SessionManager replay orchestration.

Uses real SQLite Database + FakeEmuBackend instead of MagicMocks so the
schema is actually exercised (game and segment FKs etc.) and the emu
backend matches the EmuBackend Protocol verbatim.
"""
import pytest
from tests.conftest import FakeEmuBackend

from spinlab.db import Database
from spinlab.errors import PracticeActiveError, ReferenceActiveError
from spinlab.models import Mode, Status
from spinlab.protocol import (
    LevelEntranceEvent,
    ReplayCmd,
    ReplayErrorEvent,
    ReplayFinishedEvent,
)
from spinlab.session_manager import SessionManager


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "replay.db")
    d.upsert_game("abcdef0123456789", "Test Game", "any%")
    return d


@pytest.fixture
def emu():
    return FakeEmuBackend(connected=True)


def _make_sm(db, emu, tmp_path) -> SessionManager:
    sm = SessionManager(
        db=db, emu=emu, rom_dir=tmp_path,
        default_category="any%", data_dir=tmp_path,
    )
    sm.game_id = "abcdef0123456789"
    sm.game_name = "Test Game"
    return sm


class TestStartReplay:
    async def test_sends_replay_command(self, db, emu, tmp_path):
        sm = _make_sm(db, emu, tmp_path)

        result = await sm.start_replay("/data/test.spinrec", speed=0)
        assert result.status == Status.STARTED
        assert sm.mode == Mode.REPLAY

        replay_cmds = [c for c in emu.sent_commands if isinstance(c, ReplayCmd)]
        assert len(replay_cmds) == 1
        assert replay_cmds[0].path == "/data/test.spinrec"
        assert replay_cmds[0].speed == 0

    async def test_rejects_during_practice(self, db, emu, tmp_path):
        sm = _make_sm(db, emu, tmp_path)
        sm.mode = Mode.PRACTICE

        with pytest.raises(PracticeActiveError):
            await sm.start_replay("/data/test.spinrec")

    async def test_rejects_during_reference(self, db, emu, tmp_path):
        sm = _make_sm(db, emu, tmp_path)
        sm.mode = Mode.REFERENCE

        with pytest.raises(ReferenceActiveError):
            await sm.start_replay("/data/test.spinrec")


class TestReplayEvents:
    async def test_replay_finished_returns_to_idle(self, db, emu, tmp_path):
        sm = _make_sm(db, emu, tmp_path)
        sm.mode = Mode.REPLAY

        await sm.route_event(ReplayFinishedEvent())
        assert sm.mode == Mode.IDLE

    async def test_replay_error_returns_to_idle(self, db, emu, tmp_path):
        sm = _make_sm(db, emu, tmp_path)
        sm.mode = Mode.REPLAY

        await sm.route_event(ReplayErrorEvent(message="game_id mismatch"))
        assert sm.mode == Mode.IDLE

    async def test_replay_events_still_capture_segments(self, db, emu, tmp_path):
        """Events with source=replay still flow through reference capture pipeline."""
        sm = _make_sm(db, emu, tmp_path)
        sm.mode = Mode.REPLAY
        sm.capture.recorder.capture_run_id = "replay_test123"

        await sm.route_event(LevelEntranceEvent(
            level=0x105, room=0, frame=100, state_path="/data/test.mss",
        ))
        assert sm.capture.recorder.pending_start is not None

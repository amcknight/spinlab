"""Tests for SessionManager state machine logic.

Keeps: mode transition guards, event routing, reference capture state machine.
Removed: mock-wiring-only tests covered by dashboard integration tests.

Uses real Database + FakeEmuBackend throughout so the schema and
EmuBackend Protocol are actually exercised — `mock_db` and `mock_emu`
fixtures from conftest are no longer used here.
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from tests.conftest import FakeEmuBackend

from spinlab import session_manager as session_manager_module
from spinlab.db import Database
from spinlab.errors import (
    AlreadyRunningError,
    DraftPendingError,
    MissingSaveStatesError,
    NotConnectedError,
    NotRunningError,
)
from spinlab.models import Mode, Status
from spinlab.protocol import (
    CheckpointEvent,
    DeathEvent,
    GameContextEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    RomInfoEvent,
    SpawnEvent,
)
from spinlab.session_manager import SessionManager


@pytest.fixture
def db(tmp_path):
    """Real SQLite Database — game pre-inserted for test_session_manager tests."""
    d = Database(tmp_path / "sm.db")
    d.upsert_game("game1", "Test Game", "any%")
    return d


@pytest.fixture
def emu():
    """FakeEmuBackend (real Protocol shape, records sent commands)."""
    return FakeEmuBackend(connected=True)


def make_sm(db, emu, **kwargs):
    defaults = dict(db=db, emu=emu, rom_dir=None, default_category="any%")
    defaults.update(kwargs)
    return SessionManager(**defaults)


class TestInit:
    def test_initial_state(self, db, emu):
        sm = make_sm(db, emu)
        assert sm.mode == Mode.IDLE
        assert sm.game_id is None
        assert sm.scheduler is None
        assert sm.practice_session is None


class TestEventRouting:
    async def test_rom_info_discovers_game(self, db, emu, tmp_path):
        rom_file = tmp_path / "test_hack.sfc"
        rom_file.write_bytes(b"\x00" * 1024)

        sm = make_sm(db, emu, rom_dir=tmp_path)
        await sm.route_event(RomInfoEvent(filename="test_hack.sfc"))
        assert sm.game_id is not None
        assert sm.game_name is not None

    async def test_rom_info_no_rom_dir(self, db, emu):
        sm = make_sm(db, emu)
        await sm.route_event(RomInfoEvent(filename="test.sfc"))
        assert sm.game_id is None

    async def test_game_context_switches_game(self, db, emu):
        sm = make_sm(db, emu)
        await sm.route_event(GameContextEvent(game_id="abc123", game_name="Test Game"))
        assert sm.game_id == "abc123"
        assert sm.game_name == "Test Game"

    async def test_events_ignored_outside_reference(self, db, emu):
        sm = make_sm(db, emu)
        sm.game_id = "game1"
        sm.mode = Mode.IDLE

        await sm.route_event(LevelEntranceEvent(level=1, room=0))
        await sm.route_event(LevelExitEvent(level=1, room=0, goal="normal"))
        assert sm.capture.recorder.pending_start is None
        # No segments inserted while outside REFERENCE mode.
        count = db.conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
        assert count == 0


class TestModeGuards:
    async def test_start_reference_during_practice(self, db, emu):
        from spinlab.errors import PracticeActiveError
        sm = make_sm(db, emu)
        sm.game_id = "game1"
        sm.mode = Mode.PRACTICE
        with pytest.raises(PracticeActiveError):
            await sm.start_reference()

    async def test_on_practice_done_sets_idle(self, db, emu):
        sm = make_sm(db, emu)
        sm.mode = Mode.PRACTICE
        sm._on_practice_done(MagicMock())
        assert sm.mode == Mode.IDLE


def _segment_rows(db) -> list[dict]:
    """Helper: read all segment rows ordered by id."""
    rows = db.conn.execute(
        "SELECT id, level_number, start_type, end_type, end_ordinal FROM segments ORDER BY rowid"
    ).fetchall()
    cols = ["id", "level_number", "start_type", "end_type", "end_ordinal"]
    return [dict(zip(cols, row)) for row in rows]


class TestReferenceCapture:
    async def test_entrance_buffered(self, db, emu):
        sm = make_sm(db, emu)
        sm.game_id = "game1"
        sm.mode = Mode.REFERENCE
        sm.capture.recorder.capture_run_id = "run1"

        await sm.route_event(LevelEntranceEvent(
            level=105, room=0, state_path="/path/to/state.mss",
        ))
        assert sm.capture.recorder.pending_start is not None
        assert sm.capture.recorder.pending_start.level_num == 105

    async def test_exit_pairs_with_entrance(self, db, emu):
        sm = make_sm(db, emu)
        sm.game_id = "game1"
        await sm.start_reference()  # sets up capture_run row for FK

        await sm.route_event(LevelEntranceEvent(
            level=105, room=0, state_path="/path/to/state.mss",
        ))
        await sm.route_event(LevelExitEvent(
            level=105, room=0, goal="normal", elapsed_ms=5000,
        ))
        assert len(_segment_rows(db)) == 1

    async def test_exit_pairs_across_rooms(self, db, emu):
        sm = make_sm(db, emu)
        sm.game_id = "game1"
        await sm.start_reference()  # sets up capture_run row for FK

        await sm.route_event(LevelEntranceEvent(
            level=105, room=0, state_path="/path/to/state.mss",
        ))
        await sm.route_event(LevelExitEvent(
            level=105, room=5, goal="normal", elapsed_ms=8000,
        ))
        rows = _segment_rows(db)
        assert len(rows) == 1
        assert rows[0]["level_number"] == 105

    async def test_abort_discards_entrance(self, db, emu):
        sm = make_sm(db, emu)
        sm.game_id = "game1"
        sm.mode = Mode.REFERENCE

        await sm.route_event(LevelEntranceEvent(level=105, room=0))
        await sm.route_event(LevelExitEvent(level=105, room=0, goal="abort"))
        assert _segment_rows(db) == []

    async def test_checkpoint_creates_segment(self, db, emu):
        sm = make_sm(db, emu)
        sm.game_id = "game1"
        await sm.start_reference()

        await sm.route_event(LevelEntranceEvent(
            level=105, room=0, state_path="/states/105_entrance.mss",
        ))
        await sm.route_event(CheckpointEvent(
            level_num=105, cp_type="midway", cp_ordinal=1,
            timestamp_ms=5000, state_path="/states/105_cp1_hot.mss",
        ))

        rows = _segment_rows(db)
        assert len(rows) == 1
        assert rows[0]["start_type"] == "entrance"
        assert rows[0]["end_type"] == "checkpoint"
        assert rows[0]["end_ordinal"] == 1
        assert sm.capture.recorder.pending_start.type == "checkpoint"

    async def test_checkpoint_then_exit_creates_two_segments(self, db, emu):
        sm = make_sm(db, emu)
        sm.game_id = "game1"
        await sm.start_reference()

        await sm.route_event(LevelEntranceEvent(
            level=105, room=0, state_path="/states/105_entrance.mss",
        ))
        await sm.route_event(CheckpointEvent(
            level_num=105, cp_type="midway", cp_ordinal=1,
            timestamp_ms=5000, state_path="/states/105_cp1_hot.mss",
        ))
        await sm.route_event(LevelExitEvent(
            level=105, room=0, goal="normal", elapsed_ms=10000,
        ))

        rows = _segment_rows(db)
        assert len(rows) == 2
        assert rows[1]["start_type"] == "checkpoint"
        assert rows[1]["end_type"] == "goal"

    async def test_death_sets_ref_died(self, db, emu):
        sm = make_sm(db, emu)
        sm.game_id = "game1"
        sm.mode = Mode.REFERENCE
        sm.capture.recorder.capture_run_id = "run1"  # arm recorder so is_recording=True
        await sm.route_event(DeathEvent())
        assert sm.capture.recorder.died is True

    async def test_entrance_clears_ref_died(self, db, emu):
        sm = make_sm(db, emu)
        sm.game_id = "game1"
        sm.mode = Mode.REFERENCE
        sm.capture.recorder.capture_run_id = "run1"
        sm.capture.recorder.died = True

        await sm.route_event(LevelEntranceEvent(
            level=105, room=0, state_path="/states/105.mss",
        ))
        assert sm.capture.recorder.died is False


class TestFillGap:
    async def test_fill_gap_loads_hot_and_captures_cold(self, practice_db, emu):
        from spinlab.models import WaypointSaveState
        sm = make_sm(practice_db, emu)
        sm.game_id = "g"
        sm.capture.recorder.capture_run_id = "run1"

        seg_id = practice_db._test_seg_id

        # Fill-gap is the "I have hot but not cold" recovery flow. The
        # practice_db fixture only writes the conventional cold variant for
        # entrance segments; seed a hot variant explicitly so this test
        # exercises the actual fill-gap path.
        seg = practice_db.get_segment_by_id(seg_id)
        practice_db.add_save_state(WaypointSaveState(
            waypoint_id=seg.start_waypoint_id, variant_type="hot",
            state_path=str(practice_db._test_state_file),
        ))

        result = await sm.start_fill_gap(seg_id)
        assert result.status == Status.STARTED

        # Verify hot state was sent to emulator
        fill_cmds = [c for c in emu.sent_commands if hasattr(c, "state_path")]
        assert len(fill_cmds) == 1
        assert str(practice_db._test_state_file) in fill_cmds[0].state_path

        await sm.route_event(SpawnEvent(
            level_num=1, is_cold_cp=True, cp_ordinal=0,
            timestamp_ms=1000,
            state_path="/cold.mss",
        ))

        # Verify cold save state was persisted in DB
        seg_row = practice_db.conn.execute(
            "SELECT start_waypoint_id FROM segments WHERE id = ?", (seg_id,)
        ).fetchone()
        cold = practice_db.get_save_state(seg_row[0], "cold")
        assert cold is not None
        assert cold.state_path == "/cold.mss"
        assert sm.fill_gap.is_active is False
        assert sm.mode == Mode.IDLE


def _seed_paused_run_with_hot_segments(
    db, tmp_path, game_id, run_id, n_segments,
):
    """Insert a paused capture_run + n segments with hot save states (no cold).

    Matches the "segments_missing_cold" SQL query: a segment is "missing cold"
    when it has a hot variant on its start waypoint but no cold variant.
    """
    from tests.conftest import make_seg_with_state
    db.create_capture_run(run_id, game_id, "Paused", kind="live")
    for i in range(n_segments):
        hot_file = tmp_path / f"hot{i}.mss"
        hot_file.write_bytes(b"")
        make_seg_with_state(
            db, game_id, level=105 + i,
            start_type="checkpoint", end_type="checkpoint",
            state_path=hot_file, ordinal=i + 1,
        )


class TestColdFill:
    async def test_save_draft_triggers_cold_fill(self, db, emu, tmp_path):
        sm = make_sm(db, emu)
        sm.game_id = "game1"

        # Real paused run + 2 real segments with hot state but no cold —
        # the actual "segments missing cold" scenario the cold-fill flow detects.
        _seed_paused_run_with_hot_segments(db, tmp_path, "game1", "run1", n_segments=2)
        sm.capture.paused_run_id = "run1"

        result = await sm.finalize_run("Test")
        assert result.status == Status.OK
        assert sm.mode == Mode.COLD_FILL

    async def test_save_draft_no_gaps_stays_idle(self, db, emu, tmp_path):
        sm = make_sm(db, emu)
        sm.game_id = "game1"

        # Paused run with zero segments missing cold → finalize stays IDLE.
        db.create_capture_run("run1", "game1", "Paused", kind="live")
        sm.capture.paused_run_id = "run1"

        result = await sm.finalize_run("Test")
        assert result.status == Status.OK
        assert sm.mode == Mode.IDLE

    async def test_cold_fill_spawn_routes_correctly(self, db, emu):
        sm = make_sm(db, emu)
        sm.game_id = "game1"
        sm.mode = Mode.COLD_FILL
        sm.cold_fill.current = "seg1"
        sm.cold_fill.queue = [
            {"segment_id": "seg1", "hot_state_path": "/hot1.mss",
             "level_number": 105, "start_type": "checkpoint", "start_ordinal": 1,
             "end_type": "goal", "end_ordinal": 0, "description": ""},
        ]
        sm.cold_fill.total = 1

        await sm.route_event(SpawnEvent(
            state_path="/cold1.mss",
        ))
        assert sm.mode == Mode.IDLE

    async def test_cold_fill_completion_sends_reset_command(self, db, emu):
        # When the last cold state is captured we want the emulator power-
        # cycled back to the title screen, not left mid-respawn in whatever
        # level the final capture happened in.
        from spinlab.protocol import ResetCmd

        sm = make_sm(db, emu)
        sm.game_id = "game1"
        sm.mode = Mode.COLD_FILL
        sm.cold_fill.current = "seg1"
        sm.cold_fill.queue = [
            {"segment_id": "seg1", "hot_state_path": "/hot1.mss",
             "level_number": 105, "start_type": "checkpoint", "start_ordinal": 1,
             "end_type": "goal", "end_ordinal": 0, "description": ""},
        ]
        sm.cold_fill.total = 1

        await sm.route_event(SpawnEvent(
            state_path="/cold1.mss",
        ))
        assert sm.mode == Mode.IDLE
        sent_resets = [c for c in emu.sent_commands if isinstance(c, ResetCmd)]
        assert len(sent_resets) == 1, f"expected one ResetCmd, got: {emu.sent_commands}"

    async def test_disconnect_during_cold_fill_returns_idle(self, db, emu):
        sm = make_sm(db, emu)
        sm.game_id = "game1"
        sm.mode = Mode.COLD_FILL
        sm.cold_fill.current = "seg1"
        sm.cold_fill.queue = [{"segment_id": "seg1"}]
        sm.cold_fill.total = 1

        sm.on_disconnect()
        assert sm.mode == Mode.IDLE
        assert sm.cold_fill.current is None
        assert sm.cold_fill.queue == []

    async def test_cold_fill_state_in_get_state(self, db, emu):
        sm = make_sm(db, emu)
        sm.game_id = "game1"
        sm.mode = Mode.COLD_FILL
        sm.cold_fill.current = "seg1"
        sm.cold_fill.queue = [
            {"segment_id": "seg1", "hot_state_path": "/hot1.mss",
             "level_number": 105, "start_type": "checkpoint", "start_ordinal": 1,
             "end_type": "goal", "end_ordinal": 0, "description": ""},
        ]
        sm.cold_fill.total = 2

        state = sm.get_state()
        assert state["mode"] == "cold_fill"
        assert "cold_fill" in state
        assert state["cold_fill"]["total"] == 2


@pytest.fixture
def session_manager_with_practice(db, emu):
    """A SessionManager in PRACTICE mode with a stubbed PracticeSession."""
    db.upsert_game("g", "Game", "any%")
    sm = make_sm(db, emu)
    sm.game_id = "g"
    sm.game_name = "Game"
    sm.mode = Mode.PRACTICE
    ps = MagicMock()
    ps.session_id = "sess"
    ps.started_at = "2026-04-05T00:00:00Z"
    ps.segments_attempted = 0
    ps.segments_completed = 0
    ps.current_segment_id = None
    ps.initial_expected_total_ms = None
    ps.initial_expected_clean_ms = None
    ps.current_expected_times = lambda: (None, None)
    sm.practice_session = ps
    return sm


def test_practice_state_emits_saved_ms(session_manager_with_practice):
    """get_state() emits saved_total_ms and saved_clean_ms on session."""
    sm = session_manager_with_practice
    sm.practice_session.initial_expected_total_ms = 10000.0
    sm.practice_session.initial_expected_clean_ms = 8000.0
    sm.practice_session.current_expected_times = lambda: (7500.0, 6500.0)

    state = sm.get_state()
    assert state["session"]["saved_total_ms"] == 2500.0
    assert state["session"]["saved_clean_ms"] == 1500.0


def test_practice_state_saved_ms_null_when_no_snapshot(session_manager_with_practice):
    """If initial snapshot is None, savings fields are None."""
    sm = session_manager_with_practice
    sm.practice_session.initial_expected_total_ms = None
    sm.practice_session.initial_expected_clean_ms = None
    sm.practice_session.current_expected_times = lambda: (None, None)

    state = sm.get_state()
    assert state["session"]["saved_total_ms"] is None
    assert state["session"]["saved_clean_ms"] is None


class TestColdFillMode:
    def test_cold_fill_mode_exists(self):
        assert Mode.COLD_FILL.value == "cold_fill"

    def test_idle_to_cold_fill_legal(self):
        from spinlab.models import transition_mode
        result = transition_mode(Mode.IDLE, Mode.COLD_FILL)
        assert result == Mode.COLD_FILL

    def test_cold_fill_to_idle_legal(self):
        from spinlab.models import transition_mode
        result = transition_mode(Mode.COLD_FILL, Mode.IDLE)
        assert result == Mode.IDLE

    def test_cold_fill_to_practice_illegal(self):
        from spinlab.models import transition_mode
        with pytest.raises(ValueError):
            transition_mode(Mode.COLD_FILL, Mode.PRACTICE)


class TestPracticeLifecycle:
    """Tests for start_practice, stop_practice, and _on_practice_done."""

    async def test_start_practice_blocked_by_draft(self, db, emu):
        sm = make_sm(db, emu)
        sm.game_id = "game1"
        sm.capture.paused_run_id = "fake_paused_run"
        with pytest.raises(DraftPendingError):
            await sm.start_practice()

    async def test_start_practice_blocked_by_not_connected(self, db, emu):
        sm = make_sm(db, emu)
        sm.game_id = "game1"
        emu.is_connected = False
        with pytest.raises(NotConnectedError):
            await sm.start_practice()

    async def test_start_practice_blocked_when_already_running(self, db, emu):
        sm = make_sm(db, emu)
        sm.game_id = "game1"
        fake_ps = MagicMock()
        fake_ps.is_running = True
        sm.practice_session = fake_ps
        with pytest.raises(AlreadyRunningError):
            await sm.start_practice()

    async def test_stop_practice_when_not_running(self, db, emu):
        sm = make_sm(db, emu)
        with pytest.raises(NotRunningError):
            await sm.stop_practice()

    async def test_stop_practice_clears_stale_mode(self, db, emu):
        """If mode=PRACTICE but no session, stop_practice should still reset."""
        sm = make_sm(db, emu)
        sm.mode = Mode.PRACTICE
        result = await sm.stop_practice()
        assert result.status == Status.STOPPED
        assert sm.mode == Mode.IDLE

    async def test_stop_practice_cancels_hung_task(self, db, emu, monkeypatch):
        """When practice task doesn't exit on is_running=False, stop should cancel
        after the timeout elapses."""
        monkeypatch.setattr(session_manager_module, "PRACTICE_STOP_TIMEOUT_S", 0.1)

        sm = make_sm(db, emu)
        sm.game_id = "game1"
        sm.mode = Mode.PRACTICE

        fake_ps = MagicMock()
        fake_ps.is_running = True
        sm.practice_session = fake_ps

        async def hung_loop():
            await asyncio.sleep(10)

        sm.practice_task = asyncio.create_task(hung_loop())

        result = await sm.stop_practice()

        assert result.status == Status.STOPPED
        assert sm.mode == Mode.IDLE
        assert sm.practice_task.cancelled() or sm.practice_task.done()


class TestSpeedRunLifecycle:
    """Tests for start_speed_run, stop_speed_run, and _on_speed_run_done."""

    async def test_start_blocked_by_draft(self, db, emu):
        sm = make_sm(db, emu)
        sm.game_id = "game1"
        sm.capture.paused_run_id = "fake_paused_run"
        with pytest.raises(DraftPendingError):
            await sm.start_speed_run()

    async def test_start_blocked_by_not_connected(self, db, emu):
        sm = make_sm(db, emu)
        sm.game_id = "game1"
        emu.is_connected = False
        with pytest.raises(NotConnectedError):
            await sm.start_speed_run()

    async def test_start_missing_save_states(self, db, emu, tmp_path):
        """SpeedRunSession.__init__ raises ValueError when a segment
        has no save_state path on disk; start_speed_run translates that to
        MissingSaveStatesError."""
        from tests.conftest import make_seg_with_state
        # Real segment + waypoint, but the hot save state path points at a
        # file that doesn't exist on disk — exactly the missing-state scenario.
        fake_state = tmp_path / "definitely-not-a-real-path.mss"
        # NOTE: do NOT create the file — the test wants it to be missing.
        make_seg_with_state(db, "game1", 1, "entrance", "goal", fake_state)

        sm = make_sm(db, emu)
        sm.game_id = "game1"
        with pytest.raises(MissingSaveStatesError):
            await sm.start_speed_run()

    async def test_stop_when_not_running(self, db, emu):
        sm = make_sm(db, emu)
        with pytest.raises(NotRunningError):
            await sm.stop_speed_run()

    async def test_stop_clears_stale_mode(self, db, emu):
        sm = make_sm(db, emu)
        sm.mode = Mode.SPEED_RUN
        result = await sm.stop_speed_run()
        assert result.status == Status.STOPPED
        assert sm.mode == Mode.IDLE


class TestDisconnectAndShutdown:
    def test_on_disconnect_stops_practice(self, db, emu):
        sm = make_sm(db, emu)
        fake_ps = MagicMock()
        fake_ps.is_running = True
        sm.practice_session = fake_ps
        sm.on_disconnect()
        assert fake_ps.is_running is False

    def test_on_disconnect_stops_speed_run(self, db, emu):
        sm = make_sm(db, emu)
        fake_sr = MagicMock()
        fake_sr.is_running = True
        sm.speed_run_session = fake_sr
        sm.on_disconnect()
        assert fake_sr.is_running is False

    def test_on_disconnect_clears_ref_and_idles(self, db, emu):
        sm = make_sm(db, emu)
        sm.mode = Mode.REFERENCE
        sm.on_disconnect()
        assert sm.mode == Mode.IDLE

    async def test_shutdown_stops_practice_and_emu(self, db, emu):
        sm = make_sm(db, emu)
        sm.mode = Mode.PRACTICE  # stale — no real session
        await sm.shutdown()
        # FakeEmuBackend.disconnect flips is_connected to False.
        assert emu.is_connected is False

    async def test_shutdown_clears_reference(self, db, emu):
        sm = make_sm(db, emu)
        sm.mode = Mode.REFERENCE
        await sm.shutdown()
        assert sm.mode == Mode.IDLE
        assert emu.is_connected is False


async def test_capture_event_routed_while_recording_even_if_mode_lags(db, emu):
    """The race C3 fixes: between capture.start_replay setting is_recording=True
    and the SessionManager's mode flipping to REPLAY, the poller can emit a
    LevelEntranceEvent. The handler must route it to capture based on the
    recording flag, not the mode."""
    sm = make_sm(db, emu)
    sm.game_id = "game1"
    sm.game_name = "Test Game"

    # Simulate the in-flight state: capture has been told to start replay
    # (recorder is armed with a run id), but mode hasn't flipped yet.
    sm.capture._enter_recording("replay_test123", "sess_test123")
    sm.mode = Mode.IDLE  # mode lags

    assert sm.capture.is_recording

    # Spy on the handler.
    handle_entrance_calls = []

    async def _spy(ev):
        handle_entrance_calls.append(ev)

    sm.capture.handle_entrance = _spy  # type: ignore[method-assign]

    await sm.route_event(LevelEntranceEvent(level=1, room=0))

    assert len(handle_entrance_calls) == 1, (
        "LevelEntranceEvent must be routed to capture while is_recording=True, "
        "regardless of mode"
    )

"""Tests for ColdFillController cold-fill flow.

Uses a real Database so the controller's raw SQL queries
(start_waypoint_id lookup, segments_missing_cold) hit real rows
instead of mock cursor chains.
"""
import pytest

from spinlab.capture import ColdFillController
from spinlab.db import Database
from spinlab.errors import NotConnectedError
from spinlab.models import Mode, Segment, Status, Waypoint, WaypointSaveState
from spinlab.protocol import ColdFillLoadCmd, SpawnEvent


@pytest.fixture
def emu(mock_emu):
    """Alias `emu` to conftest's `mock_emu` — preserves param name in this file."""
    return mock_emu


@pytest.fixture
def cold_fill_db(tmp_path):
    """Real DB with 2 checkpoint segments that have hot but no cold save states.

    segments_missing_cold("g") will return both segments.
    """
    db = Database(tmp_path / "cold_fill.db")
    db.upsert_game("g", "Game", "any%")

    # Create waypoints for 2 checkpoint segments within level 105
    wp_entrance = Waypoint.make("g", 105, "entrance", 0, {})
    wp_cp1 = Waypoint.make("g", 105, "checkpoint", 1, {})
    wp_cp2 = Waypoint.make("g", 105, "checkpoint", 2, {})
    wp_goal = Waypoint.make("g", 105, "goal", 0, {})
    for wp in (wp_entrance, wp_cp1, wp_cp2, wp_goal):
        db.upsert_waypoint(wp)

    # Segment 1: cp1 → cp2
    seg1_id = Segment.make_id("g", 105, "checkpoint", 1, "checkpoint", 2,
                              wp_cp1.id, wp_cp2.id)
    db.upsert_segment(Segment(
        id=seg1_id, game_id="g", level_number=105,
        start_type="checkpoint", start_ordinal=1,
        end_type="checkpoint", end_ordinal=2,
        description="", ordinal=1,
        start_waypoint_id=wp_cp1.id, end_waypoint_id=wp_cp2.id,
    ))

    # Segment 2: cp2 → goal
    seg2_id = Segment.make_id("g", 105, "checkpoint", 2, "goal", 0,
                              wp_cp2.id, wp_goal.id)
    db.upsert_segment(Segment(
        id=seg2_id, game_id="g", level_number=105,
        start_type="checkpoint", start_ordinal=2,
        end_type="goal", end_ordinal=0,
        description="", ordinal=2,
        start_waypoint_id=wp_cp2.id, end_waypoint_id=wp_goal.id,
    ))

    # Hot save states for each start waypoint (no cold → segments_missing_cold returns them)
    hot1 = tmp_path / "hot1.mss"
    hot1.write_bytes(b"fake hot 1")
    db.add_save_state(WaypointSaveState(
        waypoint_id=wp_cp1.id, variant_type="hot",
        state_path=str(hot1),
    ))

    hot2 = tmp_path / "hot2.mss"
    hot2.write_bytes(b"fake hot 2")
    db.add_save_state(WaypointSaveState(
        waypoint_id=wp_cp2.id, variant_type="hot",
        state_path=str(hot2),
    ))

    db._seg1_id = seg1_id
    db._seg2_id = seg2_id
    db._wp_cp1_id = wp_cp1.id
    db._wp_cp2_id = wp_cp2.id
    db._hot1_path = str(hot1)
    db._hot2_path = str(hot2)
    return db


class TestStartColdFill:
    async def test_start_cold_fill_sends_first_segment(self, emu, cold_fill_db):
        cc = ColdFillController(cold_fill_db, emu)
        result = await cc.start("g")

        assert result.status == Status.STARTED
        assert result.new_mode == Mode.COLD_FILL

        # Verify command sent for first segment
        cmd = emu.send_command.call_args[0][0]
        assert isinstance(cmd, ColdFillLoadCmd)
        assert cmd.state_path == cold_fill_db._hot1_path
        assert cmd.segment_id == cold_fill_db._seg1_id

    async def test_start_cold_fill_no_gaps(self, emu, cold_fill_db):
        # Add cold save states so there are no gaps
        cold_fill_db.add_save_state(WaypointSaveState(
            waypoint_id=cold_fill_db._wp_cp1_id, variant_type="cold",
            state_path="/cold1.mss",
        ))
        cold_fill_db.add_save_state(WaypointSaveState(
            waypoint_id=cold_fill_db._wp_cp2_id, variant_type="cold",
            state_path="/cold2.mss",
        ))
        cc = ColdFillController(cold_fill_db, emu)
        result = await cc.start("g")
        assert result.status == Status.NO_GAPS

    async def test_start_cold_fill_not_connected(self, emu, cold_fill_db):
        emu.is_connected = False
        cc = ColdFillController(cold_fill_db, emu)
        with pytest.raises(NotConnectedError):
            await cc.start("g")


class TestHandleColdFillSpawn:
    async def test_stores_cold_save_state_and_advances(self, emu, cold_fill_db):
        cc = ColdFillController(cold_fill_db, emu)
        await cc.start("g")

        # Simulate spawn event for first segment
        done = await cc.handle_spawn(
            SpawnEvent(state_path="/cold1.mss"),
        )
        assert done is False  # still have one more

        # Backend save_state was called so the file gets written for the
        # cold-fill segment (was the orchestrator's job under the old hook).
        emu.save_state.assert_awaited_with(cold_fill_db._seg1_id)

        # Verify cold save state stored in DB
        cold = cold_fill_db.get_save_state(cold_fill_db._wp_cp1_id, "cold")
        assert cold is not None
        assert cold.variant_type == "cold"
        assert cold.state_path == "/cold1.mss"

        # Verify second segment loaded
        cmd = emu.send_command.call_args[0][0]
        assert isinstance(cmd, ColdFillLoadCmd)
        assert cmd.segment_id == cold_fill_db._seg2_id

    async def test_returns_true_when_queue_empty(self, emu, cold_fill_db):
        cc = ColdFillController(cold_fill_db, emu)
        await cc.start("g")

        # Process both segments
        await cc.handle_spawn(
            SpawnEvent(state_path="/cold1.mss"),
        )
        done = await cc.handle_spawn(
            SpawnEvent(state_path="/cold2.mss"),
        )
        assert done is True

    async def test_ignores_spawn_without_state(self, emu, cold_fill_db):
        cc = ColdFillController(cold_fill_db, emu)
        await cc.start("g")

        done = await cc.handle_spawn(
            SpawnEvent(state_path=None),
        )
        assert done is False
        # Queue unchanged — still on first segment
        assert cc.current == cold_fill_db._seg1_id


class TestGetColdFillState:
    async def test_returns_none_before_start(self, emu, cold_fill_db):
        cc = ColdFillController(cold_fill_db, emu)
        assert cc.get_state() is None

    async def test_returns_progress_mid_fill(self, emu, cold_fill_db):
        cc = ColdFillController(cold_fill_db, emu)
        await cc.start("g")

        state = cc.get_state()
        assert state["current"] == 1
        assert state["total"] == 2
        assert state["segment_label"] == "L105 cp1 > cp2"

    async def test_progress_advances(self, emu, cold_fill_db):
        cc = ColdFillController(cold_fill_db, emu)
        await cc.start("g")

        await cc.handle_spawn(
            SpawnEvent(state_path="/cold1.mss"),
        )
        state = cc.get_state()
        assert state["current"] == 2
        assert state["total"] == 2
        assert state["segment_label"] == "L105 cp2 > goal"

    async def test_returns_none_after_complete(self, emu, cold_fill_db):
        cc = ColdFillController(cold_fill_db, emu)
        await cc.start("g")
        await cc.handle_spawn(
            SpawnEvent(state_path="/cold1.mss"),
        )
        await cc.handle_spawn(
            SpawnEvent(state_path="/cold2.mss"),
        )
        assert cc.get_state() is None

    async def test_uses_description_when_present(self, emu, cold_fill_db):
        # Update segment description in DB
        cold_fill_db.update_segment(cold_fill_db._seg1_id, description="My Custom Name")

        cc = ColdFillController(cold_fill_db, emu)
        await cc.start("g")
        state = cc.get_state()
        assert state["segment_label"] == "My Custom Name"


class TestStartRunId:
    async def test_start_passes_run_id_to_query(self, emu, cold_fill_db, monkeypatch):
        cc = ColdFillController(cold_fill_db, emu)
        seen: dict = {}

        def fake_missing(game_id, run_id=None):
            seen["game_id"] = game_id
            seen["run_id"] = run_id
            return []  # no gaps → returns early

        monkeypatch.setattr(cc.db, "segments_missing_cold", fake_missing)
        await cc.start("g1", run_id="rA")
        assert seen == {"game_id": "g1", "run_id": "rA"}

    async def test_start_run_id_defaults_to_none(self, emu, cold_fill_db, monkeypatch):
        cc = ColdFillController(cold_fill_db, emu)
        seen: dict = {}

        def fake_missing(game_id, run_id=None):
            seen["game_id"] = game_id
            seen["run_id"] = run_id
            return []

        monkeypatch.setattr(cc.db, "segments_missing_cold", fake_missing)
        await cc.start("g1")
        assert seen == {"game_id": "g1", "run_id": None}


class TestSkipAndAbort:
    async def test_skip_when_already_idle(self, emu, cold_fill_db):
        """skip() on an idle controller (empty queue) returns STOPPED/IDLE."""
        cc = ColdFillController(cold_fill_db, emu)
        result = await cc.skip()
        assert result.status == Status.STOPPED
        assert result.new_mode == Mode.IDLE
        assert cc.current is None
        assert cc.cold_waypoint_id is None

    async def test_skip_advances_to_next_segment(self, emu, cold_fill_db):
        """skip() pops the current segment and loads the next one."""
        cc = ColdFillController(cold_fill_db, emu)
        # Build a queue using the real DB rows and real temp files so _load_next
        # can find the hot state file on disk.
        cc.queue = [
            {
                "segment_id": cold_fill_db._seg1_id,
                "hot_state_path": cold_fill_db._hot1_path,
                "start_type": "checkpoint", "start_ordinal": 1,
                "end_type": "checkpoint", "end_ordinal": 2,
                "level_number": 105, "description": "",
            },
            {
                "segment_id": cold_fill_db._seg2_id,
                "hot_state_path": cold_fill_db._hot2_path,
                "start_type": "checkpoint", "start_ordinal": 2,
                "end_type": "goal", "end_ordinal": 0,
                "level_number": 105, "description": "",
            },
        ]
        cc.total = 2
        cc.current = cold_fill_db._seg1_id

        result = await cc.skip()

        assert result.status == Status.STARTED
        assert result.new_mode == Mode.COLD_FILL
        assert cc.current == cold_fill_db._seg2_id
        assert len(cc.queue) == 1
        assert cc.queue[0]["segment_id"] == cold_fill_db._seg2_id

    async def test_skip_last_segment_drains_to_idle(self, emu, cold_fill_db):
        """skip() on the last item drains the queue and returns IDLE."""
        cc = ColdFillController(cold_fill_db, emu)
        cc.queue = [
            {
                "segment_id": cold_fill_db._seg1_id,
                "hot_state_path": cold_fill_db._hot1_path,
                "start_type": "checkpoint", "start_ordinal": 1,
                "end_type": "checkpoint", "end_ordinal": 2,
                "level_number": 105, "description": "",
            }
        ]
        cc.total = 1
        cc.current = cold_fill_db._seg1_id

        result = await cc.skip()

        assert result.status == Status.STOPPED
        assert result.new_mode == Mode.IDLE
        assert cc.queue == []
        assert cc.current is None

    def test_abort_clears_queue(self, emu, cold_fill_db):
        """abort() resets all queue state."""
        cc = ColdFillController(cold_fill_db, emu)
        cc.queue = [
            {
                "segment_id": cold_fill_db._seg1_id,
                "hot_state_path": cold_fill_db._hot1_path,
                "start_type": "checkpoint", "start_ordinal": 1,
                "end_type": "checkpoint", "end_ordinal": 2,
                "level_number": 105, "description": "",
            }
        ]
        cc.total = 1
        cc.current = cold_fill_db._seg1_id

        cc.abort()

        assert cc.queue == []
        assert cc.current is None
        assert cc.cold_waypoint_id is None
        assert cc.total == 0


class TestColdFillSaveStateRetryCount:
    """First save_state failure should log attempt=1; second should log
    attempt=2. Success on attempt N clears the counter for that segment."""

    async def test_repeated_failures_increment_attempt_count(
        self, emu, cold_fill_db, caplog,
    ):
        import logging
        from unittest.mock import AsyncMock

        emu.save_state = AsyncMock(side_effect=RuntimeError("simulated"))

        cc = ColdFillController(cold_fill_db, emu)
        await cc.start("g")

        with caplog.at_level(logging.WARNING, logger="spinlab.capture.cold_fill"):
            await cc.handle_spawn(SpawnEvent(state_path="/cold1.mss"))
            await cc.handle_spawn(SpawnEvent(state_path="/cold1.mss"))

        msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        # log.warn formats int values via repr → "attempt=1" / "attempt=2".
        assert any("attempt=1" in m for m in msgs), f"missing attempt=1 log; got {msgs}"
        assert any("attempt=2" in m for m in msgs), f"missing attempt=2 log; got {msgs}"

    async def test_success_clears_retry_counter(self, emu, cold_fill_db):
        from unittest.mock import AsyncMock

        # Fail once, then succeed.
        emu.save_state = AsyncMock(side_effect=[RuntimeError("simulated"), None])

        cc = ColdFillController(cold_fill_db, emu)
        await cc.start("g")
        seg1_id = cc.current

        await cc.handle_spawn(SpawnEvent(state_path="/cold1.mss"))
        assert cc._save_state_attempts.get(seg1_id) == 1

        await cc.handle_spawn(SpawnEvent(state_path="/cold1.mss"))
        # Success — the counter for this segment is cleared so a future
        # cold-fill of the same segment starts fresh.
        assert seg1_id not in cc._save_state_attempts

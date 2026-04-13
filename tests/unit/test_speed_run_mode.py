"""Tests for Speed Run mode enum and transitions."""
import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from spinlab.db import Database
from spinlab.models import Mode, Segment, Waypoint, WaypointSaveState, Attempt, transition_mode


def test_speed_run_mode_exists():
    assert Mode.SPEED_RUN.value == "speed_run"


def test_idle_to_speed_run_legal():
    assert transition_mode(Mode.IDLE, Mode.SPEED_RUN) == Mode.SPEED_RUN


def test_speed_run_to_idle_legal():
    assert transition_mode(Mode.SPEED_RUN, Mode.IDLE) == Mode.IDLE


def test_speed_run_to_practice_illegal():
    with pytest.raises(ValueError):
        transition_mode(Mode.SPEED_RUN, Mode.PRACTICE)


from spinlab.protocol import (
    SpeedRunLoadCmd, SpeedRunStopCmd,
    SpeedRunCheckpointEvent, SpeedRunDeathEvent, SpeedRunCompleteEvent,
    parse_event, serialize_command,
)


def test_speed_run_load_cmd_serializes():
    cmd = SpeedRunLoadCmd(
        id="seg1",
        state_path="/entrance.mss",
        description="Level 1",
        checkpoints=[
            {"ordinal": 1, "state_path": "/cp1.mss"},
            {"ordinal": 2, "state_path": "/cp2.mss"},
        ],
        expected_time_ms=45000,
        auto_advance_delay_ms=1000,
    )
    s = serialize_command(cmd)
    assert '"event": "speed_run_load"' in s or '"event":"speed_run_load"' in s


def test_speed_run_stop_cmd_serializes():
    cmd = SpeedRunStopCmd()
    s = serialize_command(cmd)
    assert "speed_run_stop" in s


def test_parse_speed_run_checkpoint_event():
    raw = {"event": "speed_run_checkpoint", "ordinal": 1, "elapsed_ms": 12340, "split_ms": 12340}
    evt = parse_event(raw)
    assert isinstance(evt, SpeedRunCheckpointEvent)
    assert evt.ordinal == 1
    assert evt.split_ms == 12340


def test_parse_speed_run_death_event():
    raw = {"event": "speed_run_death", "elapsed_ms": 5230, "split_ms": 5230}
    evt = parse_event(raw)
    assert isinstance(evt, SpeedRunDeathEvent)
    assert evt.split_ms == 5230


def test_parse_speed_run_complete_event():
    raw = {"event": "speed_run_complete", "elapsed_ms": 45600, "split_ms": 12000}
    evt = parse_event(raw)
    assert isinstance(evt, SpeedRunCompleteEvent)
    assert evt.elapsed_ms == 45600


def _make_waypoint_and_state(db, game_id, level, ep_type, ordinal, state_path, conditions=None):
    """Create a waypoint + save state, return waypoint."""
    wp = Waypoint.make(game_id, level, ep_type, ordinal, conditions or {})
    db.upsert_waypoint(wp)
    db.add_save_state(WaypointSaveState(
        waypoint_id=wp.id, variant_type="hot",
        state_path=str(state_path), is_default=True,
    ))
    return wp


def _setup_two_level_game(tmp_path):
    """Create a game with 2 levels:
    Level 1: entrance->cp1->goal (ordinals 1, 2)
    Level 2: entrance->goal (ordinal 3)
    Returns (db, segment_ids_in_order).
    """
    db = Database(tmp_path / "sr.db")
    db.upsert_game("g", "Game", "any%")

    l1_entrance = tmp_path / "l1_entrance.mss"
    l1_cp1 = tmp_path / "l1_cp1.mss"
    l1_entrance.write_bytes(b"state")
    l1_cp1.write_bytes(b"state")

    wp_l1_entrance = _make_waypoint_and_state(db, "g", 1, "entrance", 0, l1_entrance)
    wp_l1_cp1 = _make_waypoint_and_state(db, "g", 1, "checkpoint", 1, l1_cp1)
    wp_l1_goal = Waypoint.make("g", 1, "goal", 0, {})
    db.upsert_waypoint(wp_l1_goal)

    seg1 = Segment(
        id=Segment.make_id("g", 1, "entrance", 0, "checkpoint", 1, wp_l1_entrance.id, wp_l1_cp1.id),
        game_id="g", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="checkpoint", end_ordinal=1,
        description="L1 start>cp1", ordinal=1,
        start_waypoint_id=wp_l1_entrance.id, end_waypoint_id=wp_l1_cp1.id,
    )
    seg2 = Segment(
        id=Segment.make_id("g", 1, "checkpoint", 1, "goal", 0, wp_l1_cp1.id, wp_l1_goal.id),
        game_id="g", level_number=1,
        start_type="checkpoint", start_ordinal=1,
        end_type="goal", end_ordinal=0,
        description="L1 cp1>goal", ordinal=2,
        start_waypoint_id=wp_l1_cp1.id, end_waypoint_id=wp_l1_goal.id,
    )
    db.upsert_segment(seg1)
    db.upsert_segment(seg2)

    l2_entrance = tmp_path / "l2_entrance.mss"
    l2_entrance.write_bytes(b"state")

    wp_l2_entrance = _make_waypoint_and_state(db, "g", 2, "entrance", 0, l2_entrance)
    wp_l2_goal = Waypoint.make("g", 2, "goal", 0, {})
    db.upsert_waypoint(wp_l2_goal)

    seg3 = Segment(
        id=Segment.make_id("g", 2, "entrance", 0, "goal", 0, wp_l2_entrance.id, wp_l2_goal.id),
        game_id="g", level_number=2,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        description="L2 start>goal", ordinal=3,
        start_waypoint_id=wp_l2_entrance.id, end_waypoint_id=wp_l2_goal.id,
    )
    db.upsert_segment(seg3)

    return db, [seg1.id, seg2.id, seg3.id]


@pytest.fixture
def sr_db(tmp_path):
    db, seg_ids = _setup_two_level_game(tmp_path)
    db._seg_ids = seg_ids
    db._tmp_path = tmp_path
    return db


def test_speed_run_builds_level_sequence(sr_db):
    """SpeedRunSession should group segments into levels ordered by ordinal."""
    tcp = AsyncMock()
    tcp.is_connected = True
    from spinlab.speed_run import SpeedRunSession
    sr = SpeedRunSession(tcp=tcp, db=sr_db, game_id="g")
    levels = sr.levels

    assert len(levels) == 2
    assert len(levels[0].segments) == 2
    assert len(levels[1].segments) == 1
    assert len(levels[0].checkpoints) == 1


def test_speed_run_refuses_missing_state(tmp_path):
    """SpeedRunSession should raise if any segment has no save state."""
    db = Database(tmp_path / "sr.db")
    db.upsert_game("g", "Game", "any%")

    wp_start = Waypoint.make("g", 1, "entrance", 0, {})
    wp_end = Waypoint.make("g", 1, "goal", 0, {})
    db.upsert_waypoint(wp_start)
    db.upsert_waypoint(wp_end)
    seg = Segment(
        id=Segment.make_id("g", 1, "entrance", 0, "goal", 0, wp_start.id, wp_end.id),
        game_id="g", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        ordinal=1,
        start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
    )
    db.upsert_segment(seg)

    tcp = AsyncMock()
    tcp.is_connected = True
    from spinlab.speed_run import SpeedRunSession
    with pytest.raises(ValueError, match="Missing save state"):
        SpeedRunSession(tcp=tcp, db=db, game_id="g")


@pytest.mark.asyncio
async def test_speed_run_sends_level_load(sr_db):
    """First run_one should send speed_run_load for level 1."""
    tcp = AsyncMock()
    tcp.is_connected = True

    from spinlab.speed_run import SpeedRunSession
    sr = SpeedRunSession(tcp=tcp, db=sr_db, game_id="g")
    sr.is_running = True

    async def deliver():
        await asyncio.sleep(0.05)
        sr.receive_event({
            "event": "speed_run_complete",
            "elapsed_ms": 30000,
            "split_ms": 30000,
        })

    asyncio.create_task(deliver())
    result = await sr.run_one()

    assert result is True
    tcp.send_command.assert_called_once()
    cmd = tcp.send_command.call_args[0][0]
    assert cmd.event == "speed_run_load"
    assert len(cmd.checkpoints) == 1
    assert cmd.checkpoints[0]["ordinal"] == 1


@pytest.mark.asyncio
async def test_speed_run_cold_recording_on_checkpoint(sr_db):
    """Checkpoint hit after cold start should record an attempt."""
    tcp = AsyncMock()
    tcp.is_connected = True

    from spinlab.speed_run import SpeedRunSession
    sr = SpeedRunSession(tcp=tcp, db=sr_db, game_id="g")
    sr.is_running = True

    async def deliver():
        await asyncio.sleep(0.05)
        sr.receive_event({
            "event": "speed_run_checkpoint",
            "ordinal": 1,
            "elapsed_ms": 12000,
            "split_ms": 12000,
        })
        await asyncio.sleep(0.05)
        sr.receive_event({
            "event": "speed_run_complete",
            "elapsed_ms": 30000,
            "split_ms": 18000,
        })

    asyncio.create_task(deliver())
    await sr.run_one()

    seg_ids = sr_db._seg_ids
    attempts = sr_db.get_segment_attempts(seg_ids[0])
    assert len(attempts) == 1
    assert attempts[0]["completed"] == 1
    assert attempts[0]["time_ms"] == 12000

    attempts2 = sr_db.get_segment_attempts(seg_ids[1])
    assert len(attempts2) == 0


@pytest.mark.asyncio
async def test_speed_run_death_makes_next_segment_cold(sr_db):
    """Death should mark next sub-segment as cold for recording."""
    tcp = AsyncMock()
    tcp.is_connected = True

    from spinlab.speed_run import SpeedRunSession
    sr = SpeedRunSession(tcp=tcp, db=sr_db, game_id="g")
    sr.is_running = True

    async def deliver():
        await asyncio.sleep(0.02)
        sr.receive_event({
            "event": "speed_run_checkpoint",
            "ordinal": 1,
            "elapsed_ms": 12000,
            "split_ms": 12000,
        })
        await asyncio.sleep(0.02)
        sr.receive_event({
            "event": "speed_run_death",
            "elapsed_ms": 18000,
            "split_ms": 6000,
        })
        await asyncio.sleep(0.02)
        sr.receive_event({
            "event": "speed_run_complete",
            "elapsed_ms": 40000,
            "split_ms": 15000,
        })

    asyncio.create_task(deliver())
    await sr.run_one()

    seg_ids = sr_db._seg_ids
    assert len(sr_db.get_segment_attempts(seg_ids[0])) == 1
    attempts = sr_db.get_segment_attempts(seg_ids[1])
    assert len(attempts) == 1
    assert attempts[0]["time_ms"] == 15000


@pytest.mark.asyncio
async def test_speed_run_stops_after_last_level(sr_db):
    """Session should return False after last level completes."""
    tcp = AsyncMock()
    tcp.is_connected = True

    from spinlab.speed_run import SpeedRunSession
    sr = SpeedRunSession(tcp=tcp, db=sr_db, game_id="g")
    sr.is_running = True

    async def deliver_l1():
        await asyncio.sleep(0.02)
        sr.receive_event({"event": "speed_run_complete", "elapsed_ms": 30000, "split_ms": 30000})
    asyncio.create_task(deliver_l1())
    result1 = await sr.run_one()
    assert result1 is True

    async def deliver_l2():
        await asyncio.sleep(0.02)
        sr.receive_event({"event": "speed_run_complete", "elapsed_ms": 20000, "split_ms": 20000})
    asyncio.create_task(deliver_l2())
    result2 = await sr.run_one()
    assert result2 is True

    result3 = await sr.run_one()
    assert result3 is False


from spinlab.session_manager import SessionManager
from spinlab.models import Mode, ActionResult, Status


@pytest.fixture
def session_mgr(sr_db, tmp_path):
    tcp = AsyncMock()
    tcp.is_connected = True
    tcp.send_command = AsyncMock()
    mgr = SessionManager(
        db=sr_db, tcp=tcp, rom_dir=tmp_path, data_dir=tmp_path,
    )
    mgr.game_id = "g"
    mgr.game_name = "Game"
    return mgr


@pytest.mark.asyncio
async def test_session_manager_start_speed_run(session_mgr):
    result = await session_mgr.start_speed_run()
    assert result.status == Status.STARTED
    assert session_mgr.mode == Mode.SPEED_RUN
    assert session_mgr.speed_run_session is not None


@pytest.mark.asyncio
async def test_session_manager_stop_speed_run(session_mgr):
    await session_mgr.start_speed_run()
    result = await session_mgr.stop_speed_run()
    assert result.status == Status.STOPPED
    assert session_mgr.mode == Mode.IDLE


@pytest.mark.asyncio
async def test_speed_run_routes_checkpoint_event(session_mgr):
    await session_mgr.start_speed_run()
    await session_mgr.route_event({
        "event": "speed_run_checkpoint",
        "ordinal": 1,
        "elapsed_ms": 12000,
        "split_ms": 12000,
    })
    assert session_mgr.mode == Mode.SPEED_RUN

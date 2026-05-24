"""Tests for Hyper Play mode enum and transitions."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from spinlab.db import Database
from spinlab.models import (
    Mode,
    Segment,
    Status,
    Waypoint,
    WaypointSaveState,
)
from spinlab.protocol import (
    HyperPlayCheckpointEvent,
    HyperPlayCompleteEvent,
    HyperPlayDeathEvent,
    HyperPlayLoadCmd,
)
from spinlab.session_manager import SessionManager


def _make_waypoint_and_state(db, game_id, level, ep_type, ordinal, state_path, conditions=None):
    """Create a waypoint + save state, return waypoint.

    Variant follows the production convention: entrance → cold, checkpoint → hot.
    """
    wp = Waypoint.make(game_id, level, ep_type, ordinal, conditions or {})
    db.upsert_waypoint(wp)
    variant = "cold" if ep_type == "entrance" else "hot"
    db.add_save_state(WaypointSaveState(
        waypoint_id=wp.id, variant_type=variant,
        state_path=str(state_path),
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


def test_hyper_play_builds_level_sequence(sr_db):
    """HyperPlaySession should group segments into levels ordered by ordinal."""
    emu = AsyncMock()
    emu.is_connected = True
    from spinlab.hyper_play import HyperPlaySession
    sr = HyperPlaySession(emu=emu, db=sr_db, game_id="g")
    levels = sr.levels

    assert len(levels) == 2
    assert len(levels[0].segments) == 2
    assert len(levels[1].segments) == 1
    assert len(levels[0].checkpoints) == 1


def test_hyper_play_refuses_missing_state(tmp_path):
    """HyperPlaySession should raise if any segment has no save state."""
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

    emu = AsyncMock()
    emu.is_connected = True
    from spinlab.hyper_play import HyperPlaySession
    with pytest.raises(ValueError, match="Missing save state"):
        HyperPlaySession(emu=emu, db=db, game_id="g")


@pytest.mark.asyncio
async def test_hyper_play_sends_level_load(sr_db):
    """First run_one should send HyperPlayLoadCmd for level 1."""
    emu = AsyncMock()
    emu.is_connected = True

    from spinlab.hyper_play import HyperPlaySession
    sr = HyperPlaySession(emu=emu, db=sr_db, game_id="g")
    sr.is_running = True

    async def deliver():
        await asyncio.sleep(0.05)
        sr.receive_complete(HyperPlayCompleteEvent(
            elapsed_ms=30000,
            split_ms=30000,
        ))

    asyncio.create_task(deliver())
    result = await sr.run_one()

    assert result is True
    emu.send_command.assert_called_once()
    cmd = emu.send_command.call_args[0][0]
    assert isinstance(cmd, HyperPlayLoadCmd)
    assert len(cmd.checkpoints) == 1
    assert cmd.checkpoints[0]["ordinal"] == 1


@pytest.mark.asyncio
async def test_hyper_play_cold_recording_on_checkpoint(sr_db):
    """Checkpoint hit after cold start should record an attempt."""
    emu = AsyncMock()
    emu.is_connected = True

    from spinlab.hyper_play import HyperPlaySession
    sr = HyperPlaySession(emu=emu, db=sr_db, game_id="g")
    sr.is_running = True

    async def deliver():
        await asyncio.sleep(0.05)
        sr.receive_checkpoint(HyperPlayCheckpointEvent(
            ordinal=1,
            elapsed_ms=12000,
            split_ms=12000,
        ))
        await asyncio.sleep(0.05)
        sr.receive_complete(HyperPlayCompleteEvent(
            elapsed_ms=30000,
            split_ms=18000,
        ))

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
async def test_hyper_play_death_makes_next_segment_cold(sr_db):
    """Death should mark next sub-segment as cold for recording."""
    emu = AsyncMock()
    emu.is_connected = True

    from spinlab.hyper_play import HyperPlaySession
    # death_delay_ms=0 keeps the test fast and side-steps the post-death
    # queue drain — the deliver task's events arrive AFTER the death has
    # been fully processed.
    sr = HyperPlaySession(emu=emu, db=sr_db, game_id="g", death_delay_ms=0)
    sr.is_running = True

    async def deliver():
        await asyncio.sleep(0.02)
        sr.receive_checkpoint(HyperPlayCheckpointEvent(
            ordinal=1,
            elapsed_ms=12000,
            split_ms=12000,
        ))
        await asyncio.sleep(0.02)
        sr.receive_death(HyperPlayDeathEvent(
            elapsed_ms=18000,
            split_ms=6000,
        ))
        # Brief settle so the death handler's reload + queue-drain
        # completes before the complete event arrives. (death_delay_ms=0
        # means no asyncio.sleep, but the event handler still does a
        # load_state await + queue drain that needs to clear before the
        # next event is delivered.)
        await asyncio.sleep(0.02)
        sr.receive_complete(HyperPlayCompleteEvent(
            elapsed_ms=40000,
            split_ms=15000,
        ))

    asyncio.create_task(deliver())
    await sr.run_one()

    seg_ids = sr_db._seg_ids
    assert len(sr_db.get_segment_attempts(seg_ids[0])) == 1
    attempts = sr_db.get_segment_attempts(seg_ids[1])
    assert len(attempts) == 1
    assert attempts[0]["time_ms"] == 15000


@pytest.mark.asyncio
async def test_hyper_play_death_before_cp_reloads_entrance_state(sr_db):
    """Death before the in-level checkpoint should load the entrance state."""
    emu = AsyncMock()
    emu.is_connected = True

    from spinlab.hyper_play import HyperPlaySession
    sr = HyperPlaySession(emu=emu, db=sr_db, game_id="g", death_delay_ms=0)
    sr.is_running = True

    async def deliver():
        await asyncio.sleep(0.02)
        sr.receive_death(HyperPlayDeathEvent(elapsed_ms=5000, split_ms=5000))
        await asyncio.sleep(0.05)
        sr.receive_complete(HyperPlayCompleteEvent(elapsed_ms=30000, split_ms=25000))

    asyncio.create_task(deliver())
    await sr.run_one()

    # Two load_state calls happened (sub-segment indexes:
    #   pre-run: entrance via HyperPlayLoadCmd handler (orchestrator-side,
    #     not observable through emu.load_state here)
    #   on death: entrance reload via emu.load_state directly).
    emu.load_state.assert_called_once()
    path_arg = emu.load_state.call_args[0][0]
    assert path_arg == sr.levels[0].entrance_state_path


@pytest.mark.asyncio
async def test_hyper_play_death_after_cp_reloads_checkpoint_state(sr_db):
    """Death after the in-level checkpoint should load that checkpoint's
    cold-respawn state, not the entrance state."""
    emu = AsyncMock()
    emu.is_connected = True

    from spinlab.hyper_play import HyperPlaySession
    sr = HyperPlaySession(emu=emu, db=sr_db, game_id="g", death_delay_ms=0)
    sr.is_running = True

    async def deliver():
        await asyncio.sleep(0.02)
        sr.receive_checkpoint(HyperPlayCheckpointEvent(
            ordinal=1, elapsed_ms=12000, split_ms=12000,
        ))
        await asyncio.sleep(0.02)
        sr.receive_death(HyperPlayDeathEvent(elapsed_ms=18000, split_ms=6000))
        await asyncio.sleep(0.05)
        sr.receive_complete(HyperPlayCompleteEvent(elapsed_ms=40000, split_ms=15000))

    asyncio.create_task(deliver())
    await sr.run_one()

    emu.load_state.assert_called_once()
    path_arg = emu.load_state.call_args[0][0]
    expected = sr.levels[0].checkpoints[0]["state_path"]
    assert path_arg == expected
    assert path_arg != sr.levels[0].entrance_state_path


@pytest.mark.asyncio
async def test_hyper_play_stops_after_last_level(sr_db):
    """Session should return False after last level completes."""
    emu = AsyncMock()
    emu.is_connected = True

    from spinlab.hyper_play import HyperPlaySession
    sr = HyperPlaySession(emu=emu, db=sr_db, game_id="g")
    sr.is_running = True

    async def deliver_l1():
        await asyncio.sleep(0.02)
        sr.receive_complete(HyperPlayCompleteEvent(elapsed_ms=30000, split_ms=30000))
    asyncio.create_task(deliver_l1())
    result1 = await sr.run_one()
    assert result1 is True

    async def deliver_l2():
        await asyncio.sleep(0.02)
        sr.receive_complete(HyperPlayCompleteEvent(elapsed_ms=20000, split_ms=20000))
    asyncio.create_task(deliver_l2())
    result2 = await sr.run_one()
    assert result2 is True

    result3 = await sr.run_one()
    assert result3 is False


@pytest.fixture
def session_mgr(sr_db, tmp_path):
    emu = AsyncMock()
    emu.is_connected = True
    emu.send_command = AsyncMock()
    mgr = SessionManager(
        db=sr_db, emu=emu, rom_dir=tmp_path, data_dir=tmp_path,
    )
    mgr.game_id = "g"
    mgr.game_name = "Game"
    return mgr


@pytest.mark.asyncio
async def test_session_manager_start_hyper_play(session_mgr):
    result = await session_mgr.start_hyper_play()
    assert result.status == Status.STARTED
    assert session_mgr.mode == Mode.HYPER_PLAY
    assert session_mgr.hyper_play_session is not None


@pytest.mark.asyncio
async def test_session_manager_stop_hyper_play(session_mgr):
    await session_mgr.start_hyper_play()
    result = await session_mgr.stop_hyper_play()
    assert result.status == Status.STOPPED
    assert session_mgr.mode == Mode.IDLE


@pytest.mark.asyncio
async def test_hyper_play_routes_checkpoint_event(session_mgr):
    await session_mgr.start_hyper_play()
    await session_mgr.route_event(HyperPlayCheckpointEvent(
        ordinal=1,
        elapsed_ms=12000,
        split_ms=12000,
    ))
    assert session_mgr.mode == Mode.HYPER_PLAY

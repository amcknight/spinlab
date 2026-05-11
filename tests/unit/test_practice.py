"""Tests for the async practice loop."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.conftest import make_seg_with_state

from spinlab.db import Database
from spinlab.models import Attempt, AttemptSource, Segment, SegmentCommand, Waypoint
from spinlab.practice import PracticeSession
from spinlab.protocol import AttemptResultEvent, PracticeLoadCmd
from spinlab.scheduler import Scheduler


@pytest.mark.asyncio
async def test_practice_session_picks_and_sends(practice_db):
    """Practice session should pick a segment and send practice_load."""
    seg_id = practice_db._test_seg_id
    mock_emu = AsyncMock()
    mock_emu.is_connected = True
    mock_emu.send = AsyncMock()
    mock_emu.send_command = AsyncMock()

    session = PracticeSession(emu=mock_emu, db=practice_db, game_id="g")
    session.is_running = True

    # Deliver result via receive_result after a short delay
    async def deliver():
        await asyncio.sleep(0.05)
        session.receive_result(AttemptResultEvent(
            segment_id=seg_id,
            completed=True,
            time_ms=4500,
        ))

    asyncio.create_task(deliver())
    await session.run_one()

    # Verify practice_load was sent
    mock_emu.send_command.assert_called_once()
    cmd = mock_emu.send_command.call_args[0][0]
    assert isinstance(cmd, PracticeLoadCmd)

    # Verify attempt was logged
    attempts = practice_db.get_segment_attempts(seg_id)
    assert len(attempts) == 1
    assert attempts[0]["completed"] == 1


@pytest.mark.asyncio
async def test_practice_session_state(practice_db):
    session = PracticeSession(emu=AsyncMock(), db=practice_db, game_id="g")
    assert session.is_running is False
    assert session.current_segment_id is None
    assert session.segments_attempted == 0


class TestReceiveResult:
    @pytest.mark.asyncio
    async def test_receive_result_unblocks_run_one(self, practice_db):
        """run_one awaits asyncio.Event, receive_result sets it."""
        emu = MagicMock()
        emu.is_connected = True
        emu.send = AsyncMock()
        emu.send_command = AsyncMock()

        seg_id = practice_db._test_seg_id

        ps = PracticeSession(emu=emu, db=practice_db, game_id="g")
        ps.is_running = True

        # Schedule receive_result after a short delay
        async def deliver_result():
            await asyncio.sleep(0.1)
            ps.receive_result(AttemptResultEvent(
                segment_id=seg_id,
                completed=True,
                time_ms=4500,
            ))

        asyncio.create_task(deliver_result())
        result = await ps.run_one()

        assert result is True
        assert ps.segments_completed == 1


def test_snapshot_expected_times_at_start(practice_db):
    """start() should populate initial_expected_total_ms and _clean_ms
    with the sum of expected_ms across practicable segments."""
    seg_id = practice_db._test_seg_id
    # Seed an attempt so the estimator produces an expected_ms.
    sched = Scheduler(practice_db, "g")
    sched.process_attempt(seg_id, time_ms=5000, completed=True, deaths=0)

    emu = AsyncMock()
    emu.is_connected = True
    ps = PracticeSession(emu=emu, db=practice_db, game_id="g")
    ps.start()

    assert ps.initial_expected_total_ms is not None
    assert ps.initial_expected_total_ms > 0
    # clean_tail_ms was not supplied but completed+deaths=0 implies it equals time_ms
    assert ps.initial_expected_clean_ms is not None
    assert ps.initial_expected_clean_ms > 0


def test_snapshot_skips_segments_without_state_path(practice_db, tmp_path):
    """Segments whose state_path does not exist on disk are excluded."""
    seg_id = practice_db._test_seg_id
    # Add a second segment with no waypoint save state -> state_path = None
    wp_start2 = Waypoint.make("g", 2, "entrance", 0, {"n": "2"})
    wp_end2 = Waypoint.make("g", 2, "goal", 0, {"n": "2"})
    practice_db.upsert_waypoint(wp_start2)
    practice_db.upsert_waypoint(wp_end2)
    seg2_id = Segment.make_id("g", 2, "entrance", 0, "goal", 0,
                              wp_start2.id, wp_end2.id)
    seg2 = Segment(
        id=seg2_id, game_id="g", level_number=2,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        description="L2", ordinal=2,
        start_waypoint_id=wp_start2.id, end_waypoint_id=wp_end2.id,
    )
    practice_db.upsert_segment(seg2)
    # No save state for wp_start2 => state_path will be NULL

    # Seed attempts on BOTH segments so they each have estimates.
    sched = Scheduler(practice_db, "g")
    sched.process_attempt(seg_id, time_ms=5000, completed=True, deaths=0)
    sched.process_attempt(seg2_id, time_ms=8000, completed=True, deaths=0)

    emu = AsyncMock()
    emu.is_connected = True
    ps = PracticeSession(emu=emu, db=practice_db, game_id="g")
    ps.start()

    # Only seg_id had a real state_path; seg2 contributes nothing.
    # The sum should reflect only seg_id's expected_ms (~5000).
    assert ps.initial_expected_total_ms is not None
    assert ps.initial_expected_total_ms < 6000


def test_snapshot_all_missing_returns_none(practice_db):
    """When no segment has estimates at session start, both snapshots are None."""
    emu = AsyncMock()
    emu.is_connected = True
    # No process_attempt call -> no model state -> no expected_ms
    ps = PracticeSession(emu=emu, db=practice_db, game_id="g")
    ps.start()

    assert ps.initial_expected_total_ms is None
    assert ps.initial_expected_clean_ms is None


@pytest.mark.asyncio
async def test_practice_session_passes_death_penalty_ms(practice_db):
    """PracticeSession should forward death_penalty_ms to PracticeLoadCmd."""
    seg_id = practice_db._test_seg_id
    mock_emu = AsyncMock()
    mock_emu.is_connected = True
    mock_emu.send_command = AsyncMock()

    session = PracticeSession(emu=mock_emu, db=practice_db, game_id="g", death_penalty_ms=2500)
    session.is_running = True

    async def deliver():
        await asyncio.sleep(0.05)
        session.receive_result(AttemptResultEvent(
            segment_id=seg_id,
            completed=True,
            time_ms=4500,
        ))

    asyncio.create_task(deliver())
    await session.run_one()

    mock_emu.send_command.assert_called_once()
    cmd = mock_emu.send_command.call_args[0][0]
    assert isinstance(cmd, PracticeLoadCmd)
    assert cmd.death_penalty_ms == 2500


def test_record_attempt_persists_and_updates_model_in_lockstep(practice_db):
    """`Scheduler.record_attempt` is the single canonical entry point for a
    finished attempt. It must (a) persist the attempt row, (b) update the model
    state, and (c) produce the same model output as a direct
    `process_attempt` call on a fresh DB — i.e., callers should never see a
    different result by going through the bundled path."""
    seg_id = practice_db._test_seg_id
    sched = Scheduler(practice_db, "g")

    attempt = Attempt(
        segment_id=seg_id, parent_id="sess1", completed=True, time_ms=10000,
        source=AttemptSource.PRACTICE,
    )
    sched.record_attempt(attempt)

    # (a) attempt persisted
    rows = practice_db.get_segment_attempts(seg_id)
    assert len(rows) == 1
    assert rows[0]["time_ms"] == 10000
    assert rows[0]["completed"] == 1

    # (b) model state updated
    row = practice_db.load_model_state(seg_id, "rolling_mean")
    assert row is not None
    output_via_record = json.loads(row["output_json"])

    # (c) same output as direct process_attempt on a clean DB
    other_db = Database(practice_db.db_path.parent / "other.db")
    other_db.upsert_game("g", "Game", "any%")
    other_seg = make_seg_with_state(
        other_db, "g", 1, "entrance", "goal", practice_db._test_state_file,
    )
    other_sched = Scheduler(other_db, "g")
    other_sched.process_attempt(other_seg.id, time_ms=10000, completed=True)
    output_via_direct = json.loads(
        other_db.load_model_state(other_seg.id, "rolling_mean")["output_json"]
    )
    assert output_via_record == output_via_direct


def test_process_result_does_not_double_count_attempts(practice_db):
    """Regression: log_attempt was called before scheduler.process_attempt, so
    the scheduler's `db.get_segment_attempts` already contained the new attempt
    AND `all_attempts + [new_attempt]` appended a second copy. Estimators that
    consume `all_attempts_with_new` in `model_output` (rolling_mean, exp_decay)
    saw the most recent attempt twice and produced biased estimates."""
    seg_id = practice_db._test_seg_id
    emu = AsyncMock()
    emu.is_connected = True
    ps = PracticeSession(emu=emu, db=practice_db, game_id="g")

    cmd = SegmentCommand(
        id=seg_id, state_path="x", description="x",
        end_type="goal", expected_time_ms=None,
    )
    ps._process_result(
        AttemptResultEvent(segment_id=seg_id, completed=True, time_ms=10000), cmd,
    )
    ps._process_result(
        AttemptResultEvent(segment_id=seg_id, completed=True, time_ms=20000), cmd,
    )

    # rolling_mean's expected_ms is the mean of completed times. With two
    # attempts of 10s and 20s the correct mean is 15000ms; double-counting the
    # most recent attempt produces (10000 + 20000 + 20000) / 3 = 16666.67ms.
    row = practice_db.load_model_state(seg_id, "rolling_mean")
    assert row is not None
    output = json.loads(row["output_json"])
    assert output["total"]["expected_ms"] == pytest.approx(15000.0, abs=0.01)


def test_current_expected_times_reflects_model_updates(practice_db):
    """After process_attempt runs, current_expected_times() returns the new sum."""
    seg_id = practice_db._test_seg_id
    sched = Scheduler(practice_db, "g")
    sched.process_attempt(seg_id, time_ms=5000, completed=True, deaths=0)

    emu = AsyncMock()
    emu.is_connected = True
    ps = PracticeSession(emu=emu, db=practice_db, game_id="g")
    ps.start()
    initial_total = ps.initial_expected_total_ms

    # Simulate a faster attempt pulling the estimate down.
    ps.scheduler.process_attempt(seg_id, time_ms=3000, completed=True, deaths=0)

    cur_total, cur_clean = ps.current_expected_times()
    assert cur_total is not None
    assert cur_total < initial_total


class TestReloadOnDeath:
    """PracticeSession owns reload-on-death (was previously the orchestrator's
    _maybe_reload_state_on_death). The session remembers _current_state_path
    after PracticeLoadCmd is sent; Death and LevelExit(abort) trigger a
    backend.load_state(path) call."""

    @pytest.mark.asyncio
    async def test_handle_death_reloads_current_state_path(self, practice_db):
        emu = MagicMock()
        emu.is_connected = True
        emu.load_state = AsyncMock()

        ps = PracticeSession(emu=emu, db=practice_db, game_id="g")
        ps._current_state_path = "/states/seg_x.state"

        await ps.handle_death()
        emu.load_state.assert_awaited_once_with("/states/seg_x.state")

    @pytest.mark.asyncio
    async def test_handle_death_no_reload_when_unarmed(self, practice_db):
        """No state path set (between attempts) — death must NOT trigger reload."""
        emu = MagicMock()
        emu.is_connected = True
        emu.load_state = AsyncMock()

        ps = PracticeSession(emu=emu, db=practice_db, game_id="g")
        # _current_state_path defaults to None.

        await ps.handle_death()
        emu.load_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_level_exit_abort_reloads(self, practice_db):
        """Pit-falls / death-falls don't fire a Death frame in SMW — they
        manifest as LevelExit(goal='abort'). Same reload behavior."""
        emu = MagicMock()
        emu.is_connected = True
        emu.load_state = AsyncMock()

        ps = PracticeSession(emu=emu, db=practice_db, game_id="g")
        ps._current_state_path = "/states/seg_y.state"

        await ps.handle_level_exit_abort()
        emu.load_state.assert_awaited_once_with("/states/seg_y.state")

    @pytest.mark.asyncio
    async def test_receive_result_clears_current_state_path(self, practice_db):
        """Race fix: clear the armed flag the moment attempt_result arrives,
        so a Death event arriving in the same handler batch doesn't trigger
        a spurious post-attempt reload."""
        ps = PracticeSession(emu=AsyncMock(), db=practice_db, game_id="g")
        ps._current_state_path = "/states/seg_z.state"

        ps.receive_result(AttemptResultEvent(
            segment_id="seg_z", completed=True, time_ms=1000,
        ))
        assert ps._current_state_path is None

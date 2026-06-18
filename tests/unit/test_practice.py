"""Tests for the async practice loop."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.conftest import FakeEmuBackend, make_seg_with_state

from spinlab.db import Database
from spinlab.models import Attempt, AttemptSource, Segment, Waypoint
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

    session = PracticeSession(emu=mock_emu, db=practice_db, game_id="g", scheduler=Scheduler(practice_db, "g"))
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

    # Verify attempt was logged. The fixture seeds one reference-run traversal
    # episode (run-scoping membership), so the practice attempt is the second.
    attempts = practice_db.get_segment_attempts(seg_id)
    assert len(attempts) == 2
    assert attempts[-1]["completed"] == 1
    assert attempts[-1]["time_ms"] == 4500


@pytest.mark.asyncio
async def test_run_one_notifies_on_segment_load(practice_db):
    """run_one fires on_segment_load once the segment is picked + load cmd
    sent, BEFORE the attempt result arrives — and current_segment_id is set
    at notify time.

    Regression: start_practice broadcast SSE before run_loop had selected a
    segment, and nothing re-broadcast when the segment loaded, so the live
    practice card stayed hidden (current_segment null) until the first attempt
    result. HyperPlay was unaffected (its current_segment is available at
    start). The callback gives Practice the same immediate render.
    """
    seg_id = practice_db._test_seg_id
    mock_emu = AsyncMock()
    mock_emu.is_connected = True
    mock_emu.send_command = AsyncMock()

    seen: list[tuple[str, str | None]] = []

    session = PracticeSession(
        emu=mock_emu,
        db=practice_db,
        game_id="g",
        scheduler=Scheduler(practice_db, "g"),
        on_segment_load=lambda sid: seen.append((sid, session.current_segment_id)),
    )
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

    assert seen, "on_segment_load was never called"
    notified_id, current_at_notify = seen[0]
    assert notified_id == seg_id
    assert current_at_notify == seg_id, (
        "current_segment_id must be populated when on_segment_load fires, "
        "so the broadcast state carries current_segment"
    )


@pytest.mark.asyncio
async def test_practice_session_state(practice_db):
    session = PracticeSession(emu=AsyncMock(), db=practice_db, game_id="g", scheduler=Scheduler(practice_db, "g"))
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

        ps = PracticeSession(emu=emu, db=practice_db, game_id="g", scheduler=Scheduler(practice_db, "g"))
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
    """start() should populate initial_expected_total_ms with the sum of
    expected_ms across practicable segments once the em_suite gate passes."""
    seg_id = practice_db._test_seg_id
    # em_suite_sampler requires n_successes >= 2 AND n_deaths >= 2 before it
    # produces a non-None expected time. Each process_attempt with deaths=1
    # creates one 'died' event + one 'survived' event, so two such calls yield
    # 2 successes + 2 deaths -> gate passes.
    sched = Scheduler(practice_db, "g")
    sched.process_attempt(seg_id, time_ms=5000, completed=True, deaths=1)
    sched.process_attempt(seg_id, time_ms=5200, completed=True, deaths=1)

    emu = AsyncMock()
    emu.is_connected = True
    ps = PracticeSession(emu=emu, db=practice_db, game_id="g", scheduler=Scheduler(practice_db, "g"))
    ps.start()

    assert ps.initial_expected_total_ms is not None
    assert ps.initial_expected_total_ms > 0
    # em_suite Plan 1 leaves 'clean' unmodeled (Spec #2 pending); expected_ms
    # and ms_per_attempt are both None in the clean Estimate.
    assert ps.initial_expected_clean_ms is None


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

    # Seed gate-passing attempts on BOTH segments (em_suite needs >=2 successes
    # AND >=2 deaths; deaths=1 per call gives one died + one survived event).
    sched = Scheduler(practice_db, "g")
    sched.process_attempt(seg_id, time_ms=5000, completed=True, deaths=1)
    sched.process_attempt(seg_id, time_ms=5200, completed=True, deaths=1)
    sched.process_attempt(seg2_id, time_ms=8000, completed=True, deaths=1)
    sched.process_attempt(seg2_id, time_ms=8200, completed=True, deaths=1)

    emu = AsyncMock()
    emu.is_connected = True
    ps = PracticeSession(emu=emu, db=practice_db, game_id="g", scheduler=Scheduler(practice_db, "g"))
    ps.start()

    # Only seg_id had a real state_path; seg2 contributes nothing.
    # Verify the session total equals exactly seg_id's persisted expected_ms.
    seg_state_row = practice_db.load_model_state(seg_id, "em_suite_sampler")
    assert seg_state_row is not None
    seg_expected = json.loads(seg_state_row["output_json"])["total"]["expected_ms"]
    assert seg_expected is not None
    assert ps.initial_expected_total_ms is not None
    assert ps.initial_expected_total_ms == seg_expected  # seg2 excluded (no state_path)


def test_snapshot_all_missing_returns_none(practice_db):
    """When no segment has estimates at session start, both snapshots are None."""
    emu = AsyncMock()
    emu.is_connected = True
    # No process_attempt call -> no model state -> no expected_ms
    ps = PracticeSession(emu=emu, db=practice_db, game_id="g", scheduler=Scheduler(practice_db, "g"))
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

    session = PracticeSession(
        emu=mock_emu, db=practice_db, game_id="g",
        scheduler=Scheduler(practice_db, "g"),
        death_penalty_ms=2500,
    )
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
    from tests.factories import stamp_reference_traversal

    seg_id = practice_db._test_seg_id
    sched = Scheduler(practice_db, "g")
    practice_db.create_session("sess1", "g")

    attempt = Attempt(
        segment_id=seg_id, session_id="sess1", completed=True, time_ms=10000,
        source=AttemptSource.PRACTICE,
    )
    sched.record_attempt(attempt)

    # (a) attempt persisted. The practice_db fixture seeds one reference-run
    # traversal episode (membership for run-scoped views), so the new practice
    # attempt is the SECOND episode; assert on that newest row.
    rows = practice_db.get_segment_attempts(seg_id)
    assert len(rows) == 2
    assert rows[-1]["time_ms"] == 10000
    assert rows[-1]["completed"] == 1

    # (b) model state updated — em_suite_sampler is the only active estimator
    row = practice_db.load_model_state(seg_id, "em_suite_sampler")
    assert row is not None
    output_via_record = json.loads(row["output_json"])

    # (c) same output as direct process_attempt on a clean DB. The clean DB must
    # mirror the fixture's baseline reference traversal so the pooled episode
    # set (and thus the model output) matches.
    other_db = Database(practice_db.db_path.parent / "other.db")
    other_db.upsert_game("g", "Game", "any%")
    other_db.create_capture_run("g:ref", "g", "Ref", kind="live")
    other_db.promote_draft("g:ref", "Ref")
    other_db.set_active_capture_run("g:ref")
    other_seg = make_seg_with_state(
        other_db, "g", 1, "entrance", "goal", practice_db._test_state_file,
    )
    stamp_reference_traversal(other_db, other_seg.id, "g:ref")
    other_sched = Scheduler(other_db, "g")
    other_sched.process_attempt(other_seg.id, time_ms=10000, completed=True)
    output_via_direct = json.loads(
        other_db.load_model_state(other_seg.id, "em_suite_sampler")["output_json"]
    )
    assert output_via_record == output_via_direct


def test_process_result_does_not_double_count_attempts(practice_db):
    """Regression: log_attempt was called before scheduler.update_state_after_episode,
    so the scheduler's `db.get_segment_attempts` already contained the new attempt
    AND a second copy would have been added. After the fix, each episode is persisted
    exactly once before model rebuild — so n_attempts must equal the number of
    _process_result calls, not more."""
    seg_id = practice_db._test_seg_id
    emu = AsyncMock()
    emu.is_connected = True
    ps = PracticeSession(emu=emu, db=practice_db, game_id="g", scheduler=Scheduler(practice_db, "g"))

    ps._process_result(
        AttemptResultEvent(segment_id=seg_id, completed=True, time_ms=10000),
    )
    ps._process_result(
        AttemptResultEvent(segment_id=seg_id, completed=True, time_ms=20000),
    )

    # The practice_db fixture seeds one reference-run traversal episode (member-
    # ship for run-scoped views), so the baseline is n=1. Two _process_result
    # calls add exactly two more (no double-counting) -> n_attempts=3, not 5.
    row = practice_db.load_model_state(seg_id, "em_suite_sampler")
    assert row is not None
    state = json.loads(row["state_json"])
    assert state["n_attempts"] == 3
    assert state["n_completed"] == 3


def test_current_expected_times_reflects_model_updates(practice_db):
    """After process_attempt runs, current_expected_times() reflects the update."""
    seg_id = practice_db._test_seg_id
    # Seed gate-passing data (em_suite needs >=2 successes + >=2 deaths).
    sched = Scheduler(practice_db, "g")
    sched.process_attempt(seg_id, time_ms=5000, completed=True, deaths=1)
    sched.process_attempt(seg_id, time_ms=5200, completed=True, deaths=1)

    emu = AsyncMock()
    emu.is_connected = True
    ps = PracticeSession(emu=emu, db=practice_db, game_id="g", scheduler=Scheduler(practice_db, "g"))
    ps.start()
    initial_total = ps.initial_expected_total_ms
    assert initial_total is not None

    # A fast clean success (no death) pulls the success-time EMA down, so the
    # expected episode time must drop. (Adds no death, so p_die/death_time are
    # unchanged and the decrease is unambiguous.)
    ps.scheduler.process_attempt(seg_id, time_ms=1000, completed=True, deaths=0)

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

        ps = PracticeSession(emu=emu, db=practice_db, game_id="g", scheduler=Scheduler(practice_db, "g"))
        ps._current_state_path = "/states/seg_x.state"

        await ps.handle_death()
        emu.load_state.assert_awaited_once_with("/states/seg_x.state")

    @pytest.mark.asyncio
    async def test_handle_death_no_reload_when_unarmed(self, practice_db):
        """No state path set (between attempts) — death must NOT trigger reload."""
        emu = MagicMock()
        emu.is_connected = True
        emu.load_state = AsyncMock()

        ps = PracticeSession(emu=emu, db=practice_db, game_id="g", scheduler=Scheduler(practice_db, "g"))
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

        ps = PracticeSession(emu=emu, db=practice_db, game_id="g", scheduler=Scheduler(practice_db, "g"))
        ps._current_state_path = "/states/seg_y.state"

        await ps.handle_level_exit_abort()
        emu.load_state.assert_awaited_once_with("/states/seg_y.state")

    @pytest.mark.asyncio
    async def test_receive_result_clears_current_state_path(self, practice_db):
        """Race fix: clear the armed flag the moment attempt_result arrives,
        so a Death event arriving in the same handler batch doesn't trigger
        a spurious post-attempt reload."""
        ps = PracticeSession(
            emu=AsyncMock(), db=practice_db, game_id="g",
            scheduler=Scheduler(practice_db, "g"),
        )
        ps._current_state_path = "/states/seg_z.state"

        ps.receive_result(AttemptResultEvent(
            segment_id="seg_z", completed=True, time_ms=1000,
        ))
        assert ps._current_state_path is None


def test_practice_session_uses_injected_scheduler(tmp_path):
    """PracticeSession must accept a Scheduler and not construct its own."""
    db = Database(tmp_path / "p.db")
    db.upsert_game("g", "Game", "any%")
    emu = FakeEmuBackend(connected=True)
    scheduler = Scheduler(db, "g")

    ps = PracticeSession(
        emu=emu,  # type: ignore[arg-type]  # FakeEmuBackend.on_disconnect vs EmuBackend.on_disconnect — protocol invariance, pre-existing project pattern
        db=db, game_id="g",
        death_penalty_ms=3200,
        scheduler=scheduler,
    )
    assert ps.scheduler is scheduler  # same instance — no construction


class TestTogglePause:
    @pytest.mark.asyncio
    async def test_pause_disarms_then_resume_reloads_same_segment(self, practice_db):
        from spinlab.protocol import PracticeLoadCmd, PracticePauseCmd
        emu = AsyncMock()
        emu.is_connected = True
        emu.send_command = AsyncMock()
        ps = PracticeSession(emu=emu, db=practice_db, game_id="g",
                             scheduler=Scheduler(practice_db, "g"))
        ps.is_running = True
        load_cmd = PracticeLoadCmd(id="seg1", state_path="s.state", end_type="goal")
        ps._current_state_path = "s.state"
        ps._current_load_cmd = load_cmd

        await ps.toggle_pause()
        assert ps.paused is True
        assert ps.paused_at_epoch is not None
        sent = [c.args[0] for c in emu.send_command.call_args_list]
        assert any(isinstance(c, PracticePauseCmd) for c in sent)

        await ps.toggle_pause()
        assert ps.paused is False
        assert ps.paused_at_epoch is None
        assert ps.pause_offset_sec >= 0.0
        sent = [c.args[0] for c in emu.send_command.call_args_list]
        assert sent[-1] is load_cmd

    @pytest.mark.asyncio
    async def test_pause_noop_when_not_in_attempt(self, practice_db):
        emu = AsyncMock(); emu.is_connected = True; emu.send_command = AsyncMock()
        ps = PracticeSession(emu=emu, db=practice_db, game_id="g",
                             scheduler=Scheduler(practice_db, "g"))
        ps.is_running = True
        ps._current_state_path = None
        await ps.toggle_pause()
        assert ps.paused is False
        emu.send_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_death_ignored_while_paused(self, practice_db):
        emu = AsyncMock(); emu.is_connected = True
        emu.load_state = AsyncMock(); emu.send_command = AsyncMock()
        ps = PracticeSession(emu=emu, db=practice_db, game_id="g",
                             scheduler=Scheduler(practice_db, "g"))
        ps._current_state_path = "s.state"
        ps.paused = True
        await ps.handle_death()
        emu.load_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_resume_clears_stale_episode_id(self, practice_db):
        from spinlab.protocol import PracticeLoadCmd
        emu = AsyncMock(); emu.is_connected = True; emu.send_command = AsyncMock()
        ps = PracticeSession(emu=emu, db=practice_db, game_id="g",
                             scheduler=Scheduler(practice_db, "g"))
        ps.is_running = True
        ps._current_state_path = "s.state"
        ps._current_load_cmd = PracticeLoadCmd(id="seg1", state_path="s.state", end_type="goal")
        ps._current_episode_id = "E1"  # left over from a pre-pause death
        await ps.toggle_pause()  # pause
        await ps.toggle_pause()  # resume
        assert ps._current_episode_id is None

    @pytest.mark.asyncio
    async def test_stop_clears_paused_flag(self, practice_db):
        emu = AsyncMock(); emu.is_connected = True; emu.send_command = AsyncMock()
        ps = PracticeSession(emu=emu, db=practice_db, game_id="g",
                             scheduler=Scheduler(practice_db, "g"))
        ps.paused = True
        ps.paused_at_epoch = 123.0
        ps.stop()
        assert ps.paused is False
        assert ps.paused_at_epoch is None


class TestSegmentNavigation:
    def _session(self, practice_db):
        from unittest.mock import AsyncMock
        emu = AsyncMock(); emu.is_connected = True; emu.send_command = AsyncMock()
        ps = PracticeSession(emu=emu, db=practice_db, game_id="g",
                             scheduler=Scheduler(practice_db, "g"))
        ps.is_running = True
        return ps

    def test_segment_at_cursor_picks_and_appends_at_end(self, practice_db):
        ps = self._session(practice_db)
        entry = ps._segment_at_cursor()
        assert entry is not None
        assert len(ps._history) == 1
        assert ps._cursor == 0
        assert entry.load_cmd.id == ps._history[0].load_cmd.id

    def test_completion_advances_cursor(self, practice_db):
        ps = self._session(practice_db)
        ps._segment_at_cursor()
        ps._advance_after_completion()
        assert ps._cursor == 1
        ps._segment_at_cursor()
        assert len(ps._history) == 2 and ps._cursor == 1

    @pytest.mark.asyncio
    async def test_go_prev_moves_cursor_back_and_drops_attempt(self, practice_db):
        from spinlab.protocol import PracticePauseCmd
        ps = self._session(practice_db)
        ps._segment_at_cursor(); ps._advance_after_completion(); ps._segment_at_cursor()
        assert ps._cursor == 1
        ps._current_state_path = "s.state"
        await ps.go_prev()
        assert ps._cursor == 0
        assert ps._nav_pending is True
        sent = [c.args[0] for c in ps.emu.send_command.call_args_list]
        assert any(isinstance(c, PracticePauseCmd) for c in sent)
        assert ps._result_event.is_set()

    @pytest.mark.asyncio
    async def test_go_prev_at_start_is_noop(self, practice_db):
        ps = self._session(practice_db)
        ps._segment_at_cursor()
        ps._current_state_path = "s.state"
        await ps.go_prev()
        assert ps._cursor == 0 and ps._nav_pending is False

    @pytest.mark.asyncio
    async def test_skip_next_advances_cursor(self, practice_db):
        ps = self._session(practice_db)
        ps._segment_at_cursor()
        ps._current_state_path = "s.state"
        await ps.skip_next()
        assert ps._cursor == 1 and ps._nav_pending is True

    @pytest.mark.asyncio
    async def test_nav_ignored_when_not_armed_or_paused(self, practice_db):
        ps = self._session(practice_db)
        ps._segment_at_cursor()
        ps._current_state_path = None
        await ps.skip_next()
        assert ps._cursor == 0 and ps._nav_pending is False
        ps._current_state_path = "s.state"; ps.paused = True
        await ps.skip_next()
        assert ps._cursor == 0 and ps._nav_pending is False

    @pytest.mark.asyncio
    async def test_nav_during_load_send_is_honored(self, practice_db):
        """A nav command that fires WHILE run_one is awaiting send_command must
        not be swallowed: _nav_pending must survive and run_one takes the nav
        branch (cursor reflects the nav). Regression for the TOCTOU window."""
        from unittest.mock import AsyncMock
        ps = self._session(practice_db)
        # Build a 2-entry history, cursor on the second (index 1).
        ps._segment_at_cursor(); ps._advance_after_completion(); ps._segment_at_cursor()
        assert ps._cursor == 1

        nav_fired = []
        async def send_then_nav(cmd):
            # Fire a nav exactly once, simulating it arriving mid-load.
            if not nav_fired:
                nav_fired.append(True)
                await ps.go_prev()   # cursor 1 -> 0; sets _nav_pending + _result_event
        ps.emu.send_command = AsyncMock(side_effect=send_then_nav)

        # With the fix, run_one returns quickly via the nav branch; with the bug
        # the nav is wiped and run_one blocks (wait_for would time out).
        await asyncio.wait_for(ps.run_one(), timeout=2.0)
        assert ps._cursor == 0          # nav moved the cursor back
        assert ps._nav_pending is False  # consumed by the nav branch


def test_experimental_toggle_stamps_event(practice_db):
    from unittest.mock import AsyncMock

    from spinlab.protocol import EventAttemptEmission
    emu = AsyncMock(); emu.is_connected = True; emu.send_command = AsyncMock()
    ps = PracticeSession(emu=emu, db=practice_db, game_id="g",
                         scheduler=Scheduler(practice_db, "g"))
    assert ps.experimental is False
    ps.toggle_experimental()
    assert ps.experimental is True
    ps._last_allocator = None
    ps.receive_event_attempt(EventAttemptEmission(
        segment_id=practice_db._test_seg_id, episode_id="E1",
        outcome="survived", time_ms=4200, timestamp_ms=0))
    rows = practice_db.get_segment_event_rows(practice_db._test_seg_id)
    assert rows[-1]["experimental"] == 1

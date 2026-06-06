"""Tests for StateBuilder — covers branches not already exercised by
test_dashboard_integration.py (which covers the practice branch).

Uses real SessionManager + real DB instead of mocking SessionManager
attributes, so tests break if the SM interface changes.
"""

from spinlab.models import Mode
from spinlab.session_manager import SessionManager
from spinlab.hyper_play import HyperPlaySession


def _make_sm(db, emu):
    return SessionManager(db=db, emu=emu, rom_dir=None, default_category="any%")


class TestIdleBaseCase:
    def test_no_game_returns_bare_state(self, practice_db, mock_emu):
        sm = _make_sm(practice_db, mock_emu)
        state = sm.get_state()

        assert state["mode"] == "idle"
        assert state["game_id"] is None
        assert state["game_name"] is None
        assert state["current_segment"] is None
        assert state["recent"] == []
        assert state["session"] is None
        assert state["allocator_weights"] is None
        assert state["estimator"] is None


class TestHyperPlayBranch:
    def test_hyper_play_populates_current_level(self, practice_db, mock_emu):
        sm = _make_sm(practice_db, mock_emu)
        sm.game_id = "g"
        sm.game_name = "Game"

        sr = HyperPlaySession(emu=mock_emu, db=practice_db, game_id="g")
        sr.segments_recorded = 3
        sr.levels_completed = 2
        sm.hyper_play_session = sr
        sm.mode = Mode.HYPER_PLAY

        state = sm.get_state()

        assert state["mode"] == "hyper_play"
        assert state["session"]["id"] == sr.session_id
        assert state["session"]["segments_attempted"] == 3
        assert state["session"]["segments_completed"] == 2
        # Real HyperPlaySession built its levels from DB — verify current level
        assert state["current_segment"]["level_number"] == 1
        assert state["current_segment"]["state_path"] is not None


class TestColdFillBranch:
    def test_cold_fill_includes_state(self, practice_db, mock_emu):
        sm = _make_sm(practice_db, mock_emu)
        sm.game_id = "g"
        sm.game_name = "Game"
        sm.mode = Mode.COLD_FILL

        # Drive the real ColdFillController into mid-fill state
        sm.cold_fill.queue = [
            {"segment_id": "seg1", "hot_state_path": "/hot1.mss",
             "level_number": 105, "start_type": "checkpoint", "start_ordinal": 1,
             "end_type": "checkpoint", "end_ordinal": 2, "description": ""},
            {"segment_id": "seg2", "hot_state_path": "/hot2.mss",
             "level_number": 105, "start_type": "checkpoint", "start_ordinal": 2,
             "end_type": "goal", "end_ordinal": 0, "description": ""},
        ]
        sm.cold_fill.current = "seg1"
        sm.cold_fill.total = 5

        state = sm.get_state()
        assert state["mode"] == "cold_fill"
        # current_num = total - len(queue) + 1 = 5 - 2 + 1 = 4
        assert state["cold_fill"]["current"] == 4
        assert state["cold_fill"]["total"] == 5

    def test_cold_fill_none_state_present_but_null(self, practice_db, mock_emu):
        """When cold_fill has no current segment, the key is present with
        value None — AppState contract is always-present-sometimes-null."""
        sm = _make_sm(practice_db, mock_emu)
        sm.game_id = "g"
        sm.game_name = "Game"
        sm.mode = Mode.COLD_FILL
        # cold_fill.current is None by default → get_state() returns None

        state = sm.get_state()
        assert state["cold_fill"] is None


class TestSectionsCaptured:
    def test_sections_captured_none_when_idle(self, practice_db, mock_emu):
        """sections_captured is None when no active recording session."""
        sm = _make_sm(practice_db, mock_emu)
        sm.game_id = "g"
        sm.game_name = "Game"
        # No capture_run_id set → None
        state = sm.get_state()
        assert state["sections_captured"] is None

    def test_sections_captured_count_when_recording(self, practice_db, mock_emu):
        """sections_captured reflects the segments the active run traversed
        (recorded events for) — for a freshly captured segment that's the run
        that both owns it and logged its event."""
        from spinlab.models import (
            AttemptOutcome, AttemptSource, EventAttempt, Mode,
        )

        sm = _make_sm(practice_db, mock_emu)
        sm.game_id = "g"
        sm.game_name = "Game"
        sm.mode = Mode.REFERENCE

        # Create a draft capture_run and associate it with the recorder
        run_id = "run-test-count"
        practice_db.create_capture_run(run_id, "g", "Test", kind="live")

        # Add a segment linked to this run
        from spinlab.models import Segment, Waypoint
        wp_s = Waypoint.make("g", 2, "entrance", 0, {})
        wp_e = Waypoint.make("g", 2, "goal", 0, {})
        practice_db.upsert_waypoint(wp_s)
        practice_db.upsert_waypoint(wp_e)
        seg = Segment(
            id=Segment.make_id("g", 2, "entrance", 0, "goal", 0, wp_s.id, wp_e.id),
            game_id="g", level_number=2,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            start_waypoint_id=wp_s.id, end_waypoint_id=wp_e.id,
            ordinal=1,
            capture_run_id=run_id,
        )
        practice_db.upsert_segment(seg)
        # The recorder writes an event row for every segment it closes; the
        # counter is driven by those traversal rows, not by ownership alone.
        practice_db.log_event_attempt(EventAttempt(
            segment_id=seg.id, episode_id="ep1",
            outcome=AttemptOutcome.SURVIVED, time_ms=100,
            capture_run_id=run_id, source=AttemptSource.REFERENCE,
        ))

        sm.capture.recorder.capture_run_id = run_id
        state = sm.get_state()
        assert state["sections_captured"] == 1

    def test_sections_captured_counts_traversed_segment_owned_by_prior_run(
        self, practice_db, mock_emu,
    ):
        """The counter reflects segments the active run *traversed* (recorded
        an event for), not only ones it *owns*. Re-recording an already-
        captured level owns 0 segments (first-writer-wins) but still records
        events for them — those must show in the counter."""
        from spinlab.models import (
            AttemptOutcome, AttemptSource, EventAttempt, Mode, Segment,
        )

        sm = _make_sm(practice_db, mock_emu)
        sm.game_id = "g"
        sm.game_name = "Game"
        sm.mode = Mode.REFERENCE

        practice_db.create_capture_run("old", "g", "Old", kind="live")
        practice_db.promote_draft("old", "Old")
        practice_db.create_capture_run("new", "g", "New", kind="live")

        # Segment owned by the PRIOR run.
        practice_db.upsert_segment(Segment(
            id="segX", game_id="g", level_number=2,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            capture_run_id="old",
        ))
        # Active run re-traverses it: an event row stamped with the new run id.
        practice_db.log_event_attempt(EventAttempt(
            segment_id="segX", episode_id="ep1",
            outcome=AttemptOutcome.SURVIVED, time_ms=100,
            capture_run_id="new", source=AttemptSource.REFERENCE,
        ))

        sm.capture.recorder.capture_run_id = "new"
        state = sm.get_state()
        assert state["sections_captured"] == 1


class TestFrozenSessionSignal:
    def _frozen_snap(self, *, ended_at):
        from spinlab.estimators.session_snapshot import (
            RouteBaseline,
            SessionSnapshot,
        )
        return SessionSnapshot(
            started_at=1717_000_000.0, segments={},
            route=RouteBaseline(exp_run_ms=None, exp_deaths=None),
            ended_at=ended_at,
        )

    def test_no_snapshot_has_frozen_session_false(self, practice_db, mock_emu):
        sm = _make_sm(practice_db, mock_emu)
        assert sm.get_state()["has_frozen_session"] is False

    def test_live_snapshot_has_frozen_session_false(self, practice_db, mock_emu):
        sm = _make_sm(practice_db, mock_emu)
        sm.practice_session_snapshot = self._frozen_snap(ended_at=None)
        assert sm.get_state()["has_frozen_session"] is False

    def test_frozen_snapshot_has_frozen_session_true(self, practice_db, mock_emu):
        sm = _make_sm(practice_db, mock_emu)
        sm.practice_session_snapshot = self._frozen_snap(ended_at=1717_000_060.0)
        assert sm.get_state()["has_frozen_session"] is True


class TestDraftBranch:
    def test_paused_run_state_included_when_active(self, practice_db, mock_emu):
        sm = _make_sm(practice_db, mock_emu)
        sm.game_id = "g"
        sm.game_name = "Game"

        # Create a capture run in the DB and set as paused
        practice_db.create_capture_run("run-xyz", "g", "Test Run", kind="live")
        sm.capture.paused_run_id = "run-xyz"

        state = sm.get_state()
        assert state["paused_run"]["run_id"] == "run-xyz"
        assert state["paused_run"]["segments_captured"] == 0

    def test_paused_run_segments_captured_counts_traversed(self, practice_db, mock_emu):
        """The paused-run badge counts segments the run traversed (events) —
        matching the live counter — not ownership. A run that re-recorded an
        already-owned level shows its traversals, not 0."""
        from spinlab.models import (
            AttemptOutcome, AttemptSource, EventAttempt, Segment,
        )

        sm = _make_sm(practice_db, mock_emu)
        sm.game_id = "g"
        sm.game_name = "Game"

        practice_db.create_capture_run("owner", "g", "Owner", kind="live")
        practice_db.promote_draft("owner", "Owner")
        practice_db.create_capture_run("paused", "g", "Paused", kind="live")
        practice_db.upsert_segment(Segment(
            id="segP", game_id="g", level_number=3,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0, capture_run_id="owner",
        ))
        practice_db.log_event_attempt(EventAttempt(
            segment_id="segP", episode_id="ep", outcome=AttemptOutcome.SURVIVED,
            time_ms=50, capture_run_id="paused", source=AttemptSource.REFERENCE,
        ))

        sm.capture.paused_run_id = "paused"
        state = sm.get_state()
        assert state["paused_run"]["segments_captured"] == 1

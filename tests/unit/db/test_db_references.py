"""Tests for capture_run and segment reference management."""
import pytest

from spinlab.db import Database
from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt, Segment


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("g", "Game", "any%")
    return d


@pytest.fixture
def tmp_db(tmp_path):
    return Database(tmp_path / "tmp.db")


def _make_segment(db, game_id, level, start_type="entrance", start_ord=0,
                  end_type="goal", end_ord=0, desc="", ordinal=1, ref_id=None):
    seg = Segment(
        id=Segment.make_id(game_id, level, start_type, start_ord, end_type, end_ord,
                           "stub_start", "stub_end"),
        game_id=game_id, level_number=level,
        start_type=start_type, start_ordinal=start_ord,
        end_type=end_type, end_ordinal=end_ord,
        description=desc, ordinal=ordinal, capture_run_id=ref_id,
    )
    db.upsert_segment(seg)
    return seg


def test_upsert_game_preserves_existing_name(tmp_db):
    """upsert_game should not overwrite name if game already exists."""
    tmp_db.upsert_game("g1", "Original Name", "any%")
    tmp_db.upsert_game("g1", "New Name", "any%")
    row = tmp_db.conn.execute("SELECT name FROM games WHERE id = ?", ("g1",)).fetchone()
    assert row[0] == "Original Name"


def _saved(db, run_id, game_id, name):
    """Create and promote a run to 'saved'. Drafts are excluded from list_capture_runs."""
    db.create_capture_run(run_id, game_id, name)
    db.promote_draft(run_id, name)


class TestCaptureRunCRUD:
    def test_create_and_list(self, db):
        _saved(db, "ref1", "g", "First Run")
        _saved(db, "ref2", "g", "Second Run")
        refs = db.list_capture_runs("g")
        assert len(refs) == 2
        assert refs[0]["name"] == "First Run"

    def test_set_active(self, db):
        _saved(db, "ref1", "g", "Run 1")
        _saved(db, "ref2", "g", "Run 2")
        db.set_active_capture_run("ref2")
        refs = db.list_capture_runs("g")
        active = [r for r in refs if r["active"]]
        assert len(active) == 1
        assert active[0]["id"] == "ref2"

    def test_rename(self, db):
        _saved(db, "ref1", "g", "Old Name")
        db.rename_capture_run("ref1", "New Name")
        refs = db.list_capture_runs("g")
        assert refs[0]["name"] == "New Name"

    def test_run_only_delete_keeps_segments_active(self, db):
        """run_only delete removes the run + its attempts but KEEPS segments
        active (they re-surface under any future run over the same geography)."""
        _saved(db, "ref1", "g", "Run 1")
        seg = _make_segment(db, "g", 1, ref_id="ref1")
        db.log_event_attempt(EventAttempt(
            segment_id=seg.id, episode_id="ep1",
            outcome=AttemptOutcome.SURVIVED, time_ms=1000,
            capture_run_id="ref1", source=AttemptSource.REFERENCE,
        ))

        db.delete_capture_run("ref1", purge_data=False)

        # Run row gone.
        assert db.list_capture_runs("g") == []
        # Run's membership attempts gone.
        assert db.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE capture_run_id = 'ref1'"
        ).fetchone()[0] == 0
        # Segment ROW survives and stays active.
        seg_row = db.conn.execute(
            "SELECT active FROM segments WHERE id = ?", (seg.id,)
        ).fetchone()
        assert seg_row is not None
        assert seg_row[0] == 1  # still active

    def test_run_only_delete_works_when_segments_have_capture_session_id(self, db):
        """Regression: deleting a run whose segments link a capture_session
        used to fail with FOREIGN KEY constraint failed because the cascade
        chain capture_runs → capture_sessions → segments would auto-delete
        segments out from under non-cascading attempts FK.

        The fix: delete_capture_run nulls segments.capture_session_id first
        so the cascade stops before reaching segments.
        """
        _saved(db, "ref1", "g", "Run 1")
        sess_id = "sess1"
        db.create_capture_session(
            session_id=sess_id, capture_run_id="ref1",
            ordinal=1,
        )
        seg = _make_segment(db, "g", 1, ref_id="ref1")
        # Tie the segment to the session via the cascade-FK column.
        db.conn.execute(
            "UPDATE segments SET capture_session_id = ? WHERE id = ?",
            (sess_id, seg.id),
        )
        db.conn.commit()

        # Should NOT raise FOREIGN KEY constraint failed.
        db.delete_capture_run("ref1", purge_data=False)

        # Run gone, but segment row still exists and is still active.
        assert db.list_capture_runs("g") == []
        seg_row = db.conn.execute(
            "SELECT active, capture_run_id, capture_session_id FROM segments WHERE id = ?",
            (seg.id,),
        ).fetchone()
        assert seg_row is not None
        assert seg_row[0] == 1  # still active (run_only keeps segments)
        assert seg_row[2] is None  # capture_session_id NULL (cascade broken)

    def test_run_only_delete_shared_segment_survives_with_other_runs_data(self, db):
        """run_only on run A: a segment shared by A and B survives; B's
        attempts intact; A's attempts + run row gone; geography practice
        attempts (session-stamped) survive."""
        _saved(db, "refA", "g", "Run A")
        _saved(db, "refB", "g", "Run B")
        seg = _make_segment(db, "g", 1, ref_id="refA")
        # A traverses it.
        db.log_event_attempt(EventAttempt(
            segment_id=seg.id, episode_id="epA",
            outcome=AttemptOutcome.SURVIVED, time_ms=1000,
            capture_run_id="refA", source=AttemptSource.REFERENCE,
        ))
        # B traverses it.
        db.log_event_attempt(EventAttempt(
            segment_id=seg.id, episode_id="epB",
            outcome=AttemptOutcome.SURVIVED, time_ms=1100,
            capture_run_id="refB", source=AttemptSource.REFERENCE,
        ))
        # Geography-pooled practice attempt (session-stamped, not run-stamped).
        db.create_session("sessP", "g")
        db.log_event_attempt(EventAttempt(
            segment_id=seg.id, episode_id="epP",
            outcome=AttemptOutcome.SURVIVED, time_ms=1200,
            session_id="sessP", source=AttemptSource.PRACTICE,
        ))

        db.delete_capture_run("refA", purge_data=False)

        # A gone, B remains.
        assert [r["id"] for r in db.list_capture_runs("g")] == ["refB"]
        # Segment survives, active.
        assert db.conn.execute(
            "SELECT active FROM segments WHERE id = ?", (seg.id,)
        ).fetchone()[0] == 1
        # A's attempts gone, B's intact.
        assert db.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE capture_run_id = 'refA'"
        ).fetchone()[0] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE capture_run_id = 'refB'"
        ).fetchone()[0] == 1
        # Geography practice attempt survives.
        assert db.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE session_id = 'sessP'"
        ).fetchone()[0] == 1


class TestSegmentEdit:
    def test_update_segment_description(self, db):
        _make_segment(db, "g", 1)
        seg_id = Segment.make_id("g", 1, "entrance", 0, "goal", 0, "stub_start", "stub_end")
        db.update_segment(seg_id, description="Yoshi's Island 1")
        rows = db.get_all_segments_with_model("g")
        assert rows[0]["description"] == "Yoshi's Island 1"

    def test_soft_delete_segment(self, db):
        _make_segment(db, "g", 1)
        seg_id = Segment.make_id("g", 1, "entrance", 0, "goal", 0, "stub_start", "stub_end")
        db.soft_delete_segment(seg_id)
        rows = db.get_all_segments_with_model("g")
        assert len(rows) == 0  # deactivated


class TestStatusColumn:
    def test_create_capture_run_starts_as_draft(self, tmp_db):
        """All capture runs start as drafts; finalize promotes them."""
        tmp_db.upsert_game("g1", "Game", "any%")
        tmp_db.create_capture_run("r1", "g1", "Run 1")
        rows = tmp_db.conn.execute(
            "SELECT status FROM capture_runs WHERE id = 'r1'"
        ).fetchone()
        assert rows[0] == "draft"

    def test_create_replay_kind(self, tmp_db):
        """Replay-kind runs are created as drafts but stay out of recovery."""
        tmp_db.upsert_game("g1", "Game", "any%")
        tmp_db.create_capture_run("r1", "g1", "Replay 1", kind="replay")
        rows = tmp_db.conn.execute(
            "SELECT status, kind FROM capture_runs WHERE id = 'r1'"
        ).fetchone()
        assert rows[0] == "draft"
        assert rows[1] == "replay"

    def test_list_capture_runs_excludes_drafts(self, tmp_db):
        tmp_db.upsert_game("g1", "Game", "any%")
        tmp_db.create_capture_run("r1", "g1", "Saved", kind="live")
        tmp_db.promote_draft("r1", "Saved")
        tmp_db.create_capture_run("r2", "g1", "Draft", kind="replay")
        refs = tmp_db.list_capture_runs("g1")
        assert len(refs) == 1
        assert refs[0]["id"] == "r1"

    def test_promote_draft(self, tmp_db):
        tmp_db.upsert_game("g1", "Game", "any%")
        tmp_db.create_capture_run("r1", "g1", "Draft", kind="live")
        tmp_db.promote_draft("r1", "My Run")
        refs = tmp_db.list_capture_runs("g1")
        assert len(refs) == 1
        assert refs[0]["name"] == "My Run"
        assert refs[0]["status"] == "saved"


class TestIsRunDraft:
    def test_draft_returns_true(self, db):
        db.create_capture_run("r1", "g", "Run 1", kind="live")
        assert db.is_run_draft("r1") is True

    def test_promoted_returns_false(self, db):
        db.create_capture_run("r1", "g", "Run 1", kind="live")
        db.promote_draft("r1", "Run 1")
        assert db.is_run_draft("r1") is False

    def test_missing_returns_false(self, db):
        assert db.is_run_draft("does_not_exist") is False


class TestGetActiveCaptureRun:
    def test_returns_active_id(self, db):
        db.create_capture_run("r1", "g", "Run 1", kind="live")
        db.promote_draft("r1", "Run 1")
        db.create_capture_run("r2", "g", "Run 2", kind="live")
        db.promote_draft("r2", "Run 2")
        db.set_active_capture_run("r2")
        assert db.get_active_capture_run("g") == "r2"
        assert db.get_active_capture_run("g") != "r1"

    def test_none_when_no_active(self, db):
        db.create_capture_run("r1", "g", "Run 1", kind="live")
        assert db.get_active_capture_run("g") is None


class TestHardDelete:
    def test_hard_delete_removes_everything(self, tmp_db):
        """Hard delete cascades: model_state, attempts, segments, run."""
        from spinlab.models import Segment, Waypoint, WaypointSaveState
        tmp_db.upsert_game("g1", "Game", "any%")
        tmp_db.create_capture_run("r1", "g1", "Draft", kind="live")
        wp_start = Waypoint.make("g1", 0x105, "entrance", 0, {})
        wp_end = Waypoint.make("g1", 0x105, "goal", 0, {})
        tmp_db.upsert_waypoint(wp_start)
        tmp_db.upsert_waypoint(wp_end)
        seg_id = Segment.make_id("g1", 0x105, "entrance", 0, "goal", 0,
                                 wp_start.id, wp_end.id)
        seg = Segment(
            id=seg_id, game_id="g1", level_number=0x105,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            ordinal=1, capture_run_id="r1",
            start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
        )
        tmp_db.upsert_segment(seg)
        tmp_db.add_save_state(WaypointSaveState(
            waypoint_id=wp_start.id, variant_type="cold",
            state_path="/tmp/s.mss",
        ))
        # Add a model_state row
        tmp_db.conn.execute(
            "INSERT INTO model_state (segment_id, estimator, state_json, updated_at) "
            f"VALUES ('{seg_id}', 'em_suite_sampler', '{{}}', '2026-01-01')"
        )
        # Add an attempt event row (Phase 0 event-level shape).
        tmp_db.create_session("sess1", "g1")
        tmp_db.conn.execute(
            "INSERT INTO attempts "
            "(segment_id, session_id, episode_id, outcome, time_ms, source, created_at) "
            f"VALUES ('{seg_id}', 'sess1', 'ep1', 'survived', 5000, 'practice', '2026-01-01')"
        )
        tmp_db.conn.commit()

        tmp_db.hard_delete_capture_run("r1")

        assert tmp_db.conn.execute("SELECT COUNT(*) FROM capture_runs WHERE id='r1'").fetchone()[0] == 0
        assert tmp_db.conn.execute(f"SELECT COUNT(*) FROM segments WHERE id='{seg_id}'").fetchone()[0] == 0
        assert tmp_db.conn.execute(f"SELECT COUNT(*) FROM model_state WHERE segment_id='{seg_id}'").fetchone()[0] == 0
        assert tmp_db.conn.execute(f"SELECT COUNT(*) FROM attempts WHERE segment_id='{seg_id}'").fetchone()[0] == 0


def _seg_with_states(db, game_id, level, run_id, *, ordinal=1):
    """Build a segment with real start/end waypoints + a cold save-state on each.
    Returns (seg_id, wp_start_id, wp_end_id)."""
    from spinlab.models import Segment, Waypoint, WaypointSaveState
    wp_start = Waypoint.make(game_id, level, "entrance", 0, {})
    wp_end = Waypoint.make(game_id, level, "goal", 0, {})
    db.upsert_waypoint(wp_start)
    db.upsert_waypoint(wp_end)
    seg_id = Segment.make_id(game_id, level, "entrance", 0, "goal", 0,
                             wp_start.id, wp_end.id)
    db.upsert_segment(Segment(
        id=seg_id, game_id=game_id, level_number=level,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        ordinal=ordinal, capture_run_id=run_id,
        start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
    ))
    db.add_save_state(WaypointSaveState(
        waypoint_id=wp_start.id, variant_type="cold",
        state_path=f"/tmp/{seg_id}_start.mss",
    ))
    db.add_save_state(WaypointSaveState(
        waypoint_id=wp_end.id, variant_type="cold",
        state_path=f"/tmp/{seg_id}_end.mss",
    ))
    return seg_id, wp_start.id, wp_end.id


def _ref_attempt(db, seg_id, run_id, episode):
    db.log_event_attempt(EventAttempt(
        segment_id=seg_id, episode_id=episode,
        outcome=AttemptOutcome.SURVIVED, time_ms=1000,
        capture_run_id=run_id, source=AttemptSource.REFERENCE,
    ))


class TestRunAndDataDelete:
    def test_exclusive_segment_fully_purged(self, db):
        """run_and_data: a segment exclusive to the deleted run is fully purged
        (segment row, attempts, waypoint_save_states, model_state all gone)."""
        _saved(db, "refA", "g", "Run A")
        seg_id, wp_s, wp_e = _seg_with_states(db, "g", 1, "refA")
        _ref_attempt(db, seg_id, "refA", "epA")
        db.conn.execute(
            "INSERT INTO model_state (segment_id, estimator, state_json, updated_at) "
            f"VALUES ('{seg_id}', 'em_suite_sampler', '{{}}', '2026-01-01')"
        )
        db.conn.commit()

        db.delete_capture_run("refA", purge_data=True)

        assert db.conn.execute(
            f"SELECT COUNT(*) FROM segments WHERE id='{seg_id}'"
        ).fetchone()[0] == 0
        assert db.conn.execute(
            f"SELECT COUNT(*) FROM attempts WHERE segment_id='{seg_id}'"
        ).fetchone()[0] == 0
        assert db.conn.execute(
            f"SELECT COUNT(*) FROM model_state WHERE segment_id='{seg_id}'"
        ).fetchone()[0] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) FROM waypoint_save_states WHERE waypoint_id IN (?, ?)",
            (wp_s, wp_e),
        ).fetchone()[0] == 0

    def test_exclusive_segment_with_fit_purged_no_fk_crash(self, db):
        """Regression: an exclusive segment that has a segment_fits row used to
        abort run_and_data with FOREIGN KEY constraint failed — segment_fits has
        an enforced FK to segments(id) with no ON DELETE, and the purge never
        deleted its rows. Fits are written on the live practice path, so the
        first real run_and_data delete of a practiced run crashed.

        After the fix: no exception AND the segment_fits row is gone."""
        _saved(db, "refA", "g", "Run A")
        seg_id, _wp_s, _wp_e = _seg_with_states(db, "g", 1, "refA")
        _ref_attempt(db, seg_id, "refA", "epA")
        db.save_segment_fit(seg_id, "segment_fit", {"n_attempts": 1, "status": {}})

        # Must NOT raise FOREIGN KEY constraint failed.
        db.delete_capture_run("refA", purge_data=True)

        assert db.conn.execute(
            f"SELECT COUNT(*) FROM segments WHERE id='{seg_id}'"
        ).fetchone()[0] == 0
        # The fit row for the purged segment is gone.
        assert db.conn.execute(
            "SELECT COUNT(*) FROM segment_fits WHERE segment_id=?", (seg_id,)
        ).fetchone()[0] == 0

    def test_shared_segment_protected(self, db):
        """run_and_data on run A: a segment also traversed by run B is PROTECTED
        — segment + save-states survive; only A's attempts are removed."""
        _saved(db, "refA", "g", "Run A")
        _saved(db, "refB", "g", "Run B")
        seg_id, wp_s, wp_e = _seg_with_states(db, "g", 1, "refA")
        _ref_attempt(db, seg_id, "refA", "epA")
        _ref_attempt(db, seg_id, "refB", "epB")

        db.delete_capture_run("refA", purge_data=True)

        # Segment + save-states survive.
        assert db.conn.execute(
            f"SELECT COUNT(*) FROM segments WHERE id='{seg_id}'"
        ).fetchone()[0] == 1
        assert db.conn.execute(
            "SELECT COUNT(*) FROM waypoint_save_states WHERE waypoint_id IN (?, ?)",
            (wp_s, wp_e),
        ).fetchone()[0] == 2
        # A's attempts gone, B's intact.
        assert db.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE capture_run_id='refA'"
        ).fetchone()[0] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE capture_run_id='refB'"
        ).fetchone()[0] == 1

    def test_shared_waypoint_save_state_guarded(self, db):
        """run_and_data: deleting an exclusive segment must NOT delete a
        save-state on a waypoint still referenced by a SURVIVING segment."""
        from spinlab.models import Segment, Waypoint, WaypointSaveState
        _saved(db, "refA", "g", "Run A")
        _saved(db, "refB", "g", "Run B")
        # Shared waypoint used as the END of A's segment and the START of B's.
        wp_shared = Waypoint.make("g", 5, "goal", 0, {})
        wp_a_start = Waypoint.make("g", 5, "entrance", 0, {})
        wp_b_end = Waypoint.make("g", 6, "goal", 0, {})
        for wp in (wp_shared, wp_a_start, wp_b_end):
            db.upsert_waypoint(wp)
        seg_a = Segment.make_id("g", 5, "entrance", 0, "goal", 0, wp_a_start.id, wp_shared.id)
        seg_b = Segment.make_id("g", 6, "goal", 0, "goal", 0, wp_shared.id, wp_b_end.id)
        db.upsert_segment(Segment(
            id=seg_a, game_id="g", level_number=5,
            start_type="entrance", start_ordinal=0, end_type="goal", end_ordinal=0,
            ordinal=1, capture_run_id="refA",
            start_waypoint_id=wp_a_start.id, end_waypoint_id=wp_shared.id,
        ))
        db.upsert_segment(Segment(
            id=seg_b, game_id="g", level_number=6,
            start_type="goal", start_ordinal=0, end_type="goal", end_ordinal=0,
            ordinal=2, capture_run_id="refB",
            start_waypoint_id=wp_shared.id, end_waypoint_id=wp_b_end.id,
        ))
        for wp in (wp_shared, wp_a_start, wp_b_end):
            db.add_save_state(WaypointSaveState(
                waypoint_id=wp.id, variant_type="cold", state_path=f"/tmp/{wp.id}.mss",
            ))
        _ref_attempt(db, seg_a, "refA", "epA")  # seg_a exclusive to A
        _ref_attempt(db, seg_b, "refB", "epB")  # seg_b owned/traversed by B

        db.delete_capture_run("refA", purge_data=True)

        # seg_a purged.
        assert db.conn.execute(f"SELECT COUNT(*) FROM segments WHERE id='{seg_a}'").fetchone()[0] == 0
        # seg_b survives.
        assert db.conn.execute(f"SELECT COUNT(*) FROM segments WHERE id='{seg_b}'").fetchone()[0] == 1
        # A's exclusive start waypoint save-state removed.
        assert db.conn.execute(
            "SELECT COUNT(*) FROM waypoint_save_states WHERE waypoint_id=?", (wp_a_start.id,)
        ).fetchone()[0] == 0
        # Shared waypoint's save-state PRESERVED (seg_b still uses it).
        assert db.conn.execute(
            "SELECT COUNT(*) FROM waypoint_save_states WHERE waypoint_id=?", (wp_shared.id,)
        ).fetchone()[0] == 1


class TestDeleteActiveReassignment:
    def test_deleting_active_reassigns_to_most_recent_remaining(self, db):
        import time
        _saved(db, "r1", "g", "Run 1")
        time.sleep(0.01)
        _saved(db, "r2", "g", "Run 2")
        db.set_active_capture_run("r2")
        assert db.get_active_capture_run("g") == "r2"

        db.delete_capture_run("r2", purge_data=False)

        assert db.get_active_capture_run("g") == "r1"

    def test_deleting_last_run_leaves_no_active(self, db):
        _saved(db, "r1", "g", "Run 1")
        db.set_active_capture_run("r1")
        db.delete_capture_run("r1", purge_data=False)
        assert db.get_active_capture_run("g") is None


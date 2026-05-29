"""Tests for capture_run and segment reference management."""
import pytest

from spinlab.db import Database
from spinlab.models import Segment


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

    def test_delete_deactivates_segments(self, db):
        db.create_capture_run("ref1", "g", "Run 1")
        _make_segment(db, "g", 1, ref_id="ref1")
        db.delete_capture_run("ref1")
        segments = db.get_all_segments_with_model("g")
        assert len(segments) == 0  # deactivated, not returned

    def test_delete_works_when_segments_have_capture_session_id(self, db):
        """Regression: deleting a run whose segments link a capture_session
        used to fail with FOREIGN KEY constraint failed because the cascade
        chain capture_runs → capture_sessions → segments would auto-delete
        segments out from under non-cascading attempts FK.

        The fix: delete_capture_run nulls segments.capture_session_id first
        so the cascade stops before reaching segments.
        """
        db.create_capture_run("ref1", "g", "Run 1")
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
        db.delete_capture_run("ref1")

        # Run gone, but segment row still exists (deactivated).
        assert db.list_capture_runs("g") == []
        seg_row = db.conn.execute(
            "SELECT active, capture_run_id, capture_session_id FROM segments WHERE id = ?",
            (seg.id,),
        ).fetchone()
        assert seg_row is not None
        assert seg_row[0] == 0  # active=False
        assert seg_row[1] is None  # capture_run_id NULL
        assert seg_row[2] is None  # capture_session_id NULL


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

    def test_get_segments_by_reference(self, db):
        db.create_capture_run("ref1", "g", "Run 1")
        for i in range(3):
            _make_segment(db, "g", i, ordinal=i+1, ref_id="ref1")
        rows = db.get_segments_by_reference("ref1")
        assert len(rows) == 3
        assert rows[0]["ordinal"] == 1


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
            f"VALUES ('{seg_id}', 'kalman', '{{}}', '2026-01-01')"
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


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
        description=desc, ordinal=ordinal, reference_id=ref_id,
    )
    db.upsert_segment(seg)
    return seg


def test_upsert_game_preserves_existing_name(tmp_db):
    """upsert_game should not overwrite name if game already exists."""
    tmp_db.upsert_game("g1", "Original Name", "any%")
    tmp_db.upsert_game("g1", "New Name", "any%")
    row = tmp_db.conn.execute("SELECT name FROM games WHERE id = ?", ("g1",)).fetchone()
    assert row[0] == "Original Name"


class TestCaptureRunCRUD:
    def test_create_and_list(self, db):
        db.create_capture_run("ref1", "g", "First Run")
        db.create_capture_run("ref2", "g", "Second Run")
        refs = db.list_capture_runs("g")
        assert len(refs) == 2
        assert refs[0]["name"] == "First Run"

    def test_set_active(self, db):
        db.create_capture_run("ref1", "g", "Run 1")
        db.create_capture_run("ref2", "g", "Run 2")
        db.set_active_capture_run("ref2")
        refs = db.list_capture_runs("g")
        active = [r for r in refs if r["active"]]
        assert len(active) == 1
        assert active[0]["id"] == "ref2"

    def test_rename(self, db):
        db.create_capture_run("ref1", "g", "Old Name")
        db.rename_capture_run("ref1", "New Name")
        refs = db.list_capture_runs("g")
        assert refs[0]["name"] == "New Name"

    def test_delete_deactivates_segments(self, db):
        db.create_capture_run("ref1", "g", "Run 1")
        _make_segment(db, "g", 1, ref_id="ref1")
        db.delete_capture_run("ref1")
        segments = db.get_all_segments_with_model("g")
        assert len(segments) == 0  # deactivated, not returned


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


class TestDraftColumn:
    def test_create_capture_run_defaults_draft_zero(self, tmp_db):
        """Existing behavior: create_capture_run sets draft=0 (backwards compat)."""
        tmp_db.upsert_game("g1", "Game", "any%")
        tmp_db.create_capture_run("r1", "g1", "Run 1")
        rows = tmp_db.conn.execute(
            "SELECT draft FROM capture_runs WHERE id = 'r1'"
        ).fetchone()
        assert rows[0] == 0

    def test_create_draft_capture_run(self, tmp_db):
        tmp_db.upsert_game("g1", "Game", "any%")
        tmp_db.create_capture_run("r1", "g1", "Run 1", draft=True)
        rows = tmp_db.conn.execute(
            "SELECT draft FROM capture_runs WHERE id = 'r1'"
        ).fetchone()
        assert rows[0] == 1

    def test_list_capture_runs_excludes_drafts(self, tmp_db):
        tmp_db.upsert_game("g1", "Game", "any%")
        tmp_db.create_capture_run("r1", "g1", "Saved", draft=False)
        tmp_db.create_capture_run("r2", "g1", "Draft", draft=True)
        refs = tmp_db.list_capture_runs("g1")
        assert len(refs) == 1
        assert refs[0]["id"] == "r1"

    def test_promote_draft(self, tmp_db):
        tmp_db.upsert_game("g1", "Game", "any%")
        tmp_db.create_capture_run("r1", "g1", "Draft", draft=True)
        tmp_db.promote_draft("r1", "My Run")
        refs = tmp_db.list_capture_runs("g1")
        assert len(refs) == 1
        assert refs[0]["name"] == "My Run"
        assert refs[0]["draft"] == 0


class TestHardDelete:
    @pytest.mark.skip(reason="Task 8 restores: test references segment_variants table and add_variant, both removed in Task 7")
    def test_hard_delete_removes_everything(self, tmp_db):
        """Hard delete cascades: variants, model_state, attempts, segments, run."""
        from spinlab.models import Segment, SegmentVariant
        tmp_db.upsert_game("g1", "Game", "any%")
        tmp_db.create_capture_run("r1", "g1", "Draft", draft=True)
        seg = Segment(
            id="seg1", game_id="g1", level_number=0x105,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            ordinal=1, reference_id="r1",
        )
        tmp_db.upsert_segment(seg)
        tmp_db.add_variant(SegmentVariant(
            segment_id="seg1", variant_type="cold",
            state_path="/tmp/s.mss", is_default=True,
        ))
        # Add a model_state row
        tmp_db.conn.execute(
            "INSERT INTO model_state (segment_id, estimator, state_json, updated_at) "
            "VALUES ('seg1', 'kalman', '{}', '2026-01-01')"
        )
        # Add an attempt row
        tmp_db.conn.execute(
            "INSERT INTO attempts (segment_id, session_id, completed, time_ms, strat_version, created_at) "
            "VALUES ('seg1', 'sess1', 1, 5000, 1, '2026-01-01')"
        )
        tmp_db.conn.commit()

        tmp_db.hard_delete_capture_run("r1")

        assert tmp_db.conn.execute("SELECT COUNT(*) FROM capture_runs WHERE id='r1'").fetchone()[0] == 0
        assert tmp_db.conn.execute("SELECT COUNT(*) FROM segments WHERE id='seg1'").fetchone()[0] == 0
        assert tmp_db.conn.execute("SELECT COUNT(*) FROM segment_variants WHERE segment_id='seg1'").fetchone()[0] == 0
        assert tmp_db.conn.execute("SELECT COUNT(*) FROM model_state WHERE segment_id='seg1'").fetchone()[0] == 0
        assert tmp_db.conn.execute("SELECT COUNT(*) FROM attempts WHERE segment_id='seg1'").fetchone()[0] == 0

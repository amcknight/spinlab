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
        id=Segment.make_id(game_id, level, start_type, start_ord, end_type, end_ord),
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
        seg_id = Segment.make_id("g", 1, "entrance", 0, "goal", 0)
        db.update_segment(seg_id, description="Yoshi's Island 1")
        rows = db.get_all_segments_with_model("g")
        assert rows[0]["description"] == "Yoshi's Island 1"

    def test_soft_delete_segment(self, db):
        _make_segment(db, "g", 1)
        seg_id = Segment.make_id("g", 1, "entrance", 0, "goal", 0)
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

"""Tests for capture_run and split reference management."""
import pytest
from spinlab.db import Database
from spinlab.models import Split
from spinlab.manifest import seed_db_from_manifest


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("g", "Game", "any%")
    return d


@pytest.fixture
def tmp_db(tmp_path):
    return Database(tmp_path / "tmp.db")


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

    def test_delete_deactivates_splits(self, db):
        db.create_capture_run("ref1", "g", "Run 1")
        s = Split(id="s1", game_id="g", level_number=1, room_id=0,
                  goal="normal", reference_id="ref1")
        db.upsert_split(s)
        db.delete_capture_run("ref1")
        splits = db.get_all_splits_with_model("g")
        assert len(splits) == 0  # s1 deactivated, not returned


class TestSplitEdit:
    def test_update_split_description(self, db):
        s = Split(id="s1", game_id="g", level_number=1, room_id=0, goal="normal")
        db.upsert_split(s)
        db.update_split("s1", description="Yoshi's Island 1")
        rows = db.get_all_splits_with_model("g")
        assert rows[0]["description"] == "Yoshi's Island 1"

    def test_update_split_goal(self, db):
        s = Split(id="s1", game_id="g", level_number=1, room_id=0, goal="normal")
        db.upsert_split(s)
        db.update_split("s1", goal="key")
        rows = db.get_all_splits_with_model("g")
        assert rows[0]["goal"] == "key"

    def test_soft_delete_split(self, db):
        s = Split(id="s1", game_id="g", level_number=1, room_id=0, goal="normal")
        db.upsert_split(s)
        db.soft_delete_split("s1")
        rows = db.get_all_splits_with_model("g")
        assert len(rows) == 0  # deactivated

    def test_get_splits_by_reference(self, db):
        db.create_capture_run("ref1", "g", "Run 1")
        for i in range(3):
            s = Split(id=f"s{i}", game_id="g", level_number=i, room_id=0,
                      goal="normal", reference_id="ref1", ordinal=i+1)
            db.upsert_split(s)
        rows = db.get_splits_by_reference("ref1")
        assert len(rows) == 3
        assert rows[0]["ordinal"] == 1


class TestManifestMigration:
    def test_seed_creates_capture_run(self, db):
        manifest = {
            "game_id": "g",
            "category": "any%",
            "captured_at": "2026-03-12T00:00:00Z",
            "splits": [
                {"id": "g:1:0:normal", "level_number": 1, "room_id": 0,
                 "goal": "normal", "name": "Level 1", "reference_time_ms": 5000},
                {"id": "g:2:0:key", "level_number": 2, "room_id": 0,
                 "goal": "key", "name": "Level 2", "reference_time_ms": 8000},
            ],
        }
        seed_db_from_manifest(db, manifest, "Game")
        refs = db.list_capture_runs("g")
        assert len(refs) == 1
        splits = db.get_splits_by_reference(refs[0]["id"])
        assert len(splits) == 2
        assert splits[0]["ordinal"] == 1
        assert splits[1]["ordinal"] == 2

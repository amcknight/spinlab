"""Tests for routes/system.py: GET /api/roms stem matching + POST /api/cold-fill/start."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from spinlab.db import Database
from spinlab.errors import NotConnectedError
from spinlab.models import ActionResult, Mode, Status

GAME_ID = "test_game"


def _make_client(db: Database, rom_dir=None, tmp_path=None):
    from tests.conftest import make_test_config
    from spinlab.dashboard import create_app
    overrides = {}
    if rom_dir is not None:
        overrides["rom_dir"] = rom_dir
    if tmp_path is not None:
        overrides["data_dir"] = tmp_path
    cfg = make_test_config(**overrides)
    app = create_app(db=db, config=cfg)
    app.state.session.game_id = GAME_ID
    return TestClient(app)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game(GAME_ID, "Test Game", "any%")
    return d


# ---------------------------------------------------------------------------
# GET /api/roms — recently_played / recently_added
# ---------------------------------------------------------------------------

class TestListRoms:
    def test_recently_played_resolved_by_stem(self, tmp_path):
        """Game name from DB is matched to ROM filename by case-insensitive stem."""
        rom_dir = tmp_path / "roms"
        rom_dir.mkdir()
        (rom_dir / "Love Yourself.smc").write_bytes(b"")
        (rom_dir / "Other Game.sfc").write_bytes(b"")

        db = Database(tmp_path / "g.db")
        db.upsert_game("lv", "Love Yourself", "any%")

        client = _make_client(db, rom_dir=rom_dir, tmp_path=tmp_path)
        resp = client.get("/api/roms")
        assert resp.status_code == 200
        data = resp.json()
        assert "Love Yourself.smc" in data["recently_played"]
        assert "Other Game.sfc" not in data["recently_played"]

    def test_recently_played_case_insensitive_match(self, tmp_path):
        """Matching is case-insensitive on both the game name and the ROM stem."""
        rom_dir = tmp_path / "roms"
        rom_dir.mkdir()
        (rom_dir / "LOVE YOURSELF.smc").write_bytes(b"")

        db = Database(tmp_path / "g.db")
        db.upsert_game("lv", "love yourself", "any%")

        client = _make_client(db, rom_dir=rom_dir, tmp_path=tmp_path)
        resp = client.get("/api/roms")
        assert resp.status_code == 200
        assert "LOVE YOURSELF.smc" in resp.json()["recently_played"]

    def test_recently_added_excludes_recently_played(self, tmp_path):
        """A ROM already in recently_played is not duplicated in recently_added."""
        rom_dir = tmp_path / "roms"
        rom_dir.mkdir()
        (rom_dir / "Love Yourself.smc").write_bytes(b"")

        db = Database(tmp_path / "g.db")
        db.upsert_game("lv", "Love Yourself", "any%")

        client = _make_client(db, rom_dir=rom_dir, tmp_path=tmp_path)
        resp = client.get("/api/roms")
        data = resp.json()
        assert "Love Yourself.smc" not in data["recently_added"]

    def test_recently_added_sorted_newest_first(self, tmp_path):
        """recently_added lists files by creation time, newest first."""
        rom_dir = tmp_path / "roms"
        rom_dir.mkdir()
        older = rom_dir / "Older.smc"
        older.write_bytes(b"")
        time.sleep(0.02)
        newer = rom_dir / "Newer.smc"
        newer.write_bytes(b"")

        db = Database(tmp_path / "g.db")
        client = _make_client(db, rom_dir=rom_dir, tmp_path=tmp_path)
        resp = client.get("/api/roms")
        data = resp.json()
        added = data["recently_added"]
        assert added.index("Newer.smc") < added.index("Older.smc")

    def test_empty_lists_when_rom_dir_missing(self, tmp_path):
        db = Database(tmp_path / "g.db")
        client = _make_client(db, rom_dir=None, tmp_path=tmp_path)
        resp = client.get("/api/roms")
        assert resp.status_code == 200
        assert resp.json()["roms"] == []


# ---------------------------------------------------------------------------
# POST /api/cold-fill/start
# ---------------------------------------------------------------------------

class TestColdFillStart:
    @staticmethod
    def _with_active_run(client):
        db = client.app.state.db
        db.create_capture_run("r1", GAME_ID, "R1", kind="live")
        db.set_active_capture_run("r1")

    def test_400_when_no_game_loaded(self, db, tmp_path):
        client = _make_client(db, tmp_path=tmp_path)
        client.app.state.session.game_id = None
        resp = client.post("/api/cold-fill/start")
        assert resp.status_code == 400
        assert "No game loaded" in resp.json()["detail"]

    def test_409_when_mode_not_idle(self, db, tmp_path):
        client = _make_client(db, tmp_path=tmp_path)
        client.app.state.session.mode = Mode.REFERENCE
        resp = client.post("/api/cold-fill/start")
        assert resp.status_code == 409

    def test_400_when_no_active_run(self, db, tmp_path):
        client = _make_client(db, tmp_path=tmp_path)
        resp = client.post("/api/cold-fill/start")
        assert resp.status_code == 400
        assert "active" in resp.json()["detail"].lower()

    def test_503_when_not_connected(self, db, tmp_path):
        client = _make_client(db, tmp_path=tmp_path)
        self._with_active_run(client)
        client.app.state.session.cold_fill.start = AsyncMock(
            side_effect=NotConnectedError()
        )
        resp = client.post("/api/cold-fill/start")
        assert resp.status_code == 503

    def test_success_returns_ok_when_cold_fill_started(self, db, tmp_path):
        client = _make_client(db, tmp_path=tmp_path)
        self._with_active_run(client)
        client.app.state.session.cold_fill.start = AsyncMock(
            return_value=ActionResult(status=Status.STARTED, new_mode=Mode.COLD_FILL)
        )
        resp = client.post("/api/cold-fill/start")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_success_returns_no_gaps_when_nothing_to_fill(self, db, tmp_path):
        client = _make_client(db, tmp_path=tmp_path)
        self._with_active_run(client)
        client.app.state.session.cold_fill.start = AsyncMock(
            return_value=ActionResult(status=Status.NO_GAPS)
        )
        resp = client.post("/api/cold-fill/start")
        assert resp.status_code == 200
        assert resp.json()["status"] == "no_gaps"

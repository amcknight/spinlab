"""Integration tests for multi-game support: switching, isolation, reset scoping."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spinlab.db import Database
from spinlab.models import Split, Attempt
from spinlab.romid import rom_checksum, game_name_from_filename

FIXTURES = Path(__file__).parent / "fixtures"


# -- ROM identity --------------------------------------------------------------

def test_two_fixtures_have_different_checksums():
    """Fixture ROMs must produce distinct game IDs."""
    c_a = rom_checksum(FIXTURES / "game_a.sfc")
    c_b = rom_checksum(FIXTURES / "game_b.sfc")
    assert c_a != c_b
    assert len(c_a) == 16
    assert len(c_b) == 16


def test_game_name_strips_extension():
    assert game_name_from_filename("game_a.sfc") == "game_a"
    assert game_name_from_filename("game_b.sfc") == "game_b"


# -- Dashboard game switching --------------------------------------------------

@pytest.fixture
def app_with_rom_dir(tmp_path):
    """Dashboard app with rom_dir pointing to test fixtures."""
    from spinlab.dashboard import create_app

    db = Database(tmp_path / "test.db")
    app = create_app(db=db, rom_dir=FIXTURES, host="127.0.0.1", port=59999)
    return app, db


def test_switch_game_creates_db_record(app_with_rom_dir):
    """Switching to a new game auto-creates a game row in the DB."""
    app, db = app_with_rom_dir
    checksum = rom_checksum(FIXTURES / "game_a.sfc")
    app.state._switch_game(checksum, "game_a", "any%")
    row = db.conn.execute("SELECT name FROM games WHERE id = ?", (checksum,)).fetchone()
    assert row is not None
    assert row[0] == "game_a"


def test_switch_between_two_games(app_with_rom_dir):
    """Switching games updates state and preserves both games in DB."""
    app, db = app_with_rom_dir
    c_a = rom_checksum(FIXTURES / "game_a.sfc")
    c_b = rom_checksum(FIXTURES / "game_b.sfc")

    app.state._switch_game(c_a, "game_a", "any%")
    assert app.state._game_id[0] == c_a
    assert app.state._game_name[0] == "game_a"

    app.state._switch_game(c_b, "game_b", "any%")
    assert app.state._game_id[0] == c_b
    assert app.state._game_name[0] == "game_b"

    # Both games exist in DB
    games = db.conn.execute("SELECT id FROM games").fetchall()
    game_ids = {g[0] for g in games}
    assert c_a in game_ids
    assert c_b in game_ids


def test_switch_game_invalidates_scheduler(app_with_rom_dir):
    """Switching games nulls the cached scheduler so it's rebuilt for the new game."""
    app, db = app_with_rom_dir
    c_a = rom_checksum(FIXTURES / "game_a.sfc")
    c_b = rom_checksum(FIXTURES / "game_b.sfc")

    app.state._switch_game(c_a, "game_a", "any%")
    client = TestClient(app)
    # Access state to trigger scheduler creation
    client.get("/api/state")
    assert app.state._scheduler[0] is not None

    # Switch to game B — scheduler should be invalidated
    app.state._switch_game(c_b, "game_b", "any%")
    assert app.state._scheduler[0] is None


def test_api_state_shows_game_info(app_with_rom_dir):
    """The /api/state endpoint includes game_id and game_name."""
    app, db = app_with_rom_dir
    checksum = rom_checksum(FIXTURES / "game_a.sfc")
    app.state._switch_game(checksum, "game_a", "any%")
    client = TestClient(app)

    data = client.get("/api/state").json()
    assert data["game_id"] == checksum
    assert data["game_name"] == "game_a"


# -- Data isolation ------------------------------------------------------------

def test_reset_is_game_scoped(app_with_rom_dir):
    """Reset only clears data for the active game, not all games."""
    app, db = app_with_rom_dir
    c_a = rom_checksum(FIXTURES / "game_a.sfc")
    c_b = rom_checksum(FIXTURES / "game_b.sfc")

    # Set up game A with data
    app.state._switch_game(c_a, "game_a", "any%")
    s_a = Split(id=f"{c_a}:1:0:normal", game_id=c_a, level_number=1, room_id=0, goal="normal")
    db.upsert_split(s_a)
    db.create_session("sa", c_a)
    db.log_attempt(Attempt(split_id=s_a.id, time_ms=5000, completed=True, session_id="sa"))

    # Set up game B with data
    app.state._switch_game(c_b, "game_b", "any%")
    s_b = Split(id=f"{c_b}:1:0:normal", game_id=c_b, level_number=1, room_id=0, goal="normal")
    db.upsert_split(s_b)
    db.create_session("sb", c_b)
    db.log_attempt(Attempt(split_id=s_b.id, time_ms=6000, completed=True, session_id="sb"))

    # Reset game B (active game)
    client = TestClient(app)
    resp = client.post("/api/reset")
    assert resp.json()["status"] == "ok"

    # Game B data gone
    assert db.get_recent_attempts(c_b) == []
    # Game A data intact
    assert len(db.get_recent_attempts(c_a)) == 1


def test_splits_are_game_scoped(app_with_rom_dir):
    """The /api/splits endpoint returns only splits for the active game."""
    app, db = app_with_rom_dir
    c_a = rom_checksum(FIXTURES / "game_a.sfc")
    c_b = rom_checksum(FIXTURES / "game_b.sfc")

    # Splits for game A
    app.state._switch_game(c_a, "game_a", "any%")
    db.upsert_split(Split(id=f"{c_a}:1:0:normal", game_id=c_a, level_number=1, room_id=0, goal="normal"))
    db.upsert_split(Split(id=f"{c_a}:2:0:normal", game_id=c_a, level_number=2, room_id=0, goal="normal"))

    # Splits for game B
    app.state._switch_game(c_b, "game_b", "any%")
    db.upsert_split(Split(id=f"{c_b}:1:0:key", game_id=c_b, level_number=1, room_id=0, goal="key"))

    client = TestClient(app)

    # While game B is active, should see only 1 split
    data = client.get("/api/splits").json()
    assert len(data["splits"]) == 1
    assert data["splits"][0]["id"] == f"{c_b}:1:0:key"

    # Switch to game A, should see 2 splits
    app.state._switch_game(c_a, "game_a", "any%")
    data = client.get("/api/splits").json()
    assert len(data["splits"]) == 2

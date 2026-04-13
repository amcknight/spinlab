"""Integration tests for multi-game support: switching, isolation, reset scoping."""
import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spinlab.db import Database
from spinlab.models import Segment, Attempt
from spinlab.romid import rom_checksum, game_name_from_filename

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _sync_switch(app, game_id, game_name):
    """Helper to call async switch_game from sync test code."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(app.state.session.switch_game(game_id, game_name))
    finally:
        loop.close()


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
    from conftest import make_test_config

    db = Database(tmp_path / "test.db")
    app = create_app(db=db, config=make_test_config(rom_dir=FIXTURES))
    return app, db


def test_switch_game_creates_db_record(app_with_rom_dir):
    """Switching to a new game auto-creates a game row in the DB."""
    app, db = app_with_rom_dir
    checksum = rom_checksum(FIXTURES / "game_a.sfc")
    _sync_switch(app, checksum, "game_a")
    row = db.conn.execute("SELECT name FROM games WHERE id = ?", (checksum,)).fetchone()
    assert row is not None
    assert row[0] == "game_a"


def test_switch_between_two_games(app_with_rom_dir):
    """Switching games updates state and preserves both games in DB."""
    app, db = app_with_rom_dir
    c_a = rom_checksum(FIXTURES / "game_a.sfc")
    c_b = rom_checksum(FIXTURES / "game_b.sfc")

    _sync_switch(app, c_a, "game_a")
    assert app.state.session.game_id == c_a
    assert app.state.session.game_name == "game_a"

    _sync_switch(app, c_b, "game_b")
    assert app.state.session.game_id == c_b
    assert app.state.session.game_name == "game_b"

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

    _sync_switch(app, c_a, "game_a")
    client = TestClient(app)
    # Access state to trigger scheduler creation
    client.get("/api/state")
    assert app.state.session.scheduler is not None

    # Switch to game B — scheduler should be invalidated
    _sync_switch(app, c_b, "game_b")
    assert app.state.session.scheduler is None


def test_api_state_shows_game_info(app_with_rom_dir):
    """The /api/state endpoint includes game_id and game_name."""
    app, db = app_with_rom_dir
    checksum = rom_checksum(FIXTURES / "game_a.sfc")
    _sync_switch(app, checksum, "game_a")
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
    _sync_switch(app, c_a, "game_a")
    s_a = Segment(
        id=f"{c_a}:1:entrance.0:goal.0",
        game_id=c_a,
        level_number=1,
        start_type="entrance",
        start_ordinal=0,
        end_type="goal",
        end_ordinal=0,
    )
    db.upsert_segment(s_a)
    db.create_session("sa", c_a)
    db.log_attempt(Attempt(segment_id=s_a.id, time_ms=5000, completed=True, session_id="sa"))

    # Set up game B with data
    _sync_switch(app, c_b, "game_b")
    s_b = Segment(
        id=f"{c_b}:1:entrance.0:goal.0",
        game_id=c_b,
        level_number=1,
        start_type="entrance",
        start_ordinal=0,
        end_type="goal",
        end_ordinal=0,
    )
    db.upsert_segment(s_b)
    db.create_session("sb", c_b)
    db.log_attempt(Attempt(segment_id=s_b.id, time_ms=6000, completed=True, session_id="sb"))

    # Reset game B (active game)
    client = TestClient(app)
    resp = client.post("/api/reset")
    assert resp.json()["status"] == "ok"

    # Game B data gone
    assert db.get_recent_attempts(c_b) == []
    # Game A data intact
    assert len(db.get_recent_attempts(c_a)) == 1


def test_segments_are_game_scoped(app_with_rom_dir):
    """The /api/segments endpoint returns only segments for the active game."""
    app, db = app_with_rom_dir
    c_a = rom_checksum(FIXTURES / "game_a.sfc")
    c_b = rom_checksum(FIXTURES / "game_b.sfc")

    # Segments for game A
    _sync_switch(app, c_a, "game_a")
    db.upsert_segment(Segment(
        id=f"{c_a}:1:entrance.0:goal.0",
        game_id=c_a,
        level_number=1,
        start_type="entrance",
        start_ordinal=0,
        end_type="goal",
        end_ordinal=0,
    ))
    db.upsert_segment(Segment(
        id=f"{c_a}:2:entrance.0:goal.0",
        game_id=c_a,
        level_number=2,
        start_type="entrance",
        start_ordinal=0,
        end_type="goal",
        end_ordinal=0,
    ))

    # Segments for game B
    _sync_switch(app, c_b, "game_b")
    db.upsert_segment(Segment(
        id=f"{c_b}:1:entrance.0:checkpoint.0",
        game_id=c_b,
        level_number=1,
        start_type="entrance",
        start_ordinal=0,
        end_type="checkpoint",
        end_ordinal=0,
    ))

    client = TestClient(app)

    # While game B is active, should see only 1 segment
    data = client.get("/api/segments").json()
    assert len(data["segments"]) == 1
    assert data["segments"][0]["id"] == f"{c_b}:1:entrance.0:checkpoint.0"

    # Switch to game A, should see 2 segments
    _sync_switch(app, c_a, "game_a")
    data = client.get("/api/segments").json()
    assert len(data["segments"]) == 2

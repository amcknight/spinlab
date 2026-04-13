"""Tests for PATCH /api/attempts/:id invalidation toggle."""
import pytest
from fastapi.testclient import TestClient

from spinlab.db import Database
from spinlab.models import Attempt, AttemptSource


GAME_ID = "g"


def _seed(db: Database) -> int:
    db.upsert_game(GAME_ID, "Game", "any%")
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, start_type, start_ordinal,"
        " end_type, end_ordinal, created_at, updated_at)"
        " VALUES ('s1', 'g', 1, 'entrance', 0, 'goal', 0, '2026-01-01', '2026-01-01')"
    )
    db.conn.commit()
    return db.log_attempt(Attempt(
        segment_id="s1", session_id="sess1", completed=True,
        time_ms=1000, source=AttemptSource.PRACTICE,
    ))


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


@pytest.fixture
def client(db):
    from spinlab.dashboard import create_app
    from conftest import make_test_config
    app = create_app(db=db, config=make_test_config())
    app.state.session.game_id = GAME_ID
    app.state.session.game_name = "Game"
    return TestClient(app)


def test_patch_attempt_invalidates(db, client):
    """PATCH with invalidated=true marks the attempt as invalidated."""
    aid = _seed(db)
    resp = client.patch(f"/api/attempts/{aid}", json={"invalidated": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["id"] == aid
    assert body["invalidated"] is True

    row = db.conn.execute(
        "SELECT invalidated FROM attempts WHERE id = ?", (aid,)
    ).fetchone()
    assert row[0] == 1


def test_patch_attempt_can_unset(db, client):
    """PATCH with invalidated=false clears a previously set invalidation."""
    aid = _seed(db)
    db.set_attempt_invalidated(aid, True)

    resp = client.patch(f"/api/attempts/{aid}", json={"invalidated": False})
    assert resp.status_code == 200
    assert resp.json()["invalidated"] is False

    row = db.conn.execute(
        "SELECT invalidated FROM attempts WHERE id = ?", (aid,)
    ).fetchone()
    assert row[0] == 0


def test_patch_unknown_attempt_returns_404(client):
    """PATCH for a non-existent attempt id returns 404."""
    resp = client.patch("/api/attempts/99999", json={"invalidated": True})
    assert resp.status_code == 404

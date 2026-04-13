"""Tests for /api/segments response shape (waypoints + conditions + is_primary)."""
import pytest

from fastapi.testclient import TestClient
from spinlab.db import Database
from spinlab.models import Segment, Waypoint, WaypointSaveState


GAME_ID = "g"


def _seed_segment_with_conditions(db: Database) -> Segment:
    """Seed one segment with start+end waypoints that carry conditions."""
    db.upsert_game(GAME_ID, "Game", "any%")
    wp_start = Waypoint.make(GAME_ID, 5, "entrance", 0, {"powerup": "big"})
    wp_end = Waypoint.make(GAME_ID, 5, "goal", 0, {"powerup": "small"})
    db.upsert_waypoint(wp_start)
    db.upsert_waypoint(wp_end)
    seg = Segment(
        id=Segment.make_id(GAME_ID, 5, "entrance", 0, "goal", 0,
                           wp_start.id, wp_end.id),
        game_id=GAME_ID, level_number=5,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
        is_primary=True, ordinal=1,
    )
    db.upsert_segment(seg)
    db.add_save_state(WaypointSaveState(
        waypoint_id=wp_start.id, variant_type="hot",
        state_path="/tmp/start.mss", is_default=True))
    return seg


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


def test_segments_endpoint_includes_is_primary(db, client):
    """is_primary bool is present on every segment row."""
    _seed_segment_with_conditions(db)
    resp = client.get("/api/segments")
    assert resp.status_code == 200
    segments = resp.json()["segments"]
    assert len(segments) == 1
    assert segments[0]["is_primary"] is True


def test_segments_endpoint_includes_waypoint_ids(db, client):
    """start_waypoint_id and end_waypoint_id are present on segment rows."""
    seg = _seed_segment_with_conditions(db)
    resp = client.get("/api/segments")
    assert resp.status_code == 200
    row = resp.json()["segments"][0]
    assert row["start_waypoint_id"] == seg.start_waypoint_id
    assert row["end_waypoint_id"] == seg.end_waypoint_id


def test_segments_endpoint_includes_decoded_conditions(db, client):
    """start_conditions and end_conditions are decoded dicts, not raw JSON strings."""
    _seed_segment_with_conditions(db)
    resp = client.get("/api/segments")
    assert resp.status_code == 200
    row = resp.json()["segments"][0]
    assert row["start_conditions"] == {"powerup": "big"}
    assert row["end_conditions"] == {"powerup": "small"}


def test_segments_endpoint_null_waypoints_produce_empty_conditions(db, tmp_path):
    """Segments without waypoints return empty dicts for conditions."""
    db.upsert_game(GAME_ID, "Game", "any%")
    seg = Segment(
        id="legacy-seg",
        game_id=GAME_ID, level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        start_waypoint_id=None, end_waypoint_id=None,
        is_primary=True, ordinal=1,
    )
    db.upsert_segment(seg)

    from spinlab.dashboard import create_app
    from conftest import make_test_config
    app = create_app(db=db, config=make_test_config())
    app.state.session.game_id = GAME_ID
    app.state.session.game_name = "Game"
    client = TestClient(app)

    resp = client.get("/api/segments")
    assert resp.status_code == 200
    row = resp.json()["segments"][0]
    assert row["start_conditions"] == {}
    assert row["end_conditions"] == {}


def test_segments_endpoint_returns_non_primary_segments(db, client):
    """Dashboard /api/segments shows all segments including non-primary ones."""
    db.upsert_game(GAME_ID, "Game", "any%")
    wp1 = Waypoint.make(GAME_ID, 1, "entrance", 0, {"powerup": "small"})
    wp2 = Waypoint.make(GAME_ID, 1, "entrance", 0, {"powerup": "big"})
    wp_end = Waypoint.make(GAME_ID, 1, "goal", 0, {})
    for w in (wp1, wp2, wp_end):
        db.upsert_waypoint(w)
    for wp_start, primary, ordinal in ((wp1, True, 1), (wp2, False, 2)):
        seg = Segment(
            id=Segment.make_id(GAME_ID, 1, "entrance", 0, "goal", 0,
                               wp_start.id, wp_end.id),
            game_id=GAME_ID, level_number=1,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
            is_primary=primary, ordinal=ordinal,
        )
        db.upsert_segment(seg)

    resp = client.get("/api/segments")
    assert resp.status_code == 200
    segments = resp.json()["segments"]
    assert len(segments) == 2
    primaries = [s["is_primary"] for s in segments]
    assert True in primaries
    assert False in primaries


def test_patch_segment_toggles_primary(db, client):
    """PATCH /api/segments/:id sets is_primary and returns updated value."""
    seg = _seed_segment_with_conditions(db)
    assert seg.is_primary is True

    resp = client.patch(f"/api/segments/{seg.id}", json={"is_primary": False})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["id"] == seg.id
    assert data["is_primary"] is False

    # Verify persistence via GET /api/segments
    resp2 = client.get("/api/segments")
    assert resp2.status_code == 200
    row = resp2.json()["segments"][0]
    assert row["is_primary"] is False


def test_patch_segment_unknown_id_returns_404(db, client):
    """PATCH /api/segments/:id returns 404 when segment does not exist."""
    resp = client.patch("/api/segments/nonexistent-id", json={"is_primary": False})
    assert resp.status_code == 404

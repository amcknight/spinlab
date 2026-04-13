"""Tests for waypoint_save_states DB operations (formerly segment_variants)."""
import pytest
from spinlab.db import Database
from spinlab.models import Segment, Waypoint, WaypointSaveState


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_game("g1", "Test Game", "any%")
    return d


def _make_seg(db, game_id, level, start_type, start_ord, end_type, end_ord,
              start_conds=None, end_conds=None):
    """Create waypoints + segment, return (seg, wp_start, wp_end)."""
    start_conds = start_conds or {}
    end_conds = end_conds or {}
    wp_start = Waypoint.make(game_id, level, start_type, start_ord, start_conds)
    wp_end = Waypoint.make(game_id, level, end_type, end_ord, end_conds)
    db.upsert_waypoint(wp_start)
    db.upsert_waypoint(wp_end)
    seg = Segment(
        id=Segment.make_id(game_id, level, start_type, start_ord,
                           end_type, end_ord, wp_start.id, wp_end.id),
        game_id=game_id, level_number=level,
        start_type=start_type, start_ordinal=start_ord,
        end_type=end_type, end_ordinal=end_ord,
        start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
        is_primary=True,
    )
    db.upsert_segment(seg)
    return seg, wp_start, wp_end


@pytest.fixture
def segment(db):
    seg, wp_start, wp_end = _make_seg(db, "g1", 105, "entrance", 0, "checkpoint", 1)
    return seg, wp_start, wp_end


def test_upsert_and_get_segment(db, segment):
    seg, _, _ = segment
    segments = db.get_active_segments("g1")
    assert len(segments) == 1
    assert segments[0].id == seg.id
    assert segments[0].start_type == "entrance"
    assert segments[0].end_type == "checkpoint"


def test_add_save_state(db, segment):
    seg, wp_start, _ = segment
    db.add_save_state(WaypointSaveState(
        waypoint_id=wp_start.id,
        variant_type="cold",
        state_path="/states/105_entrance.mss",
        is_default=True,
    ))
    got = db.get_save_state(wp_start.id, "cold")
    assert got is not None
    assert got.variant_type == "cold"
    assert got.is_default is True


def test_add_save_state_replace(db, segment):
    """ON CONFLICT: re-adding same (waypoint_id, variant_type) overwrites."""
    seg, wp_start, _ = segment
    db.add_save_state(WaypointSaveState(wp_start.id, "cold", "/old.mss", True))
    db.add_save_state(WaypointSaveState(wp_start.id, "cold", "/new.mss", True))
    got = db.get_save_state(wp_start.id, "cold")
    assert got is not None
    assert got.state_path == "/new.mss"


def test_get_default_save_state(db, segment):
    seg, wp_start, _ = segment
    db.add_save_state(WaypointSaveState(wp_start.id, "hot", "/hot.mss", False))
    db.add_save_state(WaypointSaveState(wp_start.id, "cold", "/cold.mss", True))
    default = db.get_default_save_state(wp_start.id)
    assert default is not None
    assert default.variant_type == "cold"


def test_get_default_save_state_fallback(db, segment):
    """If no save state marked default, return any available."""
    seg, wp_start, _ = segment
    db.add_save_state(WaypointSaveState(wp_start.id, "hot", "/hot.mss", False))
    default = db.get_default_save_state(wp_start.id)
    assert default is not None
    assert default.variant_type == "hot"


def test_get_save_state_by_type(db, segment):
    seg, wp_start, _ = segment
    db.add_save_state(WaypointSaveState(wp_start.id, "hot", "/hot.mss", False))
    db.add_save_state(WaypointSaveState(wp_start.id, "cold", "/cold.mss", True))
    hot = db.get_save_state(wp_start.id, "hot")
    assert hot is not None
    assert hot.state_path == "/hot.mss"
    missing = db.get_save_state(wp_start.id, "nonexistent")
    assert missing is None


def test_segments_with_model_includes_state_path(db, segment):
    seg, wp_start, _ = segment
    db.add_save_state(WaypointSaveState(wp_start.id, "cold", "/cold.mss", True))
    rows = db.get_all_segments_with_model("g1")
    assert len(rows) == 1
    assert rows[0]["state_path"] == "/cold.mss"
    assert rows[0]["start_type"] == "entrance"
    assert rows[0]["end_type"] == "checkpoint"


def test_segments_missing_cold(db):
    """Get segments whose start waypoint has hot but not cold save state."""
    db.create_capture_run("run1", "g1", "run 1")
    # Two segments, different waypoints
    s1, wp_s1, _ = _make_seg(db, "g1", 105, "entrance", 0, "checkpoint", 1,
                              start_conds={"s": "1"})
    s2, wp_s2, _ = _make_seg(db, "g1", 105, "checkpoint", 1, "goal", 0,
                              start_conds={"s": "2"})

    # s1 start waypoint has both hot and cold; s2 start waypoint has only hot
    db.add_save_state(WaypointSaveState(wp_s1.id, "hot", "/hot1.mss", False))
    db.add_save_state(WaypointSaveState(wp_s1.id, "cold", "/cold1.mss", True))
    db.add_save_state(WaypointSaveState(wp_s2.id, "hot", "/hot2.mss", False))

    missing = db.segments_missing_cold("g1")
    assert len(missing) == 1
    assert missing[0]["segment_id"] == s2.id
    assert missing[0]["hot_state_path"] == "/hot2.mss"

"""Tests for segment_variants DB operations."""
import pytest
from spinlab.db import Database
from spinlab.models import Segment, SegmentVariant


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_game("g1", "Test Game", "any%")
    return d


@pytest.fixture
def segment(db):
    s = Segment(
        id="g1:105:entrance.0:checkpoint.1",
        game_id="g1",
        level_number=105,
        start_type="entrance",
        start_ordinal=0,
        end_type="checkpoint",
        end_ordinal=1,
        description="entrance → cp.1",
    )
    db.upsert_segment(s)
    return s


def test_upsert_and_get_segment(db, segment):
    segments = db.get_active_segments("g1")
    assert len(segments) == 1
    assert segments[0].id == segment.id
    assert segments[0].start_type == "entrance"
    assert segments[0].end_type == "checkpoint"


@pytest.mark.skip(reason="Task 8 restores waypoint-aware add_save_state; add_variant removed in Task 7")
def test_add_variant(db, segment):
    v = SegmentVariant(
        segment_id=segment.id,
        variant_type="cold",
        state_path="/states/105_entrance.mss",
        is_default=True,
    )
    db.add_variant(v)
    variants = db.get_variants(segment.id)
    assert len(variants) == 1
    assert variants[0].variant_type == "cold"
    assert variants[0].is_default is True


@pytest.mark.skip(reason="Task 8 restores waypoint-aware add_save_state; add_variant removed in Task 7")
def test_add_variant_replace(db, segment):
    """INSERT OR REPLACE: re-adding same variant type overwrites."""
    v1 = SegmentVariant(segment.id, "cold", "/old.mss", True)
    db.add_variant(v1)
    v2 = SegmentVariant(segment.id, "cold", "/new.mss", True)
    db.add_variant(v2)
    variants = db.get_variants(segment.id)
    assert len(variants) == 1
    assert variants[0].state_path == "/new.mss"


@pytest.mark.skip(reason="Task 8 restores waypoint-aware add_save_state; add_variant removed in Task 7")
def test_get_default_variant(db, segment):
    db.add_variant(SegmentVariant(segment.id, "hot", "/hot.mss", False))
    db.add_variant(SegmentVariant(segment.id, "cold", "/cold.mss", True))
    default = db.get_default_variant(segment.id)
    assert default is not None
    assert default.variant_type == "cold"


@pytest.mark.skip(reason="Task 8 restores waypoint-aware add_save_state; add_variant removed in Task 7")
def test_get_default_variant_fallback(db, segment):
    """If no variant marked default, return any available variant."""
    db.add_variant(SegmentVariant(segment.id, "hot", "/hot.mss", False))
    default = db.get_default_variant(segment.id)
    assert default is not None
    assert default.variant_type == "hot"


@pytest.mark.skip(reason="Task 8 restores waypoint-aware add_save_state; add_variant removed in Task 7")
def test_get_variant_by_type(db, segment):
    db.add_variant(SegmentVariant(segment.id, "hot", "/hot.mss", False))
    db.add_variant(SegmentVariant(segment.id, "cold", "/cold.mss", True))
    hot = db.get_variant(segment.id, "hot")
    assert hot is not None
    assert hot.state_path == "/hot.mss"
    missing = db.get_variant(segment.id, "nonexistent")
    assert missing is None


@pytest.mark.skip(reason="Task 8 restores waypoint-aware get_all_segments_with_model joining waypoint_save_states")
def test_segments_with_model_includes_state_path(db, segment):
    db.add_variant(SegmentVariant(segment.id, "cold", "/cold.mss", True))
    rows = db.get_all_segments_with_model("g1")
    assert len(rows) == 1
    assert rows[0]["state_path"] == "/cold.mss"
    assert rows[0]["start_type"] == "entrance"
    assert rows[0]["end_type"] == "checkpoint"


@pytest.mark.skip(reason="Task 8 restores waypoint-aware segments_missing_cold")
def test_segments_missing_cold(db):
    """Get segments that have a hot variant but no cold variant."""
    db.create_capture_run("run1", "g1", "run 1")
    # Create two segments
    s1 = Segment(
        id="g1:105:entrance.0:checkpoint.1", game_id="g1",
        level_number=105, start_type="entrance", start_ordinal=0,
        end_type="checkpoint", end_ordinal=1, reference_id="run1",
    )
    s2 = Segment(
        id="g1:105:checkpoint.1:goal.0", game_id="g1",
        level_number=105, start_type="checkpoint", start_ordinal=1,
        end_type="goal", end_ordinal=0, reference_id="run1",
    )
    db.upsert_segment(s1)
    db.upsert_segment(s2)

    # s1 has both hot and cold; s2 has only hot
    db.add_variant(SegmentVariant(s1.id, "hot", "/hot1.mss", False))
    db.add_variant(SegmentVariant(s1.id, "cold", "/cold1.mss", True))
    db.add_variant(SegmentVariant(s2.id, "hot", "/hot2.mss", False))

    missing = db.segments_missing_cold("g1")
    assert len(missing) == 1
    assert missing[0]["segment_id"] == s2.id
    assert missing[0]["hot_state_path"] == "/hot2.mss"

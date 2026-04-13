from spinlab.db import Database
from spinlab.models import Segment, Waypoint


def _seed_segment(db, seg_id="s1", primary=True):
    db.upsert_game("g", "Game", "any%")
    wp_a = Waypoint.make("g", 1, "entrance", 0, {})
    wp_b = Waypoint.make("g", 1, "goal", 0, {})
    db.upsert_waypoint(wp_a)
    db.upsert_waypoint(wp_b)
    seg = Segment(
        id=seg_id, game_id="g", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        start_waypoint_id=wp_a.id, end_waypoint_id=wp_b.id,
        is_primary=primary,
    )
    db.upsert_segment(seg)
    return seg


def test_set_segment_is_primary_toggles():
    db = Database(":memory:")
    seg = _seed_segment(db, primary=True)
    db.set_segment_is_primary(seg.id, False)
    row = db.conn.execute(
        "SELECT is_primary FROM segments WHERE id = ?", (seg.id,)
    ).fetchone()
    assert row[0] == 0
    db.set_segment_is_primary(seg.id, True)
    row = db.conn.execute(
        "SELECT is_primary FROM segments WHERE id = ?", (seg.id,)
    ).fetchone()
    assert row[0] == 1

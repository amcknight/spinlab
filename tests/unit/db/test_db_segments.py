"""Tests for segment capture_session_id round-trip."""
from spinlab.db import Database
from spinlab.models import EndpointType, Segment, Waypoint


def test_segment_persists_capture_session_id():
    d = Database(":memory:")
    d.upsert_game("smw", "Super Mario World", "any%")
    d.create_capture_run("run_1", "smw", "Test Run", draft=True)
    d.create_capture_session("sess_1", "run_1", 1, "/tmp/1.spinrec")
    wp_a = Waypoint.make("smw", 1, EndpointType.ENTRANCE, 0, {})
    wp_b = Waypoint.make("smw", 1, EndpointType.GOAL, 0, {})
    d.upsert_waypoint(wp_a)
    d.upsert_waypoint(wp_b)
    seg = Segment(
        id="seg_x", game_id="smw", level_number=1,
        start_type=EndpointType.ENTRANCE, start_ordinal=0,
        end_type=EndpointType.GOAL, end_ordinal=0,
        start_waypoint_id=wp_a.id, end_waypoint_id=wp_b.id,
        reference_id="run_1", capture_session_id="sess_1",
    )
    d.upsert_segment(seg)
    fetched = d.get_segment_by_id("seg_x")
    assert fetched is not None
    assert fetched.capture_session_id == "sess_1"
    d.close()

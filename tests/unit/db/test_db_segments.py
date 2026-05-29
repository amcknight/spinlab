"""Tests for segment capture_session_id round-trip."""
from spinlab.db import Database
from spinlab.models import EndpointType, Segment, Waypoint


def test_segment_persists_capture_session_id():
    d = Database(":memory:")
    d.upsert_game("smw", "Super Mario World", "any%")
    d.create_capture_run("run_1", "smw", "Test Run", kind="live")
    d.create_capture_session("sess_1", "run_1", 1)
    wp_a = Waypoint.make("smw", 1, EndpointType.ENTRANCE, 0, {})
    wp_b = Waypoint.make("smw", 1, EndpointType.GOAL, 0, {})
    d.upsert_waypoint(wp_a)
    d.upsert_waypoint(wp_b)
    seg = Segment(
        id="seg_x", game_id="smw", level_number=1,
        start_type=EndpointType.ENTRANCE, start_ordinal=0,
        end_type=EndpointType.GOAL, end_ordinal=0,
        start_waypoint_id=wp_a.id, end_waypoint_id=wp_b.id,
        capture_run_id="run_1", capture_session_id="sess_1",
    )
    d.upsert_segment(seg)
    fetched = d.get_segment_by_id("seg_x")
    assert fetched is not None
    assert fetched.capture_session_id == "sess_1"
    d.close()


def test_count_segments_for_run(tmp_path):
    from spinlab.db import Database
    from spinlab.models import Segment

    db = Database(tmp_path / "t.db")
    db.upsert_game("g", "G", "any%")
    db.create_capture_run("ref1", "g", "Run 1")

    def _seg(seg_id: str, active: bool = True):
        db.upsert_segment(Segment(
            id=seg_id, game_id="g", level_number=1,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            capture_run_id="ref1", active=active,
        ))

    _seg("s1", active=True)
    _seg("s2", active=True)
    _seg("s3", active=False)

    assert db.count_segments_for_run("ref1") == 3
    assert db.count_segments_for_run("ref1", active_only=True) == 2
    assert db.count_segments_for_run("missing") == 0


def test_count_segments_for_capture_session(tmp_path):
    from spinlab.db import Database
    from spinlab.models import Segment

    db = Database(tmp_path / "t.db")
    db.upsert_game("g", "G", "any%")
    db.create_capture_run("ref1", "g", "Run 1")
    db.create_capture_session(session_id="sess1", capture_run_id="ref1", ordinal=1)
    db.create_capture_session(session_id="sess2", capture_run_id="ref1", ordinal=2)

    def _seg(seg_id: str, sess_id: str | None):
        db.upsert_segment(Segment(
            id=seg_id, game_id="g", level_number=1,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            capture_run_id="ref1", capture_session_id=sess_id,
        ))

    _seg("s1", "sess1")
    _seg("s2", "sess1")
    _seg("s3", "sess2")

    assert db.count_segments_for_capture_session("sess1") == 2
    assert db.count_segments_for_capture_session("sess2") == 1
    assert db.count_segments_for_capture_session("missing") == 0


def test_segments_missing_cold_scoped_by_run(tmp_path):
    from spinlab.db import Database
    from spinlab.models import Segment, Waypoint, WaypointSaveState

    db = Database(tmp_path / "t.db")
    db.upsert_game("g1", "Game", "any%")

    # idx_one_live_draft_per_game prevents two live drafts; promote rA first.
    db.create_capture_run("rA", "g1", "Run A", kind="live")
    db.promote_draft("rA", "Run A")
    db.create_capture_run("rB", "g1", "Run B", kind="live")

    def mk(seg_id: str, run_id: str, ordinal: int) -> None:
        """Create a waypoint + segment + hot save state. Distinct ordinals → distinct wp ids."""
        wp = Waypoint.make("g1", 1, "checkpoint", ordinal, {})
        db.upsert_waypoint(wp)
        db.upsert_segment(Segment(
            id=seg_id, game_id="g1", level_number=1,
            start_type="checkpoint", start_ordinal=ordinal,
            end_type="goal", end_ordinal=0,
            start_waypoint_id=wp.id, end_waypoint_id=wp.id,
            capture_run_id=run_id,
        ))
        db.add_save_state(WaypointSaveState(wp.id, "hot", f"/{seg_id}.state"))

    mk("segA", "rA", 1)
    mk("segB", "rB", 2)

    all_gaps = {g["segment_id"] for g in db.segments_missing_cold("g1")}
    assert all_gaps == {"segA", "segB"}
    scoped = {g["segment_id"] for g in db.segments_missing_cold("g1", run_id="rA")}
    assert scoped == {"segA"}

    db.close()


def test_has_competing_active_segment(tmp_path):
    from spinlab.db import Database
    from spinlab.models import Segment

    db = Database(tmp_path / "t.db")
    db.upsert_game("g", "G", "any%")

    def _seg(seg_id: str, active: bool = True):
        db.upsert_segment(Segment(
            id=seg_id, game_id="g", level_number=1,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            active=active,
        ))

    _seg("existing", active=True)

    # Same endpoints, different id, existing is active → competing
    assert db.has_competing_active_segment(
        game_id="g", level=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        exclude_segment_id="new_seg",
    ) is True

    # Different endpoints → no competition
    assert db.has_competing_active_segment(
        game_id="g", level=2,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        exclude_segment_id="new_seg",
    ) is False

    # Excluding the only matching segment → no competition
    assert db.has_competing_active_segment(
        game_id="g", level=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        exclude_segment_id="existing",
    ) is False

    # Existing is inactive → no competition
    _seg("inactive", active=False)
    db.deactivate_segment("existing")
    assert db.has_competing_active_segment(
        game_id="g", level=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        exclude_segment_id="new_seg",
    ) is False

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


def test_upsert_segment_preserves_original_run_owner():
    """A re-upsert (e.g. a Replay re-running the detector) must NOT steal a
    segment's capture_run_id from the run that first captured it."""
    d = Database(":memory:")
    d.upsert_game("smw", "Super Mario World", "any%")
    d.create_capture_run("live1", "smw", "Live", kind="live")
    d.create_capture_run("replay1", "smw", "Replay", kind="replay")
    d.create_capture_session("sessL", "live1", 1)
    d.create_capture_session("sessR", "replay1", 1)
    wp_a = Waypoint.make("smw", 1, EndpointType.ENTRANCE, 0, {})
    wp_b = Waypoint.make("smw", 1, EndpointType.GOAL, 0, {})
    d.upsert_waypoint(wp_a)
    d.upsert_waypoint(wp_b)
    base = dict(
        id="seg_x", game_id="smw", level_number=1,
        start_type=EndpointType.ENTRANCE, start_ordinal=0,
        end_type=EndpointType.GOAL, end_ordinal=0,
        start_waypoint_id=wp_a.id, end_waypoint_id=wp_b.id,
    )
    d.upsert_segment(Segment(**base, capture_run_id="live1", capture_session_id="sessL"))
    # Replay re-captures the same segment id under a different run.
    d.upsert_segment(Segment(**base, capture_run_id="replay1", capture_session_id="sessR"))
    fetched = d.get_segment_by_id("seg_x")
    assert fetched is not None
    assert fetched.capture_run_id == "live1"          # original owner kept
    assert fetched.capture_session_id == "sessL"
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


def test_count_segments_traversed_in_run_counts_segments_owned_by_other_runs(tmp_path):
    """The run-segment counter must count every segment the run *traversed*
    (recorded an event for), even ones still *owned* by an earlier run.

    Ownership is first-writer-wins (see upsert_segment), so a re-record of an
    already-captured level owns 0 segments — but it traversed them, and the
    recorder still wrote event rows stamped with the new run id. The counter
    must reflect those traversals, not ownership.
    """
    from spinlab.db import Database
    from spinlab.models import (
        AttemptOutcome, AttemptSource, EventAttempt, Segment,
    )

    db = Database(tmp_path / "t.db")
    db.upsert_game("g", "G", "any%")
    db.create_capture_run("old", "g", "Old", kind="live")
    db.promote_draft("old", "Old")          # free the single-live-draft slot
    db.create_capture_run("new", "g", "New", kind="live")

    # seg1 is OWNED by the old run.
    db.upsert_segment(Segment(
        id="seg1", game_id="g", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        capture_run_id="old",
    ))

    # The new run re-traverses seg1: an event row stamped with the new run id.
    db.log_event_attempt(EventAttempt(
        segment_id="seg1", episode_id="ep1",
        outcome=AttemptOutcome.SURVIVED, time_ms=1234,
        capture_run_id="new", source=AttemptSource.REFERENCE,
    ))

    assert db.count_segments_for_run("new") == 0           # owns nothing
    assert db.count_segments_traversed_in_run("new") == 1  # traversed seg1
    db.close()


def test_count_segments_traversed_in_run_is_distinct(tmp_path):
    """Multiple events for one segment count once; distinct segments add up."""
    from spinlab.db import Database
    from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt, Segment

    db = Database(tmp_path / "t.db")
    db.upsert_game("g", "G", "any%")
    db.create_capture_run("r", "g", "R", kind="live")
    for sid in ("s1", "s2"):
        db.upsert_segment(Segment(
            id=sid, game_id="g", level_number=1,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0, capture_run_id="r",
        ))

    def ev(sid: str) -> None:
        db.log_event_attempt(EventAttempt(
            segment_id=sid, episode_id="ep", outcome=AttemptOutcome.SURVIVED,
            time_ms=1, capture_run_id="r", source=AttemptSource.REFERENCE,
        ))

    ev("s1"); ev("s1"); ev("s2")   # 3 events, 2 distinct segments
    assert db.count_segments_traversed_in_run("r") == 2
    db.close()

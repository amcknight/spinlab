"""Tests for spinlab.models."""

from spinlab.models import Segment, Waypoint


def test_waypoint_id_is_deterministic():
    a = Waypoint.make("game1", 5, "checkpoint", 1, {"powerup": "big"})
    b = Waypoint.make("game1", 5, "checkpoint", 1, {"powerup": "big"})
    assert a.id == b.id


def test_waypoint_id_differs_by_conditions():
    a = Waypoint.make("game1", 5, "checkpoint", 1, {"powerup": "big"})
    b = Waypoint.make("game1", 5, "checkpoint", 1, {"powerup": "small"})
    assert a.id != b.id


def test_waypoint_conditions_are_canonical_json():
    # key order in input must not affect id
    a = Waypoint.make("g", 1, "goal", 0, {"a": 1, "b": 2})
    b = Waypoint.make("g", 1, "goal", 0, {"b": 2, "a": 1})
    assert a.id == b.id
    assert a.conditions_json == '{"a": 1, "b": 2}'


def test_empty_conditions():
    w = Waypoint.make("g", 1, "entrance", 0, {})
    assert w.conditions_json == "{}"


def test_segment_id_includes_waypoint_ids():
    wp_a = Waypoint.make("g", 5, "entrance", 0, {"powerup": "small"})
    wp_b = Waypoint.make("g", 5, "goal", 0, {"powerup": "small"})
    wp_c = Waypoint.make("g", 5, "entrance", 0, {"powerup": "big"})
    id_small = Segment.make_id("g", 5, "entrance", 0, "goal", 0, wp_a.id, wp_b.id)
    id_big   = Segment.make_id("g", 5, "entrance", 0, "goal", 0, wp_c.id, wp_b.id)
    assert id_small != id_big
    # Same waypoints → same segment id
    assert id_small == Segment.make_id("g", 5, "entrance", 0, "goal", 0, wp_a.id, wp_b.id)

def test_segment_is_primary_default_true():
    wp_a = Waypoint.make("g", 1, "entrance", 0, {})
    wp_b = Waypoint.make("g", 1, "goal", 0, {})
    seg = Segment(
        id=Segment.make_id("g", 1, "entrance", 0, "goal", 0, wp_a.id, wp_b.id),
        game_id="g", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        start_waypoint_id=wp_a.id, end_waypoint_id=wp_b.id,
    )
    assert seg.is_primary is True


from spinlab.models import Attempt, AttemptSource


def test_attempt_has_observed_conditions_and_invalidated():
    a = Attempt(
        segment_id="s1", session_id="sess1", completed=True,
        time_ms=1000, source=AttemptSource.PRACTICE, deaths=0,
        observed_start_conditions='{"powerup": "big"}',
        observed_end_conditions='{"powerup": "small"}',
    )
    assert a.observed_start_conditions == '{"powerup": "big"}'
    assert a.observed_end_conditions == '{"powerup": "small"}'
    assert a.invalidated is False

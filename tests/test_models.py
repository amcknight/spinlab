"""Tests for spinlab.models."""

from spinlab.models import Waypoint


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

"""Tests for spinlab.models."""

import pytest

from spinlab.models import Attempt, Waypoint


def test_waypoint_conditions_are_canonical_json():
    # key order in input must not affect id — Waypoint.make canonicalizes
    a = Waypoint.make("g", 1, "goal", 0, {"a": 1, "b": 2})
    b = Waypoint.make("g", 1, "goal", 0, {"b": 2, "a": 1})
    assert a.id == b.id
    assert a.conditions_json == '{"a": 1, "b": 2}'


def test_attempt_requires_exactly_one_parent():
    """The Attempt model enforces the XOR invariant on session_id / capture_run_id."""
    with pytest.raises(ValueError):
        Attempt(segment_id="s1", completed=True)
    with pytest.raises(ValueError):
        Attempt(segment_id="s1", session_id="s", capture_run_id="r", completed=True)

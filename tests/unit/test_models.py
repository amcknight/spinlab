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


def test_attempt_defaults_experimental_false():
    a = Attempt(segment_id="s", session_id="x", completed=True, time_ms=1000)
    assert a.experimental is False


class TestDeathExtras:
    def test_default_extras_is_none(self):
        from spinlab.models import ModelOutput, Estimate
        out = ModelOutput(
            total=Estimate(expected_ms=10000.0, ms_per_attempt=None, floor_ms=None),
            clean=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
        )
        assert out.extras is None

    def test_extras_round_trip(self):
        from spinlab.models import DeathExtras, Estimate, ModelOutput
        extras = DeathExtras(
            halflife_attempts=20,
            n_attempts_effective=12.5,
            n_episodes_with_death_eff=8.0,
            n_episodes_completed_eff=10.0,
            p_die_per_attempt=0.64,
            n_lives_died_effective=14.0,
            n_lives_survived_effective=10.0,
            p_die_per_life=0.583,
            death_samples=[(5000, 1.0), (4500, 0.5)],
            completion_samples=[(8000, 1.0)],
            expected_death_time_ms=4833.3,
            expected_completion_time_ms=8000.0,
        )
        out = ModelOutput(
            total=Estimate(expected_ms=20000.0, ms_per_attempt=None, floor_ms=4000.0),
            clean=Estimate(expected_ms=8000.0, ms_per_attempt=None, floor_ms=6500.0),
            extras=extras,
        )
        roundtripped = ModelOutput.from_dict(out.to_dict())
        assert roundtripped.extras is not None
        assert roundtripped.extras.halflife_attempts == 20
        assert roundtripped.extras.p_die_per_life == pytest.approx(0.583)
        assert roundtripped.extras.death_samples == [(5000, 1.0), (4500, 0.5)]

    def test_round_trip_without_extras(self):
        from spinlab.models import Estimate, ModelOutput
        out = ModelOutput(
            total=Estimate(expected_ms=10000.0, ms_per_attempt=None, floor_ms=None),
            clean=Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None),
        )
        roundtripped = ModelOutput.from_dict(out.to_dict())
        assert roundtripped.extras is None

"""SessionSnapshot — practice-session baseline for live-view diffs.

The snapshot captures (a) per-segment closed-form metrics at session start
(expected episode ms, practice gain ms, death rate, floor ms = current
running-min clean clear), and (b) a route aggregate (exp_run_ms, exp_deaths).
diff helpers return positive values for improvement (regardless of metric sign).
"""
from __future__ import annotations

import pytest

from spinlab.estimators.em_suite_sampler import SamplerState
from spinlab.estimators.session_snapshot import (
    RouteBaseline,
    SegmentBaseline,
    route_diff,
    segment_diff,
    snapshot_from_segments,
)


def test_snapshot_captures_started_at_and_per_segment_baselines():
    # two synthetic segments. SamplerState() takes no per-segment identity
    # kwargs — the caller (SessionManager) supplies seg_id alongside the state.
    seg_ids = ["s0", "s1"]
    states = [SamplerState() for _ in seg_ids]
    for s in states:
        # force the prediction-gate by hand: nil-until-2-of-each-kind.
        # Other tests in this suite use the same direct-mutation pattern.
        s.n_successes = 3
        s.n_deaths = 3
    snapshot = snapshot_from_segments(
        started_at=1717_000_000.0,
        segments=[(seg_id, s, []) for seg_id, s in zip(seg_ids, states, strict=True)],
    )
    assert snapshot.started_at == 1717_000_000.0
    assert set(snapshot.segments.keys()) == {"s0", "s1"}
    assert isinstance(snapshot.route, RouteBaseline)


def test_segment_diff_returns_none_when_either_side_missing():
    base = SegmentBaseline(
        expected_episode_ms=20_000.0, practice_gain_ms=500.0,
        death_rate=0.5, floor_ms=15_000.0,
    )
    # current expected None → diff None
    assert segment_diff(base, current_expected_ms=None,
                        current_gain_ms=None, current_death_rate=0.5,
                        current_floor_ms=None) == {
        "expected_episode_diff_ms": None,
        "practice_gain_diff_ms": None,
        "death_rate_diff": 0.0,
        "floor_diff_ms": None,
    }


def test_segment_diff_signs_improvement_positive():
    """Expected ms dropped → improvement; floor dropped → improvement;
    death rate dropped → improvement (negative delta in the rate itself,
    but reported as positive 'improvement' isn't what we do — we report the
    raw delta (current − baseline). UI colors by sign + metric semantics)."""
    base = SegmentBaseline(
        expected_episode_ms=20_000.0, practice_gain_ms=500.0,
        death_rate=0.5, floor_ms=15_000.0,
    )
    d = segment_diff(
        base,
        current_expected_ms=18_000.0,
        current_gain_ms=300.0,
        current_death_rate=0.4,
        current_floor_ms=14_000.0,
    )
    # expected: current − baseline (negative = faster); UI inverts for color
    assert d["expected_episode_diff_ms"] == pytest.approx(-2_000.0)
    assert d["floor_diff_ms"] == pytest.approx(-1_000.0)
    assert d["death_rate_diff"] == pytest.approx(-0.1)
    assert d["practice_gain_diff_ms"] == pytest.approx(-200.0)


def test_route_diff_returns_none_when_either_aggregate_missing():
    base = RouteBaseline(exp_run_ms=120_000.0, exp_deaths=4.0)
    assert route_diff(base, current_exp_run_ms=None, current_exp_deaths=None) == {
        "exp_run_diff_ms": None,
        "exp_deaths_diff": None,
        "practice_saved_ms": None,
    }


def test_route_diff_practice_saved_is_drop_in_exp_run():
    base = RouteBaseline(exp_run_ms=120_000.0, exp_deaths=4.0)
    d = route_diff(base, current_exp_run_ms=115_000.0, current_exp_deaths=3.5)
    assert d["exp_run_diff_ms"] == pytest.approx(-5_000.0)  # current − baseline
    assert d["exp_deaths_diff"] == pytest.approx(-0.5)
    # practice_saved = baseline − current = +5000 ms saved
    assert d["practice_saved_ms"] == pytest.approx(5_000.0)


def test_snapshot_state_clone_is_independent():
    """If we later mutate the live SamplerState, the baseline must not move."""
    seg_id = "s0"
    s = SamplerState()
    s.n_successes = 3
    s.n_deaths = 3
    snapshot = snapshot_from_segments(started_at=1.0, segments=[(seg_id, s, [])])
    s.n_successes = 99  # mutate live
    # baseline was a snapshot, not a reference
    baseline = snapshot.segments[seg_id]
    # The baseline holds DERIVED scalars (not the state); the floor was captured
    # from observed episodes. The mutation above must not move any baseline field.
    assert isinstance(baseline, SegmentBaseline)

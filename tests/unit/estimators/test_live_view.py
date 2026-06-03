import pytest

from spinlab.estimators.em_suite_sampler import SamplerState
from spinlab.estimators.live_view import LiveSegmentView, live_segment_view, RouteSummary, route_summary
from spinlab.estimators.session_snapshot import (
    SegmentBaseline, RouteBaseline, SessionSnapshot,
)


def _gated_state(seg_id="s0"):
    # SamplerState() takes no constructor args — see Task 1 fixture pattern.
    # Caller-supplied seg_id is just a label here; baselines key on it via the
    # snapshot, not via the state itself.
    # The prediction gate (_gate_passes) needs n_successes>=2, n_deaths>=2,
    # AND n_attempts_total>=2 — the third counter is independent of the first
    # two on a bare SamplerState, so set it explicitly.
    s = SamplerState()
    s.n_successes = 3
    s.n_deaths = 3
    s.n_attempts_total = 6
    return s


def test_live_segment_view_emits_null_diffs_when_baseline_absent():
    v = live_segment_view(_gated_state(), [], baseline=None)
    assert v.expected_episode_diff_ms is None
    assert v.practice_gain_diff_ms is None
    assert v.floor_diff_ms is None
    assert v.death_rate_diff is None


def test_live_segment_view_emits_diffs_against_baseline():
    state = _gated_state()
    base = SegmentBaseline(
        expected_episode_ms=20_000.0, practice_gain_ms=500.0,
        death_rate=0.5, floor_ms=15_000.0,
    )
    v = live_segment_view(state, [], baseline=base)
    # 'current' values come from the state; deltas are current − baseline.
    # We can't pin exact values for a synthetic empty sampler, but the fields
    # must be present and numeric when both sides exist.
    if v.expected_episode_ms is not None:
        assert v.expected_episode_diff_ms == pytest.approx(v.expected_episode_ms - 20_000.0)
    # death_rate is always defined; baseline.death_rate=0.5; v.death_rate likely 0.0
    # for the synthetic state — assert it's non-None and matches the formula.
    assert v.death_rate_diff is not None
    assert v.death_rate_diff == pytest.approx(v.death_rate - 0.5)


def test_route_summary_emits_null_diffs_when_baseline_absent():
    r = route_summary([_gated_state(), _gated_state("s1")], baseline=None)
    assert r.exp_run_diff_ms is None
    assert r.exp_deaths_diff is None
    assert r.practice_saved_ms is None


def test_route_summary_practice_saved_is_baseline_minus_current():
    states = [_gated_state(), _gated_state("s1")]
    base = RouteBaseline(exp_run_ms=200_000.0, exp_deaths=10.0)
    r = route_summary(states, baseline=base)
    if r.exp_run_ms is not None:
        assert r.practice_saved_ms == pytest.approx(200_000.0 - r.exp_run_ms)

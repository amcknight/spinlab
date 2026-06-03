import pytest
from tests.factories import make_event_attempt

from spinlab.estimators.em_suite_sampler import SamplerState, process_event
from spinlab.estimators.live_view import live_segment_view, route_summary
from spinlab.estimators.session_snapshot import RouteBaseline, SegmentBaseline


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


def _real_scalar_state() -> SamplerState:
    """Seed a SamplerState through real `process_event` calls so the
    closed-form scalar (`expected_episode_time_scalar`) actually returns a
    float — not None. The bare-counter `_gated_state` passes the prediction
    gate but leaves every Sum/Denom accumulator at 0, so the EMA reads None
    and the formula short-circuits. Using `process_event` is the only honest
    way to populate both counters and accumulators consistently."""
    st = SamplerState()
    for outcome, t in [("survived", 2000), ("died", 500), ("survived", 2100),
                       ("died", 600), ("survived", 1900), ("died", 550)]:
        st = process_event(st, make_event_attempt(outcome=outcome, time_ms=t))
    return st


def test_live_segment_view_emits_null_diffs_when_baseline_absent():
    v = live_segment_view(_gated_state(), [], baseline=None)
    assert v.expected_episode_diff_ms is None
    assert v.practice_gain_diff_ms is None
    assert v.floor_diff_ms is None
    assert v.death_rate_diff is None


def test_live_segment_view_emits_diffs_against_baseline():
    state = _real_scalar_state()
    base = SegmentBaseline(
        expected_episode_ms=20_000.0, practice_gain_ms=500.0,
        death_rate=0.5, floor_ms=15_000.0,
    )
    # One clean completion so floor_ms scans a non-empty list and the
    # floor_diff_ms formula assertion actually executes.
    episodes = [{
        "completed": True, "invalidated": False,
        "time_ms": 12_000, "deaths": 0, "clean_tail_ms": 12_000,
    }]
    v = live_segment_view(state, episodes, baseline=base)
    # All four formula assertions MUST execute — no `if ... is not None` guard.
    assert v.expected_episode_ms is not None
    assert v.expected_episode_diff_ms == pytest.approx(v.expected_episode_ms - 20_000.0)
    assert v.practice_gain_ms is not None
    assert v.practice_gain_diff_ms == pytest.approx(v.practice_gain_ms - 500.0)
    assert v.death_rate_diff == pytest.approx(v.death_rate - 0.5)
    assert v.floor_ms is not None
    assert v.floor_diff_ms == pytest.approx(v.floor_ms - 15_000.0)


def test_route_summary_emits_null_diffs_when_baseline_absent():
    r = route_summary([_gated_state(), _gated_state("s1")], baseline=None)
    assert r.exp_run_diff_ms is None
    assert r.exp_deaths_diff is None
    assert r.practice_saved_ms is None


def test_route_summary_practice_saved_is_baseline_minus_current():
    # Two real-scalar states so exp_run_ms is guaranteed non-None and the
    # practice_saved_ms formula assertion actually executes.
    states = [_real_scalar_state(), _real_scalar_state()]
    base = RouteBaseline(exp_run_ms=200_000.0, exp_deaths=10.0)
    r = route_summary(states, baseline=base)
    assert r.exp_run_ms is not None
    assert r.practice_saved_ms == pytest.approx(200_000.0 - r.exp_run_ms)
    assert r.exp_deaths is not None
    assert r.exp_run_diff_ms == pytest.approx(r.exp_run_ms - 200_000.0)
    assert r.exp_deaths_diff == pytest.approx(r.exp_deaths - 10.0)

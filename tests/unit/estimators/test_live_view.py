from datetime import datetime

import pytest
from tests.factories import make_event_attempt

from spinlab.estimators.em_suite_sampler import SamplerState, process_event
from spinlab.estimators.live_view import (
    floor_series_at,
    live_segment_view,
    route_series,
    route_summary,
)
from spinlab.estimators.session_snapshot import RouteBaseline, SegmentBaseline


def _episode(created_at: str, clean_tail_ms, *, completed=True, invalidated=False):
    """Minimal AttemptRow-shaped dict carrying just the keys floor_series_at and
    running_min_clean read (completed, invalidated, clean_tail_ms, created_at)."""
    return {
        "completed": completed,
        "invalidated": invalidated,
        "clean_tail_ms": clean_tail_ms,
        "created_at": created_at,
    }


def test_floor_series_at_empty_points():
    assert floor_series_at([], [[_episode("2026-01-01T00:00:00+00:00", 5000)]]) == []


def test_floor_series_at_steps_down_as_clean_pbs_land():
    # One segment: clean clears land at :00 (5000), :10 (4000), :20 (4500).
    # The running-min floor drops to 5000 then 4000, and stays 4000 (4500 > 4000).
    seg = [
        _episode("2026-01-01T00:00:00+00:00", 5000),
        _episode("2026-01-01T00:00:10+00:00", 4000),
        _episode("2026-01-01T00:00:20+00:00", 4500),
    ]
    times = [
        datetime.fromisoformat("2026-01-01T00:00:05+00:00"),
        datetime.fromisoformat("2026-01-01T00:00:15+00:00"),
        datetime.fromisoformat("2026-01-01T00:00:25+00:00"),
    ]
    assert floor_series_at(times, [seg]) == [5000.0, 4000.0, 4000.0]


def test_floor_series_at_none_before_any_clean_episode():
    seg = [_episode("2026-01-01T00:00:10+00:00", 5000)]
    times = [datetime.fromisoformat("2026-01-01T00:00:05+00:00")]
    assert floor_series_at(times, [seg]) == [None]


def test_floor_series_at_sums_across_segments():
    seg_a = [_episode("2026-01-01T00:00:00+00:00", 5000)]
    seg_b = [_episode("2026-01-01T00:00:00+00:00", 3000)]
    times = [datetime.fromisoformat("2026-01-01T00:00:05+00:00")]
    assert floor_series_at(times, [seg_a, seg_b]) == [8000.0]


def test_floor_series_at_skips_incomplete_and_invalidated():
    # Only the completed, non-invalidated clean clear (4000) counts.
    seg = [
        _episode("2026-01-01T00:00:00+00:00", 3000, completed=False),
        _episode("2026-01-01T00:00:01+00:00", 2000, invalidated=True),
        _episode("2026-01-01T00:00:02+00:00", 4000),
    ]
    times = [datetime.fromisoformat("2026-01-01T00:00:05+00:00")]
    assert floor_series_at(times, [seg]) == [4000.0]


def _gated_state(seg_id="s0"):
    # SamplerState() takes no constructor args — see Task 1 fixture pattern.
    # Caller-supplied seg_id is just a label here; baselines key on it via the
    # snapshot, not via the state itself.
    # The prediction gate (gate_passes) needs n_successes>=2, n_deaths>=2,
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


def test_route_series_empty_when_no_session_start():
    assert route_series([], session_start=None) == []


def test_route_series_empty_when_events_predate_session():
    # All events before session_start -> no in-session points.
    seg = [
        make_event_attempt(segment_id="s0", episode_id=f"e{i}", outcome=o,
                           time_ms=t, created_at=f"2026-01-01T00:00:0{i}")
        for i, (o, t) in enumerate(
            [("survived", 2000), ("died", 500), ("survived", 2100),
             ("died", 600), ("survived", 1900), ("died", 550)])
    ]
    start = datetime.fromisoformat("2026-01-01T00:01:00")  # after every event
    assert route_series([seg], session_start=start) == []


def test_route_series_emits_floats_for_in_session_events():
    # 3 warm-up events (pre-session) seed the EMAs so the route is estimable,
    # then 3 in-session events each yield a route Exp.Run point.
    warm = [
        make_event_attempt(segment_id="s0", episode_id=f"e{i}", outcome=o,
                           time_ms=t, created_at=f"2026-01-01T00:00:0{i}")
        for i, (o, t) in enumerate(
            [("survived", 2000), ("died", 500), ("survived", 2100)])
    ]
    in_session = [
        make_event_attempt(segment_id="s0", episode_id=f"e{i + 3}", outcome=o,
                           time_ms=t, created_at=f"2026-01-01T00:00:1{i}")
        for i, (o, t) in enumerate(
            [("died", 600), ("survived", 1900), ("died", 550)])
    ]
    start = datetime.fromisoformat("2026-01-01T00:00:10")
    series = route_series([warm + in_session], session_start=start)
    assert series, "expected at least one in-session estimable point"
    assert all(isinstance(p.exp_run_ms, float) for p in series)
    # All 3 in-session events (00:00:10, 00:00:11, 00:00:12) are >= session_start
    # and the EMAs are warm enough after the 3 warm-up events, so each should
    # yield an estimable point.
    assert len(series) == 3


def test_route_series_sums_across_segments():
    # Two identical segments -> each route point is ~2x a single segment's.
    def seg(seg_id):
        return [
            make_event_attempt(segment_id=seg_id, episode_id=f"{seg_id}_e{i}",
                               outcome=o, time_ms=t,
                               created_at=f"2026-01-01T00:00:1{i}")
            for i, (o, t) in enumerate(
                [("survived", 2000), ("died", 500), ("survived", 2100),
                 ("died", 600), ("survived", 1900), ("died", 550)])
        ]
    start = datetime.fromisoformat("2026-01-01T00:00:00")
    one = route_series([seg("s0")], session_start=start)
    two = route_series([seg("s0"), seg("s1")], session_start=start)
    assert one and two
    assert two[-1].exp_run_ms == pytest.approx(2 * one[-1].exp_run_ms, rel=1e-6)

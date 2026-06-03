"""Tests for the closed-form live-view reducers."""
from __future__ import annotations

from datetime import UTC, datetime

from spinlab.estimators.em_suite_sampler import SamplerState, process_event
from spinlab.estimators.live_view import (
    LiveSegmentView, live_segment_view,
)
from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt


def _state(success_ms: list[float], death_ms: list[float]) -> SamplerState:
    state = SamplerState()
    n = max(len(success_ms), len(death_ms))
    for i in range(n):
        if i < len(death_ms):
            state = process_event(state, EventAttempt(
                segment_id="x", session_id="s", episode_id=f"d{i}",
                outcome=AttemptOutcome.DIED, time_ms=int(death_ms[i]),
                source=AttemptSource.PRACTICE, created_at=datetime.now(UTC)))
        if i < len(success_ms):
            state = process_event(state, EventAttempt(
                segment_id="x", session_id="s", episode_id=f"c{i}",
                outcome=AttemptOutcome.SURVIVED, time_ms=int(success_ms[i]),
                source=AttemptSource.PRACTICE, created_at=datetime.now(UTC)))
    return state


def _ep(completed: int, time_ms, deaths: int, clean_tail_ms, inv: int = 0) -> dict:
    return {
        "segment_id": "x", "completed": completed, "time_ms": time_ms,
        "deaths": deaths, "clean_tail_ms": clean_tail_ms,
        "created_at": "2026-06-02T00:00:00+00:00", "invalidated": inv,
    }


class TestLiveSegmentView:
    def test_below_gate_not_ready(self):
        v = live_segment_view(SamplerState(n_successes=1, n_deaths=0, n_attempts_total=1), [])
        assert isinstance(v, LiveSegmentView)
        assert v.ready is False
        assert v.expected_episode_ms is None
        assert v.series == []

    def test_ready_payload_closed_form(self):
        state = _state(success_ms=[6000, 5000, 4500, 4200, 4000, 4000],
                       death_ms=[1500, 1500, 1500])
        episodes = [
            _ep(1, 9000, 1, 5800), _ep(0, None, 1, None),
            _ep(1, 7500, 1, 4300), _ep(1, 4200, 0, 4200),
            _ep(1, 7700, 1, 4500),
        ]
        v = live_segment_view(state, episodes)
        assert v.ready is True
        assert v.expected_episode_ms is not None and v.expected_episode_ms > 0
        assert 0.0 <= v.death_rate <= 1.0
        assert v.floor_ms == 4200.0
        assert v.last_episode_ms == 7700.0
        assert v.last_clean_ms == 4500.0
        assert v.last_deaths == 1
        assert v.last_rank == 3
        completed_pts = [p for p in v.series]
        assert len(completed_pts) == 4
        assert completed_pts[-1]["running_floor_ms"] == 4200.0

    def test_practice_gain_signed_reduction(self):
        state = _state(success_ms=[6000, 5800, 5400, 5000, 4400, 4000, 3800, 3600],
                       death_ms=[1600, 1500, 1400, 1300])
        v = live_segment_view(state, [_ep(1, 5000, 0, 5000), _ep(1, 4600, 0, 4600)])
        assert v.ready is True
        assert v.practice_gain_ms is None or isinstance(v.practice_gain_ms, float)

    def test_floor_none_when_no_completed_episodes(self):
        state = _state(success_ms=[4000, 4000], death_ms=[1500, 1500])
        v = live_segment_view(state, [_ep(0, None, 1, None), _ep(0, None, 2, None)])
        assert v.floor_ms is None
        assert v.last_episode_ms is None
        assert v.series == []

    def test_invalidated_episodes_excluded(self):
        state = _state(success_ms=[4000, 4100, 4050, 4000], death_ms=[1500, 1500])
        v = live_segment_view(state, [_ep(1, 4000, 0, 4000, inv=1), _ep(1, 4200, 0, 4200)])
        assert v.floor_ms == 4200.0
        assert v.last_episode_ms == 4200.0
        assert len(v.series) == 1

"""Tests for the segment-progress reducer (the 'am I improving?' signal)."""
from __future__ import annotations

import math

from spinlab.estimators.em_suite_sampler import SamplerState
from spinlab.estimators.segment_progress import SegmentProgress, segment_progress


def _state_with(success_ms: list[float], death_ms: list[float]) -> SamplerState:
    """Build a gated state by replaying alternating events through process_event."""
    from spinlab.estimators.em_suite_sampler import process_event
    from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt
    from datetime import UTC, datetime
    state = SamplerState()
    # Interleave so both pools fill and counters gate.
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


class TestSegmentProgress:
    def test_below_gate_returns_not_ready(self):
        state = SamplerState(n_successes=1, n_deaths=0, n_attempts_total=1)
        p = segment_progress(state, gold_ms=None)
        assert isinstance(p, SegmentProgress)
        assert p.ready is False
        assert p.verdict == "not_ready"
        assert p.now_clear_ms is None
        assert p.trend_ms == []

    def test_improving_when_recent_faster_than_baseline(self):
        # Clears start slow (~6000) and get fast (~4000); recent EMA < baseline EMA.
        state = _state_with(
            success_ms=[6000, 5800, 5600, 5000, 4400, 4200, 4000, 4000],
            death_ms=[1500, 1500, 1500, 1500])
        p = segment_progress(state, gold_ms=3900)
        assert p.ready is True
        assert p.now_clear_ms is not None and p.baseline_clear_ms is not None
        assert p.now_clear_ms < p.baseline_clear_ms
        assert p.verdict == "faster"
        # Trend is the recency-ordered recent clears (newest last), capped.
        assert p.trend_ms[-1] == 4000.0
        assert p.pb_ms == 4000.0
        assert p.gap_to_gold_ms is not None  # now - gold

    def test_slower_when_recent_slower_than_baseline(self):
        state = _state_with(
            success_ms=[4000, 4000, 4200, 4400, 5000, 5600, 5800, 6000],
            death_ms=[1500, 1500, 1500, 1500])
        p = segment_progress(state, gold_ms=3900)
        assert p.verdict == "slower"
        assert p.now_clear_ms > p.baseline_clear_ms

    def test_holding_when_within_noise(self):
        # Flat clears: now ≈ baseline, delta under the standard-error band.
        state = _state_with(
            success_ms=[5000, 5010, 4990, 5005, 4995, 5000, 5002, 4998],
            death_ms=[1500, 1500, 1500, 1500])
        p = segment_progress(state, gold_ms=4800)
        assert p.verdict == "holding"

    def test_death_rate_is_recent_p_die(self):
        state = _state_with(
            success_ms=[4000, 4000, 4000, 4000, 4000],
            death_ms=[1500, 1500, 1500])
        p = segment_progress(state, gold_ms=None)
        assert 0.0 <= p.death_rate <= 1.0

    def test_gap_to_gold_none_when_no_gold(self):
        state = _state_with(
            success_ms=[4000, 4100, 4050, 4000], death_ms=[1500, 1500])
        p = segment_progress(state, gold_ms=None)
        assert p.gap_to_gold_ms is None

    def test_trend_capped_to_recent_window(self):
        state = _state_with(
            success_ms=[float(4000 + i) for i in range(40)],
            death_ms=[1500, 1500])
        p = segment_progress(state, gold_ms=None)
        assert len(p.trend_ms) <= 20  # TREND_WINDOW

"""Tests for PracticeEngine (excluding per_segment_values; that's Task 7)."""
from __future__ import annotations

import random
from datetime import UTC, datetime

import numpy as np
import pytest

from spinlab.estimators.em_suite_sampler import SamplerState, process_event
from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt
from spinlab.practice_engine.engine import PracticeEngine
from spinlab.practice_engine.objectives import (
    expected_total_finished_time,
    expected_wall_clock_per_attempt,
)
from spinlab.practice_engine.reset_policies import no_reset, target_paced
from spinlab.practice_engine.types import PerSegmentValue


def _gated_state(seed: int = 0) -> SamplerState:
    state = SamplerState(n_completed=0, n_attempts=0)
    rng = random.Random(seed)
    for i in range(60):
        outcome = AttemptOutcome.SURVIVED if i % 3 != 0 else AttemptOutcome.DIED
        t_ms = 4000 + rng.randint(-200, 200) if outcome == AttemptOutcome.SURVIVED else 1500
        state = process_event(state, EventAttempt(
            segment_id="x", session_id="s", episode_id=f"e{i}",
            outcome=outcome, time_ms=t_ms,
            source=AttemptSource.PRACTICE,
            created_at=datetime.now(UTC),
        ))
    return state


class TestEvaluate:
    def test_returns_scalar_for_expected_total(self):
        states = {"s1": _gated_state(0), "s2": _gated_state(1)}
        engine = PracticeEngine(sampler_states=states, N=500, rng_seed=42)
        result = engine.evaluate(
            policy=no_reset, threshold_kwargs={},
            objective=expected_wall_clock_per_attempt, ctx={},
        )
        assert isinstance(result["value"], float)
        assert result["value"] > 0

    def test_returns_none_for_finished_time_when_all_aborted(self):
        states = {"s1": _gated_state(0)}
        engine = PracticeEngine(sampler_states=states, N=200, rng_seed=42)
        result = engine.evaluate(
            policy=target_paced,
            threshold_kwargs={"threshold_cum_ms": np.array([1.0]), "slack": 0.0},
            objective=expected_total_finished_time, ctx={},
        )
        assert result["value"] is None
        assert result["masks_summary"]["finished_pct"] == pytest.approx(0.0)

    def test_masks_summary_shape(self):
        states = {"s1": _gated_state(0), "s2": _gated_state(1)}
        engine = PracticeEngine(sampler_states=states, N=200, rng_seed=42)
        result = engine.evaluate(
            policy=no_reset, threshold_kwargs={},
            objective=expected_wall_clock_per_attempt, ctx={},
        )
        ms = result["masks_summary"]
        assert ms["finished_pct"] == pytest.approx(100.0)
        assert ms["aborted_by_segment"] == {}


class TestTotalTimeDistribution:
    def test_histogram_payload(self):
        states = {"s1": _gated_state(0), "s2": _gated_state(1)}
        engine = PracticeEngine(sampler_states=states, N=500, rng_seed=42)
        result = engine.total_time_distribution(policy=no_reset, threshold_kwargs={})
        assert "bins" in result and "counts" in result
        assert len(result["bins"]) == len(result["counts"]) + 1
        assert sum(result["counts"]) == 500
        assert result["mean"] > 0
        assert result["median"] > 0
        assert result["p10"] <= result["median"] <= result["p90"]


class TestColumnSummary:
    def test_per_segment_stats(self):
        states = {"s1": _gated_state(0)}
        engine = PracticeEngine(sampler_states=states, N=500, rng_seed=42)
        summary = engine.column_summary("s1")
        assert summary["seg_id"] == "s1"
        assert summary["n"] == 500
        assert summary["mean"] > 0
        assert summary["p10"] <= summary["p50"] <= summary["p90"]
        assert "e_sample_0_ms" in summary
        assert "e_sample_1_ms" in summary


class TestPerSegmentValues:
    def test_returns_one_entry_per_gated_segment(self):
        states = {"s1": _gated_state(0), "s2": _gated_state(1), "s3": _gated_state(2)}
        engine = PracticeEngine(sampler_states=states, N=500, rng_seed=42)
        values = engine.per_segment_values(
            policy=no_reset, threshold_kwargs={},
            objective=expected_wall_clock_per_attempt, ctx={},
        )
        assert set(values.keys()) == {"s1", "s2", "s3"}
        for seg_id, psv in values.items():
            assert isinstance(psv, PerSegmentValue)
            assert psv.seg_id == seg_id
            assert psv.e_sample_0_ms > 0
            assert psv.e_sample_1_ms >= 0

    def test_returns_empty_when_no_gated(self):
        empty = SamplerState(n_completed=0, n_attempts=0)
        states = {"s1": empty}
        engine = PracticeEngine(sampler_states=states, N=100, rng_seed=42)
        values = engine.per_segment_values(
            policy=no_reset, threshold_kwargs={},
            objective=expected_wall_clock_per_attempt, ctx={},
        )
        assert values == {}

    def test_value_per_second_is_value_over_cost(self):
        states = {"s1": _gated_state(0)}
        engine = PracticeEngine(sampler_states=states, N=500, rng_seed=42)
        values = engine.per_segment_values(
            policy=no_reset, threshold_kwargs={},
            objective=expected_wall_clock_per_attempt, ctx={},
        )
        psv = values["s1"]
        if psv.value_per_second is not None:
            assert psv.value_per_second == pytest.approx(psv.value / psv.e_sample_0_ms, rel=1e-9)

    def test_objective_none_skips_segment(self):
        # If baseline objective returns None (e.g. expected_total_finished_time
        # when threshold is so tight nothing finishes), the engine returns empty.
        states = {"s1": _gated_state(0)}
        engine = PracticeEngine(sampler_states=states, N=200, rng_seed=42)
        values = engine.per_segment_values(
            policy=target_paced,
            threshold_kwargs={"threshold_cum_ms": np.array([1.0]), "slack": 0.0},
            objective=expected_total_finished_time, ctx={},
        )
        assert values == {}

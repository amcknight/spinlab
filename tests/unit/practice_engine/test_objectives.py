"""Tests for objective functions."""
import numpy as np
import pytest

from spinlab.practice_engine.objectives import (
    expected_total_finished_time,
    expected_wall_clock_per_attempt,
    p_pb_this_session,
    q,
    quantile,
)
from spinlab.practice_engine.types import ResetMasks


def _masks(finished, abort_at, wall_ms):
    return ResetMasks(
        finished=np.array(finished, dtype=bool),
        abort_at=np.array(abort_at, dtype=np.int32),
        wall_ms=np.array(wall_ms, dtype=np.float64),
    )


class TestExpectedWallClockPerAttempt:
    def test_uniform_finished(self):
        T = np.zeros((3, 2))
        masks = _masks([True, True, True], [-1, -1, -1], [1000.0, 2000.0, 3000.0])
        assert expected_wall_clock_per_attempt(T, masks, {}) == pytest.approx(2000.0)

    def test_mixed_finished_aborted_includes_partials(self):
        # wall_ms reflects ABORTED partials as well as finished totals
        T = np.zeros((4, 2))
        masks = _masks(
            [True, False, True, False],
            [-1, 0, -1, 1],
            [10_000.0, 2_000.0, 8_000.0, 5_000.0],
        )
        # Average across all 4: (10000+2000+8000+5000)/4 = 6250
        assert expected_wall_clock_per_attempt(T, masks, {}) == pytest.approx(6250.0)


class TestExpectedTotalFinishedTime:
    def test_finished_only(self):
        T = np.zeros((4, 2))
        masks = _masks(
            [True, False, True, False],
            [-1, 0, -1, 1],
            [10_000.0, 2_000.0, 8_000.0, 5_000.0],
        )
        # Mean of [10000, 8000] = 9000
        assert expected_total_finished_time(T, masks, {}) == pytest.approx(9000.0)

    def test_none_when_no_finished(self):
        T = np.zeros((2, 2))
        masks = _masks([False, False], [0, 1], [1000.0, 2000.0])
        assert expected_total_finished_time(T, masks, {}) is None


class TestQ:
    def test_fraction_under_target(self):
        T = np.zeros((4, 2))
        masks = _masks(
            [True, True, True, False],
            [-1, -1, -1, 1],
            [4500.0, 6000.0, 9000.0, 5500.0],
        )
        assert q(T, masks, {"target_ms": 6500}) == pytest.approx(0.5)

    def test_zero_when_none_under(self):
        T = np.zeros((2, 2))
        masks = _masks([True, True], [-1, -1], [10_000.0, 11_000.0])
        assert q(T, masks, {"target_ms": 5_000}) == pytest.approx(0.0)


class TestQuantile:
    def test_median_of_finished(self):
        T = np.zeros((5, 2))
        masks = _masks(
            [True, True, True, True, False],
            [-1, -1, -1, -1, 0],
            [3000.0, 5000.0, 7000.0, 9000.0, 1500.0],
        )
        # Finished times: [3000, 5000, 7000, 9000]; median = 6000
        assert quantile(T, masks, {"p": 0.5}) == pytest.approx(6000.0)

    def test_none_when_no_finished(self):
        T = np.zeros((1, 2))
        masks = _masks([False], [0], [500.0])
        assert quantile(T, masks, {"p": 0.5}) is None


class TestPpbThisSession:
    def test_one_minus_one_minus_q_to_H_over_tau(self):
        T = np.zeros((2, 2))
        masks = _masks([True, False], [-1, 1], [500.0, 1500.0])
        # wall_ms.mean() = 1000; q with target 600: finished and <=600 ⇒ 1/2 = 0.5
        ctx = {"target_ms": 600, "session_remaining_ms": 5000}
        result = p_pb_this_session(T, masks, ctx)
        assert result == pytest.approx(1 - 0.5 ** 5, rel=1e-6)

    def test_none_when_tau_zero(self):
        T = np.zeros((1, 2))
        masks = _masks([True], [-1], [0.0])
        ctx = {"target_ms": 100, "session_remaining_ms": 1000}
        assert p_pb_this_session(T, masks, ctx) is None

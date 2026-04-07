"""Outside-view estimator sanity tests.

These treat estimators as black boxes: feed attempt sequences, assert on
physical invariants of the output. Parametrized across all registered
estimators so new estimators automatically get coverage.
"""
import math

import pytest

from spinlab.estimators import Estimator, get_estimator, list_estimators

# Force registration of all estimators at import time
from spinlab.estimators.kalman import KalmanEstimator  # noqa: F401
from spinlab.estimators.rolling_mean import RollingMeanEstimator  # noqa: F401
try:
    from spinlab.estimators.exp_decay import ExpDecayEstimator  # noqa: F401
except ImportError:
    pass

from spinlab.models import AttemptRecord, Estimate, ModelOutput
from tests.factories import make_attempt_record, make_incomplete

ALL_ESTIMATOR_NAMES = list_estimators()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _feed_attempts(est: Estimator, attempts: list[AttemptRecord]) -> ModelOutput:
    """Feed a sequence through init_state + process_attempt, return model_output."""
    completed = [a for a in attempts if a.completed and a.time_ms is not None]
    if not completed:
        state = est.rebuild_state(attempts)
        return est.model_output(state, attempts)
    first = completed[0]
    state = est.init_state(first, priors={})
    first_idx = attempts.index(first)
    for a in attempts[:first_idx]:
        state = est.process_attempt(state, a, attempts)
    for a in attempts[first_idx + 1:]:
        state = est.process_attempt(state, a, attempts)
    return est.model_output(state, attempts)


def _is_valid_float(v: float | None) -> bool:
    """True if None or a finite float (no NaN/inf)."""
    if v is None:
        return True
    return isinstance(v, (int, float)) and math.isfinite(v)


def _check_estimate_finite(est: Estimate, label: str) -> None:
    """Assert all fields are None or finite."""
    assert _is_valid_float(est.expected_ms), f"{label}.expected_ms = {est.expected_ms}"
    assert _is_valid_float(est.ms_per_attempt), f"{label}.ms_per_attempt = {est.ms_per_attempt}"
    assert _is_valid_float(est.floor_ms), f"{label}.floor_ms = {est.floor_ms}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(params=ALL_ESTIMATOR_NAMES)
def estimator(request) -> Estimator:
    return get_estimator(request.param)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

CONSTANT_TIMES = [make_attempt_record(10000, True, clean_tail_ms=10000) for _ in range(10)]
IMPROVING_TIMES = [make_attempt_record(t, True, clean_tail_ms=t) for t in [15000, 14000, 13000, 12000, 11000, 10000, 9000, 8000, 7000, 6000]]
REGRESSING_TIMES = [make_attempt_record(t, True, clean_tail_ms=t) for t in [6000, 7000, 8000, 9000, 10000, 11000, 12000, 13000, 14000, 15000]]
SINGLE_ATTEMPT = [make_attempt_record(12000, True, clean_tail_ms=12000)]
ALL_INCOMPLETE = [make_incomplete() for _ in range(5)]
CLEAN_EQUALS_TOTAL = [make_attempt_record(t, True, clean_tail_ms=t) for t in [12000, 11500, 11000, 10500, 10000]]
DIRTY_ATTEMPTS = [
    make_attempt_record(20000, True, deaths=2, clean_tail_ms=8000),
    make_attempt_record(18000, True, deaths=1, clean_tail_ms=9000),
    make_attempt_record(15000, True, deaths=0, clean_tail_ms=15000),
    make_attempt_record(19000, True, deaths=2, clean_tail_ms=7000),
    make_attempt_record(14000, True, deaths=0, clean_tail_ms=14000),
    make_attempt_record(17000, True, deaths=1, clean_tail_ms=8500),
    make_attempt_record(13000, True, deaths=0, clean_tail_ms=13000),
    make_attempt_record(16000, True, deaths=1, clean_tail_ms=7500),
]


# ---------------------------------------------------------------------------
# U1: expected_ms > 0 when non-None
# ---------------------------------------------------------------------------

class TestPositiveExpected:
    def test_constant(self, estimator):
        out = _feed_attempts(estimator, CONSTANT_TIMES)
        if out.total.expected_ms is not None:
            assert out.total.expected_ms > 0, f"{estimator.name}: total.expected_ms = {out.total.expected_ms}"
        if out.clean.expected_ms is not None:
            assert out.clean.expected_ms > 0, f"{estimator.name}: clean.expected_ms = {out.clean.expected_ms}"

    def test_improving(self, estimator):
        out = _feed_attempts(estimator, IMPROVING_TIMES)
        if out.total.expected_ms is not None:
            assert out.total.expected_ms > 0
        if out.clean.expected_ms is not None:
            assert out.clean.expected_ms > 0

    def test_regressing(self, estimator):
        out = _feed_attempts(estimator, REGRESSING_TIMES)
        if out.total.expected_ms is not None:
            assert out.total.expected_ms > 0
        if out.clean.expected_ms is not None:
            assert out.clean.expected_ms > 0

    def test_single(self, estimator):
        out = _feed_attempts(estimator, SINGLE_ATTEMPT)
        if out.total.expected_ms is not None:
            assert out.total.expected_ms > 0

    def test_dirty(self, estimator):
        out = _feed_attempts(estimator, DIRTY_ATTEMPTS)
        if out.total.expected_ms is not None:
            assert out.total.expected_ms > 0
        if out.clean.expected_ms is not None:
            assert out.clean.expected_ms > 0


# ---------------------------------------------------------------------------
# U2: floor_ms > 0 when non-None
# ---------------------------------------------------------------------------

class TestPositiveFloor:
    def test_constant(self, estimator):
        out = _feed_attempts(estimator, CONSTANT_TIMES)
        if out.total.floor_ms is not None:
            assert out.total.floor_ms > 0
        if out.clean.floor_ms is not None:
            assert out.clean.floor_ms > 0

    def test_improving(self, estimator):
        out = _feed_attempts(estimator, IMPROVING_TIMES)
        if out.total.floor_ms is not None:
            assert out.total.floor_ms > 0
        if out.clean.floor_ms is not None:
            assert out.clean.floor_ms > 0


# ---------------------------------------------------------------------------
# U3: No NaN or inf in any Estimate field
# ---------------------------------------------------------------------------

class TestFiniteValues:
    def test_constant(self, estimator):
        out = _feed_attempts(estimator, CONSTANT_TIMES)
        _check_estimate_finite(out.total, f"{estimator.name}.total")
        _check_estimate_finite(out.clean, f"{estimator.name}.clean")

    def test_improving(self, estimator):
        out = _feed_attempts(estimator, IMPROVING_TIMES)
        _check_estimate_finite(out.total, f"{estimator.name}.total")
        _check_estimate_finite(out.clean, f"{estimator.name}.clean")

    def test_regressing(self, estimator):
        out = _feed_attempts(estimator, REGRESSING_TIMES)
        _check_estimate_finite(out.total, f"{estimator.name}.total")
        _check_estimate_finite(out.clean, f"{estimator.name}.clean")

    def test_single(self, estimator):
        out = _feed_attempts(estimator, SINGLE_ATTEMPT)
        _check_estimate_finite(out.total, f"{estimator.name}.total")
        _check_estimate_finite(out.clean, f"{estimator.name}.clean")

    def test_all_incomplete(self, estimator):
        out = _feed_attempts(estimator, ALL_INCOMPLETE)
        _check_estimate_finite(out.total, f"{estimator.name}.total")
        _check_estimate_finite(out.clean, f"{estimator.name}.clean")

    def test_dirty(self, estimator):
        out = _feed_attempts(estimator, DIRTY_ATTEMPTS)
        _check_estimate_finite(out.total, f"{estimator.name}.total")
        _check_estimate_finite(out.clean, f"{estimator.name}.clean")


# ---------------------------------------------------------------------------
# U4: clean.floor_ms <= total.floor_ms when both non-None
# ---------------------------------------------------------------------------

class TestCleanFloorLeTotalFloor:
    def test_dirty_attempts(self, estimator):
        out = _feed_attempts(estimator, DIRTY_ATTEMPTS)
        if out.clean.floor_ms is not None and out.total.floor_ms is not None:
            assert out.clean.floor_ms <= out.total.floor_ms, (
                f"{estimator.name}: clean.floor_ms={out.clean.floor_ms} > "
                f"total.floor_ms={out.total.floor_ms}"
            )


# ---------------------------------------------------------------------------
# C1: Constant times -> ms_per_attempt ≈ 0
# ---------------------------------------------------------------------------

class TestConstantTrend:
    def test_flat_data(self, estimator):
        out = _feed_attempts(estimator, CONSTANT_TIMES)
        if out.total.ms_per_attempt is not None:
            # Kalman starts with d=0.0, should stay near 0 for constant data.
            assert abs(out.total.ms_per_attempt) < 200, (
                f"{estimator.name}: ms_per_attempt = {out.total.ms_per_attempt} for constant data"
            )


# ---------------------------------------------------------------------------
# C2: Strictly decreasing times -> ms_per_attempt > 0
# ---------------------------------------------------------------------------

class TestImprovingTrend:
    def test_improving(self, estimator):
        out = _feed_attempts(estimator, IMPROVING_TIMES)
        if out.total.ms_per_attempt is not None:
            assert out.total.ms_per_attempt > 0, (
                f"{estimator.name}: ms_per_attempt = {out.total.ms_per_attempt} "
                f"for strictly improving data"
            )


# ---------------------------------------------------------------------------
# C3: Strictly increasing times -> ms_per_attempt < 0
# ---------------------------------------------------------------------------

class TestRegressingTrend:
    def test_regressing(self, estimator):
        out = _feed_attempts(estimator, REGRESSING_TIMES)
        if out.total.ms_per_attempt is not None:
            # exp_decay can only model decay (not growth), so it returns 0.0
            # when times are increasing. Allow <= 0 to accommodate this.
            assert out.total.ms_per_attempt <= 1e-6, (
                f"{estimator.name}: ms_per_attempt = {out.total.ms_per_attempt} "
                f"for strictly regressing data (should be ~0 or negative)"
            )


# ---------------------------------------------------------------------------
# C4: Zero deaths, clean_tail == time -> clean ≈ total (or clean is None)
# ---------------------------------------------------------------------------

class TestCleanEqualsTotal:
    def test_no_deaths(self, estimator):
        out = _feed_attempts(estimator, CLEAN_EQUALS_TOTAL)
        if out.clean.expected_ms is not None and out.total.expected_ms is not None:
            assert out.clean.expected_ms == pytest.approx(out.total.expected_ms, rel=0.01), (
                f"{estimator.name}: clean.expected_ms={out.clean.expected_ms} != "
                f"total.expected_ms={out.total.expected_ms} with zero deaths"
            )


# ---------------------------------------------------------------------------
# C5: All estimators agree on sign of ms_per_attempt for monotonic data
# ---------------------------------------------------------------------------

class TestCrossEstimatorAgreement:
    """Not parametrized — runs all estimators and compares."""

    def _signs(self, attempts: list[AttemptRecord]) -> dict[str, int]:
        """Returns {estimator_name: sign} where sign is -1, 0, or 1."""
        signs = {}
        for name in list_estimators():
            est = get_estimator(name)
            out = _feed_attempts(est, attempts)
            mpa = out.total.ms_per_attempt
            if mpa is not None:
                # Use epsilon tolerance for near-zero values (exp_decay floating-point)
                signs[name] = 1 if mpa > 1e-6 else (-1 if mpa < -1e-6 else 0)
        return signs

    def test_all_agree_improving(self):
        signs = self._signs(IMPROVING_TIMES)
        if signs:
            values = set(signs.values())
            # All should be positive (or zero for estimators that can't detect trend)
            assert all(v >= 0 for v in signs.values()), (
                f"Estimators disagree on improving data: {signs}"
            )

    def test_all_agree_regressing(self):
        signs = self._signs(REGRESSING_TIMES)
        if signs:
            assert all(v <= 0 for v in signs.values()), (
                f"Estimators disagree on regressing data: {signs}"
            )


# ---------------------------------------------------------------------------
# Edge: all-incomplete produces no crash and all-None output
# ---------------------------------------------------------------------------

class TestAllIncomplete:
    def test_all_none_output(self, estimator):
        out = _feed_attempts(estimator, ALL_INCOMPLETE)
        assert out.total.expected_ms is None, f"{estimator.name}: expected non-None from incomplete data"
        assert out.clean.expected_ms is None


# ---------------------------------------------------------------------------
# EstimatorState.deserialize round-trips
# ---------------------------------------------------------------------------

class TestEstimatorStateDeserialize:
    def test_kalman_round_trip(self):
        from spinlab.estimators import EstimatorState
        from spinlab.estimators.kalman import KalmanState
        import json
        original = KalmanState(mu=12.0, d=-0.5, n_completed=5, n_attempts=8)
        json_str = json.dumps(original.to_dict())
        restored = EstimatorState.deserialize("kalman", json_str)
        assert isinstance(restored, KalmanState)
        assert restored.mu == 12.0
        assert restored.n_completed == 5

    def test_rolling_mean_round_trip(self):
        from spinlab.estimators import EstimatorState
        from spinlab.estimators.rolling_mean import RollingMeanState
        import json
        original = RollingMeanState(n_completed=10, n_attempts=15)
        json_str = json.dumps(original.to_dict())
        restored = EstimatorState.deserialize("rolling_mean", json_str)
        assert isinstance(restored, RollingMeanState)
        assert restored.n_completed == 10

    def test_unknown_estimator_raises(self):
        from spinlab.estimators import EstimatorState
        with pytest.raises(ValueError, match="No state class"):
            EstimatorState.deserialize("nonexistent", "{}")

    def test_malformed_json_raises(self):
        from spinlab.estimators import EstimatorState
        import json
        with pytest.raises(json.JSONDecodeError):
            EstimatorState.deserialize("kalman", "{bad json")

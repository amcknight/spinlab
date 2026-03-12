"""Tests for KalmanEstimator."""
import pytest
from spinlab.estimators.kalman import KalmanState


class TestKalmanState:
    def test_round_trip_serialization(self):
        state = KalmanState(
            mu=15.0,
            d=-0.5,
            P_mm=25.0,
            P_md=0.0,
            P_dm=0.0,
            P_dd=1.0,
            R=25.0,
            Q_mm=0.1,
            Q_md=0.0,
            Q_dm=0.0,
            Q_dd=0.01,
            gold=14.2,
            n_completed=5,
            n_attempts=7,
        )
        d = state.to_dict()
        restored = KalmanState.from_dict(d)
        assert restored.mu == state.mu
        assert restored.d == state.d
        assert restored.P_dd == state.P_dd
        assert restored.gold == state.gold
        assert restored.n_completed == state.n_completed
        assert restored.n_attempts == state.n_attempts

    def test_from_dict_missing_keys_uses_defaults(self):
        """Handles missing keys gracefully for forward-compat."""
        minimal = {"mu": 10.0, "d": -0.3, "gold": 9.5, "n_completed": 3, "n_attempts": 4}
        state = KalmanState.from_dict(minimal)
        assert state.mu == 10.0
        assert state.P_mm == 25.0  # default
        assert state.Q_dd == 0.01  # default

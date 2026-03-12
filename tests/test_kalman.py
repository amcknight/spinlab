"""Tests for KalmanEstimator."""
import math
import pytest
from spinlab.estimators.kalman import (
    KalmanState,
    KalmanEstimator,
    DEFAULT_D,
    DEFAULT_R,
)


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


class TestKalmanPredict:
    def test_predict_shifts_mu_by_drift(self):
        est = KalmanEstimator()
        state = KalmanState(mu=20.0, d=-1.0, P_mm=25.0, P_md=0.0, P_dm=0.0, P_dd=1.0,
                            R=25.0, Q_mm=0.1, Q_md=0.0, Q_dm=0.0, Q_dd=0.01)
        pred = est._predict(state)
        assert pred.mu == pytest.approx(19.0)  # 20 + (-1)
        assert pred.d == pytest.approx(-1.0)   # drift unchanged

    def test_predict_grows_covariance(self):
        est = KalmanEstimator()
        state = KalmanState(mu=20.0, d=-1.0, P_mm=25.0, P_md=0.0, P_dm=0.0, P_dd=1.0,
                            R=25.0, Q_mm=0.1, Q_md=0.0, Q_dm=0.0, Q_dd=0.01)
        pred = est._predict(state)
        # P_mm_pred = P_mm + 2*P_md + P_dd + Q_mm = 25 + 0 + 1 + 0.1 = 26.1
        assert pred.P_mm == pytest.approx(26.1)
        # P_dd_pred = P_dd + Q_dd = 1 + 0.01 = 1.01
        assert pred.P_dd == pytest.approx(1.01)


class TestKalmanUpdate:
    def test_update_pulls_mu_toward_observation(self):
        est = KalmanEstimator()
        pred = KalmanState(mu=19.0, d=-1.0, P_mm=26.1, P_md=1.0, P_dm=1.0, P_dd=1.01,
                           R=25.0)
        updated = est._update(pred, observed_time=17.0)
        assert updated.mu < 19.0

    def test_update_adjusts_drift(self):
        est = KalmanEstimator()
        pred = KalmanState(mu=19.0, d=-1.0, P_mm=26.1, P_md=1.0, P_dm=1.0, P_dd=1.01,
                           R=25.0)
        updated = est._update(pred, observed_time=17.0)
        assert updated.d < -1.0

    def test_update_shrinks_covariance(self):
        est = KalmanEstimator()
        pred = KalmanState(mu=19.0, d=-1.0, P_mm=26.1, P_md=1.0, P_dm=1.0, P_dd=1.01,
                           R=25.0)
        updated = est._update(pred, observed_time=17.0)
        assert updated.P_mm < pred.P_mm
        assert updated.P_dd < pred.P_dd


class TestKalmanInitState:
    def test_init_from_first_observation(self):
        est = KalmanEstimator()
        state = est.init_state(first_time=15.0, priors={})
        assert state.mu == 15.0
        assert state.d == -0.5  # default prior
        assert state.gold == 15.0
        assert state.n_completed == 1
        assert state.n_attempts == 1
        assert state.R == 25.0

    def test_init_with_population_priors(self):
        est = KalmanEstimator()
        priors = {"d": -0.8, "R": 16.0, "Q_mm": 0.2, "Q_dd": 0.02}
        state = est.init_state(first_time=10.0, priors=priors)
        assert state.d == -0.8
        assert state.R == 16.0
        assert state.Q_mm == 0.2
        assert state.Q_dd == 0.02


class TestKalmanProcessAttempt:
    def test_completed_attempt_updates_state(self):
        est = KalmanEstimator()
        state = est.init_state(first_time=20.0, priors={})
        state2 = est.process_attempt(state, observed_time=18.0)
        assert state2.mu < 20.0
        assert state2.n_completed == 2
        assert state2.n_attempts == 2
        assert state2.gold == 18.0

    def test_incomplete_attempt_skips_kalman_update(self):
        est = KalmanEstimator()
        state = est.init_state(first_time=20.0, priors={})
        state2 = est.process_attempt(state, observed_time=None)
        assert state2.mu == state.mu
        assert state2.d == state.d
        assert state2.n_completed == 1
        assert state2.n_attempts == 2

    def test_gold_tracks_minimum(self):
        est = KalmanEstimator()
        state = est.init_state(first_time=20.0, priors={})
        state = est.process_attempt(state, 22.0)
        assert state.gold == 20.0
        state = est.process_attempt(state, 18.0)
        assert state.gold == 18.0


class TestKalmanMarginalReturn:
    def test_improving_split_has_positive_return(self):
        est = KalmanEstimator()
        state = KalmanState(mu=20.0, d=-1.0)
        assert est.marginal_return(state) == pytest.approx(0.05)

    def test_regressing_split_has_negative_return(self):
        est = KalmanEstimator()
        state = KalmanState(mu=20.0, d=0.5)
        assert est.marginal_return(state) == pytest.approx(-0.025)

    def test_zero_mu_returns_zero(self):
        est = KalmanEstimator()
        state = KalmanState(mu=0.0, d=-1.0)
        assert est.marginal_return(state) == 0.0


class TestKalmanDriftInfo:
    def test_confident_improving(self):
        est = KalmanEstimator()
        state = KalmanState(mu=20.0, d=-1.0, P_dd=0.01)
        info = est.drift_info(state)
        assert info["drift"] == -1.0
        assert info["label"] == "improving"
        assert info["confidence"] == "confident"
        assert info["ci_lower"] < -1.0
        assert info["ci_upper"] < 0

    def test_uncertain_drift(self):
        est = KalmanEstimator()
        state = KalmanState(mu=20.0, d=-0.3, P_dd=4.0)
        info = est.drift_info(state)
        assert info["confidence"] == "uncertain"

    def test_regressing(self):
        est = KalmanEstimator()
        state = KalmanState(mu=20.0, d=0.5, P_dd=0.01)
        info = est.drift_info(state)
        assert info["label"] == "regressing"


class TestKalmanPopulationPriors:
    def test_computes_mean_from_mature_splits(self):
        est = KalmanEstimator()
        states = [
            KalmanState(d=-0.8, R=20.0, Q_mm=0.1, Q_dd=0.02, n_completed=15),
            KalmanState(d=-0.4, R=30.0, Q_mm=0.1, Q_dd=0.01, n_completed=20),
        ]
        priors = est.get_population_priors(states)
        assert priors["d"] == pytest.approx(-0.6)
        assert priors["R"] == pytest.approx(25.0)

    def test_returns_defaults_when_no_mature_splits(self):
        est = KalmanEstimator()
        states = [KalmanState(n_completed=3), KalmanState(n_completed=5)]
        priors = est.get_population_priors(states)
        assert priors["d"] == DEFAULT_D
        assert priors["R"] == DEFAULT_R


class TestKalmanRebuildState:
    def test_rebuild_matches_sequential_processing(self):
        est = KalmanEstimator()
        times = [20.0, 19.0, None, 18.5, 17.0]

        # Sequential
        state = est.init_state(times[0], priors={})
        for t in times[1:]:
            state = est.process_attempt(state, t)

        # Rebuild
        rebuilt = est.rebuild_state(times)

        assert rebuilt.mu == pytest.approx(state.mu)
        assert rebuilt.d == pytest.approx(state.d)
        assert rebuilt.P_dd == pytest.approx(state.P_dd)
        assert rebuilt.gold == pytest.approx(state.gold)
        assert rebuilt.n_completed == state.n_completed
        assert rebuilt.n_attempts == state.n_attempts


class TestKalmanConvergence:
    def test_improving_runner_detected(self):
        """Simulate a runner improving from 20s to ~15s over 30 runs."""
        est = KalmanEstimator()
        import random
        random.seed(42)
        state = est.init_state(first_time=20.0, priors={})

        for run in range(29):
            true_mean = 20.0 - 0.2 * (run + 1)
            observed = true_mean + random.gauss(0, 2.0)
            state = est.process_attempt(state, observed)

        assert state.d < 0, "Should detect improvement"
        info = est.drift_info(state)
        assert info["label"] == "improving"
        assert est.marginal_return(state) > 0

    def test_flat_runner_near_zero_drift(self):
        """Simulate a runner with no improvement — drift should stay near zero."""
        est = KalmanEstimator()
        import random
        random.seed(99)
        state = est.init_state(first_time=15.0, priors={})

        for _ in range(29):
            observed = 15.0 + random.gauss(0, 2.0)
            state = est.process_attempt(state, observed)

        assert abs(state.d) < 1.0, f"Drift should be near zero, got {state.d}"

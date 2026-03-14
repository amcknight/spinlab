"""Kalman filter estimator for speedrun split times."""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from spinlab.estimators import Estimator, EstimatorState, register_estimator


# === Defaults ===
DEFAULT_D = -0.5
DEFAULT_R = 25.0
DEFAULT_P_D0 = 1.0
DEFAULT_Q_MM = 0.1
DEFAULT_Q_MD = 0.0
DEFAULT_Q_DD = 0.01
R_FLOOR = 1.0
R_REESTIMATE_INTERVAL = 10


@dataclass
class KalmanState(EstimatorState):
    """Per-split Kalman filter state."""

    # State vector
    mu: float = 0.0  # expected time (seconds)
    d: float = DEFAULT_D  # drift (seconds/run, negative = improving)

    # Covariance matrix P (2x2, stored as 4 scalars)
    P_mm: float = DEFAULT_R  # variance of mu
    P_md: float = 0.0
    P_dm: float = 0.0
    P_dd: float = DEFAULT_P_D0  # variance of drift

    # Noise parameters
    R: float = DEFAULT_R  # observation noise variance
    Q_mm: float = DEFAULT_Q_MM  # process noise for mu
    Q_md: float = DEFAULT_Q_MD
    Q_dm: float = DEFAULT_Q_MD
    Q_dd: float = DEFAULT_Q_DD  # process noise for drift

    # Tracking
    gold: float = float("inf")  # best observed time
    n_completed: int = 0
    n_attempts: int = 0

    def to_dict(self) -> dict:
        return {
            "mu": self.mu,
            "d": self.d,
            "P_mm": self.P_mm,
            "P_md": self.P_md,
            "P_dm": self.P_dm,
            "P_dd": self.P_dd,
            "R": self.R,
            "Q_mm": self.Q_mm,
            "Q_md": self.Q_md,
            "Q_dm": self.Q_dm,
            "Q_dd": self.Q_dd,
            "gold": self.gold,
            "n_completed": self.n_completed,
            "n_attempts": self.n_attempts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "KalmanState":
        return cls(
            mu=d.get("mu", 0.0),
            d=d.get("d", DEFAULT_D),
            P_mm=d.get("P_mm", DEFAULT_R),
            P_md=d.get("P_md", 0.0),
            P_dm=d.get("P_dm", 0.0),
            P_dd=d.get("P_dd", DEFAULT_P_D0),
            R=d.get("R", DEFAULT_R),
            Q_mm=d.get("Q_mm", DEFAULT_Q_MM),
            Q_md=d.get("Q_md", DEFAULT_Q_MD),
            Q_dm=d.get("Q_dm", DEFAULT_Q_MD),
            Q_dd=d.get("Q_dd", DEFAULT_Q_DD),
            gold=d.get("gold", float("inf")),
            n_completed=d.get("n_completed", 0),
            n_attempts=d.get("n_attempts", 0),
        )


@register_estimator
class KalmanEstimator(Estimator):
    name = "kalman"

    def _predict(self, state: KalmanState) -> KalmanState:
        """Predict step: propagate state one step forward.
        F = [[1, 1], [0, 1]]
        x_pred = F @ x
        P_pred = F @ P @ F^T + Q
        """
        mu_pred = state.mu + state.d
        d_pred = state.d

        # F @ P @ F^T expanded:
        P_mm_pred = state.P_mm + state.P_md + state.P_dm + state.P_dd + state.Q_mm
        P_md_pred = state.P_md + state.P_dd + state.Q_md
        P_dm_pred = state.P_dm + state.P_dd + state.Q_dm
        P_dd_pred = state.P_dd + state.Q_dd

        return replace(state,
            mu=mu_pred, d=d_pred,
            P_mm=P_mm_pred, P_md=P_md_pred, P_dm=P_dm_pred, P_dd=P_dd_pred,
        )

    def _update(self, predicted: KalmanState, observed_time: float) -> KalmanState:
        """Update step: incorporate observation.
        H = [1, 0]
        z = y - H @ x_pred = y - mu_pred
        S = H @ P_pred @ H^T + R = P_mm + R
        K = P_pred @ H^T / S = [P_mm/S, P_dm/S]
        x = x_pred + K * z
        P = (I - K @ H) @ P_pred
        """
        z = observed_time - predicted.mu
        S = predicted.P_mm + predicted.R

        K_mu = predicted.P_mm / S
        K_d = predicted.P_dm / S

        mu_new = predicted.mu + K_mu * z
        d_new = predicted.d + K_d * z

        P_mm_new = (1 - K_mu) * predicted.P_mm
        P_md_new = (1 - K_mu) * predicted.P_md
        P_dm_new = -K_d * predicted.P_mm + predicted.P_dm
        P_dd_new = -K_d * predicted.P_md + predicted.P_dd

        return replace(predicted,
            mu=mu_new, d=d_new,
            P_mm=P_mm_new, P_md=P_md_new, P_dm=P_dm_new, P_dd=P_dd_new,
        )

    def _reestimate_R(self, state: KalmanState, predicted: KalmanState, observed_time: float) -> KalmanState:
        innovation_sq = (observed_time - predicted.mu) ** 2
        R_est = innovation_sq - predicted.P_mm
        R_new = max(R_est, R_FLOOR)
        R_blended = 0.7 * state.R + 0.3 * R_new
        return replace(state, R=max(R_blended, R_FLOOR))

    def init_state(self, first_time: float, priors: dict) -> KalmanState:
        d = priors.get("d", DEFAULT_D)
        R = priors.get("R", DEFAULT_R)
        Q_mm = priors.get("Q_mm", DEFAULT_Q_MM)
        Q_md = priors.get("Q_md", DEFAULT_Q_MD)
        Q_dd = priors.get("Q_dd", DEFAULT_Q_DD)
        return KalmanState(
            mu=first_time, d=d,
            P_mm=R, P_md=0.0, P_dm=0.0, P_dd=DEFAULT_P_D0,
            R=R, Q_mm=Q_mm, Q_md=Q_md, Q_dm=Q_md, Q_dd=Q_dd,
            gold=first_time, n_completed=1, n_attempts=1,
        )

    def process_attempt(self, state: KalmanState, observed_time: float | None) -> KalmanState:
        if observed_time is None:
            return replace(state, n_attempts=state.n_attempts + 1)

        predicted = self._predict(state)
        updated = self._update(predicted, observed_time)

        n_completed = state.n_completed + 1
        gold = min(state.gold, observed_time)

        result = replace(updated,
            Q_mm=state.Q_mm, Q_md=state.Q_md, Q_dm=state.Q_dm, Q_dd=state.Q_dd,
            gold=gold, n_completed=n_completed, n_attempts=state.n_attempts + 1,
        )

        if n_completed >= R_REESTIMATE_INTERVAL and n_completed % R_REESTIMATE_INTERVAL == 0:
            result = self._reestimate_R(result, predicted, observed_time)

        return result

    def marginal_return(self, state: KalmanState) -> float:
        if state.mu == 0.0:
            return 0.0
        return -state.d / state.mu

    def drift_info(self, state: KalmanState) -> dict:
        import math
        p_dd_sqrt = math.sqrt(max(state.P_dd, 0.0))
        ci_lower = state.d - 1.96 * p_dd_sqrt
        ci_upper = state.d + 1.96 * p_dd_sqrt

        if state.d < 0:
            label = "improving"
        elif state.d > 0:
            label = "regressing"
        else:
            label = "flat"

        if ci_lower > 0 or ci_upper < 0:
            confidence = "confident"
        elif p_dd_sqrt < 0.5:
            confidence = "moderate"
        else:
            confidence = "uncertain"

        return {
            "drift": state.d,
            "ci_lower": ci_lower,
            "ci_upper": ci_upper,
            "label": label,
            "confidence": confidence,
        }

    def get_population_priors(self, all_states: list[KalmanState]) -> dict:
        mature = [s for s in all_states if s.n_completed >= R_REESTIMATE_INTERVAL]
        if not mature:
            return {
                "d": DEFAULT_D,
                "R": DEFAULT_R,
                "Q_mm": DEFAULT_Q_MM,
                "Q_dd": DEFAULT_Q_DD,
            }
        n = len(mature)
        return {
            "d": sum(s.d for s in mature) / n,
            "R": sum(s.R for s in mature) / n,
            "Q_mm": sum(s.Q_mm for s in mature) / n,
            "Q_dd": sum(s.Q_dd for s in mature) / n,
        }

    def rebuild_state(self, attempts: list[float | None]) -> KalmanState:
        completed = [t for t in attempts if t is not None]
        if not completed:
            state = KalmanState(n_attempts=len(attempts))
            return state
        first_time = completed[0]
        state = self.init_state(first_time, priors={})

        first_idx = attempts.index(first_time)
        for i in range(first_idx):
            state = self.process_attempt(state, None)

        for t in attempts[first_idx + 1:]:
            state = self.process_attempt(state, t)

        return state

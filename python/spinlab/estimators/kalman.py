"""Kalman filter estimator for speedrun split times."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from spinlab.estimators import Estimator, EstimatorState, ParamDef, register_estimator
from spinlab.models import AttemptRecord, Estimate, ModelOutput

if TYPE_CHECKING:
    from spinlab.db import Database

# === Defaults ===
DEFAULT_D = 0.0
DEFAULT_R = 25.0
DEFAULT_P_D0 = 1.0
DEFAULT_Q_MM = 0.1
DEFAULT_Q_MD = 0.0
DEFAULT_Q_DD = 0.01
R_FLOOR = 1.0
R_BLEND = 0.3
CI_MULTIPLIER = 1.96
MATURITY_THRESHOLD = 10


@dataclass
class KalmanState(EstimatorState):
    """Per-split Kalman filter state."""

    mu: float = 0.0
    d: float = DEFAULT_D
    P_mm: float = DEFAULT_R
    P_md: float = 0.0
    P_dm: float = 0.0
    P_dd: float = DEFAULT_P_D0
    R: float = DEFAULT_R
    Q_mm: float = DEFAULT_Q_MM
    Q_md: float = DEFAULT_Q_MD
    Q_dm: float = DEFAULT_Q_MD
    Q_dd: float = DEFAULT_Q_DD
    gold: float = float("inf")
    n_completed: int = 0
    n_attempts: int = 0

    # Clean tail filter state (parallel Kalman on clean_tail_ms)
    c_mu: float = 0.0
    c_d: float = DEFAULT_D
    c_P_mm: float = DEFAULT_R
    c_P_md: float = 0.0
    c_P_dm: float = 0.0
    c_P_dd: float = DEFAULT_P_D0
    c_R: float = DEFAULT_R
    c_n_completed: int = 0

    def to_dict(self) -> dict:
        return {
            "mu": self.mu, "d": self.d,
            "P_mm": self.P_mm, "P_md": self.P_md,
            "P_dm": self.P_dm, "P_dd": self.P_dd,
            "R": self.R,
            "Q_mm": self.Q_mm, "Q_md": self.Q_md,
            "Q_dm": self.Q_dm, "Q_dd": self.Q_dd,
            "gold": self.gold,
            "n_completed": self.n_completed,
            "n_attempts": self.n_attempts,
            "c_mu": self.c_mu, "c_d": self.c_d,
            "c_P_mm": self.c_P_mm, "c_P_md": self.c_P_md,
            "c_P_dm": self.c_P_dm, "c_P_dd": self.c_P_dd,
            "c_R": self.c_R,
            "c_n_completed": self.c_n_completed,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "KalmanState":
        return cls(
            mu=d.get("mu", 0.0), d=d.get("d", DEFAULT_D),
            P_mm=d.get("P_mm", DEFAULT_R), P_md=d.get("P_md", 0.0),
            P_dm=d.get("P_dm", 0.0), P_dd=d.get("P_dd", DEFAULT_P_D0),
            R=d.get("R", DEFAULT_R),
            Q_mm=d.get("Q_mm", DEFAULT_Q_MM), Q_md=d.get("Q_md", DEFAULT_Q_MD),
            Q_dm=d.get("Q_dm", DEFAULT_Q_MD), Q_dd=d.get("Q_dd", DEFAULT_Q_DD),
            gold=d.get("gold", float("inf")),
            n_completed=d.get("n_completed", 0),
            n_attempts=d.get("n_attempts", 0),
            c_mu=d.get("c_mu", 0.0), c_d=d.get("c_d", DEFAULT_D),
            c_P_mm=d.get("c_P_mm", DEFAULT_R), c_P_md=d.get("c_P_md", 0.0),
            c_P_dm=d.get("c_P_dm", 0.0), c_P_dd=d.get("c_P_dd", DEFAULT_P_D0),
            c_R=d.get("c_R", DEFAULT_R),
            c_n_completed=d.get("c_n_completed", 0),
        )


EstimatorState.register_state("kalman", KalmanState)


@register_estimator
class KalmanEstimator(Estimator):
    name = "kalman"
    display_name = "Kalman Filter"

    def declared_params(self) -> list[ParamDef]:
        return [
            ParamDef("D0", "Initial Drift", 0.0, -5.0, 5.0, 0.1,
                     "Assumed improvement rate before data (seconds/attempt). 0 = no assumption."),
            ParamDef("R", "Obs. Noise", 25.0, 0.01, 1000.0, 0.1,
                     "How noisy individual attempts are. Higher = smoother, slower to react."),
            ParamDef("P_D0", "Drift Variance", 1.0, 0.01, 50.0, 0.1,
                     "Initial uncertainty about drift. Higher = more willing to learn drift from data."),
            ParamDef("Q_mm", "Process Noise (Mean)", 0.1, 0.001, 10.0, 0.01,
                     "How fast true skill is expected to change. Higher = more reactive."),
            ParamDef("Q_dd", "Process Noise (Drift)", 0.01, 0.001, 5.0, 0.001,
                     "How fast drift itself changes. Higher = trend estimates shift faster."),
            ParamDef("R_floor", "Noise Floor", 1.0, 0.01, 10.0, 0.01,
                     "Minimum observation noise. Prevents filter from over-trusting single attempts."),
            ParamDef("R_blend", "R Learning Rate", 0.3, 0.01, 1.0, 0.01,
                     "How fast observation noise adapts. 1.0 = fully trust new estimate."),
        ]

    def _resolve_params(self, params: dict | None) -> dict:
        defaults = {p.name: p.default for p in self.declared_params()}
        if params:
            defaults.update(params)
        return defaults

    def _predict(self, state: KalmanState) -> KalmanState:
        mu_pred = state.mu + state.d
        d_pred = state.d
        P_mm_pred = state.P_mm + state.P_md + state.P_dm + state.P_dd + state.Q_mm
        P_md_pred = state.P_md + state.P_dd + state.Q_md
        P_dm_pred = state.P_dm + state.P_dd + state.Q_dm
        P_dd_pred = state.P_dd + state.Q_dd
        return replace(state,
            mu=mu_pred, d=d_pred,
            P_mm=P_mm_pred, P_md=P_md_pred, P_dm=P_dm_pred, P_dd=P_dd_pred,
        )

    def _update(self, predicted: KalmanState, observed_time: float) -> KalmanState:
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

    def _reestimate_R(self, state: KalmanState, predicted: KalmanState,
                      observed_time: float, r_floor: float, r_blend: float) -> KalmanState:
        innovation_sq = (observed_time - predicted.mu) ** 2
        R_est = innovation_sq - predicted.P_mm
        R_new = max(R_est, r_floor)
        R_blended = (1 - r_blend) * state.R + r_blend * R_new
        return replace(state, R=max(R_blended, r_floor))

    def init_state(self, first_attempt: AttemptRecord, priors: dict,
                   params: dict | None = None) -> KalmanState:
        p = self._resolve_params(params)
        assert first_attempt.time_ms is not None  # init_state is called with completed attempts
        first_time = first_attempt.time_ms / 1000.0
        d = priors.get("d", p["D0"])
        R = priors.get("R", p["R"])
        Q_mm = priors.get("Q_mm", p["Q_mm"])
        Q_md = priors.get("Q_md", DEFAULT_Q_MD)
        Q_dd = priors.get("Q_dd", p["Q_dd"])

        # Initialize clean tail filter if clean_tail_ms is available
        ct = first_attempt.clean_tail_ms
        if ct is not None:
            c_time = ct / 1000.0
            c_mu, c_n = c_time, 1
        else:
            c_mu, c_n = 0.0, 0

        return KalmanState(
            mu=first_time, d=d,
            P_mm=R, P_md=0.0, P_dm=0.0, P_dd=p["P_D0"],
            R=R, Q_mm=Q_mm, Q_md=Q_md, Q_dm=Q_md, Q_dd=Q_dd,
            gold=first_time, n_completed=1, n_attempts=1,
            c_mu=c_mu, c_d=d,
            c_P_mm=R, c_P_md=0.0, c_P_dm=0.0, c_P_dd=p["P_D0"],
            c_R=R, c_n_completed=c_n,
        )

    def _predict_clean(self, state: KalmanState) -> KalmanState:
        c_mu_pred = state.c_mu + state.c_d
        c_P_mm_pred = state.c_P_mm + state.c_P_md + state.c_P_dm + state.c_P_dd + state.Q_mm
        c_P_md_pred = state.c_P_md + state.c_P_dd + state.Q_md
        c_P_dm_pred = state.c_P_dm + state.c_P_dd + state.Q_dm
        c_P_dd_pred = state.c_P_dd + state.Q_dd
        return replace(state,
            c_mu=c_mu_pred,
            c_P_mm=c_P_mm_pred, c_P_md=c_P_md_pred,
            c_P_dm=c_P_dm_pred, c_P_dd=c_P_dd_pred,
        )

    def _update_clean(self, predicted: KalmanState, observed: float) -> KalmanState:
        z = observed - predicted.c_mu
        S = predicted.c_P_mm + predicted.c_R
        K_mu = predicted.c_P_mm / S
        K_d = predicted.c_P_dm / S
        return replace(predicted,
            c_mu=predicted.c_mu + K_mu * z,
            c_d=predicted.c_d + K_d * z,
            c_P_mm=(1 - K_mu) * predicted.c_P_mm,
            c_P_md=(1 - K_mu) * predicted.c_P_md,
            c_P_dm=-K_d * predicted.c_P_mm + predicted.c_P_dm,
            c_P_dd=-K_d * predicted.c_P_md + predicted.c_P_dd,
        )

    def _reestimate_c_R(self, state: KalmanState, predicted: KalmanState,
                        observed: float, r_floor: float, r_blend: float) -> KalmanState:
        innovation_sq = (observed - predicted.c_mu) ** 2
        R_est = innovation_sq - predicted.c_P_mm
        R_new = max(R_est, r_floor)
        R_blended = (1 - r_blend) * state.c_R + r_blend * R_new
        return replace(state, c_R=max(R_blended, r_floor))

    def process_attempt(  # type: ignore[override]
        self, state: KalmanState, new_attempt: AttemptRecord,
        all_attempts: list[AttemptRecord],
        params: dict | None = None,
    ) -> KalmanState:
        observed_time = (
            new_attempt.time_ms / 1000.0
            if new_attempt.completed and new_attempt.time_ms is not None
            else None
        )
        if observed_time is None:
            return replace(state, n_attempts=state.n_attempts + 1)

        p = self._resolve_params(params)
        predicted = self._predict(state)
        updated = self._update(predicted, observed_time)
        n_completed = state.n_completed + 1
        gold = min(state.gold, observed_time)

        result = replace(updated,
            Q_mm=state.Q_mm, Q_md=state.Q_md, Q_dm=state.Q_dm, Q_dd=state.Q_dd,
            gold=gold, n_completed=n_completed, n_attempts=state.n_attempts + 1,
        )
        if n_completed >= 2:
            result = self._reestimate_R(result, predicted, observed_time,
                                         r_floor=p["R_floor"], r_blend=p["R_blend"])

        # Clean tail filter update
        ct = new_attempt.clean_tail_ms
        if ct is not None:
            c_obs = ct / 1000.0
            if state.c_n_completed == 0:
                # First clean observation — initialize
                result = replace(result,
                    c_mu=c_obs, c_d=state.c_d,
                    c_P_mm=state.c_R, c_P_md=0.0, c_P_dm=0.0, c_P_dd=state.c_P_dd,
                    c_R=state.c_R, c_n_completed=1,
                )
            else:
                c_predicted = self._predict_clean(result)
                c_updated = self._update_clean(c_predicted, c_obs)
                c_n = state.c_n_completed + 1
                result = replace(result,
                    c_mu=c_updated.c_mu, c_d=c_updated.c_d,
                    c_P_mm=c_updated.c_P_mm, c_P_md=c_updated.c_P_md,
                    c_P_dm=c_updated.c_P_dm, c_P_dd=c_updated.c_P_dd,
                    c_n_completed=c_n,
                )
                if c_n >= 2:
                    result = self._reestimate_c_R(result, c_predicted, c_obs,
                                                   r_floor=p["R_floor"], r_blend=p["R_blend"])

        return result

    def model_output(self, state: KalmanState, all_attempts: list[AttemptRecord]) -> ModelOutput:  # type: ignore[override]
        none_estimate = Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None)
        if state.n_completed == 0:
            return ModelOutput(total=none_estimate, clean=none_estimate)

        if state.c_n_completed > 0:
            clean = Estimate(
                expected_ms=(state.c_mu + state.c_d) * 1000,
                ms_per_attempt=-state.c_d * 1000,
                floor_ms=None,
            )
        else:
            clean = none_estimate

        return ModelOutput(
            total=Estimate(
                expected_ms=(state.mu + state.d) * 1000,
                ms_per_attempt=-state.d * 1000,
                floor_ms=None,
            ),
            clean=clean,
        )

    def drift_info(self, state: KalmanState) -> dict:
        import math
        p_dd_sqrt = math.sqrt(max(state.P_dd, 0.0))
        ci_lower = state.d - CI_MULTIPLIER * p_dd_sqrt
        ci_upper = state.d + CI_MULTIPLIER * p_dd_sqrt
        if state.d < 0:
            label = "improving"
        elif state.d > 0:
            label = "regressing"
        else:
            label = "flat"
        return {
            "drift": state.d, "ci_lower": ci_lower, "ci_upper": ci_upper,
            "label": label,
        }

    def get_population_priors(self, all_states: list[KalmanState]) -> dict:
        mature = [s for s in all_states if s.n_completed >= MATURITY_THRESHOLD]
        if not mature:
            return {"d": DEFAULT_D, "R": DEFAULT_R, "Q_mm": DEFAULT_Q_MM, "Q_dd": DEFAULT_Q_DD}
        n = len(mature)
        return {
            "d": sum(s.d for s in mature) / n,
            "R": sum(s.R for s in mature) / n,
            "Q_mm": sum(s.Q_mm for s in mature) / n,
            "Q_dd": sum(s.Q_dd for s in mature) / n,
        }

    def get_priors(self, db: "Database", game_id: str) -> dict:
        """Load population priors from all mature kalman states for this game."""
        import json
        all_rows = db.load_all_model_states(game_id)
        kalman_rows = [r for r in all_rows if r["estimator"] == "kalman"]
        all_states = []
        for r in kalman_rows:
            if r["state_json"]:
                try:
                    all_states.append(KalmanState.from_dict(json.loads(r["state_json"])))
                except (json.JSONDecodeError, KeyError):
                    pass
        return self.get_population_priors(all_states)

    def rebuild_state(self, attempts: list[AttemptRecord],
                      params: dict | None = None) -> KalmanState:
        completed = [a for a in attempts if a.completed and a.time_ms is not None]
        if not completed:
            return KalmanState(n_attempts=len(attempts))
        first = completed[0]
        state = self.init_state(first, priors={}, params=params)
        first_idx = attempts.index(first)
        for a in attempts[:first_idx]:
            state = self.process_attempt(state, a, attempts, params=params)
        for a in attempts[first_idx + 1:]:
            state = self.process_attempt(state, a, attempts, params=params)
        return state

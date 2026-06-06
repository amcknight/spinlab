"""Unit tests for the EMA-Suite Sampler.

Spec: docs/superpowers/specs/2026-05-30-em-suite-sampler-design.md
"""
import math

import pytest

from spinlab.estimators.em_suite_sampler import POOL_SIZE, SamplerState, process_event


def _evt(outcome: str, time_ms: int):
    """Module-level helper: build a minimal EventAttempt for sampler tests."""
    from tests.factories import make_event_attempt
    return make_event_attempt(outcome=outcome, time_ms=time_ms)


def _seed_balanced(st: SamplerState) -> SamplerState:
    """Module-level helper: seed a state with >=2 successes and >=2 deaths
    so the prediction gate passes. Used by TestSampleEpisode and TestModelOutput."""
    for outcome, t in [("survived", 2000), ("died", 500), ("survived", 2100),
                       ("died", 600), ("survived", 1900), ("died", 550)]:
        st = process_event(st, _evt(outcome, t))
    return st


class TestAlphaGrid:
    def test_grid_has_ten_values_including_endpoints(self):
        from spinlab.estimators.em_suite_sampler import ALPHA_GRID
        assert len(ALPHA_GRID) == 10
        assert ALPHA_GRID[0] == 0.0
        assert ALPHA_GRID[-1] == 1.0
        # Strictly ascending
        assert list(ALPHA_GRID) == sorted(ALPHA_GRID)


class TestEmaStep:
    def test_applies_formula_per_alpha(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, ema_step,
        )
        prior = [10.0] * len(ALPHA_GRID)
        result = ema_step(prior, 0.0)
        # For each alpha: new = alpha*0 + (1-alpha)*10 = 10*(1-alpha)
        for v, alpha in zip(result, ALPHA_GRID):
            assert math.isclose(v, 10.0 * (1.0 - alpha))

    def test_alpha_zero_accumulates_additively(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, ema_step,
        )
        n = len(ALPHA_GRID)
        result = ema_step([10.0] * n, 100.0)
        # alpha=0.0 is the zero-decay anchor: new = old + driver (additive accumulation).
        assert result[0] == 110.0

    def test_alpha_one_replaces_entirely(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, ema_step,
        )
        n = len(ALPHA_GRID)
        result = ema_step([10.0] * n, 100.0)
        # alpha=1.0 at the last index → new value is the observation
        assert result[-1] == 100.0

    def test_denom_accumulation_pattern(self):
        # Denom = ema_step with driver=1.0 starting from [0.0]*n.
        # For alpha > 0: D_1 = alpha * 1 + (1-alpha) * 0 = alpha.
        # For alpha == 0: D_1 = 0 + 1.0 = 1.0 (additive accumulation).
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, ema_step,
        )
        n = len(ALPHA_GRID)
        d_after_1 = ema_step([0.0] * n, 1.0)
        for d, alpha in zip(d_after_1, ALPHA_GRID):
            if alpha == 0.0:
                assert d == 1.0  # zero-decay anchor: additive
            else:
                assert math.isclose(d, alpha)

    def test_rejects_wrong_length_values(self):
        from spinlab.estimators.em_suite_sampler import ema_step
        with pytest.raises(ValueError, match="length"):
            ema_step([10.0, 10.0], 100.0)


class TestSamplerState:
    def test_default_state_has_zero_accumulators_and_counts(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, SamplerState,
        )
        s = SamplerState()
        n = len(ALPHA_GRID)
        assert s.log_success_time_sums == [0.0] * n
        assert s.log_success_time_denoms == [0.0] * n
        assert s.log_death_time_sums == [0.0] * n
        assert s.log_death_time_denoms == [0.0] * n
        assert s.p_die_sums == [0.0] * n
        assert s.p_die_denoms == [0.0] * n
        assert s.n_successes == 0
        assert s.n_deaths == 0
        assert s.n_attempts_total == 0

    def test_ema_accessors_return_none_when_denom_zero(self):
        from spinlab.estimators.em_suite_sampler import SamplerState
        s = SamplerState()
        # All accessors return None when no data has been observed.
        assert s.log_success_time_ema(5) is None
        assert s.log_death_time_ema(5) is None
        assert s.p_die_ema(5) is None

    def test_to_dict_from_dict_roundtrip(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, SamplerState,
        )
        n = len(ALPHA_GRID)
        s = SamplerState(
            log_success_time_sums=[1.0] * n,
            log_success_time_denoms=[0.5] * n,
            log_death_time_sums=[2.0] * n,
            log_death_time_denoms=[0.4] * n,
            p_die_sums=[0.15] * n,
            p_die_denoms=[0.5] * n,
            n_successes=5,
            n_deaths=2,
            n_attempts_total=7,
        )
        d = s.to_dict()
        s2 = SamplerState.from_dict(d)
        assert s2 == s

    def test_serialization_roundtrips_base_counters(self):
        # Regression: n_completed / n_attempts are inherited from EstimatorState
        # and were omitted from to_dict/from_dict, so they reset to 0 on reload —
        # which broke the scheduler's bare-state routing AND the table's "Runs"
        # column (SegmentWithModel reads n_completed from state_json).
        st = SamplerState()
        st = process_event(st, _evt("survived", 2000))
        st = process_event(st, _evt("died", 500))
        st.n_completed = 3
        st.n_attempts = 7
        restored = SamplerState.from_dict(st.to_dict())
        assert restored.n_completed == 3
        assert restored.n_attempts == 7
        # and the rest still round-trips
        assert restored.n_successes == st.n_successes
        assert restored.success_time_pool == st.success_time_pool

    def test_state_class_is_a_valid_estimator_state(self):
        """SamplerState must satisfy the EstimatorState ABC (the scheduler/route
        rely on the to_dict/from_dict ingestion seam). The legacy registry
        lookup (EstimatorState._state_classes) was removed in the model purge —
        the scheduler instantiates SamplerState directly via the estimator."""
        from spinlab.estimators import EstimatorState
        from spinlab.estimators.em_suite_sampler import SamplerState
        assert issubclass(SamplerState, EstimatorState)
        # Round-trip a default state to confirm the seam works.
        st = SamplerState()
        restored = SamplerState.from_dict(st.to_dict())
        assert restored == st


class TestProcessEvent:
    def test_success_updates_success_time_and_p_die_only(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, SamplerState, process_event,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        ev = make_event_attempt(outcome="survived", time_ms=20_000)
        new_state = process_event(state, ev)
        # success_time and p_die have non-zero denoms at every alpha (incl. 0);
        # death_time denoms remain at 0 (no death observed).
        for i, alpha in enumerate(ALPHA_GRID):
            assert new_state.log_success_time_denoms[i] > 0.0
            assert new_state.p_die_denoms[i] > 0.0
            assert new_state.log_death_time_denoms[i] == 0.0
        assert new_state.n_successes == 1
        assert new_state.n_deaths == 0
        assert new_state.n_attempts_total == 1
        # p_die EMA for success = 0 (Bernoulli bit = 0 / denom)
        assert new_state.p_die_ema(5) == 0.0

    def test_death_updates_death_time_and_p_die_only(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, SamplerState, process_event,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        ev = make_event_attempt(outcome="died", time_ms=5_000)
        new_state = process_event(state, ev)
        # death_time and p_die have non-zero denoms at every alpha (incl. 0);
        # success_time denoms remain at 0 (no success observed).
        for i, alpha in enumerate(ALPHA_GRID):
            assert new_state.log_death_time_denoms[i] > 0.0
            assert new_state.p_die_denoms[i] > 0.0
            assert new_state.log_success_time_denoms[i] == 0.0
        assert new_state.n_successes == 0
        assert new_state.n_deaths == 1
        # p_die EMA for death = 1.0 (Bernoulli bit = 1 / denom)
        assert new_state.p_die_ema(5) == 1.0

    def test_invalidated_event_does_not_update_state(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        ev = make_event_attempt(outcome="survived", time_ms=20_000, invalidated=True)
        new_state = process_event(state, ev)
        assert new_state == state

    def test_log_time_first_observation_seeds_ema_at_observed_log(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        ev = make_event_attempt(outcome="survived", time_ms=20_000)
        new_state = process_event(state, ev)
        # First observation → EMA = log(20000) at every alpha > 0.
        expected = math.log(20_000)
        assert math.isclose(new_state.log_success_time_ema(5), expected)

    def test_two_successes_apply_normalized_ema_on_second(self):
        # With S_0=0, D_0=0, after two observations the normalized weights
        # are NOT equal at α=0.5 (recent observation gets more weight than
        # the prior because we don't bake in a "X_0 = X_1" synthetic).
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, SamplerState, process_event,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        state = process_event(
            state, make_event_attempt(outcome="survived", time_ms=10_000),
        )
        state = process_event(
            state, make_event_attempt(outcome="survived", time_ms=40_000),
        )
        # At alpha=0.5 normalized:
        #   S_2 = 0.5*log(40000) + 0.5*0.5*log(10000) = 0.5*L40 + 0.25*L10
        #   D_2 = 0.5 + 0.5*0.5 = 0.75
        #   EMA = (0.5*L40 + 0.25*L10) / 0.75 = (2*L40 + L10) / 3
        idx = ALPHA_GRID.index(0.5)
        L40 = math.log(40_000)
        L10 = math.log(10_000)
        expected = (2.0 * L40 + L10) / 3.0
        assert math.isclose(state.log_success_time_ema(idx), expected)
        assert state.n_successes == 2
        assert state.n_attempts_total == 2

    def test_normalization_fixes_anchoring_at_low_alpha(self):
        # The cure for the bug Andrew identified: with all five observations
        # equal to a constant K, the normalized EMA at α=0.01 should be K
        # (matching the empirical mean) — NOT anchored 96% to X_1.
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, SamplerState, process_event,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        for _ in range(5):
            state = process_event(
                state, make_event_attempt(outcome="survived", time_ms=12_345),
            )
        idx_slow = ALPHA_GRID.index(0.01)
        assert math.isclose(state.log_success_time_ema(idx_slow), math.log(12_345))

    def test_p_die_normalized_matches_empirical_under_low_alpha(self):
        # 1 death then 4 survives. Under the standard EMA at α=0.01 the
        # p_die value would stay near 1.0 (heavily anchored to the first
        # observation). Under the normalized EMA it should drop toward the
        # empirical rate 1/5 = 0.20.
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, SamplerState, process_event,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        state = process_event(state, make_event_attempt(outcome="died", time_ms=5_000))
        for _ in range(4):
            state = process_event(
                state, make_event_attempt(outcome="survived", time_ms=20_000),
            )
        idx_slow = ALPHA_GRID.index(0.01)
        p = state.p_die_ema(idx_slow)
        # Should be near 0.20 (within a couple percentage points; the
        # recency weighting still tilts a little toward the recent four
        # survives, pulling p slightly below 1/5).
        assert p is not None
        assert 0.15 < p < 0.25


class TestTrendSignalSlopes:
    def test_returns_none_when_gate_fails_n_successes(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event, trend_signal_slopes,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        # 1 success + 2 deaths → fails n_successes >= 2
        state = process_event(state, make_event_attempt(outcome="survived", time_ms=20_000))
        state = process_event(state, make_event_attempt(outcome="died", time_ms=5_000))
        state = process_event(state, make_event_attempt(outcome="died", time_ms=4_000))
        assert trend_signal_slopes(state, fast_idx=5, slow_idx=2) is None

    def test_returns_none_when_gate_fails_n_deaths(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event, trend_signal_slopes,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        # 2 successes + 1 death → fails n_deaths >= 2
        for _ in range(2):
            state = process_event(
                state, make_event_attempt(outcome="survived", time_ms=20_000),
            )
        state = process_event(state, make_event_attempt(outcome="died", time_ms=5_000))
        assert trend_signal_slopes(state, fast_idx=5, slow_idx=2) is None

    def test_alpha_zero_as_slow_anchor_yields_valid_slope(self):
        # α=0.0 is the zero-decay (uniform-mean) anchor. It accumulates data
        # like any other alpha, so a pair (fast_idx>0, slow_idx=0) produces
        # a valid slope when the gate passes.
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event, trend_signal_slopes,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        for t in (10_000, 8_000):
            state = process_event(
                state, make_event_attempt(outcome="survived", time_ms=t),
            )
        for t in (5_000, 4_000):
            state = process_event(
                state, make_event_attempt(outcome="died", time_ms=t),
            )
        # Pair (fast=8, slow=0) — α=0 has data now; slope is computable.
        slopes = trend_signal_slopes(state, fast_idx=8, slow_idx=0)
        assert slopes is not None
        assert len(slopes) == 3
        assert all(isinstance(x, float) and math.isfinite(x) for x in slopes)

    def test_returns_slopes_when_gate_passes_and_both_alphas_have_data(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event, trend_signal_slopes,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        for t in (10_000, 8_000):
            state = process_event(
                state, make_event_attempt(outcome="survived", time_ms=t),
            )
        for t in (5_000, 4_000):
            state = process_event(
                state, make_event_attempt(outcome="died", time_ms=t),
            )
        slopes = trend_signal_slopes(state, fast_idx=8, slow_idx=2)
        assert slopes is not None
        slope_log_success, slope_log_death, slope_logit_p_die = slopes
        assert isinstance(slope_log_success, float)
        assert isinstance(slope_log_death, float)
        assert isinstance(slope_logit_p_die, float)


class TestExpectedEpisodeTime:
    def _populated_state(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event,
        )
        from tests.factories import make_event_attempt
        state = SamplerState()
        # 3 successes around 20s, 3 deaths around 5s
        for t in (20_000, 21_000, 19_000):
            state = process_event(
                state, make_event_attempt(outcome="survived", time_ms=t),
            )
        for t in (5_000, 4_500, 5_500):
            state = process_event(
                state, make_event_attempt(outcome="died", time_ms=t),
            )
        return state

    def test_returns_none_when_gate_fails(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, expected_episode_time_ms,
        )
        state = SamplerState()  # no attempts yet
        assert expected_episode_time_ms(state, 5, 2, apply_slope=False) is None
        assert expected_episode_time_ms(state, 5, 2, apply_slope=True) is None

    def test_alpha_zero_baseline_produces_uniform_mean_prediction(self):
        # α=0 is the zero-decay anchor (uniform all-time mean). After
        # sufficient data it must equal the geometric-process formula evaluated
        # at the uniform means: success_time=exp(mean_log_success),
        # death_time=exp(mean_log_death), p=3/6=0.5.
        # _populated_state: successes (20000, 21000, 19000), deaths (5000, 4500, 5500).
        from spinlab.estimators.em_suite_sampler import (
            expected_episode_time_ms,
        )
        from spinlab.models import DEFAULT_DEATH_PENALTY_MS
        state = self._populated_state()
        result = expected_episode_time_ms(state, 0, 0, apply_slope=False)
        assert result is not None
        # Uniform mean of log-times -> state.log_success_time_ema(0) and log_death_time_ema(0).
        s = math.exp(state.log_success_time_ema(0))
        d = math.exp(state.log_death_time_ema(0))
        p = state.p_die_ema(0)  # = 0.5 (3 deaths / 6 attempts)
        assert math.isclose(p, 0.5, rel_tol=1e-9)
        expected = s + (p / (1.0 - p)) * (d + DEFAULT_DEATH_PENALTY_MS)
        assert math.isclose(result, expected, rel_tol=1e-9)

    def test_baseline_matches_geometric_formula(self):
        from spinlab.estimators.em_suite_sampler import (
            expected_episode_time_ms,
        )
        from spinlab.models import DEFAULT_DEATH_PENALTY_MS
        state = self._populated_state()
        fast_idx = 8  # alpha=0.5
        result = expected_episode_time_ms(
            state, fast_idx, fast_idx, apply_slope=False,
        )
        assert result is not None
        # Reconstruct the formula from the accessor EMAs.
        s = math.exp(state.log_success_time_ema(fast_idx))
        d = math.exp(state.log_death_time_ema(fast_idx))
        p = state.p_die_ema(fast_idx)
        expected = s + (p / (1.0 - p)) * (d + DEFAULT_DEATH_PENALTY_MS)
        assert math.isclose(result, expected, rel_tol=1e-9)

    def test_slope_shifts_prediction(self):
        from spinlab.estimators.em_suite_sampler import (
            expected_episode_time_ms,
        )
        state = self._populated_state()
        baseline = expected_episode_time_ms(
            state, 8, 2, apply_slope=False,
        )
        sloped = expected_episode_time_ms(
            state, 8, 2, apply_slope=True,
        )
        assert baseline is not None
        assert sloped is not None
        # The two should differ unless the data is exactly stationary
        assert not math.isclose(baseline, sloped, rel_tol=1e-9)

    def test_returns_none_when_p_die_too_close_to_one(self):
        # Force the p_die EMA at index 8 to ≈ 1 via direct Sum/Denom set.
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, expected_episode_time_ms, process_event,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        for t in (20_000, 21_000):
            state = process_event(
                state, make_event_attempt(outcome="survived", time_ms=t),
            )
        for t in (5_000, 4_500):
            state = process_event(
                state, make_event_attempt(outcome="died", time_ms=t),
            )
        # Force EMA = 1.0 by setting Sum == Denom at idx 8
        state.p_die_sums[8] = 0.5
        state.p_die_denoms[8] = 0.5
        # Now p_die_ema(8) = 1.0 ≥ 1 - LOGIT_EPS → diverges
        assert expected_episode_time_ms(state, 8, 2, apply_slope=False) is None


class TestBuildMatrix:
    def _populated_state(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event,
        )
        from tests.factories import make_event_attempt
        state = SamplerState()
        for t in (20_000, 21_000, 19_000):
            state = process_event(
                state, make_event_attempt(outcome="survived", time_ms=t),
            )
        for t in (5_000, 4_500, 5_500):
            state = process_event(
                state, make_event_attempt(outcome="died", time_ms=t),
            )
        return state

    def test_matrix_shape_and_alpha_grid(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, build_matrix,
        )
        result = build_matrix(self._populated_state())
        n = len(ALPHA_GRID)
        assert result["alpha_grid"] == list(ALPHA_GRID)
        assert len(result["baseline"]) == n
        assert len(result["matrix"]) == n
        assert all(len(row) == n for row in result["matrix"])

    def test_matrix_is_upper_triangular(self):
        # Lower triangle + diagonal are None by construction; upper triangle
        # has at least one non-None cell. α=0 endpoints and α=1 + death-streak
        # can legitimately produce None even in the upper triangle.
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, build_matrix,
        )
        result = build_matrix(self._populated_state())
        n = len(ALPHA_GRID)
        upper_triangle_values = []
        for fast_idx in range(n):
            for slow_idx in range(n):
                if slow_idx >= fast_idx:
                    assert result["matrix"][fast_idx][slow_idx] is None, (
                        f"cell [{fast_idx}][{slow_idx}] should be None (fast<=slow)"
                    )
                else:
                    upper_triangle_values.append(
                        result["matrix"][fast_idx][slow_idx]
                    )
        assert any(v is not None for v in upper_triangle_values)

    def test_alpha_zero_baseline_row_produces_uniform_mean_prediction(self):
        # The α=0.0 anchor is the zero-decay (uniform all-time mean) slot.
        # baseline[0] must equal the geometric-process formula at the uniform
        # means, mirroring test_baseline_matches_geometric_formula for α=0.5.
        from spinlab.estimators.em_suite_sampler import build_matrix
        from spinlab.models import DEFAULT_DEATH_PENALTY_MS
        state = self._populated_state()
        result = build_matrix(state)
        assert result["baseline"][0] is not None
        s = math.exp(state.log_success_time_ema(0))
        d = math.exp(state.log_death_time_ema(0))
        p = state.p_die_ema(0)  # = 0.5 (3 deaths / 6 attempts)
        assert math.isclose(p, 0.5, rel_tol=1e-9)
        expected = s + (p / (1.0 - p)) * (d + DEFAULT_DEATH_PENALTY_MS)
        assert math.isclose(result["baseline"][0], expected, rel_tol=1e-9)

    def test_baseline_contains_no_slope_predictions(self):
        from spinlab.estimators.em_suite_sampler import (
            build_matrix, expected_episode_time_ms,
        )
        state = self._populated_state()
        result = build_matrix(state)
        for fast_idx, baseline_value in enumerate(result["baseline"]):
            expected = expected_episode_time_ms(
                state, fast_idx, fast_idx, apply_slope=False,
            )
            assert (baseline_value is None and expected is None) or (
                baseline_value is not None
                and expected is not None
                and math.isclose(baseline_value, expected, rel_tol=1e-9)
            )

    def test_returns_none_for_empty_state(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, build_matrix,
        )
        result = build_matrix(SamplerState())
        assert all(v is None for v in result["baseline"])
        assert all(all(v is None for v in row) for row in result["matrix"])


class TestEstimatorIntegration:
    def test_identity(self):
        from spinlab.estimators.em_suite_sampler import EmSuiteSamplerEstimator
        est = EmSuiteSamplerEstimator()
        assert est.name == "em_suite_sampler"
        assert est.display_name == "EMA-Suite Sampler"

    def test_rebuild_state_replays_all_events(self):
        from spinlab.estimators.em_suite_sampler import EmSuiteSamplerEstimator
        from tests.factories import make_event_attempt

        events = [
            make_event_attempt(outcome="survived", time_ms=20_000),
            make_event_attempt(outcome="died", time_ms=5_000),
            make_event_attempt(outcome="survived", time_ms=21_000),
            make_event_attempt(outcome="died", time_ms=4_500),
        ]
        est = EmSuiteSamplerEstimator()
        state = est.rebuild_state(attempts=[], events=events)
        assert state.n_attempts_total == 4
        assert state.n_successes == 2
        assert state.n_deaths == 2

    def test_rebuild_state_updates_inherited_counters_from_attempts(self):
        from spinlab.estimators.em_suite_sampler import EmSuiteSamplerEstimator
        from tests.factories import make_attempt_record, make_event_attempt

        attempts = [
            make_attempt_record(time_ms=10_000, completed=True),
            make_attempt_record(time_ms=12_000, completed=False, deaths=2),
            make_attempt_record(time_ms=9_000, completed=True),
        ]
        events = [make_event_attempt(outcome="survived", time_ms=10_000)]
        est = EmSuiteSamplerEstimator()
        state = est.rebuild_state(attempts=attempts, events=events)
        # Inherited fields reflect episode-level totals
        assert state.n_completed == 2  # 2 of 3 attempts completed
        assert state.n_attempts == 3

    def test_model_output_clean_is_unmodeled(self):
        # clean stays None in Plan 1; the "Success Attempt" distribution lands
        # with the UI work (Spec #2). This test documents that invariant.
        from spinlab.estimators.em_suite_sampler import EmSuiteSamplerEstimator
        from tests.factories import make_event_attempt

        events = [
            make_event_attempt(outcome="survived", time_ms=20_000),
            make_event_attempt(outcome="died", time_ms=5_000),
        ]
        est = EmSuiteSamplerEstimator()
        state = est.rebuild_state(attempts=[], events=events)
        output = est.model_output(state, all_attempts=[], events=events)
        assert output.clean.expected_ms is None
        assert output.clean.ms_per_attempt is None


class TestReplayWithHistory:
    def _events(self):
        from tests.factories import make_event_attempt
        return [
            make_event_attempt(outcome="survived", time_ms=20_000),
            make_event_attempt(outcome="died", time_ms=5_000),
            make_event_attempt(outcome="survived", time_ms=21_000),
            make_event_attempt(outcome="died", time_ms=4_500),
        ]

    def test_history_shape_matches_alphas_and_snapshots(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, replay_with_history,
        )
        events = self._events()
        _, history = replay_with_history(events)
        n_alphas = len(ALPHA_GRID)
        n_snapshots = len(events) + 1
        for key in ("p_die", "log_success_time", "log_death_time"):
            assert key in history
            assert len(history[key]) == n_alphas
            for row in history[key]:
                assert len(row) == n_snapshots

    def test_snapshot_zero_is_all_none(self):
        # The empty initial state has denom = 0 for every alpha; every cell
        # at snapshot 0 must read None.
        from spinlab.estimators.em_suite_sampler import replay_with_history
        _, history = replay_with_history(self._events())
        for key in ("p_die", "log_success_time", "log_death_time"):
            for row in history[key]:
                assert row[0] is None

    def test_final_snapshot_matches_replayed_state(self):
        # After replay, the last snapshot for each quantity at each alpha
        # must equal what the state's accessor returns for that alpha.
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, replay_with_history,
        )
        state, history = replay_with_history(self._events())
        last = len(self._events())
        for i in range(len(ALPHA_GRID)):
            assert history["p_die"][i][last] == state.p_die_ema(i)
            assert (
                history["log_success_time"][i][last]
                == state.log_success_time_ema(i)
            )
            assert (
                history["log_death_time"][i][last]
                == state.log_death_time_ema(i)
            )

    def test_alpha_zero_row_is_none_only_at_snapshot_zero(self):
        # The α=0.0 anchor is the zero-decay (uniform all-time mean) slot.
        # Snapshot 0 (empty state) is always None; after observations, values
        # become available.
        from spinlab.estimators.em_suite_sampler import replay_with_history
        _, history = replay_with_history(self._events())
        for key in ("p_die", "log_success_time", "log_death_time"):
            # Snapshot 0 (empty state) must be None.
            assert history[key][0][0] is None
            # After events, at least one snapshot in the α=0 row is non-None.
            assert any(v is not None for v in history[key][0][1:])

    def test_invalidated_event_emits_same_snapshot_as_prior(self):
        # An invalidated event is a no-op for state but still counts as a
        # snapshot tick — its column must equal the previous column.
        from spinlab.estimators.em_suite_sampler import replay_with_history
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(outcome="survived", time_ms=20_000),
            make_event_attempt(outcome="died", time_ms=5_000),
            make_event_attempt(outcome="survived", time_ms=99_000, invalidated=True),
        ]
        _, history = replay_with_history(events)
        for key in ("p_die", "log_success_time", "log_death_time"):
            for row in history[key]:
                assert row[2] == row[3]

    def test_empty_event_list_yields_single_all_none_snapshot(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, replay_with_history,
        )
        state, history = replay_with_history([])
        assert state.n_attempts_total == 0
        for key in ("p_die", "log_success_time", "log_death_time"):
            assert len(history[key]) == len(ALPHA_GRID)
            for row in history[key]:
                assert row == [None]


class TestAlphaZeroUniformMean:
    """α=0.0 is the zero-decay anchor: uniform (equal-weight) all-time mean."""

    def test_alpha_zero_is_uniform_mean_of_log_times(self):
        from spinlab.estimators.em_suite_sampler import ALPHA_GRID, SamplerState, process_event
        from tests.factories import make_event_attempt
        # alpha=0.0 is the first grid entry: the zero-decay / uniform anchor.
        assert ALPHA_GRID[0] == 0.0
        st = SamplerState()
        # Three successes at 1000, 2000, 4000 ms.
        for t in (1000, 2000, 4000):
            st = process_event(st, make_event_attempt(outcome="survived", time_ms=t))
        # alpha=0 success-time EMA must equal the unbiased mean of log(time).
        expected = (math.log(1000) + math.log(2000) + math.log(4000)) / 3.0
        got = st.log_success_time_ema(0)
        assert got is not None
        assert math.isclose(got, expected, rel_tol=1e-9)

    def test_alpha_zero_p_die_is_uniform_rate(self):
        from spinlab.estimators.em_suite_sampler import SamplerState, process_event
        from tests.factories import make_event_attempt
        st = SamplerState()
        for outcome in ("died", "survived", "died", "died"):
            st = process_event(st, make_event_attempt(outcome=outcome, time_ms=1500))
        # 3 deaths of 4 attempts -> uniform p_die = 0.75 at alpha=0.
        assert math.isclose(st.p_die_ema(0), 0.75, rel_tol=1e-9)


class TestBuildSlopeMatrices:
    def _populated_state(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event,
        )
        from tests.factories import make_event_attempt
        state = SamplerState()
        for t in (20_000, 21_000, 19_000):
            state = process_event(
                state, make_event_attempt(outcome="survived", time_ms=t),
            )
        for t in (5_000, 4_500, 5_500):
            state = process_event(
                state, make_event_attempt(outcome="died", time_ms=t),
            )
        return state

    def test_returns_three_named_matrices(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, build_slope_matrices,
        )
        result = build_slope_matrices(self._populated_state())
        n = len(ALPHA_GRID)
        for key in ("slope_log_success", "slope_log_death", "slope_logit_p"):
            assert key in result
            assert len(result[key]) == n
            assert all(len(row) == n for row in result[key])

    def test_only_upper_triangle_populated(self):
        # Lower triangle + diagonal must be None; at least one upper-triangle
        # cell must be non-None on populated data.
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, build_slope_matrices,
        )
        result = build_slope_matrices(self._populated_state())
        n = len(ALPHA_GRID)
        for key, grid in result.items():
            upper_non_none = 0
            for fast_idx in range(n):
                for slow_idx in range(n):
                    if slow_idx >= fast_idx:
                        assert grid[fast_idx][slow_idx] is None, (
                            f"{key}[{fast_idx}][{slow_idx}] should be None"
                        )
                    elif grid[fast_idx][slow_idx] is not None:
                        upper_non_none += 1
            assert upper_non_none > 0, f"{key} has no populated cells"

    def test_cells_match_trend_signal_slopes(self):
        # The matrices are a lifted view of trend_signal_slopes; cell-by-cell
        # equality where the slope tuple is non-None.
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, build_slope_matrices, trend_signal_slopes,
        )
        state = self._populated_state()
        result = build_slope_matrices(state)
        n = len(ALPHA_GRID)
        for fast_idx in range(n):
            for slow_idx in range(fast_idx):
                slopes = trend_signal_slopes(state, fast_idx, slow_idx)
                if slopes is None:
                    assert result["slope_log_success"][fast_idx][slow_idx] is None
                    assert result["slope_log_death"][fast_idx][slow_idx] is None
                    assert result["slope_logit_p"][fast_idx][slow_idx] is None
                else:
                    assert result["slope_log_success"][fast_idx][slow_idx] == slopes[0]
                    assert result["slope_log_death"][fast_idx][slow_idx] == slopes[1]
                    assert result["slope_logit_p"][fast_idx][slow_idx] == slopes[2]

    def test_empty_state_returns_all_none(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, build_slope_matrices,
        )
        result = build_slope_matrices(SamplerState())
        for grid in result.values():
            for row in grid:
                for cell in row:
                    assert cell is None


class TestDrawFromPool:
    def test_draw_alpha_one_always_returns_newest(self):
        import random
        from spinlab.estimators.em_suite_sampler import draw_from_pool
        pool = [10.0, 20.0, 30.0]  # newest is last
        rng = random.Random(0)
        assert all(draw_from_pool(pool, 1.0, rng) == 30.0 for _ in range(20))

    def test_draw_alpha_zero_is_roughly_uniform(self):
        import random
        from spinlab.estimators.em_suite_sampler import draw_from_pool
        pool = [10.0, 20.0, 30.0]
        rng = random.Random(1)
        draws = [draw_from_pool(pool, 0.0, rng) for _ in range(3000)]
        counts = {v: draws.count(v) for v in pool}
        # Uniform: each ~1000 of 3000. Loose bounds for sampling noise.
        assert all(800 < c < 1200 for c in counts.values())

    def test_draw_empty_pool_returns_none(self):
        import random
        from spinlab.estimators.em_suite_sampler import draw_from_pool
        assert draw_from_pool([], 0.2, random.Random(0)) is None


class TestSampleEpisode:
    def _seed_balanced(self, st: SamplerState) -> SamplerState:
        # Delegate to module-level helper shared with TestModelOutput.
        return _seed_balanced(st)

    def test_sample_episode_returns_none_below_gate(self):
        import random
        from spinlab.estimators.em_suite_sampler import sample_episode
        st = SamplerState()
        st = process_event(st, _evt("survived", 2000))  # only 1 success, 0 deaths
        assert sample_episode(st, fast_idx=6, slow_idx=4, rng=random.Random(0)) is None

    def test_sample_episode_draws_a_positive_time(self):
        import random
        from spinlab.estimators.em_suite_sampler import sample_episode
        st = self._seed_balanced(SamplerState())
        rng = random.Random(0)
        draws = [sample_episode(st, fast_idx=6, slow_idx=4, k=0, rng=rng)
                 for _ in range(200)]
        assert all(d is not None and d > 0 for d in draws)
        # Max pure-success draw is 2100 (success pool max); exceeding it proves >=1 death contributed.
        assert max(draws) > 2100

    def test_sample_episode_gated_out_when_no_deaths(self):
        import random
        from spinlab.estimators.em_suite_sampler import sample_episode
        st = SamplerState()
        # All successes -> 0 deaths, so the >=2-deaths gate fails and the empty
        # death pool would have nothing to draw: must return None, not a fudge.
        for t in (2000, 2100, 1900):
            st = process_event(st, _evt("survived", t))
        assert sample_episode(st, fast_idx=6, slow_idx=4, rng=random.Random(0)) is None

    def test_sample_episode_caps_and_returns_none_when_never_survives(self):
        from spinlab.estimators.em_suite_sampler import sample_episode
        st = self._seed_balanced(SamplerState())

        class AlwaysDie:
            def random(self):
                return 0.0  # always < p -> always "died"

            def choices(self, seq, weights, k):
                return [seq[-1]]

        assert sample_episode(st, fast_idx=6, slow_idx=4, rng=AlwaysDie()) is None

    def test_sample_episode_k1_applies_success_slide_deterministically(self):
        from spinlab.estimators.em_suite_sampler import sample_episode, trend_signal_slopes
        # Clear improving trend so the success slope is non-trivial.
        st = SamplerState()
        for outcome, t in [("died", 1000), ("survived", 3000), ("died", 900),
                           ("survived", 2500), ("died", 800), ("survived", 2000),
                           ("survived", 1500)]:
            st = process_event(st, _evt(outcome, t))

        class SurviveNewest:
            def random(self):
                return 1.0  # never < p in [0,1) -> always survives first attempt

            def choices(self, seq, weights, k):
                return [seq[-1]]  # always the newest pool entry

        rng = SurviveNewest()
        r0 = sample_episode(st, fast_idx=6, slow_idx=4, k=0, rng=rng)
        r1 = sample_episode(st, fast_idx=6, slow_idx=4, k=1, rng=rng)
        slopes = trend_signal_slopes(st, 6, 4)
        assert slopes is not None
        slope_log_success = slopes[0]
        # k=1 = one trend-step of the improving trend applied multiplicatively.
        assert r0 is not None and r1 is not None
        assert math.isclose(r1 / r0, math.exp(slope_log_success), rel_tol=1e-9)


class TestRecencyDrawPools:
    def test_pools_split_by_outcome(self):
        st = SamplerState()
        st = process_event(st, _evt("survived", 1000))
        st = process_event(st, _evt("died", 500))
        st = process_event(st, _evt("survived", 1200))
        assert st.success_time_pool == [1000.0, 1200.0]
        assert st.death_time_pool == [500.0]

    def test_pool_is_ring_buffered_to_pool_size(self):
        st = SamplerState()
        for i in range(POOL_SIZE + 50):
            st = process_event(st, _evt("survived", 1000 + i))
        # Oldest 50 dropped; newest POOL_SIZE kept, in order.
        assert len(st.success_time_pool) == POOL_SIZE
        assert st.success_time_pool[0] == float(1000 + 50)
        assert st.success_time_pool[-1] == float(1000 + POOL_SIZE + 49)

    def test_pools_roundtrip_through_serialization(self):
        st = SamplerState()
        st = process_event(st, _evt("survived", 1000))
        st = process_event(st, _evt("died", 500))
        restored = SamplerState.from_dict(st.to_dict())
        assert restored.success_time_pool == [1000.0]
        assert restored.death_time_pool == [500.0]


class TestModelOutput:
    """Task 5: model_output emits a closed-form scalar at the default α pair."""

    def test_default_alpha_pair_indices_map_to_expected_rates(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX,
        )
        assert ALPHA_GRID[DEFAULT_FAST_IDX] == 0.2
        assert ALPHA_GRID[DEFAULT_SLOW_IDX] == 0.05

    def test_scalar_matches_closed_form_no_slope_at_default_fast(self):
        from spinlab.estimators.em_suite_sampler import (
            DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX,
            expected_episode_time_ms, expected_episode_time_scalar,
        )
        st = _seed_balanced(SamplerState())
        expected = expected_episode_time_ms(
            st, DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX, apply_slope=False,
        )
        assert expected_episode_time_scalar(st) == expected
        assert expected is not None and expected > 0

    def test_model_output_total_carries_the_scalar(self):
        from spinlab.estimators.em_suite_sampler import (
            EmSuiteSamplerEstimator, expected_episode_time_scalar,
        )
        st = _seed_balanced(SamplerState())
        est = EmSuiteSamplerEstimator()
        out = est.model_output(st, [])
        scalar = expected_episode_time_scalar(st)
        assert out.total.expected_ms == scalar
        assert out.total.ms_per_attempt == scalar  # greedy reads this
        assert out.clean.expected_ms is None       # clean unmodeled in Plan 1

    def test_model_output_is_none_scalar_below_gate(self):
        from spinlab.estimators.em_suite_sampler import EmSuiteSamplerEstimator
        st = SamplerState()  # no data
        out = EmSuiteSamplerEstimator().model_output(st, [])
        assert out.total.expected_ms is None
        assert out.total.ms_per_attempt is None

    def test_model_output_floor_is_running_min_clean(self):
        # Floor = running-min clean clear (clean_tail_ms) over completed
        # attempts; deaths (no clean tail) are ignored.
        from spinlab.estimators.em_suite_sampler import EmSuiteSamplerEstimator
        from tests.factories import make_attempt_record, make_incomplete
        st = _seed_balanced(SamplerState())
        attempts = [
            make_attempt_record(time_ms=20_000, completed=True, clean_tail_ms=18_000),
            make_attempt_record(time_ms=16_000, completed=True, clean_tail_ms=13_600),
            make_incomplete(),  # a death — no clean tail, ignored
        ]
        out = EmSuiteSamplerEstimator().model_output(st, attempts)
        assert out.total.floor_ms == 13_600

    def test_model_output_floor_none_without_clean_clears(self):
        from spinlab.estimators.em_suite_sampler import EmSuiteSamplerEstimator
        from tests.factories import make_incomplete
        st = _seed_balanced(SamplerState())
        out = EmSuiteSamplerEstimator().model_output(
            st, [make_incomplete(), make_incomplete()],
        )
        assert out.total.floor_ms is None

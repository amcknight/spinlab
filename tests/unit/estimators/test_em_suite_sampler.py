"""Unit tests for the EMA-Suite Sampler.

Spec: docs/superpowers/specs/2026-05-30-em-suite-sampler-design.md
"""
import math

import pytest  # noqa: F401  — kept for later tests that use pytest.raises


class TestAlphaGrid:
    def test_grid_has_ten_values_including_endpoints(self):
        from spinlab.estimators.em_suite_sampler import ALPHA_GRID
        assert len(ALPHA_GRID) == 10
        assert ALPHA_GRID[0] == 0.0
        assert ALPHA_GRID[-1] == 1.0
        # Strictly ascending
        assert list(ALPHA_GRID) == sorted(ALPHA_GRID)


class TestUpdateEmaArray:
    def test_seeds_unset_values_at_observation(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, update_ema_array,
        )
        result = update_ema_array([None] * len(ALPHA_GRID), 5.0)
        assert all(v == 5.0 for v in result)

    def test_applies_ema_formula_per_alpha(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, update_ema_array,
        )
        prior = [10.0] * len(ALPHA_GRID)
        result = update_ema_array(prior, 0.0)
        # For each alpha: new = alpha*0 + (1-alpha)*10 = 10*(1-alpha)
        for v, alpha in zip(result, ALPHA_GRID):
            assert math.isclose(v, 10.0 * (1.0 - alpha))

    def test_alpha_zero_never_updates(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, update_ema_array,
        )
        n = len(ALPHA_GRID)
        result = update_ema_array([10.0] * n, 100.0)
        # alpha=0.0 at index 0 means observation is ignored; value sticks.
        assert result[0] == 10.0

    def test_rejects_wrong_length_values(self):
        from spinlab.estimators.em_suite_sampler import update_ema_array
        with pytest.raises(ValueError, match="length"):
            update_ema_array([10.0, 10.0], 100.0)

    def test_alpha_one_replaces_entirely(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, update_ema_array,
        )
        n = len(ALPHA_GRID)
        result = update_ema_array([10.0] * n, 100.0)
        # alpha=1.0 at the last index → new value is the observation
        assert result[-1] == 100.0


class TestSamplerState:
    def test_default_state_has_unset_emas_and_zero_counts(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, SamplerState,
        )
        s = SamplerState()
        n = len(ALPHA_GRID)
        assert s.log_success_time_emas == [None] * n
        assert s.log_death_time_emas == [None] * n
        assert s.p_die_emas == [None] * n
        assert s.n_successes == 0
        assert s.n_deaths == 0
        assert s.n_attempts_total == 0

    def test_to_dict_from_dict_roundtrip(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, SamplerState,
        )
        n = len(ALPHA_GRID)
        s = SamplerState(
            log_success_time_emas=[1.0] * n,
            log_death_time_emas=[2.0] * n,
            p_die_emas=[0.3] * n,
            n_successes=5,
            n_deaths=2,
            n_attempts_total=7,
        )
        d = s.to_dict()
        s2 = SamplerState.from_dict(d)
        assert s2 == s

    def test_registered_with_estimator_state(self):
        from spinlab.estimators import EstimatorState
        # Importing the module registers SamplerState
        import spinlab.estimators.em_suite_sampler  # noqa: F401
        # Should be in the registry now (attribute is _state_classes; see
        # EstimatorState.register_state in python/spinlab/estimators/__init__.py)
        assert "em_suite_sampler" in EstimatorState._state_classes  # type: ignore[attr-defined]


class TestProcessEvent:
    def test_success_updates_success_time_and_p_die_only(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        ev = make_event_attempt(outcome="survived", time_ms=20_000)
        new_state = process_event(state, ev)
        # success_time and p_die seeded; death_time unchanged
        assert all(v is not None for v in new_state.log_success_time_emas)
        assert all(v is not None for v in new_state.p_die_emas)
        assert all(v is None for v in new_state.log_death_time_emas)
        assert new_state.n_successes == 1
        assert new_state.n_deaths == 0
        assert new_state.n_attempts_total == 1
        # outcome bit for success = 0 (death=1)
        assert new_state.p_die_emas[5] == 0.0

    def test_death_updates_death_time_and_p_die_only(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        ev = make_event_attempt(outcome="died", time_ms=5_000)
        new_state = process_event(state, ev)
        assert all(v is not None for v in new_state.log_death_time_emas)
        assert all(v is not None for v in new_state.p_die_emas)
        assert all(v is None for v in new_state.log_success_time_emas)
        assert new_state.n_successes == 0
        assert new_state.n_deaths == 1
        # outcome bit for death = 1
        assert new_state.p_die_emas[5] == 1.0

    def test_invalidated_event_does_not_update_state(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        ev = make_event_attempt(outcome="survived", time_ms=20_000, invalidated=True)
        new_state = process_event(state, ev)
        assert new_state == state

    def test_log_time_stored_in_log_space(self):
        from spinlab.estimators.em_suite_sampler import (
            SamplerState, process_event,
        )
        from tests.factories import make_event_attempt

        state = SamplerState()
        ev = make_event_attempt(outcome="survived", time_ms=20_000)
        new_state = process_event(state, ev)
        # Seeded value is log(20000)
        expected = math.log(20_000)
        assert math.isclose(new_state.log_success_time_emas[5], expected)

    def test_two_successes_apply_ema_update_on_second(self):
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
        # At alpha=0.5: new = 0.5*log(40000) + 0.5*log(10000)
        idx = ALPHA_GRID.index(0.5)
        expected = 0.5 * math.log(40_000) + 0.5 * math.log(10_000)
        assert math.isclose(state.log_success_time_emas[idx], expected)
        assert state.n_successes == 2
        assert state.n_attempts_total == 2

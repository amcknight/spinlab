"""Unit tests for the EMA-Suite Sampler.

Spec: docs/superpowers/specs/2026-05-30-em-suite-sampler-design.md
"""
import math

import pytest


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
        from spinlab.estimators.em_suite_sampler import update_ema_array
        result = update_ema_array([10.0, 10.0], 100.0)
        # alpha=0.0 at index 0 means observation is ignored.
        # We can't test full grid without checking shape; just confirm
        # that with alpha=0.0 the value sticks at the prior.
        # NOTE: this test passes through the grid order; index 0 == 0.0.
        assert result[0] == 10.0

    def test_alpha_one_replaces_entirely(self):
        from spinlab.estimators.em_suite_sampler import (
            ALPHA_GRID, update_ema_array,
        )
        n = len(ALPHA_GRID)
        result = update_ema_array([10.0] * n, 100.0)
        # alpha=1.0 at the last index → new value is the observation
        assert result[-1] == 100.0

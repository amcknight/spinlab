"""Pure-function tests for the v07 fit HTML renderer."""
from __future__ import annotations

import math

import pytest

from spinlab.fit_renderer import _theta_n


class TestThetaN:
    """The learning-curve formula must match boundary conditions exactly.

    log theta(n) = log theta_inf + (log theta_1 - log theta_inf) * 2^(-(n-1)/halflife)
    """

    def test_n_equals_1_returns_theta_1(self):
        log_inf = math.log(0.1)
        log_1 = math.log(0.5)
        log_halflife = math.log(20.0)
        assert _theta_n(log_inf, log_1, log_halflife, 1) == pytest.approx(0.5)

    def test_large_n_approaches_theta_inf(self):
        log_inf = math.log(0.1)
        log_1 = math.log(0.5)
        log_halflife = math.log(20.0)
        # After 20 halflives, residual gap is 2^-20 ~ 1e-6 of original.
        assert _theta_n(log_inf, log_1, log_halflife, 1 + 20 * 20) == pytest.approx(0.1, rel=1e-5)

    def test_n_equals_one_plus_halflife_halves_log_gap(self):
        log_inf = math.log(0.1)
        log_1 = math.log(0.5)
        halflife = 20.0
        log_halflife = math.log(halflife)
        # At n = 1 + halflife, log theta should be halfway between log_1 and log_inf.
        expected_log = log_inf + 0.5 * (log_1 - log_inf)
        assert math.log(_theta_n(log_inf, log_1, log_halflife, 1 + halflife)) == pytest.approx(expected_log)

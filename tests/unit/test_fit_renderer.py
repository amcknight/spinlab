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


from spinlab.fit_renderer import render_headline_html


def _fittable_payload(**overrides):
    """Construct a complete fittable v1 envelope. Override-as-you-go."""
    payload = {
        "schema": "segments-v1",
        "kind": "segment_fit",
        "segment_id": "test-seg",
        "n_attempts": 17,
        "model": "haz1",
        "wall_time_s": 0.05,
        "status": {
            "converged": True, "band_source": "laplace",
            "laplace_pd": True, "ppc_tension": False, "fittable": True,
        },
        "result": {
            "map": {"log_theta": [0.0] * 10, "natural": {
                "bpt_ms": 25000.0,
                "sf_inf": 0.07, "sf_1": 0.24,
                "ssp_inf": 0.46, "ssp_1": 0.46,
                "alpha_inf": 0.37, "alpha_1": 3.75,
                "halflife_sf": 34.0, "halflife_ssp": 28.0, "halflife_alpha": 21.0,
            }},
            "bands": {f"log_{k}": {"p5": -0.1, "p50": 0.0, "p95": 0.1} for k in (
                "bpt", "sf_inf", "ssp_inf", "alpha_inf",
                "sf_1", "ssp_1", "alpha_1",
                "hl_sf", "hl_ssp", "hl_alpha",
            )},
            "derived": {
                "M_clear": {"median_ms": 81200.0, "p5_ms": 53200.0, "p95_ms": 153200.0},
                "death_rate_next": 0.75,
            },
            "ppc": {
                "died_rate": {"obs": 0.88, "p_two_sided": 0.998},
                "died_tau_skew": {"obs": 0.91, "p_two_sided": 0.608},
                "died_tau_kurt": {"obs": 0.49, "p_two_sided": 0.364},
                "died_s_mid_third": {"obs": 0.20, "p_two_sided": 0.560},
            },
        },
        "caveats": ["low_n"],
    }
    payload.update(overrides)
    return payload


def _unfittable_payload():
    """Minimal envelope for a segment whose fit didn't converge."""
    return {
        "schema": "segments-v1", "kind": "segment_fit",
        "segment_id": "test-seg", "n_attempts": 20, "model": "haz1",
        "wall_time_s": 0.0014,
        "status": {
            "converged": False, "band_source": "none",
            "laplace_pd": False, "ppc_tension": False, "fittable": False,
        },
        "result": {},
        "caveats": ["unconverged"],
    }


class TestRenderHeadlineHtml:
    def test_fittable_shows_m_clear_seconds(self):
        out = render_headline_html(_fittable_payload())
        assert "M_clear" in out
        # 81200 ms ⇒ 81.2 s
        assert "81.2" in out
        assert "53.2" in out
        assert "153.2" in out

    def test_fittable_shows_death_rate_next(self):
        out = render_headline_html(_fittable_payload())
        assert "death_rate_next" in out
        assert "0.75" in out

    def test_unfittable_uses_em_dash_for_derived(self):
        out = render_headline_html(_unfittable_payload())
        # Either the stat label is absent or rendered with em-dash; either
        # is acceptable as long as no fake number leaks through.
        assert "81.2" not in out
        assert "—" in out or "M_clear" not in out

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


import xml.etree.ElementTree as ET

from spinlab.fit_renderer import render_learning_curve_svg


class TestRenderLearningCurveSvg:
    def test_fittable_returns_parseable_svg(self):
        svg = render_learning_curve_svg(_fittable_payload())
        # Strip xmlns prefixes for terse parsing.
        root = ET.fromstring(svg)
        assert root.tag.endswith("svg")
        # Three subplots (α, sf, ssp), each with at least a line path.
        paths = root.findall(".//{http://www.w3.org/2000/svg}path")
        assert len(paths) >= 3

    def test_unfittable_returns_placeholder(self):
        svg = render_learning_curve_svg(_unfittable_payload())
        # Either an empty <svg/> or a string containing "no fit" — never an
        # SVG with fabricated curves.
        assert "<svg" in svg
        assert "fit" not in svg or "no" in svg.lower()


from spinlab.fit_renderer import render_attempts_strip_svg


def _event_row(outcome: str, time_ms: int) -> dict:
    """Minimal event-row dict matching the get_segment_event_rows shape."""
    return {"outcome": outcome, "time_ms": time_ms}


class TestRenderAttemptsStripSvg:
    def test_returns_parseable_svg_with_at_least_one_marker(self):
        events = [
            _event_row("died", 12000), _event_row("died", 9000),
            _event_row("survived", 25000),
            _event_row("died", 8000), _event_row("survived", 24000),
        ]
        svg = render_attempts_strip_svg(events)
        root = ET.fromstring(svg)
        assert root.tag.endswith("svg")
        # Scatter markers render as <path> or <use> elements with `clip-path`
        # attributes set. Either way at least one drawing element.
        assert len(root.findall(".//*")) > 5

    def test_empty_events_returns_placeholder(self):
        svg = render_attempts_strip_svg([])
        assert "<svg" in svg
        assert "no attempts" in svg.lower() or "—" in svg


from spinlab.fit_renderer import render_ppc_table_html


class TestRenderPpcTableHtml:
    def test_fittable_emits_all_four_stats(self):
        out = render_ppc_table_html(_fittable_payload())
        for stat in (
            "died_rate", "died_tau_skew", "died_tau_kurt", "died_s_mid_third",
        ):
            assert stat in out
        assert "0.998" in out  # died_rate p_two_sided
        assert "0.608" in out  # died_tau_skew

    def test_unfittable_emits_placeholder(self):
        out = render_ppc_table_html(_unfittable_payload())
        assert "no PPC" in out or "—" in out
        assert "0.998" not in out


from spinlab.fit_renderer import render_history_table_html


class TestRenderHistoryTableHtml:
    def test_two_fits_oldest_first_with_flip_visible(self):
        # n=14 fittable, n=20 not — the L1 entrance flip we're auditing for.
        history = [
            {**_fittable_payload(n_attempts=14), "fitted_at": "2026-05-24T07:00:00Z"},
            {**_unfittable_payload(), "fitted_at": "2026-05-24T08:00:00Z"},
        ]
        out = render_history_table_html(history)
        # Headers
        for h in ("n", "fittable", "M_clear", "wall_ms", "fitted_at"):
            assert h in out
        # Both rows present
        assert ">14<" in out
        assert ">20<" in out
        # Fittable flip is visible as both Y and N
        assert ">Y<" in out
        assert ">N<" in out

    def test_empty_history_emits_placeholder(self):
        out = render_history_table_html([])
        assert "no fit history" in out.lower()

    def test_oldest_first_ordering(self):
        # Caller passes oldest-first; renderer preserves order. Hand a
        # newest-first list to verify the renderer does NOT silently
        # reorder.
        h = [
            {**_fittable_payload(n_attempts=20), "fitted_at": "2026-05-24T08:00:00Z"},
            {**_fittable_payload(n_attempts=14), "fitted_at": "2026-05-24T07:00:00Z"},
        ]
        out = render_history_table_html(h)
        # 20 should appear before 14 in the rendered table — renderer
        # respects caller-supplied order.
        idx_20 = out.find(">20<")
        idx_14 = out.find(">14<")
        assert 0 <= idx_20 < idx_14


from spinlab.fit_renderer import _anchor_id, render_segment_section


def _segment_row(**overrides):
    """Minimal sqlite-Row-like dict matching the segments table shape."""
    row = {
        "id": "test-seg",
        "level_number": 49,
        "start_type": "checkpoint", "start_ordinal": 1,
        "end_type": "checkpoint", "end_ordinal": 2,
    }
    row.update(overrides)
    return row


class TestAnchorId:
    def test_stable_format(self):
        assert _anchor_id(_segment_row()) == "seg-49-checkpoint_1-checkpoint_2"

    def test_entrance_to_checkpoint(self):
        row = _segment_row(
            start_type="entrance", start_ordinal=0,
            end_type="checkpoint", end_ordinal=1,
        )
        assert _anchor_id(row) == "seg-49-entrance_0-checkpoint_1"


class TestRenderSegmentSection:
    def test_includes_all_six_parts(self):
        section = render_segment_section(
            segment_row=_segment_row(),
            latest_payload=_fittable_payload(),
            history_oldest_first=[
                {**_fittable_payload(n_attempts=14), "fitted_at": "2026-05-24T07:00:00Z"},
                {**_fittable_payload(n_attempts=17), "fitted_at": "2026-05-24T08:00:00Z"},
            ],
            events=[_event_row("died", 9000), _event_row("survived", 25000)],
        )
        assert 'id="seg-49-checkpoint_1-checkpoint_2"' in section
        assert "<section" in section
        # Each of the five rendered blocks present:
        assert "fittable: Y" in section          # headline
        assert "<svg" in section                  # learning curve + attempts strip
        assert "died_rate" in section             # PPC table
        assert "wall_ms" in section               # history table


from spinlab.fit_renderer import render_game_index_html


class TestRenderGameIndexHtml:
    def test_lists_segments_with_anchor_links(self):
        bundle = [
            {
                "segment_row": _segment_row(level_number=1, start_type="entrance",
                                            start_ordinal=0, end_type="checkpoint",
                                            end_ordinal=1),
                "latest_payload": _unfittable_payload(),
            },
            {
                "segment_row": _segment_row(level_number=49, start_type="checkpoint",
                                            start_ordinal=1, end_type="checkpoint",
                                            end_ordinal=2),
                "latest_payload": _fittable_payload(n_attempts=17),
            },
        ]
        out = render_game_index_html(bundle)
        # Both segments present
        assert "L1 entrance_0→checkpoint_1" in out
        assert "L49 checkpoint_1→checkpoint_2" in out
        # Anchor links present
        assert "#seg-1-entrance_0-checkpoint_1" in out
        assert "#seg-49-checkpoint_1-checkpoint_2" in out
        # Status icons: one ✓ for fittable, one ✗ for unfittable
        assert "✓" in out
        assert "✗" in out

    def test_empty_bundle_emits_placeholder(self):
        out = render_game_index_html([])
        assert "no segments" in out.lower()

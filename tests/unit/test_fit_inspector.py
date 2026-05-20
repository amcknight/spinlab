"""Pure-Python tests for the v07 fit-payload pretty-printer.

Drives the formatter directly with constructed payloads — no DB, no
subprocess, no JAX. Each test is one rendering rule.
"""
from __future__ import annotations

from spinlab.fit_inspector import (
    format_fit_payload,
    format_fit_summary_row,
    format_history_line,
)


def _full_payload(**overrides):
    """Construct a complete v1 envelope; overrides patch the top level."""
    payload = {
        "schema": "segments-v1",
        "kind": "segment_fit",
        "segment_id": "w1-2-castle",
        "n_attempts": 234,
        "model": "haz1",
        "wall_time_s": 0.043,
        "status": {
            "converged": True, "band_source": "laplace",
            "laplace_pd": True, "ppc_tension": False,
            "fittable": True,
        },
        "result": {
            "map": {
                "log_theta": [9.901] + [0.0] * 9,
                "natural": {
                    "bpt_ms": 20000.0,
                    "sf_inf": 0.10, "sf_1": 0.20,
                    "ssp_inf": 0.20, "ssp_1": 0.30,
                    "alpha_inf": 0.30, "alpha_1": 0.40,
                    "halflife_sf": 15.0, "halflife_ssp": 15.0,
                    "halflife_alpha": 15.0,
                },
            },
            "bands": {
                "log_bpt": {"p5": 9.85, "p50": 9.90, "p95": 9.95},
                "log_hl_ssp": None,
            },
            "derived": {
                "M_clear": {
                    "median_ms": 31250.0, "p5_ms": 28900.0, "p95_ms": 35100.0,
                },
                "death_rate_next": 0.21,
            },
            "ppc": {
                "died_rate": {"obs": 0.32, "p_two_sided": 0.43},
            },
        },
        "caveats": [],
    }
    payload.update(overrides)
    return payload


def test_format_fit_payload_includes_all_headline_sections():
    """A well-formed envelope renders the header + 5 sections."""
    out = format_fit_payload(_full_payload(), fitted_at="2026-05-19T12:00:00Z")
    # Header
    assert "w1-2-castle" in out
    assert "n_attempts: 234" in out
    assert "model: haz1" in out
    # Status section
    assert "Status" in out
    assert "converged: yes" in out
    assert "band_source: laplace" in out
    assert "fittable: yes" in out
    # Derived section
    assert "Derived" in out
    assert "M_clear" in out
    # The headline number rendered as ms with seconds in parens.
    assert "31250 ms" in out  # median
    assert "death_rate_next: 21.0%" in out
    # Bands section
    assert "Bands" in out
    assert "log_bpt" in out
    assert "log_hl_ssp" in out
    assert "(suppressed)" in out  # null band
    # Caveats section — empty case shows "(none)" so the reader sees nothing was withheld.
    assert "Caveats" in out
    assert "(none)" in out


def test_format_fit_payload_renders_caveats_list():
    p = _full_payload(caveats=["low_n", "nuts_fallback"])
    out = format_fit_payload(p)
    assert "low_n" in out
    assert "nuts_fallback" in out


def test_format_fit_payload_handles_unconverged_envelope():
    """When converged=false, we don't promise bands or derived stats —
    the renderer should print the status block and a warning, not crash
    on missing `derived` fields."""
    p = _full_payload()
    p["status"]["converged"] = False
    p["status"]["fittable"] = False
    p["status"]["band_source"] = "none"
    p["result"]["derived"] = {}
    p["caveats"] = ["unconverged"]
    out = format_fit_payload(p)
    assert "converged: no" in out
    assert "band_source: none" in out
    # The renderer marks an absent derived block explicitly.
    assert "Derived" in out
    assert "(no derived stats — fit did not converge)" in out


def test_format_fit_payload_handles_pool_fit_kind():
    """Pool envelopes carry a different `result` shape (pool + segments
    instead of map/bands/derived). The renderer should detect kind and
    print a short pool summary."""
    pool = {
        "schema": "segments-v1",
        "kind": "pool_fit",
        "segment_id": None,
        "n_attempts": 712,
        "model": "haz1",
        "wall_time_s": 14.2,
        "status": {
            "converged": True, "band_source": "laplace",
            "laplace_pd": True, "ppc_tension": False, "fittable": True,
        },
        "result": {
            "pool": {
                "halflife_sf":    {"mean": 2.71, "sigma": 0.58},
                "halflife_ssp":   {"mean": 2.83, "sigma": 0.42},
                "halflife_alpha": {"mean": 2.90, "sigma": 0.71},
                "n_segments_used": 3,
            },
            "segments": [],
        },
        "caveats": [],
    }
    out = format_fit_payload(pool)
    assert "kind: pool_fit" in out
    assert "halflife_sf" in out
    assert "halflife_alpha" in out
    assert "n_segments_used: 3" in out


def test_format_fit_summary_row_renders_tab_separated_columns():
    """One row of `spinlab fit list` output."""
    summary = {
        "segment_id": "w1-2-castle",
        "level_number": 2,
        "active": 1,
        "kind": "segment_fit",
        "n_attempts": 234,
        "band_source": "laplace",
        "fittable": 1,
        "ppc_tension": 0,
        "wall_time_ms": 43,
        "fitted_at": "2026-05-19T12:00:00.000Z",
        "payload": _full_payload(),
    }
    row = format_fit_summary_row(summary)
    # Tab-separated. The first field is the truncated segment id.
    parts = row.split("\t")
    assert parts[0].startswith("w1-2-castle")
    # Eight columns total per the design (segment_id, level, n, fittable,
    # ppc, band, M50, fitted).
    assert len(parts) == 8
    assert parts[1] == "2"             # level
    assert parts[2] == "234"           # n
    assert parts[3] == "Y"             # fittable
    assert parts[4] == "N"             # ppc tension
    assert parts[5] == "lap"           # band_source short
    assert parts[6] == "31250"         # M_clear.median_ms
    assert parts[7] == "2026-05-19"    # fitted_at date only


def test_format_fit_summary_row_renders_dash_when_derived_missing():
    """Pool fits or unconverged envelopes have no derived.M_clear — the
    row shows `-` rather than crashing."""
    summary = {
        "segment_id": "s1", "level_number": 1, "active": 1,
        "kind": "segment_fit", "n_attempts": 3,
        "band_source": None, "fittable": 0, "ppc_tension": None,
        "wall_time_ms": 50, "fitted_at": "2026-05-19T12:00:00.000Z",
        "payload": {"result": {"derived": {}}, "status": {}},
    }
    parts = format_fit_summary_row(summary).split("\t")
    assert parts[3] == "N"   # fittable=0
    assert parts[4] == "-"   # ppc_tension None
    assert parts[5] == "-"   # band_source None
    assert parts[6] == "-"   # M50 absent


def test_format_history_line_is_one_line_per_fit():
    """`--history N` renders one line per fit. Format:
       `<fitted_at>  n=<N>  fittable=<Y/N>  M50=<ms>  band=<source>`"""
    p = _full_payload()
    line = format_history_line(p, fitted_at="2026-05-19T12:00:00.000Z")
    # Single line; no embedded newlines.
    assert "\n" not in line
    assert "2026-05-19T12:00:00" in line
    assert "n=234" in line
    assert "fittable=Y" in line
    assert "M50=31250" in line
    assert "band=laplace" in line

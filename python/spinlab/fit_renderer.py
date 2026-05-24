"""Static HTML renderer for v07 segment_fits payloads (Phase 2b).

Produces a single self-contained HTML page per game showing every
segment's latest fit (learning curve + raw attempts strip + PPC table +
fit history table). matplotlib renders all plots inline as <svg>; no
JavaScript, no external assets.

Pure helpers in / strings out. No DB, no FastAPI, no JAX imports.
matplotlib is loaded with the Agg backend (headless) inside the plot
helpers so importing this module is cheap even on systems without a
display server.

Contract: input payloads follow the v1 envelope at
``python/spinlab/_segments_v07/external_docs/api_contract.md``.
"""
from __future__ import annotations

import math
from typing import Any


def _theta_n(log_inf: float, log_1: float, log_halflife: float, n: float) -> float:
    """Evaluate the v07 learning-curve at attempt number ``n``.

    Formula (from ``_segments_v07/learning_model_v07.py``):
        log theta(n) = log_inf + (log_1 - log_inf) * 2 ^ (-(n-1) / halflife)

    Returns theta(n) in natural units. Used by the learning-curve plot
    helper to evaluate alpha/sf/ssp at each n along the x-axis.
    """
    halflife = math.exp(log_halflife)
    decay = 2.0 ** (-(n - 1) / halflife)
    log_theta = log_inf + (log_1 - log_inf) * decay
    return math.exp(log_theta)


def _fmt_seconds(ms: float) -> str:
    """ms → '12.3' (one decimal). Used in headline stats."""
    return f"{ms / 1000.0:.1f}"


def render_headline_html(payload: dict[str, Any]) -> str:
    """Render the per-segment headline stats block as HTML.

    Two lines:
      Status line  — n=<N>  fittable: Y/N  caveats: <a, b>
      Stats card   — M_clear <median>s [<p5>s, <p95>s]  death_rate_next <0.xx>

    For unfittable payloads (no ``result.derived``) the stats card shows
    em-dashes rather than fabricating numbers.
    """
    status = payload["status"]
    n = payload["n_attempts"]
    fittable = "Y" if status["fittable"] else "N"
    caveats = payload.get("caveats") or []
    caveats_str = ", ".join(caveats) if caveats else "—"

    derived = payload.get("result", {}).get("derived") or {}
    m_clear = derived.get("M_clear")
    if m_clear:
        m_str = (
            f"M_clear {_fmt_seconds(m_clear['median_ms'])}s "
            f"[{_fmt_seconds(m_clear['p5_ms'])}s, "
            f"{_fmt_seconds(m_clear['p95_ms'])}s]"
        )
    else:
        m_str = "M_clear —"
    drn = derived.get("death_rate_next")
    drn_str = f"death_rate_next {drn:.2f}" if drn is not None else "death_rate_next —"

    return (
        f'<div class="status">n={n}  fittable: {fittable}  '
        f'caveats: {caveats_str}</div>\n'
        f'<div class="headline">{m_str}  ·  {drn_str}</div>\n'
    )

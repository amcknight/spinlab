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

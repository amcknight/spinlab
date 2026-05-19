"""Smoke tests for the vendored segments_v07 package.

These do NOT exercise the math — that's the prototype's own validation
harness's job (which we still run via tox / the migration verification
step). They prove the *import path* into SpinLab works and that a
trivial fit returns a v1-schema dict.
"""
from __future__ import annotations

import pytest

pytest.importorskip("jax")          # skip cleanly when [fits] not installed
pytest.importorskip("numpyro")


def test_import_clean_surface():
    """The public re-export gives us the v1 contract helpers."""
    from spinlab.segments_model import (
        SCHEMA, fit_segment, refit_segment, fit_pool, prewarm_buckets,
    )
    assert SCHEMA == "segments-v1"
    assert callable(fit_segment)
    assert callable(refit_segment)
    assert callable(fit_pool)
    assert callable(prewarm_buckets)


def test_fit_segment_minimal_returns_v1_envelope():
    """A trivial 30-attempt sequence produces a well-formed v1 payload."""
    from spinlab.segments_model import fit_segment

    # 24 survives @ 20000ms, 6 deaths @ 8000ms — enough to exit `low_n`.
    attempts = (
        [{"outcome": "survived", "time_ms": 20000}] * 24
        + [{"outcome": "died", "time_ms": 8000}] * 6
    )
    payload = fit_segment(attempts, segment_id="smoke")

    assert payload["schema"] == "segments-v1"
    assert payload["kind"] == "segment_fit"
    assert payload["segment_id"] == "smoke"
    assert payload["n_attempts"] == 30
    assert payload["model"] == "haz1"
    assert "status" in payload and "converged" in payload["status"]
    assert "result" in payload

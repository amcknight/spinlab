"""Public SpinLab surface for the V07 segments model.

Re-exports the v1 contract helpers from the vendored
``_segments_v07`` package so application code can import a stable
clean name without the underscore prefix.

The reason for the indirection: the vendored tree uses flat module
names (`fit_jax`, `learning_model_v07`, `config`, ...) that we don't
want bleeding into autocomplete or grep hits in normal SpinLab work.
Code outside of `_segments_v07/` MUST import from here, not from the
vendored tree directly.
"""
from __future__ import annotations

# The vendor package's __init__ sets sys.path so the flat imports
# inside it resolve. Importing it as a side effect is what makes
# `segments_v07` available.
from spinlab._segments_v07 import _sv as _vendor  # noqa: F401

# The clean re-export surface. These are what the rest of SpinLab uses.
from segments_v07 import (
    SCHEMA,
    fit_segment,
    refit_segment,
    fit_pool,
    prewarm_buckets,
)

__all__ = [
    "SCHEMA",
    "fit_segment",
    "refit_segment",
    "fit_pool",
    "prewarm_buckets",
]

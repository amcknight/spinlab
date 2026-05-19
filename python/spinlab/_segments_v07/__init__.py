"""Vendored ``segments_experiment`` (V07 segments model).

Lives under an underscore-prefixed name so the rest of SpinLab treats it
as opaque. The clean re-export surface is `spinlab.segments_model`.

The prototype was written with flat imports (`import fit_jax`,
`import learning_model_v07 as lm_np`, etc.) rather than relative
imports. Rather than rewrite those — the prototype's validation harness
pins numerics to ~5 sig figs and a careless rewrite could drift JIT
compilation order — we prepend this directory to ``sys.path`` so the
flat imports resolve here. Cost: a (~30) module-name pollution risk.
Benefit: zero-touch on the validation harness, easy to refresh from
the prototype's upstream.

If a flat name ever collides with an external package SpinLab pulls in,
the fix is the relative-import rewrite, not deeper magic here.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_VENDOR_DIR = str(_Path(__file__).resolve().parent)
if _VENDOR_DIR not in _sys.path:
    # Prepend rather than append: in the unlikely event a flat name
    # collides with another package, the vendored copy wins for any
    # caller that goes through this module first.
    _sys.path.insert(0, _VENDOR_DIR)

# Trigger the public surface eagerly so any import-time errors surface
# at `import spinlab._segments_v07` rather than later. `segments_v07`
# re-exports the v1 contract helpers via `from api import ...`.
import segments_v07 as _sv  # noqa: E402,F401  (sys.path setup must happen first)

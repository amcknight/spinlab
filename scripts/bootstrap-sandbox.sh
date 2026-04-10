#!/usr/bin/env bash
# Idempotent environment bootstrap for running the spinlab test suite in
# a Linux sandbox / container that does not have a Python 3.11 venv
# pre-provisioned.
#
# Why this script exists:
#   Running the tests from a fresh sandbox requires several non-obvious
#   steps that previously burned ~10 failed tool calls on each run:
#     - System python3 is often 3.13 and has no pytest
#     - `uv sync --dev` can fail with symlink errors on some mounts
#     - `pip install -e .[dev]` fails on externally-managed envs (PEP 668)
#     - The [dev] extra historically missed `requests`, so pytest collection
#       crashed until it was installed separately
#
# Usage:
#   scripts/bootstrap-sandbox.sh
#   source /tmp/spinlab-env/bin/activate        # optional
#   python -m pytest -m "not (emulator or slow or frontend)"
#
# Idempotent: safe to re-run. If the venv already exists and pytest is
# importable, it does nothing beyond printing the path.

set -euo pipefail

VENV=/tmp/spinlab-env
PYVER=3.11
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import pytest, spinlab" 2>/dev/null; then
    echo "[bootstrap] venv already ready at $VENV"
    echo "[bootstrap] PATH prepend: export PATH=$VENV/bin:\$PATH"
    exit 0
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "[bootstrap] ERROR: 'uv' not found. Install from https://docs.astral.sh/uv/" >&2
    exit 1
fi

echo "[bootstrap] installing cpython $PYVER via uv"
uv python install "$PYVER"

echo "[bootstrap] creating venv at $VENV"
# Remove any half-baked venv from a previous failed run before recreating.
rm -rf "$VENV"
uv venv "$VENV" --python "$PYVER"

echo "[bootstrap] installing spinlab + [dev] extras (editable)"
uv pip install --python "$VENV/bin/python" -e "${REPO_ROOT}[dev]"

echo "[bootstrap] sanity check: import spinlab, run pytest --version"
"$VENV/bin/python" -c "import spinlab; print('spinlab ok')"
"$VENV/bin/python" -m pytest --version

cat <<EOF

[bootstrap] READY.

  venv:  $VENV
  repo:  $REPO_ROOT

To use in the current shell:
  export PATH=$VENV/bin:\$PATH
  python -m pytest -m "not (emulator or slow or frontend)" -q

EOF

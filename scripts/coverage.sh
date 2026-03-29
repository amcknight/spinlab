#!/usr/bin/env bash
# Generate coverage reports for SpinLab Python code.
#
# Usage:
#   ./scripts/coverage.sh           # unit tests only (fast)
#   ./scripts/coverage.sh --all     # unit + integration (needs Mesen2)
#   ./scripts/coverage.sh --html    # unit tests, then open HTML report
#
# Reports land in coverage/:
#   coverage/unit-html/     — unit test coverage
#   coverage/integ-html/    — integration test coverage (--all only)
#   coverage/combined-html/ — merged coverage (--all only)

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

mkdir -p coverage

MODE="${1:-}"

echo "=== Unit test coverage ==="
python -m pytest tests/ \
  --ignore=tests/integration/test_transitions.py \
  --cov=spinlab \
  --cov-report=term-missing \
  --cov-report=html:coverage/unit-html \
  --cov-config=pyproject.toml \
  -q

if [[ "$MODE" == "--all" ]]; then
  echo ""
  echo "=== Integration test coverage ==="
  python -m pytest tests/integration/test_transitions.py \
    --cov=spinlab \
    --cov-report=term-missing \
    --cov-report=html:coverage/integ-html \
    --cov-config=pyproject.toml \
    -q

  echo ""
  echo "=== Combined coverage ==="
  python -m pytest tests/ \
    --cov=spinlab \
    --cov-report=term-missing \
    --cov-report=html:coverage/combined-html \
    --cov-config=pyproject.toml \
    -q
fi

if [[ "$MODE" == "--html" ]]; then
  # Open the HTML report (works on Windows via start, macOS via open)
  if command -v start &>/dev/null; then
    start coverage/unit-html/index.html
  elif command -v open &>/dev/null; then
    open coverage/unit-html/index.html
  else
    echo "HTML report: coverage/unit-html/index.html"
  fi
fi

echo ""
echo "Done. Reports in coverage/"

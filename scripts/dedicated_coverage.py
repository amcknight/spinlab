#!/usr/bin/env python
"""Dedicated coverage report: per-module honest vs. suite-wide coverage.

Runs the fast test suite with per-test coverage contexts, then for each
spinlab module reports:
  - Dedicated %: coverage from tests in tests/test_<module>.py only
  - Suite-wide %: coverage from the entire fast suite
  - Gap: suite-wide minus dedicated (larger = more incidental coverage)

A "dedicated" test file is identified purely by convention:
  python/spinlab/foo.py  →  tests/test_foo.py

No mapping table. If tests/test_<module>.py does not exist, dedicated
coverage is reported as 0% (no dedicated tests).

Usage:
  python scripts/dedicated_coverage.py

Fast tests only. Emulator/integration tests are inherently cross-cutting
and including them would make "incidental" a misleading label.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
COVERAGE_DB = REPO_ROOT / "coverage" / ".coverage"
SPINLAB_DIR = REPO_ROOT / "python" / "spinlab"
TESTS_DIR = REPO_ROOT / "tests"


def run_fast_tests_with_contexts() -> None:
    """Run the fast test suite with per-test coverage contexts."""
    cmd = [
        sys.executable, "-m", "pytest", "tests/",
        "--ignore=tests/integration",
        "--ignore=tests/playwright",
        "-m", "not (emulator or slow or frontend)",
        "--cov=spinlab",
        "--cov-report=",  # suppress terminal report, we'll build our own
        "-q",
    ]
    print("Running fast suite with per-test coverage contexts...")
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    if result.returncode != 0:
        print(f"pytest exited with code {result.returncode}", file=sys.stderr)
        sys.exit(result.returncode)


def spinlab_modules() -> list[Path]:
    """Return all non-empty spinlab .py files, sorted."""
    modules = []
    for p in sorted(SPINLAB_DIR.rglob("*.py")):
        if p.name == "__init__.py":
            continue
        if p.stat().st_size == 0:
            continue
        modules.append(p)
    return modules


def dedicated_test_file(module: Path) -> Path | None:
    """Return tests/test_<basename>.py if it exists, else None."""
    candidate = TESTS_DIR / f"test_{module.stem}.py"
    return candidate if candidate.exists() else None


def module_coverage(conn: sqlite3.Connection, module: Path) -> tuple[int, int, int]:
    """Return (total_stmts, covered_suite_wide, covered_dedicated) for a module.

    All three values are in "executable statement" units so percentages are
    always ≤ 100%.  Coverage tracks every physical line of multi-line
    expressions; we intersect with the module's executable-statement set
    (from analysis2) to keep units consistent.

    Dedicated counts only contexts from tests/test_<module>.py.
    """
    # coverage.py stores full absolute paths. Match by the spinlab-relative
    # suffix (e.g. "spinlab/routes/practice.py") to avoid collisions between
    # files that share a basename (e.g. practice.py in two different dirs).
    rel_suffix = str(module.relative_to(REPO_ROOT / "python")).replace("\\", "/")

    file_row = conn.execute(
        "SELECT id FROM file WHERE path LIKE ?",
        (f"%{rel_suffix}",),
    ).fetchone()
    if file_row is None:
        return (0, 0, 0)
    file_id = file_row[0]

    # Executable statements: use analysis2 as the authoritative statement set.
    executable: set[int] = set(_statement_lines(module))
    if not executable:
        return (0, 0, 0)

    # Suite-wide: distinct executable lines covered by any context.
    rows = conn.execute(
        "SELECT numbits FROM line_bits WHERE file_id = ?",
        (file_id,),
    ).fetchall()
    suite_lines: set[int] = set()
    for (numbits,) in rows:
        suite_lines |= _numbits_to_lines(numbits)
    suite_covered = executable & suite_lines

    # Dedicated: only contexts from tests/test_<module>.py.
    # dynamic_context="test_function" stores contexts as "test_<module>.<class>.<func>"
    test_file = dedicated_test_file(module)
    dedicated_lines: set[int] = set()
    if test_file is not None:
        prefix = f"test_{module.stem}."
        rows = conn.execute(
            """
            SELECT lb.numbits
            FROM line_bits lb
            JOIN context c ON lb.context_id = c.id
            WHERE lb.file_id = ?
              AND c.context LIKE ?
            """,
            (file_id, f"{prefix}%"),
        ).fetchall()
        for (numbits,) in rows:
            dedicated_lines |= _numbits_to_lines(numbits)
    dedicated_covered = executable & dedicated_lines

    return (len(executable), len(suite_covered), len(dedicated_covered))


def _numbits_to_lines(numbits: bytes) -> set[int]:
    """Decode coverage.py's numbits blob into a set of line numbers."""
    from coverage.numbits import numbits_to_nums
    return set(numbits_to_nums(numbits))


def _statement_lines(module: Path) -> list[int]:
    """Return the executable statement line numbers for a module via coverage.py."""
    from coverage import Coverage
    cov = Coverage(data_file=str(COVERAGE_DB))
    cov.load()
    analysis = cov.analysis2(str(module))
    # analysis2 returns (filename, executable_lines, excluded_lines,
    #                    missing_lines, missing_formatted)
    return analysis[1]


def format_pct(covered: int, total: int) -> str:
    if total == 0:
        return "  n/a"
    return f"{100 * covered / total:5.0f}%"


def main() -> int:
    run_fast_tests_with_contexts()

    if not COVERAGE_DB.exists():
        print(f"ERROR: {COVERAGE_DB} not found after pytest run", file=sys.stderr)
        return 1

    conn = sqlite3.connect(COVERAGE_DB)
    rows = []
    for module in spinlab_modules():
        total, suite, dedicated = module_coverage(conn, module)
        if total == 0:
            continue
        has_tests = dedicated_test_file(module) is not None
        rows.append((module, total, suite, dedicated, has_tests))
    conn.close()

    # Sort by gap, descending (biggest gaps first)
    def gap_pct(r):
        _, total, suite, dedicated, _ = r
        if total == 0:
            return 0
        return (suite - dedicated) / total
    rows.sort(key=gap_pct, reverse=True)

    print()
    print(f"{'Module':<32}{'Dedicated':>12}{'Suite-wide':>12}{'Gap':>8}  Has test file")
    print("-" * 78)
    for module, total, suite, dedicated, has_tests in rows:
        name = str(module.relative_to(SPINLAB_DIR)).replace("\\", "/").removesuffix(".py")
        ded_str = format_pct(dedicated, total)
        suite_str = format_pct(suite, total)
        if total > 0:
            gap_str = f"{100 * (suite - dedicated) / total:5.0f}%"
        else:
            gap_str = "  n/a"
        marker = "yes" if has_tests else "no"
        print(f"{name:<32}{ded_str:>12}{suite_str:>12}{gap_str:>8}  {marker}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Shared CLI helpers — config-path resolution.

Lives as a sibling to ``cli.py`` / ``cli_fit.py`` / ``cli_fit_pool.py`` to
match SpinLab's existing flat CLI layout. Pulled out so every fit-* CLI
gets the same walk-up search behavior without re-implementing it.
"""
from __future__ import annotations

from pathlib import Path

# The default config filename SpinLab uses everywhere. Only this name
# triggers the parent-walk; an explicit non-default name (e.g.
# ``--config custom.yaml``) is treated as user-specified-exact.
_DEFAULT_CONFIG_NAME = "config.yaml"


def resolve_config_path(config_arg: str) -> Path:
    """Resolve a ``--config`` argument into an existing path or raise.

    Resolution order:
      1. If the literal path exists, return it.
      2. If the basename is the default (``config.yaml``), walk up from
         CWD looking for a ``config.yaml`` in any ancestor directory.
      3. Otherwise raise ``FileNotFoundError`` with an actionable message.

    The walk-up matches a common ergonomic from tools like ``git`` —
    SpinLab repos and the data dir typically share a root with
    ``config.yaml`` at the top, so anywhere inside the tree should resolve
    cleanly without the user having to ``cd`` first.
    """
    literal = Path(config_arg)
    if literal.exists():
        return literal

    # Walk up only for the default name. If the user explicitly typed a
    # custom path that doesn't exist, that's a user-correctable typo —
    # we shouldn't silently substitute the project's default config.
    if literal.name == _DEFAULT_CONFIG_NAME:
        cwd = Path.cwd()
        for parent in [cwd, *cwd.parents]:
            candidate = parent / _DEFAULT_CONFIG_NAME
            if candidate.exists():
                return candidate

    raise FileNotFoundError(
        f"config not found: {literal}\n"
        f"  literal path:    {literal.resolve() if literal.is_absolute() else literal}\n"
        f"  cwd:             {Path.cwd()}\n"
        f"  searched parents: walked up from cwd looking for "
        f"'{_DEFAULT_CONFIG_NAME}'\n"
        f"\nFix: pass --config <path> or run from a directory under "
        f"the spinlab project root."
    )

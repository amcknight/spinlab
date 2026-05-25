"""spinlab fit — read-only inspector over the segment_fits table.

Two subcommands:

  spinlab fit show <segment_id> [--kind ...] [--history N] [--json]
      Pretty-prints the latest v1 envelope for a segment (default) or
      a one-line summary per recent fit (--history). --json dumps raw.

  spinlab fit list --game <id> [--kind ...] [--json]
      One row per segment in the game that has a fit. Tab-separated by
      default; --json dumps a list of summary dicts.

This module is intentionally JAX-free — it only reads what the silent
pipeline has already persisted, so `spinlab fit show` runs in <100ms
even without the [fits] extra installed.
"""
from __future__ import annotations

import argparse
import json

from spinlab.fit_inspector import (
    format_fit_payload,
    format_fit_summary_row,
    format_history_line,
)

# `fit list` header. Match the column order in
# `fit_inspector.format_fit_summary_row` exactly. Reader-friendly: the
# header is also tab-separated so it lines up with the data rows in
# most monospaced terminals.
_LIST_HEADER = "\t".join(
    ["segment_id", "lvl", "n", "fit", "ppc", "band", "M50", "fitted"]
)


def _positive_int(s: str) -> int:
    """argparse type validator: int >= 1 only.

    SQLite treats ``LIMIT -1`` as "no limit", so a negative ``--history``
    would silently dump every fit ever recorded for the segment. Rejecting
    zero and negatives at the argparse layer keeps the CLI honest per
    CLAUDE.md's no-silent-fallbacks rule.
    """
    n = int(s)
    if n < 1:
        raise argparse.ArgumentTypeError(
            f"must be a positive integer (got {n})"
        )
    return n


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the `spinlab fit` parent subcommand and its children."""
    p_fit = subparsers.add_parser(
        "fit", help="Inspect segments-v07 silent fit payloads.",
    )
    fit_sub = p_fit.add_subparsers(dest="fit_command", required=True)

    p_show = fit_sub.add_parser(
        "show", help="Pretty-print the latest fit for a segment.",
    )
    p_show.add_argument("segment_id", help="Segment id to inspect.")
    p_show.add_argument(
        "--config", default="config.yaml",
        help="Path to the SpinLab YAML config (default: config.yaml).",
    )
    p_show.add_argument(
        "--kind", choices=("segment_fit", "pool_fit"), default="segment_fit",
        help="Which fit kind to load (default: segment_fit).",
    )
    p_show.add_argument(
        "--history", type=_positive_int, default=None, metavar="N",
        help="Print one-line summaries for the most recent N fits "
             "instead of pretty-printing the latest.",
    )
    p_show.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Output raw JSON (the v1 envelope) instead of pretty text.",
    )

    p_list = fit_sub.add_parser(
        "list", help="One row per segment with a fit; tab-separated.",
    )
    p_list.add_argument(
        "--config", default="config.yaml",
        help="Path to the SpinLab YAML config (default: config.yaml).",
    )
    p_list.add_argument(
        "--game", required=True,
        help="Game id to list (must exist in the games table).",
    )
    p_list.add_argument(
        "--kind", choices=("segment_fit", "pool_fit"), default="segment_fit",
        help="Which fit kind to summarize (default: segment_fit).",
    )
    p_list.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Output a JSON array of summary objects.",
    )

    # Sibling commands attach themselves to the same `fit` parent.
    # Keeps the user-facing surface (`spinlab fit <sub>`) flat while
    # letting each subcommand own its module (and tests) independently.
    from spinlab import cli_fit_inventory, cli_fit_rebuild, cli_fit_render
    cli_fit_inventory.add_to_fit_subparsers(fit_sub)
    cli_fit_rebuild.add_to_fit_subparsers(fit_sub)
    cli_fit_render.add_to_fit_subparsers(fit_sub)


def _open_db(config_path: str):
    """Resolve config + open the SQLite DB the dashboard uses.

    ``config_path`` runs through ``resolve_config_path`` so the default
    ``config.yaml`` finds the project config from any subdir of the repo
    (matches the ergonomic of running ``git`` commands from anywhere).
    """
    from spinlab.cli_common import resolve_config_path
    from spinlab.config import AppConfig
    from spinlab.db import Database
    resolved = resolve_config_path(config_path)
    cfg = AppConfig.from_yaml(resolved)
    return Database(cfg.data_dir / "spinlab.db")


def run_show(parsed: argparse.Namespace) -> int:
    db = _open_db(parsed.config)
    segment_id = parsed.segment_id
    kind = parsed.kind

    if parsed.history is not None:
        # History mode: newest-first, one line per fit. We can't pull
        # fitted_at from the payload (it's only in the row), so we drop
        # back to a raw SQL fetch that returns both columns together.
        rows = db.conn.execute(
            """SELECT payload_json, fitted_at FROM segment_fits
               WHERE segment_id = ? AND kind = ?
               ORDER BY id DESC LIMIT ?""",
            (segment_id, kind, int(parsed.history)),
        ).fetchall()
        if not rows:
            print(f"no fits found for segment {segment_id!r} kind={kind!r}")
            return 1
        for row in rows:
            payload = json.loads(row["payload_json"])
            print(format_history_line(payload, row["fitted_at"]))
        return 0

    # Default: pretty-print the latest payload.
    payload = db.load_latest_segment_fit(segment_id, kind)  # type: ignore[arg-type]
    if payload is None:
        print(f"no fit found for segment {segment_id!r} kind={kind!r}")
        return 1
    fitted_at_row = db.conn.execute(
        """SELECT fitted_at FROM segment_fits
           WHERE segment_id = ? AND kind = ?
           ORDER BY id DESC LIMIT 1""",
        (segment_id, kind),
    ).fetchone()
    fitted_at = fitted_at_row["fitted_at"] if fitted_at_row else None

    if parsed.json_output:
        print(json.dumps(payload, indent=2, sort_keys=False))
        return 0

    print(format_fit_payload(payload, fitted_at=fitted_at))
    return 0


def run_list(parsed: argparse.Namespace) -> int:
    db = _open_db(parsed.config)
    game_id = parsed.game
    kind = parsed.kind

    summaries = list(db.iter_segment_fit_summaries(game_id, kind=kind))
    if not summaries:
        if parsed.json_output:
            print("[]")
        else:
            print(f"no fits found for game {game_id!r} kind={kind!r}")
        return 0

    if parsed.json_output:
        # The payload field is already a dict — JSON-dump everything.
        print(json.dumps(summaries, indent=2, sort_keys=False, default=str))
        return 0

    print(_LIST_HEADER)
    for summary in summaries:
        print(format_fit_summary_row(summary))
    return 0

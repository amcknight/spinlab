"""spinlab fit render — write the Phase 2b static HTML audit report.

Loads every segment with a stored fit for the requested game, plus
each segment's full fit history and raw attempt events, and renders a
single self-contained HTML file via ``spinlab.fit_renderer``.

Read-only over the DB. Does NOT import spinlab.segments_model (no
JAX at CLI time) — the renderer is pure-Python over the v1 envelope.
"""
from __future__ import annotations

import argparse
import logging
import webbrowser
from pathlib import Path

logger = logging.getLogger(__name__)


def add_to_fit_subparsers(fit_sub: argparse._SubParsersAction) -> None:
    """Attach `render` to the existing `spinlab fit` parent subparser."""
    p = fit_sub.add_parser(
        "render",
        help="Write a static HTML audit report of all fits for a game.",
    )
    p.add_argument(
        "--config", default="config.yaml",
        help="Path to the SpinLab YAML config (default: config.yaml).",
    )
    p.add_argument(
        "--game", required=True,
        help="Game id to render (must exist in the games table).",
    )
    p.add_argument(
        "--out", required=True,
        help="Path to write the HTML report to.",
    )
    p.add_argument(
        "--open", dest="open_browser", action="store_true",
        help="After writing, open the report in the default web browser.",
    )


def _open_db(config_path: str):
    """Resolve config + open the SQLite DB (mirrors cli_fit._open_db)."""
    from spinlab.cli_common import resolve_config_path
    from spinlab.config import AppConfig
    from spinlab.db import Database
    resolved = resolve_config_path(config_path)
    cfg = AppConfig.from_yaml(resolved)
    return Database(cfg.data_dir / "spinlab.db")


def run(parsed: argparse.Namespace) -> int:
    """Load → render → write. Returns process exit code."""
    from spinlab import fit_renderer
    db = _open_db(parsed.config)
    game_id = parsed.game

    # Look up the game label for the report title; fall back to the id
    # if the row is missing rather than erroring out — the report is a
    # diagnostic, not a precision instrument.
    game_row = db.conn.execute(
        "SELECT name FROM games WHERE id = ?", (game_id,)
    ).fetchone()
    game_label = game_row["name"] if game_row else game_id

    summaries = list(db.iter_segment_fit_summaries(game_id))
    if not summaries:
        print(f"no fits found for game {game_id!r}")
        return 1

    bundle = []
    for summary in summaries:
        seg_id = summary["segment_id"]
        seg_row = db.conn.execute(
            "SELECT id, level_number, start_type, start_ordinal, "
            "       end_type, end_ordinal "
            "FROM segments WHERE id = ?",
            (seg_id,),
        ).fetchone()
        if seg_row is None:
            # The fit references a segment that has been hard-deleted.
            # Skip and log; the report is still useful without it.
            logger.warning("fit references missing segment %s; skipping", seg_id)
            continue
        # iter_recent_segment_fits yields newest-first; reverse so the
        # history table reads top-to-bottom chronologically.
        history_newest_first = list(db.iter_recent_segment_fits(seg_id, limit=50))
        history_oldest_first = list(reversed(history_newest_first))
        events = list(db.get_segment_event_rows(seg_id))
        bundle.append({
            "segment_row": seg_row,
            "latest_payload": summary["payload"],
            "history_oldest_first": history_oldest_first,
            "events": events,
        })

    # Sort by level then start ordinal so the report reads in
    # speedrun order. iter_segment_fit_summaries doesn't guarantee
    # ordering by geography — it groups by latest fit id.
    bundle.sort(key=lambda item: (
        item["segment_row"]["level_number"],
        item["segment_row"]["start_ordinal"],
    ))

    html = fit_renderer.build_report(
        game_label=game_label, game_id=game_id, bundle=bundle,
    )

    out_path = Path(parsed.out)
    out_path.write_text(html, encoding="utf-8")
    print(f"wrote {out_path} ({len(html)} bytes, {len(bundle)} segments)")

    if parsed.open_browser:
        webbrowser.open(out_path.resolve().as_uri())

    return 0

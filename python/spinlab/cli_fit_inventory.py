"""spinlab fit inventory — "did this session produce the data v07 expects?"

Read-only diagnostic that joins ``games`` / ``segments`` / ``attempts`` /
``segment_fits`` into a per-game summary. The goal is one screen Andrew
runs between practice / reference / speedrun sessions to confirm:

  * the segment-recording pipeline captured what he expected
  * event-level attempt rows are flowing in (and from which source)
  * enough segments have crossed the n>=5 fittable floor
  * the silent-fit pipeline has actually written fits

JAX-free by design: this command must boot in <100ms so it's cheap to
run mid-session. All work is plain SQL + format strings.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

# Floor below which a segment can't be fit meaningfully — must match
# ``scheduler._MIN_EVENTS_FOR_FIT`` and ``cli_fit_pool.POOL_MIN_EVENTS``.
# Hard-coding rather than importing because cli_fit_inventory is
# JAX-free and the scheduler import path drags in segments_model.
_FIT_FLOOR_N = 5

# All AttemptSource enum values. The inventory renders one row per
# source so a zero-count source is visible (e.g. "hyper_play: 0
# events" tells Andrew the hyper play wiring isn't producing data,
# which is exactly the kind of silent gap inventory exists to surface).
_KNOWN_SOURCES = ("practice", "hyper_play", "reference", "replay")


def add_to_fit_subparsers(fit_sub: argparse._SubParsersAction) -> None:
    """Attach `inventory` to the existing `spinlab fit` parent subparser."""
    p = fit_sub.add_parser(
        "inventory",
        help="Per-game data inventory: segments, attempts, fits.",
    )
    p.add_argument(
        "--config", default="config.yaml",
        help="Path to the SpinLab YAML config (default: config.yaml).",
    )
    grp = p.add_mutually_exclusive_group()
    grp.add_argument(
        "--game", default=None,
        help="Game id to inventory (mutually exclusive with --all).",
    )
    grp.add_argument(
        "--all", dest="all_games", action="store_true",
        help="Iterate every game in the DB.",
    )
    p.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Output a JSON object instead of pretty text.",
    )


def _open_db(config_path: str):
    from spinlab.cli_common import resolve_config_path
    from spinlab.config import AppConfig
    from spinlab.db import Database
    cfg = AppConfig.from_yaml(resolve_config_path(config_path))
    return Database(cfg.data_dir / "spinlab.db")


def _gather_game_inventory(db, game_id: str) -> dict[str, Any] | None:
    """Build the per-game inventory dict, or return None if the game doesn't exist."""
    game_row = db.conn.execute(
        "SELECT id, name, category FROM games WHERE id = ?", (game_id,),
    ).fetchone()
    if game_row is None:
        return None

    seg_rows = db.conn.execute(
        "SELECT COUNT(*) AS total, "
        "COALESCE(SUM(active), 0) AS active "
        "FROM segments WHERE game_id = ?",
        (game_id,),
    ).fetchone()

    # Per-outcome event counts.
    outcome_rows = db.conn.execute(
        """SELECT a.outcome, COUNT(*) AS n
           FROM attempts a JOIN segments s ON a.segment_id = s.id
           WHERE s.game_id = ? AND a.invalidated = 0
           GROUP BY a.outcome""",
        (game_id,),
    ).fetchall()
    by_outcome = {r["outcome"]: r["n"] for r in outcome_rows}
    by_outcome.setdefault("survived", 0)
    by_outcome.setdefault("died", 0)

    # Per-source event counts + distinct segment counts.
    source_rows = db.conn.execute(
        """SELECT a.source, COUNT(*) AS n,
                  COUNT(DISTINCT a.segment_id) AS segs
           FROM attempts a JOIN segments s ON a.segment_id = s.id
           WHERE s.game_id = ? AND a.invalidated = 0
           GROUP BY a.source""",
        (game_id,),
    ).fetchall()
    by_source: dict[str, int] = {src: 0 for src in _KNOWN_SOURCES}
    by_source_segs: dict[str, int] = {src: 0 for src in _KNOWN_SOURCES}
    for r in source_rows:
        by_source[r["source"]] = r["n"]
        by_source_segs[r["source"]] = r["segs"]

    total_events = sum(by_outcome.values())
    segments_with_events = db.conn.execute(
        """SELECT COUNT(DISTINCT a.segment_id) AS n
           FROM attempts a JOIN segments s ON a.segment_id = s.id
           WHERE s.game_id = ? AND a.invalidated = 0""",
        (game_id,),
    ).fetchone()["n"]

    # Fittable segments: per-segment event count (event_id-level) where
    # n >= floor. Episode count would be the more honest unit but the
    # refit gate (`scheduler._maybe_refit_segment`) uses raw event count,
    # so we mirror that to keep the inventory's "fittable" tally aligned
    # with what the live pipeline actually triggers on.
    fittable_rows = db.conn.execute(
        """SELECT a.segment_id AS segment_id,
                  s.level_number AS level_number,
                  COUNT(*) AS n,
                  COALESCE(fc.fit_count, 0) AS fit_count,
                  fc.latest AS latest_fitted_at
           FROM attempts a
           JOIN segments s ON a.segment_id = s.id
           LEFT JOIN (
             SELECT segment_id, COUNT(*) AS fit_count, MAX(fitted_at) AS latest
             FROM segment_fits WHERE kind = 'segment_fit'
             GROUP BY segment_id
           ) fc ON fc.segment_id = a.segment_id
           WHERE s.game_id = ? AND a.invalidated = 0
           GROUP BY a.segment_id
           HAVING COUNT(*) >= ?
           ORDER BY s.ordinal ASC, s.level_number ASC, s.id ASC""",
        (game_id, _FIT_FLOOR_N),
    ).fetchall()
    fittable_list = [
        {
            "segment_id": r["segment_id"],
            "level_number": r["level_number"],
            "n": r["n"],
            "fits": r["fit_count"],
            "latest_fitted_at": r["latest_fitted_at"],
        }
        for r in fittable_rows
    ]

    # Fit kind tallies + global latest.
    fit_rows = db.conn.execute(
        """SELECT sf.kind AS kind, COUNT(*) AS n, MAX(sf.fitted_at) AS latest
           FROM segment_fits sf JOIN segments s ON sf.segment_id = s.id
           WHERE s.game_id = ?
           GROUP BY sf.kind""",
        (game_id,),
    ).fetchall()
    fits = {"segment_fit": 0, "pool_fit": 0, "latest_fitted_at": None}
    for r in fit_rows:
        fits[r["kind"]] = r["n"]
        if r["latest"] and (fits["latest_fitted_at"] is None
                            or r["latest"] > fits["latest_fitted_at"]):
            fits["latest_fitted_at"] = r["latest"]

    return {
        "game_id": game_row["id"],
        "name": game_row["name"],
        "category": game_row["category"],
        "segments": {
            "total": seg_rows["total"],
            "active": seg_rows["active"],
            "with_events": segments_with_events,
        },
        "events": {
            "total": total_events,
            "by_outcome": by_outcome,
            "by_source": by_source,
            "by_source_segments": by_source_segs,
        },
        "fittable": {
            "count": len(fittable_list),
            "floor_n": _FIT_FLOOR_N,
            "segments": fittable_list,
        },
        "fits": fits,
    }


def _format_game(inv: dict[str, Any]) -> str:
    """Pretty-print one game's inventory record."""
    lines: list[str] = []
    name = inv["name"] or "?"
    category = inv["category"] or "?"
    lines.append(f"Game: {inv['game_id']} — \"{name}\" ({category})")
    segs = inv["segments"]
    lines.append(
        f"  Segments:        {segs['total']} ({segs['active']} active)"
    )

    ev = inv["events"]
    bo = ev["by_outcome"]
    lines.append(
        f"  Event attempts:  {ev['total']} across "
        f"{segs['with_events']} segment{'s' if segs['with_events'] != 1 else ''} "
        f"({bo['survived']} survived, {bo['died']} died)"
    )
    if ev["total"] > 0:
        lines.append("    by source:")
        for src in _KNOWN_SOURCES:
            n = ev["by_source"][src]
            n_segs = ev["by_source_segments"][src]
            event_word = "event" if n == 1 else "events"
            seg_word = "segment" if n_segs == 1 else "segments"
            lines.append(
                f"      {src:<11}: {n:>4} {event_word:<7} ({n_segs} {seg_word})"
            )
    else:
        lines.append("    (no attempts yet)")

    fit = inv["fittable"]
    floor = fit["floor_n"]
    if fit["count"] == 0:
        lines.append(
            f"  Fittable (n>={floor}): 0 segments — "
            f"need >= {floor} events on a segment to trigger a fit"
        )
    else:
        seg_word = "segment" if fit["count"] == 1 else "segments"
        lines.append(f"  Fittable (n>={floor}): {fit['count']} {seg_word}")
        lines.append("      segment_id               lvl    n  fits  latest_fit_at")
        for s in fit["segments"]:
            sid_col = s["segment_id"][:24].ljust(24)
            lvl_col = str(s["level_number"]).rjust(3)
            n_col = str(s["n"]).rjust(4)
            f_col = str(s["fits"]).rjust(4)
            latest = s["latest_fitted_at"] or "(no fits yet)"
            lines.append(f"      {sid_col} {lvl_col}  {n_col}  {f_col}  {latest}")

    fits = inv["fits"]
    fits_summary = f"  Fits stored:     {fits['segment_fit']} segment_fit, {fits['pool_fit']} pool_fit"
    if fits["latest_fitted_at"]:
        fits_summary += f", latest {fits['latest_fitted_at']}"
    lines.append(fits_summary)

    return "\n".join(lines)


def run(parsed: argparse.Namespace) -> int:
    if not parsed.game and not parsed.all_games:
        print(
            "error: must pass either --game <id> or --all",
            file=sys.stdout,
        )
        return 2

    db = _open_db(parsed.config)

    if parsed.game:
        inv = _gather_game_inventory(db, parsed.game)
        if inv is None:
            print(f"no game found with id {parsed.game!r}")
            return 1
        games_out = [inv]
    else:
        rows = db.conn.execute(
            "SELECT id FROM games ORDER BY id"
        ).fetchall()
        if not rows:
            if parsed.json_output:
                print(json.dumps({"games": []}, indent=2))
            else:
                print("no games in database — "
                      "run a reference run first to create one.")
            return 0
        games_out = [
            _gather_game_inventory(db, r["id"]) for r in rows
        ]
        # _gather_game_inventory only returns None for missing games; the
        # ids we just pulled all exist, so the cast is safe.
        games_out = [g for g in games_out if g is not None]

    if parsed.json_output:
        print(json.dumps({"games": games_out}, indent=2, default=str))
        return 0

    chunks = [_format_game(g) for g in games_out]
    print("\n\n".join(chunks))
    return 0

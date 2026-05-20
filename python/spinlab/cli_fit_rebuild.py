"""spinlab fit rebuild — wipe and cold-refit every segment_fit for a game.

The use case is *dataset revalidation*: a long practice session has built
up a permanent corpus of event-level attempts; you want every
``segment_fit`` recomputed from scratch under the current model version
(no warm-start chaining, no stale prior payloads). Run this after:

  * the [fits] extra was upgraded to a new model version
  * a long speed_run that built rows but didn't trigger live refits
  * any change to the prototype that should re-render existing data

The pool fit lives separately under ``spinlab fit-pool`` — rebuild is
strictly segment-level. Pool rows are NOT touched by this command.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

logger = logging.getLogger(__name__)

# Matches ``scheduler._MIN_EVENTS_FOR_FIT`` and ``cli_fit_pool.POOL_MIN_EVENTS``.
# Hard-coded rather than imported to keep the JAX/scheduler dependency
# lazy — the ImportError handler below prints an install hint instead of
# crashing if [fits] is missing.
_FIT_FLOOR_N = 5


def add_to_fit_subparsers(fit_sub: argparse._SubParsersAction) -> None:
    """Attach `rebuild` under the `spinlab fit` parent."""
    p = fit_sub.add_parser(
        "rebuild",
        help="Wipe and cold-refit every segment_fit for a game.",
    )
    p.add_argument(
        "--config", default="config.yaml",
        help="Path to the SpinLab YAML config (default: config.yaml).",
    )
    p.add_argument(
        "--game", required=True,
        help="Game id to rebuild (must exist in the games table).",
    )
    p.add_argument(
        "--kind", choices=("segment_fit",), default="segment_fit",
        help="Fit kind to rebuild (currently only segment_fit; pool fits "
             "are owned by `spinlab fit-pool`).",
    )


def _open_db(config_path: str):
    from spinlab.cli_common import resolve_config_path
    from spinlab.config import AppConfig
    from spinlab.db import Database
    cfg = AppConfig.from_yaml(resolve_config_path(config_path))
    return Database(cfg.data_dir / "spinlab.db")


def run(parsed: argparse.Namespace) -> int:
    # Lazy import: keeps the [fits] install requirement local to this
    # command so the rest of the CLI keeps booting cleanly without JAX.
    try:
        from spinlab.segments_model import fit_segment
    except ImportError:
        print(
            "error: install the [fits] extra: pip install -e '.[fits]'",
            file=sys.stdout,
        )
        return 2

    db = _open_db(parsed.config)
    game_id = parsed.game

    game_row = db.conn.execute(
        "SELECT id FROM games WHERE id = ?", (game_id,),
    ).fetchone()
    if game_row is None:
        print(f"no game found with id {game_id!r}")
        return 1

    # Eligibility query: every segment in this game with at least
    # _FIT_FLOOR_N non-invalidated event rows. The same gate the
    # live `_maybe_refit_segment` uses.
    eligible = db.conn.execute(
        """SELECT a.segment_id AS segment_id, COUNT(*) AS n
           FROM attempts a JOIN segments s ON a.segment_id = s.id
           WHERE s.game_id = ? AND a.invalidated = 0
           GROUP BY a.segment_id
           HAVING COUNT(*) >= ?
           ORDER BY s.ordinal ASC, s.level_number ASC, s.id ASC""",
        (game_id, _FIT_FLOOR_N),
    ).fetchall()
    if not eligible:
        print(
            f"no eligible segments for rebuild (game={game_id!r}, "
            f"floor n>={_FIT_FLOOR_N}). nothing to do."
        )
        return 0

    # Wipe before refit. We do this AFTER confirming eligibility so a
    # mistyped --game or all-low-N game doesn't silently nuke the prior
    # fits with nothing to replace them.
    db.conn.execute(
        """DELETE FROM segment_fits
           WHERE kind = 'segment_fit'
             AND segment_id IN (
               SELECT id FROM segments WHERE game_id = ?
             )""",
        (game_id,),
    )
    db.conn.commit()

    total_start = time.monotonic()
    written = 0
    for row in eligible:
        sid = row["segment_id"]
        n = row["n"]
        events = db.get_segment_event_rows(sid)
        attempts = [
            {"outcome": e["outcome"], "time_ms": int(e["time_ms"])}
            for e in events
            if not int(e["invalidated"])
        ]
        # Sanity: the SQL eligibility count should match the in-memory
        # filtered count. If it doesn't, an event has flipped invalidated
        # between the two queries — fine to proceed, but worth surfacing.
        if len(attempts) != n:
            logger.warning(
                "rebuild: segment %s eligibility count (%d) != "
                "in-memory attempts (%d); using in-memory count",
                sid, n, len(attempts),
            )

        t0 = time.monotonic()
        try:
            # Cold fit: `fit_segment` (not `refit_segment`) — no warm start,
            # which is exactly the property rebuild wants. The signature is
            # `(attempts, segment_id=...)`; there is no `prev_result` kwarg.
            payload = fit_segment(attempts, segment_id=sid)
        except Exception:
            logger.exception("rebuild: fit_segment failed for %s", sid)
            print(f"  {sid:<24} n={n:<4} FAILED (see log)")
            continue
        wall_ms = int((time.monotonic() - t0) * 1000)

        db.save_segment_fit(sid, "segment_fit", payload)
        status = payload.get("status", {})
        band = status.get("band_source") or "-"
        fittable = "Y" if status.get("fittable") else "N"
        print(
            f"  {sid:<24} n={n:<4} {wall_ms:>5}ms  fittable={fittable}  "
            f"band={band}"
        )
        written += 1

    total_s = time.monotonic() - total_start
    print(
        f"\nfit rebuild: wrote {written}/{len(eligible)} segment_fit rows "
        f"in {total_s:.1f}s"
    )
    return 0

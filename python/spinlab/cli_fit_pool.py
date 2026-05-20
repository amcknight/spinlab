"""spinlab fit-pool — manual EB pool refit across a game's segments.

Phase 1 trigger model is intentionally manual: Andrew runs this after a
session, daily, or on demand. Phase 2 picks cron-vs-on-startup based on
how stale the pool actually gets in practice.

The CLI is a thin wrapper around sv.fit_pool with DB persistence
plumbed in. Lives at module top level (not inside a ``cli/`` package)
to match SpinLab's existing single-file CLI layout in ``spinlab.cli``.
"""
from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger(__name__)

# Matches V1_ESSENCE POOL_MIN_PER_SEGMENT. Segments below this floor
# don't have enough data for their per-segment posterior to inform the
# pool prior; including them drags pool variance toward noise.
POOL_MIN_EVENTS = 5

# A pool fit needs at least two segments to learn a hyper-prior — one
# segment can't be pooled against itself. Below this floor we bail out
# cleanly with an informational message rather than calling fit_pool
# and getting a confusing internal error.
POOL_MIN_SEGMENTS = 2


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``spinlab fit-pool`` on the top-level argparse subparsers."""
    sp = subparsers.add_parser(
        "fit-pool",
        help="Run an empirical-Bayes pool fit across a game's segments.",
    )
    sp.add_argument(
        "--config", default="config.yaml",
        help="Path to the SpinLab YAML config (default: config.yaml).",
    )
    sp.add_argument(
        "--game", required=True,
        help="Game id to pool over (must exist in the games table).",
    )


def run(parsed: argparse.Namespace) -> int:
    """Execute the fit-pool subcommand. Returns a process exit code.

    Called from ``spinlab.cli.main`` after argparse routes ``fit-pool``
    here. Lazy-imports the heavy JAX-backed segments_model so plain
    ``spinlab --help`` and other subcommands don't pay the boot cost.
    """
    try:
        from spinlab.segments_model import fit_pool
    except ImportError:
        print(
            "error: install the [fits] extra: pip install -e '.[fits]'",
            file=sys.stderr,
        )
        return 2

    from spinlab.cli_common import resolve_config_path
    from spinlab.config import AppConfig
    from spinlab.db import Database

    cfg = AppConfig.from_yaml(resolve_config_path(parsed.config))
    db_path = cfg.data_dir / "spinlab.db"
    db = Database(db_path)

    game_id = parsed.game
    segs = db.conn.execute(
        "SELECT id FROM segments WHERE game_id = ? AND active = 1",
        (game_id,),
    ).fetchall()
    if not segs:
        print(f"no active segments for game {game_id!r}")
        return 0

    inputs: list[dict] = []
    for row in segs:
        sid = row["id"]
        events = db.get_segment_event_rows(sid)
        attempts = [
            {"outcome": e["outcome"], "time_ms": int(e["time_ms"])}
            for e in events
            if not int(e["invalidated"])
        ]
        if len(attempts) >= POOL_MIN_EVENTS:
            inputs.append({"segment_id": sid, "attempts": attempts})

    if len(inputs) < POOL_MIN_SEGMENTS:
        print(
            f"only {len(inputs)} segment(s) meet n>={POOL_MIN_EVENTS}; "
            f"pool needs >={POOL_MIN_SEGMENTS}. nothing to do."
        )
        return 0

    logger.info(
        "fit-pool: %d segments, total %d attempts",
        len(inputs), sum(len(s["attempts"]) for s in inputs),
    )
    pool_payload = fit_pool(inputs)

    # Persist each per-segment fit under kind='pool_fit'. The wrapper
    # envelope itself is informational; the per-segment bodies are what
    # the inspector/UI will eventually consume.
    for seg in pool_payload["result"]["segments"]:
        sid = seg["segment_id"]
        # Reconstruct a per-segment envelope so save_segment_fit's
        # status-column extraction works (it expects the outer shape).
        n_attempts = next(
            len(s["attempts"]) for s in inputs if s["segment_id"] == sid
        )
        per_seg_envelope = {
            "schema": pool_payload["schema"],
            "kind": "pool_fit",
            "segment_id": sid,
            "n_attempts": n_attempts,
            "model": pool_payload["model"],
            "wall_time_s": pool_payload["wall_time_s"],
            "status": seg["status"],
            "result": seg["result"],
            "caveats": seg["caveats"],
        }
        db.save_segment_fit(sid, "pool_fit", per_seg_envelope)

    print(
        f"fit-pool: wrote {len(pool_payload['result']['segments'])} "
        f"pool_fit rows; wall {pool_payload['wall_time_s']:.1f}s"
    )
    return 0

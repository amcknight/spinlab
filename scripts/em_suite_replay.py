#!/usr/bin/env python3
"""Offline replay of the EMA-suite sampler against historical event data.

For each segment with sufficient history in the given game, walks the event
log forward, recomputes the per-segment prediction at each step, scores
one-step-ahead MAE-log per (alpha_fast, alpha_slow) pair, and writes
per-segment CSV + heatmap PNG showing whether the slope-augmented predictor
beats the no-slope baseline.

See docs/superpowers/specs/2026-05-30-em-suite-sampler-design.md §Offline replay mode.

Usage:
    python scripts/em_suite_replay.py --game-id <game> [--db <path>] [--out-dir <path>] [--min-events N]
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from spinlab.db import Database
from spinlab.estimators.em_suite_sampler import (
    ALPHA_GRID,
    SamplerState,
    expected_episode_time_ms,
    process_event,
)
from spinlab.models import DEFAULT_DEATH_PENALTY_MS, EventAttempt
from spinlab.scheduler import _events_from_rows


def _load_events(db: Database, segment_id: str) -> list[EventAttempt]:
    rows = db.get_segment_event_rows(segment_id)
    return list(_events_from_rows(rows))


def _score_pair(
    events: list[EventAttempt], fast_idx: int, slow_idx: int,
    *, apply_slope: bool,
) -> tuple[float, int] | None:
    """One-step-ahead MAE-log per episode boundary.

    For each completed episode, predict its total time using the state from
    BEFORE that episode started, then advance state through the episode's
    events. Returns (mean_abs_log_error, n_scored) or None if no predictions
    fire.
    """
    from spinlab.estimators._episode_helpers import _group_into_episodes

    episodes = _group_into_episodes(events)
    state = SamplerState()
    errors: list[float] = []
    for ep in episodes:
        predicted = expected_episode_time_ms(
            state, fast_idx, slow_idx, apply_slope=apply_slope,
        )
        for ev in ep.events:
            state = process_event(state, ev)
        if ep.outcome != "completed":
            continue
        deaths = sum(1 for ev in ep.events if ev.outcome.value == "died")
        actual = (
            sum(ev.time_ms for ev in ep.events)
            + DEFAULT_DEATH_PENALTY_MS * deaths
        )
        if predicted is None or actual <= 0:
            continue
        errors.append(abs(math.log(actual) - math.log(predicted)))
    if not errors:
        return None
    return sum(errors) / len(errors), len(errors)


def _write_csv(csv_path: Path, results: list[tuple]) -> None:
    with csv_path.open("w") as f:
        f.write("alpha_fast,alpha_slow,mae_log_slope,mae_log_flat,n_scored\n")
        for row in results:
            f.write(",".join(str(x) for x in row) + "\n")


def _plot_segment(csv_path: Path, plot_path: Path) -> None:
    """Heatmap pair: slope MAE-log vs flat MAE-log."""
    import csv

    import numpy as np
    import matplotlib.pyplot as plt

    n = len(ALPHA_GRID)
    alpha_to_idx = {a: i for i, a in enumerate(ALPHA_GRID)}
    slope = np.full((n, n), np.nan)
    flat = np.full((n, n), np.nan)
    with csv_path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            f_idx = alpha_to_idx[float(row["alpha_fast"])]
            s_idx = alpha_to_idx[float(row["alpha_slow"])]
            slope[f_idx, s_idx] = float(row["mae_log_slope"])
            flat[f_idx, s_idx] = float(row["mae_log_flat"])

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    for ax, data, title in (
        (axes[0], slope, "slope MAE-log (lower=better)"),
        (axes[1], flat, "flat MAE-log (lower=better)"),
    ):
        im = ax.imshow(data, origin="lower")
        ax.set_xticks(range(n))
        ax.set_yticks(range(n))
        ax.set_xticklabels([str(a) for a in ALPHA_GRID], rotation=45)
        ax.set_yticklabels([str(a) for a in ALPHA_GRID])
        ax.set_xlabel("alpha_slow")
        ax.set_ylabel("alpha_fast")
        ax.set_title(title)
        fig.colorbar(im, ax=ax)
    fig.suptitle(csv_path.stem)
    fig.tight_layout()
    fig.savefig(plot_path, dpi=100)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-id", required=True, help="Game ID to replay")
    parser.add_argument("--db", default="spinlab.db", help="Path to DB file")
    parser.add_argument("--out-dir", default="out/em_suite_replay", help="Output directory")
    parser.add_argument("--min-events", type=int, default=10, help="Skip segments with fewer events")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    db = Database(Path(args.db))

    segments = db.get_all_segments_with_model(args.game_id)
    print(f"Found {len(segments)} segments in game {args.game_id}")
    n_processed = 0
    for seg in segments:
        events = _load_events(db, seg.segment_id)
        if len(events) < args.min_events:
            continue
        results = []
        for fast_idx in range(len(ALPHA_GRID)):
            base = _score_pair(events, fast_idx, fast_idx, apply_slope=False)
            for slow_idx in range(fast_idx):
                sloped = _score_pair(events, fast_idx, slow_idx, apply_slope=True)
                if base is None or sloped is None:
                    continue
                results.append((
                    ALPHA_GRID[fast_idx],
                    ALPHA_GRID[slow_idx],
                    sloped[0],
                    base[0],
                    sloped[1],
                ))
        if not results:
            print(f"  {seg.segment_id}: no scoreable pairs (events={len(events)})")
            continue
        csv_path = out_dir / f"{seg.segment_id}.csv"
        _write_csv(csv_path, results)
        try:
            plot_path = out_dir / f"{seg.segment_id}.png"
            _plot_segment(csv_path, plot_path)
            print(f"  {seg.segment_id}: {len(results)} pairs scored, wrote {csv_path.name} + {plot_path.name}")
        except Exception as exc:
            print(f"  {seg.segment_id}: csv ok, plot failed: {exc}")
        n_processed += 1

    print(f"Done. Processed {n_processed} segments.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

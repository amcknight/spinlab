"""Closed-form live-view reducers — the data behind the live practice view.

Everything here is EXACT closed form (no Monte-Carlo): valid because the live
view uses only the additive total-run-time objective under no_reset. See the
D-Live spec's Computation Sources table. The Monte-Carlo engine stays the
Simulator's.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from spinlab.estimators.em_suite_sampler import (
    DEFAULT_DEATH_PENALTY_MS,
    DEFAULT_FAST_IDX,
    DEFAULT_SLOW_IDX,
    SamplerState,
    _gate_passes,
    expected_episode_time_ms,
    expected_episode_time_scalar,
)


@dataclass
class LiveSegmentView:
    """Closed-form per-segment payload for the live view. ms fields None below gate."""
    ready: bool
    expected_episode_ms: float | None
    practice_gain_ms: float | None
    death_rate: float
    floor_ms: float | None
    last_episode_ms: float | None
    last_clean_ms: float | None
    last_deaths: int | None
    last_rank: int | None
    series: list[dict] = field(default_factory=list)


def _valid_completed(episodes: list[dict]) -> list[dict]:
    return [e for e in episodes if e["completed"] and not e["invalidated"]
            and e["time_ms"] is not None]


def live_segment_view(
    state: SamplerState,
    episodes: list[dict],
    *,
    reload_penalty_ms: int = DEFAULT_DEATH_PENALTY_MS,
) -> LiveSegmentView:
    if not _gate_passes(state):
        return LiveSegmentView(
            ready=False, expected_episode_ms=None, practice_gain_ms=None,
            death_rate=0.0, floor_ms=None, last_episode_ms=None,
            last_clean_ms=None, last_deaths=None, last_rank=None, series=[],
        )

    expected = expected_episode_time_scalar(state)
    slid = expected_episode_time_ms(
        state, DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX,
        apply_slope=True, reload_penalty_ms=reload_penalty_ms,
    )
    practice_gain = (expected - slid) if (expected is not None and slid is not None) else None

    p_die = state.p_die_ema(DEFAULT_FAST_IDX)
    death_rate = float(p_die) if p_die is not None else 0.0

    valid = _valid_completed(episodes)
    floor_ms: float | None = None
    series: list[dict] = []
    for e in valid:
        clean = e["clean_tail_ms"]
        if clean is not None:
            floor_ms = float(clean) if floor_ms is None else min(floor_ms, float(clean))
        series.append({
            "episode_ms": float(e["time_ms"]),
            "deaths": int(e["deaths"]),
            "clean_ms": float(clean) if clean is not None else None,
            "running_floor_ms": floor_ms,
        })

    if valid:
        last = valid[-1]
        last_episode_ms = float(last["time_ms"])
        last_clean_ms = float(last["clean_tail_ms"]) if last["clean_tail_ms"] is not None else None
        last_deaths = int(last["deaths"])
        totals = sorted(float(e["time_ms"]) for e in valid)
        last_rank = totals.index(last_episode_ms) + 1
    else:
        last_episode_ms = last_clean_ms = last_deaths = last_rank = None

    return LiveSegmentView(
        ready=True, expected_episode_ms=expected, practice_gain_ms=practice_gain,
        death_rate=death_rate, floor_ms=floor_ms, last_episode_ms=last_episode_ms,
        last_clean_ms=last_clean_ms, last_deaths=last_deaths, last_rank=last_rank,
        series=series,
    )

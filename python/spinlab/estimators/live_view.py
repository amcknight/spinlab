"""Closed-form live-view reducers — the data behind the live practice view.

Everything here is EXACT closed form (no Monte-Carlo): valid because the live
view uses only the additive total-run-time objective under no_reset. See the
D-Live spec's Computation Sources table. The Monte-Carlo engine stays the
Simulator's. Optional `baseline` arguments thread a `SessionSnapshot` through
so the reducer emits per-request diffs vs the session-start values.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from spinlab.estimators.em_suite_sampler import (
    DEFAULT_DEATH_PENALTY_MS,
    DEFAULT_FAST_IDX,
    DEFAULT_SLOW_IDX,
    LOGIT_EPS,
    SamplerState,
    _gate_passes,
    expected_episode_time_ms,
    expected_episode_time_scalar,
)
from spinlab.estimators.session_snapshot import (
    RouteBaseline, SegmentBaseline, route_diff, segment_diff,
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
    # Session diffs (None when no baseline / either side missing).
    expected_episode_diff_ms: float | None = None
    practice_gain_diff_ms: float | None = None
    floor_diff_ms: float | None = None
    death_rate_diff: float | None = None


def _valid_completed(
    episodes: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return [e for e in episodes if e["completed"] and not e["invalidated"]
            and e["time_ms"] is not None]


def live_segment_view(
    state: SamplerState,
    episodes: Sequence[Mapping[str, Any]],  # AttemptRow (TypedDict) or plain dict
    *,
    reload_penalty_ms: int = DEFAULT_DEATH_PENALTY_MS,
    baseline: SegmentBaseline | None = None,
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
        # 1-based; .index finds the first occurrence, so ties share the best
        # (competition-style) rank — a tied-best completion reads "1st".
        last_rank = totals.index(last_episode_ms) + 1
    else:
        last_episode_ms = last_clean_ms = last_deaths = last_rank = None

    diffs: dict[str, float | None] = {
        "expected_episode_diff_ms": None,
        "practice_gain_diff_ms": None,
        "floor_diff_ms": None,
        "death_rate_diff": None,
    }
    if baseline is not None:
        diffs = segment_diff(
            baseline,
            current_expected_ms=expected,
            current_gain_ms=practice_gain,
            current_death_rate=death_rate,
            current_floor_ms=floor_ms,
        )

    return LiveSegmentView(
        ready=True, expected_episode_ms=expected, practice_gain_ms=practice_gain,
        death_rate=death_rate, floor_ms=floor_ms, last_episode_ms=last_episode_ms,
        last_clean_ms=last_clean_ms, last_deaths=last_deaths, last_rank=last_rank,
        series=series,
        expected_episode_diff_ms=diffs["expected_episode_diff_ms"],
        practice_gain_diff_ms=diffs["practice_gain_diff_ms"],
        floor_diff_ms=diffs["floor_diff_ms"],
        death_rate_diff=diffs["death_rate_diff"],
    )


@dataclass
class RouteSummary:
    """Closed-form whole-run aggregate. None when no segment is estimable."""
    exp_run_ms: float | None
    exp_deaths: float | None
    n_estimable: int
    n_skipped: int
    # Session diffs (None when no baseline / either side missing).
    exp_run_diff_ms: float | None = None
    exp_deaths_diff: float | None = None
    practice_saved_ms: float | None = None


def route_summary(
    states: list[SamplerState],
    *,
    baseline: RouteBaseline | None = None,
) -> RouteSummary:
    run_ms = 0.0
    deaths = 0.0
    n_est = 0
    n_skip = 0
    for state in states:
        exp = expected_episode_time_scalar(state)
        p = state.p_die_ema(DEFAULT_FAST_IDX) if _gate_passes(state) else None
        # A segment contributes only if BOTH closed forms are defined; p->1 makes
        # the geometric mean diverge (exp is None there too), so skip honestly.
        if exp is None or p is None or p >= 1.0 - LOGIT_EPS:
            n_skip += 1
            continue
        run_ms += exp
        deaths += p / (1.0 - p)
        n_est += 1
    if n_est == 0:
        cur_run: float | None = None
        cur_deaths: float | None = None
    else:
        cur_run = run_ms
        cur_deaths = deaths
    diffs = (
        route_diff(baseline, current_exp_run_ms=cur_run, current_exp_deaths=cur_deaths)
        if baseline is not None
        else {"exp_run_diff_ms": None, "exp_deaths_diff": None, "practice_saved_ms": None}
    )
    return RouteSummary(
        exp_run_ms=cur_run, exp_deaths=cur_deaths,
        n_estimable=n_est, n_skipped=n_skip,
        exp_run_diff_ms=diffs["exp_run_diff_ms"],
        exp_deaths_diff=diffs["exp_deaths_diff"],
        practice_saved_ms=diffs["practice_saved_ms"],
    )

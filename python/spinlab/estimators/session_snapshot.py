"""Practice-session baseline snapshot for the live view's session diffs.

A snapshot is taken once at practice/hyper-play start:
  - per-segment: derived scalars (expected_episode_ms, practice_gain_ms,
    death_rate, floor_ms = running-min clean clear at session start),
  - route aggregate: exp_run_ms, exp_deaths.

The reducers (live_segment_view, route_summary) accept the snapshot as an
optional baseline and emit diff fields. The snapshot stores DERIVED scalars
rather than SamplerState clones — the live reducer recomputes 'current'
from the live state on every request, so we only need the comparison anchors.

`segment_diff` and `route_diff` report `current − baseline` (raw deltas).
The UI inverts sign per metric (expected/floor lower = improvement → green
when delta < 0; death_rate lower = improvement; exp_run lower = improvement;
practice_saved is the explicit `baseline − current` of exp_run so positive
always means 'time saved this session').
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from spinlab.db.attempts import AttemptRow
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


@dataclass(frozen=True)
class SegmentBaseline:
    expected_episode_ms: float | None
    practice_gain_ms: float | None
    death_rate: float
    floor_ms: float | None


@dataclass(frozen=True)
class RouteBaseline:
    exp_run_ms: float | None
    exp_deaths: float | None


@dataclass(frozen=True)
class SessionSnapshot:
    """Taken at practice/hyper-play start. Read-only thereafter."""
    started_at: float  # epoch seconds (time.time())
    segments: Mapping[str, SegmentBaseline]
    route: RouteBaseline


def _baseline_for_segment(
    state: SamplerState,
    episodes: Sequence[AttemptRow],
    *,
    reload_penalty_ms: int = DEFAULT_DEATH_PENALTY_MS,
) -> SegmentBaseline:
    if not _gate_passes(state):
        return SegmentBaseline(
            expected_episode_ms=None, practice_gain_ms=None,
            death_rate=0.0, floor_ms=_running_min_clean(episodes),
        )
    expected = expected_episode_time_scalar(state)
    slid = expected_episode_time_ms(
        state, DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX,
        apply_slope=True, reload_penalty_ms=reload_penalty_ms,
    )
    gain = (expected - slid) if (expected is not None and slid is not None) else None
    p = state.p_die_ema(DEFAULT_FAST_IDX)
    return SegmentBaseline(
        expected_episode_ms=expected,
        practice_gain_ms=gain,
        death_rate=float(p) if p is not None else 0.0,
        floor_ms=_running_min_clean(episodes),
    )


def _running_min_clean(episodes: Sequence[AttemptRow]) -> float | None:
    floor: float | None = None
    for e in episodes:
        if not e["completed"] or e["invalidated"]:
            continue
        clean = e["clean_tail_ms"]
        if clean is None:
            continue
        floor = float(clean) if floor is None else min(floor, float(clean))
    return floor


def _route_baseline(
    items: Sequence[tuple[str, SamplerState, Sequence[AttemptRow]]],
) -> RouteBaseline:
    run_ms = 0.0
    deaths = 0.0
    n_est = 0
    for _seg_id, state, _episodes in items:
        if not _gate_passes(state):
            continue
        exp = expected_episode_time_scalar(state)
        p = state.p_die_ema(DEFAULT_FAST_IDX)
        if exp is None or p is None or p >= 1.0 - LOGIT_EPS:
            continue
        run_ms += exp
        deaths += p / (1.0 - p)
        n_est += 1
    if n_est == 0:
        return RouteBaseline(exp_run_ms=None, exp_deaths=None)
    return RouteBaseline(exp_run_ms=run_ms, exp_deaths=deaths)


def snapshot_from_segments(
    *,
    started_at: float,
    segments: Sequence[tuple[str, SamplerState, Sequence[AttemptRow]]],
) -> SessionSnapshot:
    """Build a session snapshot from current sampler states + observed episodes.

    Each input tuple is (seg_id, state, episodes). The caller (SessionManager)
    owns the segment IDs — SamplerState itself doesn't carry one, so we accept
    the ID alongside the state rather than pulling a non-existent attribute.
    """
    per_seg: dict[str, SegmentBaseline] = {}
    for seg_id, state, episodes in segments:
        per_seg[seg_id] = _baseline_for_segment(state, episodes)
    return SessionSnapshot(
        started_at=started_at,
        segments=per_seg,
        route=_route_baseline(segments),
    )


def segment_diff(
    baseline: SegmentBaseline,
    *,
    current_expected_ms: float | None,
    current_gain_ms: float | None,
    current_death_rate: float,
    current_floor_ms: float | None,
) -> dict[str, float | None]:
    """Compute current − baseline for each comparable scalar. None when either side missing."""
    def _delta(c: float | None, b: float | None) -> float | None:
        if c is None or b is None:
            return None
        return float(c - b)
    return {
        "expected_episode_diff_ms": _delta(current_expected_ms, baseline.expected_episode_ms),
        "practice_gain_diff_ms": _delta(current_gain_ms, baseline.practice_gain_ms),
        # death_rate is always a float [0,1]; delta is always defined.
        "death_rate_diff": float(current_death_rate - baseline.death_rate),
        "floor_diff_ms": _delta(current_floor_ms, baseline.floor_ms),
    }


def route_diff(
    baseline: RouteBaseline,
    *,
    current_exp_run_ms: float | None,
    current_exp_deaths: float | None,
) -> dict[str, float | None]:
    """Compute route diffs. practice_saved_ms = baseline − current (positive = saved)."""
    def _delta(c: float | None, b: float | None) -> float | None:
        if c is None or b is None:
            return None
        return float(c - b)
    saved: float | None = None
    if baseline.exp_run_ms is not None and current_exp_run_ms is not None:
        saved = float(baseline.exp_run_ms - current_exp_run_ms)
    return {
        "exp_run_diff_ms": _delta(current_exp_run_ms, baseline.exp_run_ms),
        "exp_deaths_diff": _delta(current_exp_deaths, baseline.exp_deaths),
        "practice_saved_ms": saved,
    }

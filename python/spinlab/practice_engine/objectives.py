"""Objective functions for the practice simulation engine.

Each objective is a pure function (T, masks, ctx) -> float | None.
Returns None when the gate fails (e.g. zero finished rollouts). Never silently
fall back to a default — None is the honest answer.

Sign convention: objectives return the raw value in their natural units. The
engine's per_segment_values returns baseline-minus-swap, signed; the UI inverts
color per objective direction (e.g. for q, "value < 0" means practice helped).
"""
from __future__ import annotations

import numpy as np

from spinlab.practice_engine.types import ResetMasks


def expected_wall_clock_per_attempt(T: np.ndarray, masks: ResetMasks, ctx: dict) -> float | None:
    """Mean wall-clock per attempt (aborted + finished both contribute their partials)."""
    if masks.wall_ms.size == 0:
        return None
    return float(masks.wall_ms.mean())


def expected_total_finished_time(T: np.ndarray, masks: ResetMasks, ctx: dict) -> float | None:
    """Mean total time across rollouts that FINISHED."""
    if not masks.finished.any():
        return None
    return float(masks.wall_ms[masks.finished].mean())


def q(T: np.ndarray, masks: ResetMasks, ctx: dict) -> float | None:
    """Fraction of rollouts that finished under ctx['target_ms']."""
    target = ctx["target_ms"]
    if masks.finished.size == 0:
        return None
    under = masks.finished & (masks.wall_ms <= target)
    return float(under.mean())


def quantile(T: np.ndarray, masks: ResetMasks, ctx: dict) -> float | None:
    """p-th quantile of FINISHED total times. ctx['p'] in (0, 1)."""
    p = ctx["p"]
    finished_times = masks.wall_ms[masks.finished]
    if finished_times.size == 0:
        return None
    return float(np.quantile(finished_times, p))


def p_pb_this_session(T: np.ndarray, masks: ResetMasks, ctx: dict) -> float | None:
    """1 − (1 − q)^(H/τ̄). ctx: target_ms, session_remaining_ms."""
    q_val = q(T, masks, ctx)
    tau_bar = expected_wall_clock_per_attempt(T, masks, ctx)
    if q_val is None or tau_bar is None or tau_bar <= 0:
        return None
    H = ctx["session_remaining_ms"]
    attempts_remaining = H / tau_bar
    return float(1.0 - (1.0 - q_val) ** attempts_remaining)

"""Profile the Monte Carlo rollout engine in isolation.

Reproduces what the /evaluate route does (engine.evaluate +
engine.per_segment_values + engine.total_time_distribution) against a
synthetic fleet of K=20 gated SamplerStates and N=10000 rollouts, then
prints the top hotspots from cProfile.

Run from the repo root:
    python scripts/profile_mc_engine.py

No DB, no HTTP, no scheduler — just the practice_engine kernels.
"""
from __future__ import annotations

import cProfile
import io
import pstats
import random
import time
from datetime import UTC, datetime

import numpy as np

from spinlab.estimators.em_suite_sampler import SamplerState, process_event
from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt
from spinlab.practice_engine import PracticeEngine
from spinlab.practice_engine import objectives, reset_policies
from spinlab.practice_engine.threshold_sources import thresholds_from_user

K_SEGMENTS = 20
N_ROLLOUTS = 10_000
EVENTS_PER_SEGMENT = 60
SUCCESS_TIME_BASE_MS = 4000
SUCCESS_TIME_JITTER_MS = 200
DEATH_TIME_BASE_MS = 1500
DEATH_TIME_JITTER_MS = 50
DEATH_FRACTION = 1 / 3
TOP_N_HOTSPOTS = 30


def _gated_state(seed: int) -> SamplerState:
    state = SamplerState(n_completed=0, n_attempts=0)
    rng = random.Random(seed)
    for i in range(EVENTS_PER_SEGMENT):
        died = (i % int(1 / DEATH_FRACTION)) == 0
        outcome = AttemptOutcome.DIED if died else AttemptOutcome.SURVIVED
        if died:
            t_ms = DEATH_TIME_BASE_MS + rng.randint(
                -DEATH_TIME_JITTER_MS, DEATH_TIME_JITTER_MS,
            )
        else:
            t_ms = SUCCESS_TIME_BASE_MS + rng.randint(
                -SUCCESS_TIME_JITTER_MS, SUCCESS_TIME_JITTER_MS,
            )
        state = process_event(state, EventAttempt(
            segment_id=f"s{seed}", session_id="profile",
            episode_id=f"e{i}",
            outcome=outcome, time_ms=t_ms,
            source=AttemptSource.PRACTICE,
            created_at=datetime.now(UTC),
        ))
    return state


def build_states() -> dict[str, SamplerState]:
    return {f"s{i}": _gated_state(seed=i) for i in range(K_SEGMENTS)}


def run_route_workload(engine: PracticeEngine) -> None:
    """One simulated /evaluate hit: target_paced + expected_total_finished_time."""
    seg_ids = engine.matrix.seg_ids
    cum_splits_ms = {
        s: int((i + 1) * (SUCCESS_TIME_BASE_MS * 1.15))
        for i, s in enumerate(seg_ids)
    }
    threshold_cum_ms = thresholds_from_user(seg_ids=seg_ids, cum_splits_ms=cum_splits_ms)
    threshold_kwargs = {"threshold_cum_ms": threshold_cum_ms, "slack": 0.0}
    policy = reset_policies.target_paced
    objective = objectives.expected_total_finished_time
    ctx: dict = {}

    engine.evaluate(policy, threshold_kwargs, objective, ctx)
    engine.per_segment_values(policy, threshold_kwargs, objective, ctx)
    engine.total_time_distribution(policy, threshold_kwargs)


def main() -> None:
    print(f"Building {K_SEGMENTS} synthetic SamplerStates (each from "
          f"{EVENTS_PER_SEGMENT} events)...")
    states = build_states()
    n_gated = sum(1 for st in states.values()
                  if st.n_successes >= 2 and st.n_deaths >= 2)
    print(f"  {n_gated}/{K_SEGMENTS} gated.")

    print(f"\nConstructing PracticeEngine(N={N_ROLLOUTS})...")
    t0 = time.perf_counter()
    engine = PracticeEngine(sampler_states=states, N=N_ROLLOUTS, rng_seed=42)
    engine.matrix.ensure_fresh()  # force the first full build
    build_ms = (time.perf_counter() - t0) * 1000
    print(f"  initial ensure_fresh() built {engine.matrix.T.shape} matrix in "
          f"{build_ms:.0f} ms ({build_ms / N_ROLLOUTS / K_SEGMENTS * 1e6:.1f} ns/cell)")

    print("\nWarming up route workload once (uncounted)...")
    run_route_workload(engine)

    print("\nProfiling one full route hit (evaluate + per_segment_values + "
          "total_time_distribution)...")
    profiler = cProfile.Profile()
    t0 = time.perf_counter()
    profiler.enable()
    run_route_workload(engine)
    profiler.disable()
    eval_ms = (time.perf_counter() - t0) * 1000
    print(f"  wall clock: {eval_ms:.0f} ms")

    print("\n--- Top hotspots (cumulative time, top "
          f"{TOP_N_HOTSPOTS}) ---")
    buf = io.StringIO()
    stats = pstats.Stats(profiler, stream=buf).sort_stats("cumulative")
    stats.print_stats(TOP_N_HOTSPOTS)
    print(buf.getvalue())

    print("\n--- Top hotspots (total time, top "
          f"{TOP_N_HOTSPOTS}) ---")
    buf = io.StringIO()
    stats = pstats.Stats(profiler, stream=buf).sort_stats("tottime")
    stats.print_stats(TOP_N_HOTSPOTS)
    print(buf.getvalue())


if __name__ == "__main__":
    main()

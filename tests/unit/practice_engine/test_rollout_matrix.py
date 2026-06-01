"""Tests for RolloutMatrix."""
from __future__ import annotations

import random
from datetime import UTC, datetime

import numpy as np
import pytest

from spinlab.estimators.em_suite_sampler import SamplerState, process_event
from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt
from spinlab.practice_engine.rollout_matrix import RolloutMatrix


def _gated_state(seed: int = 0, n_events: int = 60) -> SamplerState:
    """Build a synthetic SamplerState past the (>=2 successes, >=2 deaths) gate."""
    state = SamplerState(n_completed=0, n_attempts=0)
    rng = random.Random(seed)
    for i in range(n_events):
        outcome = AttemptOutcome.SURVIVED if i % 3 != 0 else AttemptOutcome.DIED
        t_ms = 4000 + rng.randint(-200, 200) if outcome == AttemptOutcome.SURVIVED else 1500 + rng.randint(-50, 50)
        state = process_event(state, EventAttempt(
            segment_id="x", session_id="s", episode_id=f"e{i}",
            outcome=outcome, time_ms=t_ms,
            source=AttemptSource.PRACTICE,
            created_at=datetime.now(UTC),
        ))
    return state


class TestRolloutMatrixBuild:
    def test_shape_matches_states(self):
        states = {"s1": _gated_state(0), "s2": _gated_state(1)}
        m = RolloutMatrix(sampler_states=states, N=500, rng_seed=42)
        m.ensure_fresh()
        assert m.T.shape == (500, 2)
        assert set(m.seg_ids) == {"s1", "s2"}
        assert m.dirty == set()

    def test_cost_ms_populated(self):
        states = {"s1": _gated_state(0)}
        m = RolloutMatrix(sampler_states=states, N=500, rng_seed=42)
        m.ensure_fresh()
        assert m.cost_ms.shape == (1,)
        assert m.cost_ms[0] > 0

    def test_initial_state_is_dirty(self):
        states = {"s1": _gated_state(0)}
        m = RolloutMatrix(sampler_states=states, N=100, rng_seed=42)
        assert m.dirty == {"s1"}

    def test_reproducibility_same_seed(self):
        states = {"s1": _gated_state(0)}
        m1 = RolloutMatrix(sampler_states=states, N=200, rng_seed=42)
        m1.ensure_fresh()
        m2 = RolloutMatrix(sampler_states=states, N=200, rng_seed=42)
        m2.ensure_fresh()
        assert np.array_equal(m1.T, m2.T)

    def test_different_seed_different_draws(self):
        states = {"s1": _gated_state(0)}
        m1 = RolloutMatrix(sampler_states=states, N=200, rng_seed=42)
        m1.ensure_fresh()
        m2 = RolloutMatrix(sampler_states=states, N=200, rng_seed=43)
        m2.ensure_fresh()
        assert not np.array_equal(m1.T, m2.T)

    def test_ungated_segment_excluded(self):
        bare = SamplerState(n_completed=0, n_attempts=0)
        states = {"s1": _gated_state(0), "s2_bare": bare}
        m = RolloutMatrix(sampler_states=states, N=100, rng_seed=42)
        m.ensure_fresh()
        assert m.seg_ids == ["s1"]
        assert m.T.shape == (100, 1)


class TestRolloutMatrixInvalidation:
    def test_invalidate_marks_dirty(self):
        states = {"s1": _gated_state(0), "s2": _gated_state(1)}
        m = RolloutMatrix(sampler_states=states, N=100, rng_seed=42)
        m.ensure_fresh()
        m.invalidate("s1")
        assert m.dirty == {"s1"}

    def test_ensure_fresh_only_rebuilds_dirty(self):
        states = {"s1": _gated_state(0), "s2": _gated_state(1)}
        m = RolloutMatrix(sampler_states=states, N=100, rng_seed=42)
        m.ensure_fresh()
        T_before = m.T.copy()
        # Track column indices BEFORE the mutation so we can compare them after.
        s1_idx_before = m.seg_ids.index("s1")
        s2_idx_before = m.seg_ids.index("s2")
        # Mutate s1's state and invalidate ONLY s1:
        states["s1"] = _gated_state(seed=999, n_events=80)
        m.invalidate("s1")
        m.ensure_fresh()
        s1_idx_after = m.seg_ids.index("s1")
        s2_idx_after = m.seg_ids.index("s2")
        # Column for s1 should change; column for s2 should be identical.
        assert not np.array_equal(m.T[:, s1_idx_after], T_before[:, s1_idx_before])
        assert np.array_equal(m.T[:, s2_idx_after], T_before[:, s2_idx_before])

    def test_invalidate_unknown_segment_is_noop(self):
        states = {"s1": _gated_state(0)}
        m = RolloutMatrix(sampler_states=states, N=100, rng_seed=42)
        m.ensure_fresh()
        m.invalidate("does_not_exist")
        m.ensure_fresh()
        assert m.seg_ids == ["s1"]

    def test_newly_gated_segment_added_on_refresh(self):
        bare = SamplerState(n_completed=0, n_attempts=0)
        states = {"s1": _gated_state(0), "s2": bare}
        m = RolloutMatrix(sampler_states=states, N=100, rng_seed=42)
        m.ensure_fresh()
        assert m.seg_ids == ["s1"]
        # Now s2 gates:
        states["s2"] = _gated_state(7)
        m.invalidate("s2")
        m.ensure_fresh()
        assert sorted(m.seg_ids) == ["s1", "s2"]
        assert m.T.shape == (100, 2)


class TestRolloutMatrixSwapColumn:
    def test_draw_column_returns_correct_shape(self):
        states = {"s1": _gated_state(0)}
        m = RolloutMatrix(sampler_states=states, N=100, rng_seed=42)
        m.ensure_fresh()
        swap = m.draw_column("s1", k_param=1)
        assert swap.shape == (100,)
        assert np.all(swap >= 0)

    def test_draw_column_unknown_segment_raises(self):
        states = {"s1": _gated_state(0)}
        m = RolloutMatrix(sampler_states=states, N=100, rng_seed=42)
        m.ensure_fresh()
        with pytest.raises(KeyError):
            m.draw_column("does_not_exist", k_param=1)

"""Tests for the Death-Aware Rolling estimator."""
import json
import pytest


class TestRegistration:
    def test_registered_in_registry(self):
        from spinlab.estimators import list_estimators, get_estimator
        assert "death_aware_rolling" in list_estimators()
        est = get_estimator("death_aware_rolling")
        assert est.name == "death_aware_rolling"
        assert est.display_name == "Death-Aware Rolling"

    def test_declared_params_has_halflife(self):
        from spinlab.estimators import get_estimator
        est = get_estimator("death_aware_rolling")
        names = {p.name for p in est.declared_params()}
        assert "halflife" in names
        halflife_param = next(p for p in est.declared_params() if p.name == "halflife")
        assert halflife_param.default == 20.0
        assert halflife_param.min_val == 1.0
        assert halflife_param.max_val == 200.0


class TestEpisodeGrouping:
    def test_groups_events_by_episode_id(self):
        from spinlab.estimators.death_aware_rolling import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=7500),
        ]
        episodes = _group_into_episodes(events)
        assert len(episodes) == 2
        ep1, ep2 = episodes
        assert ep1.episode_id == "ep1"
        assert len(ep1.events) == 2
        assert ep1.outcome == "completed"
        assert ep1.had_any_death is True
        assert ep2.episode_id == "ep2"
        assert ep2.outcome == "completed"
        assert ep2.had_any_death is False

    def test_aborted_episode_outcome_is_died(self):
        from spinlab.estimators.death_aware_rolling import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2500),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2000),
        ]
        episodes = _group_into_episodes(events)
        assert len(episodes) == 1
        assert episodes[0].outcome == "died"
        assert episodes[0].had_any_death is True

    def test_invalidated_event_drops_whole_episode(self):
        from spinlab.estimators.death_aware_rolling import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000, invalidated=True),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=7500),
        ]
        episodes = _group_into_episodes(events)
        assert len(episodes) == 1
        assert episodes[0].episode_id == "ep2"

    def test_chronological_order_preserved(self):
        """Episodes appear in the order their FIRST event occurred."""
        from spinlab.estimators.death_aware_rolling import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="old", outcome="survived", time_ms=8000),
            make_event_attempt(episode_id="middle", outcome="survived", time_ms=7500),
            make_event_attempt(episode_id="new", outcome="survived", time_ms=7000),
        ]
        episodes = _group_into_episodes(events)
        assert [ep.episode_id for ep in episodes] == ["old", "middle", "new"]


class TestWeighting:
    def test_most_recent_episode_has_weight_one(self):
        from spinlab.estimators.death_aware_rolling import _compute_weights
        weights = _compute_weights(n_episodes=10, halflife=5)
        assert weights[-1] == pytest.approx(1.0)

    def test_halflife_ago_has_weight_half(self):
        from spinlab.estimators.death_aware_rolling import _compute_weights
        weights = _compute_weights(n_episodes=10, halflife=5)
        # Index 4 is 5 episodes back from index 9 (the most-recent).
        assert weights[4] == pytest.approx(0.5)

    def test_five_halflives_ago_has_weight_small(self):
        from spinlab.estimators.death_aware_rolling import _compute_weights
        weights = _compute_weights(n_episodes=100, halflife=10)
        # Index 49 is 50 episodes (5 halflives) back from index 99.
        # Weight = 2^-5 ≈ 0.031.
        assert weights[49] < 0.05


class TestEmptyEvents:
    def test_empty_events_returns_none_output(self):
        from spinlab.estimators import get_estimator
        est = get_estimator("death_aware_rolling")
        from tests.factories import make_attempt_record
        a = make_attempt_record(10000, True, clean_tail_ms=10000)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=[])
        assert out.total.expected_ms is None
        assert out.total.ms_per_attempt is None
        assert out.total.floor_ms is None
        assert out.clean.expected_ms is None
        assert out.extras is None


class TestLifeLevelAggregates:
    def test_p_die_per_life_pure_deaths(self):
        from spinlab.estimators.death_aware_rolling import _compute_aggregates
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep2", outcome="died", time_ms=3500),
        ]
        # Use a large halflife so the two episodes have nearly equal weight and
        # the weighted mean is close to the arithmetic midpoint (3250).
        # halflife=20 would produce ~3254 because the older episode is slightly
        # down-weighted (2^(-1/20) ≈ 0.966).
        agg = _compute_aggregates(events, halflife=1000)
        assert agg.p_die_per_life == pytest.approx(1.0)
        assert agg.expected_completion_time_ms is None
        assert agg.expected_death_time_ms == pytest.approx(3250.0, abs=1.0)

    def test_p_die_per_life_pure_completions(self):
        from spinlab.estimators.death_aware_rolling import _compute_aggregates
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=7000),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=8000),
        ]
        # Use a large halflife so the two episodes have nearly equal weight and
        # the weighted mean is close to the arithmetic midpoint (7500).
        # halflife=20 would produce ~7509 because the older episode is slightly
        # down-weighted (2^(-1/20) ≈ 0.966).
        agg = _compute_aggregates(events, halflife=1000)
        assert agg.p_die_per_life == pytest.approx(0.0)
        assert agg.expected_death_time_ms is None
        assert agg.expected_completion_time_ms == pytest.approx(7500.0, abs=1.0)

    def test_p_die_per_life_mixed(self):
        """3 lives died, 1 life survived → p_die_per_life = 0.75 (long halflife)."""
        from spinlab.estimators.death_aware_rolling import _compute_aggregates
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2000),
            make_event_attempt(episode_id="ep2", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep3", outcome="died", time_ms=4000),
            make_event_attempt(episode_id="ep4", outcome="survived", time_ms=7000),
        ]
        agg = _compute_aggregates(events, halflife=100)
        assert agg.p_die_per_life == pytest.approx(0.75, abs=0.01)
        assert agg.expected_death_time_ms == pytest.approx(3000.0, abs=10)
        assert agg.expected_completion_time_ms == pytest.approx(7000.0, abs=10)

    def test_multi_death_episode_counts_each_life(self):
        """Episode [died, died, survived] contributes 2 death samples and 1 completion."""
        from spinlab.estimators.death_aware_rolling import _compute_aggregates
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2500),
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=7000),
        ]
        agg = _compute_aggregates(events, halflife=20)
        assert len(agg.death_samples) == 2
        assert len(agg.completion_samples) == 1
        # All three lives share the same episode weight, so p_die_per_life = 2/3.
        assert agg.p_die_per_life == pytest.approx(2 / 3, abs=0.01)

    def test_aborted_episode_contributes_deaths_only(self):
        """[died, died, died] gives 3 death samples and zero completion samples."""
        from spinlab.estimators.death_aware_rolling import _compute_aggregates
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2000),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2500),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
        ]
        agg = _compute_aggregates(events, halflife=20)
        assert len(agg.death_samples) == 3
        assert agg.completion_samples == []
        assert agg.p_die_per_life == pytest.approx(1.0)
        assert agg.expected_completion_time_ms is None


class TestEpisodeLevelAggregates:
    def test_p_die_per_attempt_clean_runs(self):
        from spinlab.estimators.death_aware_rolling import _compute_aggregates
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id=f"ep{i}", outcome="survived", time_ms=7000)
            for i in range(5)
        ]
        agg = _compute_aggregates(events, halflife=20)
        assert agg.p_die_per_attempt == pytest.approx(0.0)
        assert agg.n_attempts_effective == pytest.approx(agg.n_episodes_completed_eff)
        assert agg.n_episodes_with_death_eff == pytest.approx(0.0)

    def test_p_die_per_attempt_multi_death_then_survive_counts_as_death(self):
        """An episode with deaths-and-completion counts toward
        n_episodes_with_death_eff AND n_episodes_completed_eff —
        their sum can exceed n_attempts_effective."""
        from spinlab.estimators.death_aware_rolling import _compute_aggregates
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=7000),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=6500),
        ]
        agg = _compute_aggregates(events, halflife=100)
        # 2 attempts; 1 has a death; both complete.
        assert agg.n_attempts_effective == pytest.approx(2.0, abs=0.01)
        assert agg.n_episodes_with_death_eff == pytest.approx(1.0, abs=0.01)
        assert agg.n_episodes_completed_eff == pytest.approx(2.0, abs=0.01)
        assert agg.p_die_per_attempt == pytest.approx(0.5, abs=0.01)


class TestGeometricFormula:
    def test_zero_p_die_returns_completion_time(self):
        from spinlab.estimators.death_aware_rolling import _expected_total_ms
        result = _expected_total_ms(
            p_die_per_life=0.0,
            e_death_time_ms=None,
            e_completion_time_ms=8000.0,
            respawn_penalty_ms=3200,
        )
        assert result == pytest.approx(8000.0)

    def test_half_p_die_means_one_extra_death_life(self):
        """p_die_per_life=0.5 → E[death lives] = 1, so total = (death+penalty) + completion."""
        from spinlab.estimators.death_aware_rolling import _expected_total_ms
        result = _expected_total_ms(
            p_die_per_life=0.5,
            e_death_time_ms=3000.0,
            e_completion_time_ms=8000.0,
            respawn_penalty_ms=3200,
        )
        # (0.5 / 0.5) * (3000 + 3200) + 8000 = 6200 + 8000 = 14200
        assert result == pytest.approx(14200.0)

    def test_eighty_percent_p_die_means_four_death_lives(self):
        from spinlab.estimators.death_aware_rolling import _expected_total_ms
        result = _expected_total_ms(
            p_die_per_life=0.8,
            e_death_time_ms=3000.0,
            e_completion_time_ms=8000.0,
            respawn_penalty_ms=3200,
        )
        # (0.8 / 0.2) * (3000 + 3200) + 8000 = 4 * 6200 + 8000 = 32800
        assert result == pytest.approx(32800.0)

    def test_p_die_one_returns_none(self):
        """No completions observed → cannot project an expected attempt time."""
        from spinlab.estimators.death_aware_rolling import _expected_total_ms
        result = _expected_total_ms(
            p_die_per_life=1.0,
            e_death_time_ms=3000.0,
            e_completion_time_ms=None,
            respawn_penalty_ms=3200,
        )
        assert result is None

    def test_none_p_die_returns_none(self):
        from spinlab.estimators.death_aware_rolling import _expected_total_ms
        result = _expected_total_ms(
            p_die_per_life=None,
            e_death_time_ms=None,
            e_completion_time_ms=None,
            respawn_penalty_ms=3200,
        )
        assert result is None

    def test_no_completion_time_returns_none(self):
        from spinlab.estimators.death_aware_rolling import _expected_total_ms
        result = _expected_total_ms(
            p_die_per_life=0.3,
            e_death_time_ms=3000.0,
            e_completion_time_ms=None,
            respawn_penalty_ms=3200,
        )
        assert result is None

    def test_no_death_time_with_nonzero_p_die_returns_none(self):
        """If p_die > 0 but we have no death-time samples, can't compute total."""
        from spinlab.estimators.death_aware_rolling import _expected_total_ms
        result = _expected_total_ms(
            p_die_per_life=0.3,
            e_death_time_ms=None,
            e_completion_time_ms=8000.0,
            respawn_penalty_ms=3200,
        )
        assert result is None


class TestModelOutputWiring:
    def _events_one_episode_one_completion(self):
        from tests.factories import make_event_attempt
        return [make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000)]

    def _events_mixed_aborted_and_completed(self):
        from tests.factories import make_event_attempt
        return [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2000),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2500),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=8000),
            make_event_attempt(episode_id="ep3", outcome="died", time_ms=2200),
            make_event_attempt(episode_id="ep3", outcome="survived", time_ms=7500),
        ]

    def test_single_completion_no_deaths(self):
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record
        est = get_estimator("death_aware_rolling")
        a = make_attempt_record(8000, True, clean_tail_ms=8000)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=self._events_one_episode_one_completion())
        assert out.total.expected_ms == pytest.approx(8000.0)
        assert out.clean.expected_ms == pytest.approx(8000.0)
        assert out.total.floor_ms == pytest.approx(8000.0)
        assert out.clean.floor_ms == pytest.approx(8000.0)
        assert out.extras is not None
        assert out.extras.p_die_per_life == pytest.approx(0.0)
        assert out.extras.death_samples == []
        assert out.extras.completion_samples == [(8000, pytest.approx(1.0))]

    def test_geometric_total_uses_aborted_episodes(self):
        """The 3 deaths in ep1 (aborted) push p_die_per_life up;
        total.expected_ms should be larger than the mean of completed-episode
        totals would suggest."""
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record
        est = get_estimator("death_aware_rolling")
        a = make_attempt_record(8000, True, clean_tail_ms=8000)
        state = est.init_state(a, priors={})
        # halflife=100 makes weights ≈ uniform across the small dataset so the
        # hand-computed numbers below match. The test is about the geometric
        # formula's response to aborted episodes, not about decay weighting.
        out = est.model_output(state, [a], events=self._events_mixed_aborted_and_completed(), params={"halflife": 100})
        assert out.extras is not None
        # 4 died lives + 2 completed lives → p_die_per_life ≈ 4/6 ≈ 0.667
        assert out.extras.p_die_per_life == pytest.approx(4 / 6, abs=0.01)
        # E[death] = approx mean(2000, 2500, 3000, 2200) = 2425
        assert out.extras.expected_death_time_ms == pytest.approx(2425.0, abs=20)
        # E[completion] = approx mean(8000, 7500) = 7750
        assert out.extras.expected_completion_time_ms == pytest.approx(7750.0, abs=20)
        # Expected total: (4/2) * (2425 + 3200) + 7750 = 2 * 5625 + 7750 = 19000
        assert out.total.expected_ms == pytest.approx(19000.0, abs=100)
        # Sanity: geometric total is meaningfully larger than the mean
        # of completed-episode totals (which would naively ignore aborted deaths).
        assert out.total.expected_ms > 10450.0

    def test_floor_ms_is_min_across_all_completed_episodes(self):
        """floor_ms = best episode_total observed, regardless of decay window."""
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record, make_event_attempt
        events = (
            # Old great clean episode (best ever).
            [make_event_attempt(episode_id="old_great", outcome="survived", time_ms=5000)]
            # Plus 50 newer mediocre clean episodes at 9000ms.
            + [
                make_event_attempt(episode_id=f"new{i}", outcome="survived", time_ms=9000)
                for i in range(50)
            ]
        )
        est = get_estimator("death_aware_rolling")
        a = make_attempt_record(9000, True, clean_tail_ms=9000)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=events, params={"halflife": 5})
        assert out.total.floor_ms == pytest.approx(5000.0)
        assert out.clean.floor_ms == pytest.approx(5000.0)

    def test_single_completion_ms_per_attempt_none(self):
        """One data point ⇒ no slope."""
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record
        est = get_estimator("death_aware_rolling")
        a = make_attempt_record(8000, True, clean_tail_ms=8000)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=self._events_one_episode_one_completion())
        assert out.total.ms_per_attempt is None
        assert out.clean.ms_per_attempt is None

    def test_improving_completion_times_positive_ms_per_attempt(self):
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record, make_event_attempt
        events = [
            make_event_attempt(episode_id=f"ep{i}", outcome="survived", time_ms=t)
            for i, t in enumerate([12000, 11500, 11000, 10500, 10000, 9500, 9000, 8500])
        ]
        est = get_estimator("death_aware_rolling")
        a = make_attempt_record(8500, True, clean_tail_ms=8500)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=events)
        # Positive = improving (earlier slower than later).
        assert out.total.ms_per_attempt is not None
        assert out.total.ms_per_attempt > 0
        assert out.clean.ms_per_attempt is not None
        assert out.clean.ms_per_attempt > 0

    def test_halflife_param_applied(self):
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record, make_event_attempt
        events = [
            # 5 old episodes at 5000ms.
            *[make_event_attempt(episode_id=f"old{i}", outcome="survived", time_ms=5000) for i in range(5)],
            # 3 recent episodes at 10000ms.
            *[make_event_attempt(episode_id=f"new{i}", outcome="survived", time_ms=10000) for i in range(3)],
        ]
        est = get_estimator("death_aware_rolling")
        a = make_attempt_record(10000, True, clean_tail_ms=10000)
        state = est.init_state(a, priors={})
        # Short halflife should give heavier weight to the recent 10000s.
        out_short = est.model_output(state, [a], events=events, params={"halflife": 1})
        # Long halflife mixes old and new more evenly.
        out_long = est.model_output(state, [a], events=events, params={"halflife": 100})
        assert out_short.extras is not None and out_long.extras is not None
        assert out_short.extras.expected_completion_time_ms is not None
        assert out_long.extras.expected_completion_time_ms is not None
        assert out_short.extras.expected_completion_time_ms > out_long.extras.expected_completion_time_ms

    def test_halflife_out_of_bounds_raises(self):
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record, make_event_attempt
        est = get_estimator("death_aware_rolling")
        a = make_attempt_record(8000, True, clean_tail_ms=8000)
        state = est.init_state(a, priors={})
        events = [make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000)]
        with pytest.raises(ValueError, match="halflife"):
            est.model_output(state, [a], events=events, params={"halflife": 0})
        with pytest.raises(ValueError, match="halflife"):
            est.model_output(state, [a], events=events, params={"halflife": 500})

    def test_all_deaths_no_completions_total_is_none(self):
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record, make_event_attempt
        est = get_estimator("death_aware_rolling")
        a = make_attempt_record(0, False, clean_tail_ms=None)
        state = est.init_state(a, priors={})
        events = [
            make_event_attempt(episode_id=f"ep{i}", outcome="died", time_ms=3000)
            for i in range(5)
        ]
        out = est.model_output(state, [a], events=events)
        assert out.total.expected_ms is None
        assert out.total.floor_ms is None
        assert out.clean.expected_ms is None
        assert out.clean.floor_ms is None
        assert out.extras is not None
        assert out.extras.p_die_per_life == pytest.approx(1.0)
        assert len(out.extras.death_samples) == 5
        assert out.extras.completion_samples == []


class TestStateRoundTrip:
    def test_state_serializes_minimally(self):
        from spinlab.estimators.death_aware_rolling import DeathAwareRollingState
        s = DeathAwareRollingState(n_completed=7, n_attempts=12)
        d = s.to_dict()
        assert d == {"n_completed": 7, "n_attempts": 12}
        s2 = DeathAwareRollingState.from_dict(d)
        assert s2.n_completed == 7
        assert s2.n_attempts == 12

    def test_state_deserialize_via_registry(self):
        from spinlab.estimators import EstimatorState
        state_json = json.dumps({"n_completed": 4, "n_attempts": 6})
        s = EstimatorState.deserialize("death_aware_rolling", state_json)
        assert s.n_completed == 4
        assert s.n_attempts == 6

    def test_rebuild_state_from_attempts(self):
        from spinlab.estimators import get_estimator
        from tests.factories import make_attempt_record, make_incomplete
        est = get_estimator("death_aware_rolling")
        attempts = [
            make_attempt_record(12000, True, clean_tail_ms=12000),
            make_incomplete(),
            make_attempt_record(11000, True, clean_tail_ms=11000),
        ]
        state = est.rebuild_state(attempts)
        assert state.n_completed == 2
        assert state.n_attempts == 3

    def test_rebuild_state_empty(self):
        from spinlab.estimators import get_estimator
        est = get_estimator("death_aware_rolling")
        state = est.rebuild_state([])
        assert state.n_completed == 0
        assert state.n_attempts == 0

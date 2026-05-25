"""Tests for the Death-Aware Rolling estimator."""
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

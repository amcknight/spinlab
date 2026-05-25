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

"""Tests for the Bootstrap-Resample estimator."""
import pytest


class TestRegistration:
    def test_registered_in_registry(self):
        from spinlab.estimators import list_estimators, get_estimator
        assert "bootstrap_resample" in list_estimators()
        est = get_estimator("bootstrap_resample")
        assert est.name == "bootstrap_resample"
        assert est.display_name == "Bootstrap (Monte Carlo)"

    def test_declared_params_has_n_samples(self):
        from spinlab.estimators import get_estimator
        est = get_estimator("bootstrap_resample")
        names = {p.name for p in est.declared_params()}
        assert "n_samples" in names
        n_samples = next(p for p in est.declared_params() if p.name == "n_samples")
        # Default in the middle of [100, 10000].
        assert n_samples.default == 1000.0
        assert n_samples.min_val == 100.0
        assert n_samples.max_val == 10000.0

    def test_declared_params_has_halflife(self):
        """Bootstrap reuses the decayed sampling-weight machinery; same knob."""
        from spinlab.estimators import get_estimator
        est = get_estimator("bootstrap_resample")
        names = {p.name for p in est.declared_params()}
        assert "halflife" in names


class TestColdFilter:
    def test_all_cold_episodes_pass_through(self):
        from spinlab.estimators.bootstrap_resample import _filter_to_cold_episodes
        from spinlab.estimators._episode_helpers import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000, is_hot=False),
            make_event_attempt(episode_id="ep2", outcome="died", time_ms=3000, is_hot=False),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=7000, is_hot=False),
        ]
        episodes = _group_into_episodes(events)
        cold = _filter_to_cold_episodes(episodes)
        assert [ep.episode_id for ep in cold] == ["ep1", "ep2"]

    def test_episode_with_any_hot_life_dropped(self):
        """Even one hot event in an episode disqualifies the whole episode.

        Half-counting a mixed-state episode would muddle the cold sample
        pool; cleanest rule is all-or-nothing per episode.
        """
        from spinlab.estimators.bootstrap_resample import _filter_to_cold_episodes
        from spinlab.estimators._episode_helpers import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="cold", outcome="survived", time_ms=8000, is_hot=False),
            make_event_attempt(episode_id="mixed", outcome="died", time_ms=3000, is_hot=False),
            make_event_attempt(episode_id="mixed", outcome="survived", time_ms=7000, is_hot=True),
        ]
        episodes = _group_into_episodes(events)
        cold = _filter_to_cold_episodes(episodes)
        assert [ep.episode_id for ep in cold] == ["cold"]

    def test_all_hot_returns_empty(self):
        from spinlab.estimators.bootstrap_resample import _filter_to_cold_episodes
        from spinlab.estimators._episode_helpers import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000, is_hot=True),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=7500, is_hot=True),
        ]
        episodes = _group_into_episodes(events)
        cold = _filter_to_cold_episodes(episodes)
        assert cold == []

"""Tests for the shared episode-helpers module."""
import pytest


class TestModuleExports:
    def test_exports_episode_dataclass(self):
        from spinlab.estimators._episode_helpers import _Episode
        assert _Episode.__dataclass_fields__.keys() == {
            "episode_id", "events", "outcome", "had_any_death"
        }

    def test_exports_group_into_episodes(self):
        from spinlab.estimators._episode_helpers import _group_into_episodes
        assert callable(_group_into_episodes)

    def test_exports_compute_weights(self):
        from spinlab.estimators._episode_helpers import _compute_weights
        assert callable(_compute_weights)


class TestGroupBehaviorMatchesLegacy:
    """Sanity check: the moved function behaves the same as before."""
    def test_groups_by_episode_id_preserves_chronological_order(self):
        from spinlab.estimators._episode_helpers import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="old", outcome="survived", time_ms=8000),
            make_event_attempt(episode_id="new", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="new", outcome="survived", time_ms=7000),
        ]
        episodes = _group_into_episodes(events)
        assert [ep.episode_id for ep in episodes] == ["old", "new"]
        assert episodes[1].outcome == "completed"
        assert episodes[1].had_any_death is True

    def test_invalidated_event_drops_whole_episode(self):
        from spinlab.estimators._episode_helpers import _group_into_episodes
        from tests.factories import make_event_attempt
        events = [
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000, invalidated=True),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=7500),
        ]
        episodes = _group_into_episodes(events)
        assert [ep.episode_id for ep in episodes] == ["ep2"]


class TestWeightsBehaviorMatchesLegacy:
    def test_most_recent_episode_has_weight_one(self):
        from spinlab.estimators._episode_helpers import _compute_weights
        weights = _compute_weights(n_episodes=10, halflife=5)
        assert weights[-1] == pytest.approx(1.0)

    def test_halflife_ago_has_weight_half(self):
        from spinlab.estimators._episode_helpers import _compute_weights
        weights = _compute_weights(n_episodes=10, halflife=5)
        assert weights[4] == pytest.approx(0.5)

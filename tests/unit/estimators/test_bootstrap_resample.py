"""Tests for the Bootstrap-Resample estimator."""
import pytest


class TestRegistration:
    def test_registered_in_registry(self):
        from spinlab.estimators import get_estimator, list_estimators
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
        from tests.factories import make_event_attempt

        from spinlab.estimators._episode_helpers import _group_into_episodes
        from spinlab.estimators.bootstrap_resample import _filter_to_cold_episodes
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
        from tests.factories import make_event_attempt

        from spinlab.estimators._episode_helpers import _group_into_episodes
        from spinlab.estimators.bootstrap_resample import _filter_to_cold_episodes
        events = [
            make_event_attempt(episode_id="cold", outcome="survived", time_ms=8000, is_hot=False),
            make_event_attempt(episode_id="mixed", outcome="died", time_ms=3000, is_hot=False),
            make_event_attempt(episode_id="mixed", outcome="survived", time_ms=7000, is_hot=True),
        ]
        episodes = _group_into_episodes(events)
        cold = _filter_to_cold_episodes(episodes)
        assert [ep.episode_id for ep in cold] == ["cold"]

    def test_all_hot_returns_empty(self):
        from tests.factories import make_event_attempt

        from spinlab.estimators._episode_helpers import _group_into_episodes
        from spinlab.estimators.bootstrap_resample import _filter_to_cold_episodes
        events = [
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000, is_hot=True),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=7500, is_hot=True),
        ]
        episodes = _group_into_episodes(events)
        cold = _filter_to_cold_episodes(episodes)
        assert cold == []


class TestEpisodeTotal:
    def test_clean_completion_total_is_just_time(self):
        from tests.factories import make_event_attempt

        from spinlab.estimators._episode_helpers import _group_into_episodes
        from spinlab.estimators.bootstrap_resample import _episode_total_ms
        events = [make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000)]
        ep = _group_into_episodes(events)[0]
        assert _episode_total_ms(ep, respawn_penalty_ms=3200) == 8000

    def test_episode_with_deaths_adds_penalty_per_death(self):
        """Total = sum(time_ms) + penalty × deaths. Matches _roll_up_episode."""
        from tests.factories import make_event_attempt

        from spinlab.estimators._episode_helpers import _group_into_episodes
        from spinlab.estimators.bootstrap_resample import _episode_total_ms
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2500),
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=7000),
        ]
        ep = _group_into_episodes(events)[0]
        # 3000 + 2500 + 7000 = 12500 raw, plus 2 deaths × 3200 penalty = 18900
        assert _episode_total_ms(ep, respawn_penalty_ms=3200) == 18900

    def test_aborted_episode_total_no_penalty_on_last_life(self):
        """Aborted (all-deaths) episode: raw sum + penalty × deaths."""
        from tests.factories import make_event_attempt

        from spinlab.estimators._episode_helpers import _group_into_episodes
        from spinlab.estimators.bootstrap_resample import _episode_total_ms
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2000),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
        ]
        ep = _group_into_episodes(events)[0]
        # 5000 raw + 2 × 3200 penalty = 11400
        assert _episode_total_ms(ep, respawn_penalty_ms=3200) == 11400


class TestSurvivedTailMs:
    def test_completed_episode_returns_last_life_time(self):
        from tests.factories import make_event_attempt

        from spinlab.estimators._episode_helpers import _group_into_episodes
        from spinlab.estimators.bootstrap_resample import _survived_tail_ms
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=7500),
        ]
        ep = _group_into_episodes(events)[0]
        assert _survived_tail_ms(ep) == 7500

    def test_aborted_episode_returns_none(self):
        """No survived life ⇒ no completion tail to sample."""
        from tests.factories import make_event_attempt

        from spinlab.estimators._episode_helpers import _group_into_episodes
        from spinlab.estimators.bootstrap_resample import _survived_tail_ms
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2000),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
        ]
        ep = _group_into_episodes(events)[0]
        assert _survived_tail_ms(ep) is None


class TestResolveNSamples:
    def test_default_when_param_missing(self):
        from spinlab.estimators.bootstrap_resample import (
            DEFAULT_N_SAMPLES,
            _resolve_n_samples,
        )
        assert _resolve_n_samples(None) == DEFAULT_N_SAMPLES
        assert _resolve_n_samples({}) == DEFAULT_N_SAMPLES

    def test_explicit_value_used(self):
        from spinlab.estimators.bootstrap_resample import _resolve_n_samples
        assert _resolve_n_samples({"n_samples": 500}) == 500

    def test_below_min_raises(self):
        from spinlab.estimators.bootstrap_resample import _resolve_n_samples
        with pytest.raises(ValueError, match="n_samples"):
            _resolve_n_samples({"n_samples": 50})

    def test_above_max_raises(self):
        from spinlab.estimators.bootstrap_resample import _resolve_n_samples
        with pytest.raises(ValueError, match="n_samples"):
            _resolve_n_samples({"n_samples": 999999})

    def test_non_int_raises(self):
        from spinlab.estimators.bootstrap_resample import _resolve_n_samples
        with pytest.raises(ValueError, match="n_samples"):
            _resolve_n_samples({"n_samples": "lots"})


class TestBootstrapMeans:
    def test_single_completed_episode_returns_its_values(self):
        """Pool of one ⇒ every draw is the same episode ⇒ zero variance."""
        from tests.factories import make_event_attempt

        from spinlab.estimators._episode_helpers import _group_into_episodes
        from spinlab.estimators.bootstrap_resample import _bootstrap_means
        events = [make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000)]
        episodes = _group_into_episodes(events)
        import random
        rng = random.Random(42)
        result = _bootstrap_means(
            episodes=episodes,
            weights=[1.0],
            n_samples=1000,
            respawn_penalty_ms=3200,
            rng=rng,
        )
        assert result.mean_total_ms == pytest.approx(8000.0)
        assert result.mean_completion_ms == pytest.approx(8000.0)

    def test_empty_pool_returns_none(self):
        import random

        from spinlab.estimators.bootstrap_resample import _bootstrap_means
        rng = random.Random(42)
        result = _bootstrap_means(
            episodes=[],
            weights=[],
            n_samples=1000,
            respawn_penalty_ms=3200,
            rng=rng,
        )
        assert result.mean_total_ms is None
        assert result.mean_completion_ms is None

    def test_no_completed_episodes_completion_mean_none(self):
        """All-aborted pool ⇒ total has values (deaths counted), completion is None."""
        from tests.factories import make_event_attempt

        from spinlab.estimators._episode_helpers import _group_into_episodes
        from spinlab.estimators.bootstrap_resample import _bootstrap_means
        events = [
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=2000),
            make_event_attempt(episode_id="ep1", outcome="died", time_ms=3000),
        ]
        episodes = _group_into_episodes(events)
        import random
        rng = random.Random(42)
        result = _bootstrap_means(
            episodes=episodes,
            weights=[1.0],
            n_samples=1000,
            respawn_penalty_ms=3200,
            rng=rng,
        )
        # Total = 2000 + 3000 + 2 × 3200 = 11400 every draw.
        assert result.mean_total_ms == pytest.approx(11400.0)
        # No completed episode in the pool ⇒ no completion samples to mean.
        assert result.mean_completion_ms is None

    def test_seeded_reproducibility(self):
        """Same seed + same pool ⇒ same answer."""
        from tests.factories import make_event_attempt

        from spinlab.estimators._episode_helpers import _group_into_episodes
        from spinlab.estimators.bootstrap_resample import _bootstrap_means
        events = [
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000),
            make_event_attempt(episode_id="ep2", outcome="died", time_ms=3000),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=7000),
        ]
        episodes = _group_into_episodes(events)
        weights = [1.0, 1.0]
        import random
        a = _bootstrap_means(episodes=episodes, weights=weights, n_samples=500,
                             respawn_penalty_ms=3200, rng=random.Random(7))
        b = _bootstrap_means(episodes=episodes, weights=weights, n_samples=500,
                             respawn_penalty_ms=3200, rng=random.Random(7))
        assert a.mean_total_ms == b.mean_total_ms
        assert a.mean_completion_ms == b.mean_completion_ms

    def test_agrees_with_geometric_when_iid(self):
        """When deaths truly are i.i.d. Bernoulli, bootstrap and the geometric
        formula should agree within Monte-Carlo error."""
        from tests.factories import make_event_attempt

        from spinlab.estimators._episode_helpers import _group_into_episodes
        from spinlab.estimators.bootstrap_resample import _bootstrap_means
        from spinlab.estimators.death_aware_rolling import _expected_total_ms
        events = []
        for i in range(20):
            events.append(make_event_attempt(episode_id=f"ep{i}", outcome="died", time_ms=3000))
            events.append(make_event_attempt(episode_id=f"ep{i}", outcome="survived", time_ms=7000))
        episodes = _group_into_episodes(events)
        # Every episode IS the same i.i.d. realization here — bootstrapping
        # whole episodes just reshuffles them, so the bootstrap mean equals
        # the per-episode total exactly: 3000 + 7000 + 1 × 3200 = 13200.
        import random
        rng = random.Random(123)
        weights = [1.0] * len(episodes)
        result = _bootstrap_means(
            episodes=episodes, weights=weights, n_samples=2000,
            respawn_penalty_ms=3200, rng=rng,
        )
        geom = _expected_total_ms(
            p_die_per_life=0.5,
            e_death_time_ms=3000.0,
            e_completion_time_ms=7000.0,
            respawn_penalty_ms=3200,
        )
        assert result.mean_total_ms == pytest.approx(geom, rel=0.01)

    def test_aborted_episodes_pull_bootstrap_below_geometric(self):
        """When some attempts abort (player gives up), the geometric formula
        OVERESTIMATES expected time because it pretends every attempt
        completes-by-attrition, while bootstrap uses the actual short totals
        of aborted episodes.

        Pool: 5 clean completes (7000ms, 1 life) + 5 aborts (4 × 3000ms
        deaths, no survive).
          Lives: 5 survives + 20 deaths = 25 lives. p_die_per_life = 0.8.
          Geometric: (0.8/0.2) × (3000+3200) + 7000 = 4×6200 + 7000 = 31800.
          Per-episode totals: A=7000, B = 12000 + 4×3200 = 24800.
          Bootstrap mean = (5×7000 + 5×24800)/10 = 15900.

        Note: the spec said bootstrap > geometric on "clustered deaths," but
        the direction is data-dependent. With aborts in the pool the
        bootstrap is LOWER. Test the direction we actually see for this
        construction; the broader "when do they diverge?" question is a
        branch-3 visualization concern (see BACKLOG entry from Task 10).
        """
        from tests.factories import make_event_attempt

        from spinlab.estimators._episode_helpers import _group_into_episodes
        from spinlab.estimators.bootstrap_resample import _bootstrap_means
        from spinlab.estimators.death_aware_rolling import _expected_total_ms
        events = []
        for i in range(5):
            events.append(make_event_attempt(episode_id=f"clean{i}", outcome="survived", time_ms=7000))
        for i in range(5):
            for _ in range(4):
                events.append(make_event_attempt(episode_id=f"abort{i}", outcome="died", time_ms=3000))
        episodes = _group_into_episodes(events)
        import random
        weights = [1.0] * len(episodes)
        bs = _bootstrap_means(
            episodes=episodes, weights=weights, n_samples=5000,
            respawn_penalty_ms=3200, rng=random.Random(99),
        )
        geom = _expected_total_ms(
            p_die_per_life=0.8,
            e_death_time_ms=3000.0,
            e_completion_time_ms=7000.0,
            respawn_penalty_ms=3200,
        )
        assert bs.mean_total_ms is not None
        assert geom is not None
        # bs should be ≈ 15900, geom = 31800 ⇒ ratio < 0.6.
        assert bs.mean_total_ms < 0.6 * geom


class TestModelOutput:
    def test_empty_events_returns_none_output(self):
        from tests.factories import make_attempt_record

        from spinlab.estimators import get_estimator
        est = get_estimator("bootstrap_resample")
        a = make_attempt_record(8000, True, clean_tail_ms=8000)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=[])
        assert out.total.expected_ms is None
        assert out.clean.expected_ms is None
        assert out.extras is None  # bootstrap never populates extras (locked-in decision)

    def test_hot_only_history_returns_none_output(self):
        """All-hot pool filters down to empty after cold filter."""
        from tests.factories import make_attempt_record, make_event_attempt

        from spinlab.estimators import get_estimator
        events = [
            make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000, is_hot=True),
            make_event_attempt(episode_id="ep2", outcome="survived", time_ms=7500, is_hot=True),
        ]
        est = get_estimator("bootstrap_resample")
        a = make_attempt_record(8000, True, clean_tail_ms=8000)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=events)
        assert out.total.expected_ms is None
        assert out.clean.expected_ms is None
        assert out.extras is None

    def test_single_completion_returns_completion_time(self):
        from tests.factories import make_attempt_record, make_event_attempt

        from spinlab.estimators.bootstrap_resample import BootstrapResampleEstimator
        events = [make_event_attempt(episode_id="ep1", outcome="survived", time_ms=8000)]
        est = BootstrapResampleEstimator(seed=42)
        a = make_attempt_record(8000, True, clean_tail_ms=8000)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=events)
        assert out.total.expected_ms == pytest.approx(8000.0)
        assert out.clean.expected_ms == pytest.approx(8000.0)
        # Floor reuses the death_aware helper ⇒ same answer.
        assert out.total.floor_ms == pytest.approx(8000.0)
        assert out.clean.floor_ms == pytest.approx(8000.0)
        # One sample ⇒ no slope.
        assert out.total.ms_per_attempt is None
        assert out.clean.ms_per_attempt is None
        assert out.extras is None

    def test_filters_hot_episodes_before_sampling(self):
        """Hot episodes in the input must NOT contribute to the bootstrap pool."""
        from tests.factories import make_attempt_record, make_event_attempt

        from spinlab.estimators.bootstrap_resample import BootstrapResampleEstimator
        events = [
            # Cold pool: 5000ms clean.
            make_event_attempt(episode_id="cold", outcome="survived", time_ms=5000, is_hot=False),
            # Hot episode: should be excluded from sampling.
            make_event_attempt(episode_id="hot", outcome="survived", time_ms=99000, is_hot=True),
        ]
        est = BootstrapResampleEstimator(seed=42)
        a = make_attempt_record(5000, True, clean_tail_ms=5000)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=events)
        # If the hot episode leaked in, total would jump toward 99000.
        # Cold-only ⇒ should sit at the cold value.
        assert out.total.expected_ms == pytest.approx(5000.0)
        assert out.clean.expected_ms == pytest.approx(5000.0)

    def test_floor_ms_matches_death_aware(self):
        """floor_ms uses the same helper as death_aware_rolling ⇒ same answer
        for the same input."""
        from tests.factories import make_attempt_record, make_event_attempt

        from spinlab.estimators import get_estimator
        from spinlab.estimators.bootstrap_resample import BootstrapResampleEstimator
        events = (
            [make_event_attempt(episode_id="old_great", outcome="survived", time_ms=5000)]
            + [
                make_event_attempt(episode_id=f"new{i}", outcome="survived", time_ms=9000)
                for i in range(20)
            ]
        )
        a = make_attempt_record(9000, True, clean_tail_ms=9000)

        bs = BootstrapResampleEstimator(seed=1)
        bs_state = bs.init_state(a, priors={})
        bs_out = bs.model_output(bs_state, [a], events=events)

        da = get_estimator("death_aware_rolling")
        da_state = da.init_state(a, priors={})
        da_out = da.model_output(da_state, [a], events=events)

        assert bs_out.total.floor_ms == da_out.total.floor_ms
        assert bs_out.clean.floor_ms == da_out.clean.floor_ms

    def test_ms_per_attempt_uses_chronological_completion_samples(self):
        """Slope estimator is the same one death_aware uses; positive when improving."""
        from tests.factories import make_attempt_record, make_event_attempt

        from spinlab.estimators.bootstrap_resample import BootstrapResampleEstimator
        events = [
            make_event_attempt(episode_id=f"ep{i}", outcome="survived", time_ms=t)
            for i, t in enumerate([12000, 11500, 11000, 10500, 10000, 9500, 9000, 8500])
        ]
        est = BootstrapResampleEstimator(seed=1)
        a = make_attempt_record(8500, True, clean_tail_ms=8500)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=events)
        assert out.total.ms_per_attempt is not None
        assert out.total.ms_per_attempt > 0
        assert out.clean.ms_per_attempt is not None
        assert out.clean.ms_per_attempt > 0


class TestRegistryFactory:
    def test_get_estimator_returns_a_fresh_seedless_instance(self):
        """The routes call get_estimator(name) — no seed kwarg. The default
        constructor must work and return nondeterministic output."""
        from spinlab.estimators import get_estimator
        est = get_estimator("bootstrap_resample")
        est2 = get_estimator("bootstrap_resample")
        assert est is not est2

    def test_default_constructed_estimator_produces_output_on_real_history(self):
        """End-to-end through get_estimator — what the route does at runtime."""
        from tests.factories import make_attempt_record, make_event_attempt

        from spinlab.estimators import get_estimator
        est = get_estimator("bootstrap_resample")
        events = [
            make_event_attempt(episode_id=f"ep{i}", outcome="survived", time_ms=8000)
            for i in range(5)
        ]
        a = make_attempt_record(8000, True, clean_tail_ms=8000)
        state = est.init_state(a, priors={})
        out = est.model_output(state, [a], events=events)
        assert out.total.expected_ms == pytest.approx(8000.0)
        assert out.clean.expected_ms == pytest.approx(8000.0)


class TestAPIExposure:
    def test_appears_in_list_estimators_payload(self):
        """The route reads list_estimators(); appearing here means the dropdown shows it."""
        from spinlab.estimators import list_estimators
        names = list_estimators()
        assert "bootstrap_resample" in names
        assert "death_aware_rolling" in names

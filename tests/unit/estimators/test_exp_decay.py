"""Tests for Exp Decay estimator."""
import math

import pytest

np = pytest.importorskip("numpy")
from tests.factories import make_attempt_record, make_incomplete  # noqa: E402

from spinlab.estimators.exp_decay import ExpDecayEstimator, ExpDecayState  # noqa: E402
from spinlab.models import AttemptRecord, ModelOutput  # noqa: E402


def _synthetic_exp_attempts(
    n: int = 25, amplitude: float = 12000.0, decay_rate: float = 0.1,
    asymptote: float = 3000.0,
) -> list[AttemptRecord]:
    """Generate n attempts following exact a*exp(-b*n)+c (no noise)."""
    return [
        make_attempt_record(int(amplitude * math.exp(-decay_rate * i) + asymptote), True,
                            clean_tail_ms=int(amplitude * math.exp(-decay_rate * i) + asymptote))
        for i in range(n)
    ]


class TestExpDecayProcessAttempt:
    def test_init_from_first_attempt(self):
        est = ExpDecayEstimator()
        state = est.init_state(make_attempt_record(12000, True, clean_tail_ms=12000), priors={})
        assert state.n_completed == 1
        assert state.n_attempts == 1

    def test_process_tracks_counts(self):
        est = ExpDecayEstimator()
        attempts = _synthetic_exp_attempts(5)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        assert state.n_completed == 5
        assert state.n_attempts == 5

    def test_incomplete_increments_attempts_only(self):
        est = ExpDecayEstimator()
        state = est.init_state(make_attempt_record(12000, True, clean_tail_ms=12000), priors={})
        inc = make_incomplete()
        state = est.process_attempt(state, inc,
                                    [make_attempt_record(12000, True, clean_tail_ms=12000), inc])
        assert state.n_completed == 1
        assert state.n_attempts == 2


class TestExpDecayPriors:
    def test_init_does_not_fabricate_fit_params(self):
        # Honest cold start: with one observation the curve is undetermined,
        # so init_state must not bake population priors into the per-segment
        # fit params — that would lie about model output before any fit ran.
        est = ExpDecayEstimator()
        priors = {
            "amplitude": 5000.0, "decay_rate": 0.1, "asymptote": 8000.0,
            "total_amplitude": 6000.0, "total_decay_rate": 0.08, "total_asymptote": 9000.0,
        }
        state = est.init_state(make_attempt_record(15000, True, clean_tail_ms=15000), priors=priors)
        assert state.amplitude == 0.0
        assert state.decay_rate == 0.0
        assert state.total_amplitude == 0.0

    def test_model_output_returns_none_below_min_points(self):
        # Even with priors, < MIN_POINTS_FOR_FIT means no honest estimate.
        est = ExpDecayEstimator()
        attempt = make_attempt_record(15000, True, clean_tail_ms=15000)
        priors = {
            "amplitude": 5000.0, "decay_rate": 0.1, "asymptote": 8000.0,
            "total_amplitude": 6000.0, "total_decay_rate": 0.08, "total_asymptote": 9000.0,
        }
        state = est.init_state(attempt, priors=priors)
        out = est.model_output(state, [attempt])
        assert out.total.expected_ms is None
        assert out.clean.expected_ms is None

    def test_priors_seed_curve_fit_p0(self):
        # With enough data to fit AND a prior, the optimizer's starting point
        # is the prior — verifiable by feeding data that exactly matches the
        # prior shape: the resulting fit should land on those params.
        est = ExpDecayEstimator()
        # 5 attempts following amplitude=5000, decay=0.1, asymptote=8000
        attempts = [
            make_attempt_record(int(5000 * math.exp(-0.1 * i) + 8000), True,
                                clean_tail_ms=int(5000 * math.exp(-0.1 * i) + 8000))
            for i in range(5)
        ]
        priors = {
            "amplitude": 5000.0, "decay_rate": 0.1, "asymptote": 8000.0,
            "total_amplitude": 5000.0, "total_decay_rate": 0.1, "total_asymptote": 8000.0,
        }
        state = est.init_state(attempts[0], priors=priors)
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        # The fit should land near the priors since data is consistent with them.
        # Tolerances reflect the integer truncation in the synthetic attempts.
        assert state.asymptote == pytest.approx(8000.0, abs=10.0)
        assert state.amplitude == pytest.approx(5000.0, abs=20.0)

    def test_get_priors_averages_mature_states(self, tmp_path):
        import json

        from spinlab.db import Database
        from spinlab.models import Segment, Waypoint

        db = Database(str(tmp_path / "p.db"))
        db.upsert_game("g1", "Game", "any%")

        # Two mature segments — averages should be the midpoint of their fit params.
        for i, amp in enumerate([4000.0, 6000.0]):
            wp_s = Waypoint.make("g1", i + 1, "entrance", 0, {"i": i})
            wp_e = Waypoint.make("g1", i + 1, "goal", 0, {"i": i})
            db.upsert_waypoint(wp_s)
            db.upsert_waypoint(wp_e)
            seg = Segment(
                id=Segment.make_id("g1", i + 1, "entrance", 0, "goal", 0, wp_s.id, wp_e.id),
                game_id="g1", level_number=i + 1,
                start_type="entrance", start_ordinal=0,
                end_type="goal", end_ordinal=0,
                description=f"L{i+1}", strat_version=1,
                start_waypoint_id=wp_s.id, end_waypoint_id=wp_e.id,
            )
            db.upsert_segment(seg)
            state = ExpDecayState(
                n_completed=15, n_attempts=20,
                amplitude=amp, decay_rate=0.1, asymptote=2000.0,
                total_amplitude=amp + 1000, total_decay_rate=0.08, total_asymptote=3000.0,
            )
            db.save_model_state(seg.id, "exp_decay",
                                json.dumps(state.to_dict()), json.dumps({}))

        est = ExpDecayEstimator()
        priors = est.get_priors(db, "g1")
        assert priors["amplitude"] == pytest.approx(5000.0)  # (4000+6000)/2
        assert priors["total_amplitude"] == pytest.approx(6000.0)

    def test_get_priors_skips_immature_states(self, tmp_path):
        import json

        from spinlab.db import Database
        from spinlab.models import Segment, Waypoint

        db = Database(str(tmp_path / "p2.db"))
        db.upsert_game("g1", "Game", "any%")
        wp_s = Waypoint.make("g1", 1, "entrance", 0, {})
        wp_e = Waypoint.make("g1", 1, "goal", 0, {})
        db.upsert_waypoint(wp_s)
        db.upsert_waypoint(wp_e)
        seg = Segment(
            id=Segment.make_id("g1", 1, "entrance", 0, "goal", 0, wp_s.id, wp_e.id),
            game_id="g1", level_number=1,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            description="L1", strat_version=1,
            start_waypoint_id=wp_s.id, end_waypoint_id=wp_e.id,
        )
        db.upsert_segment(seg)
        state = ExpDecayState(n_completed=2, n_attempts=2)  # below MATURITY_THRESHOLD
        db.save_model_state(seg.id, "exp_decay",
                            json.dumps(state.to_dict()), json.dumps({}))

        est = ExpDecayEstimator()
        assert est.get_priors(db, "g1") == {}


class TestExpDecayFit:
    def test_recovers_known_asymptote(self):
        """Fit on exact exponential data should recover the asymptote."""
        est = ExpDecayEstimator()
        attempts = _synthetic_exp_attempts(25, amplitude=12000, decay_rate=0.1, asymptote=3000)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        assert state.asymptote == pytest.approx(3000, rel=0.05)

    def test_recovers_known_decay_rate(self):
        est = ExpDecayEstimator()
        attempts = _synthetic_exp_attempts(25, amplitude=12000, decay_rate=0.1, asymptote=3000)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        assert state.decay_rate == pytest.approx(0.1, rel=0.1)


class TestExpDecayModelOutput:
    def test_output_with_enough_data(self):
        est = ExpDecayEstimator()
        attempts = _synthetic_exp_attempts(25)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert isinstance(out, ModelOutput)
        assert out.total.ms_per_attempt > 0
        assert out.total.floor_ms > 0
        assert out.clean.floor_ms > 0
        assert out.clean.floor_ms < out.total.expected_ms

    def test_ms_per_attempt_is_discrete_difference(self):
        """ms_per_attempt should be f(n) - f(n+1) from total fit."""
        est = ExpDecayEstimator()
        a, b, c = 12000.0, 0.1, 3000.0
        attempts = _synthetic_exp_attempts(25, amplitude=a, decay_rate=b, asymptote=c)
        state = est.init_state(attempts[0], priors={})
        for att in attempts[1:]:
            state = est.process_attempt(state, att, attempts)
        out = est.model_output(state, attempts)
        # Discrete difference at n=25: f(25) - f(26)
        f_n = a * math.exp(-b * 25) + c
        f_n1 = a * math.exp(-b * 26) + c
        expected_mpa = f_n - f_n1
        assert out.total.ms_per_attempt == pytest.approx(expected_mpa, rel=0.15)

    def test_floor_never_negative(self):
        est = ExpDecayEstimator()
        attempts = _synthetic_exp_attempts(25, asymptote=100)
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.total.floor_ms >= 0
        assert out.clean.floor_ms >= 0

    def test_floor_none_when_asymptote_hits_lower_bound(self):
        """When curve_fit pushes asymptote to 0, floor_ms should be None."""
        est = ExpDecayEstimator()
        state = ExpDecayState(
            n_completed=5, n_attempts=5,
            amplitude=10000.0, decay_rate=0.1, asymptote=0.0, sigma=100.0,
            total_amplitude=10000.0, total_decay_rate=0.1,
            total_asymptote=0.0, total_sigma=100.0,
        )
        attempts = _synthetic_exp_attempts(5, amplitude=10000, decay_rate=0.5, asymptote=500)
        out = est.model_output(state, attempts)
        assert out.total.floor_ms is None
        assert out.clean.floor_ms is None
        # expected and trend should still be computed
        assert out.total.expected_ms is not None
        assert out.total.ms_per_attempt is not None

    def test_few_points_returns_none(self):
        """With <3 completed, returns all None — no silent fallback."""
        est = ExpDecayEstimator()
        attempts = [
            make_attempt_record(12000, True, clean_tail_ms=12000),
            make_attempt_record(11500, True, clean_tail_ms=11500),
        ]
        state = est.init_state(attempts[0], priors={})
        state = est.process_attempt(state, attempts[1], attempts)
        out = est.model_output(state, attempts)
        assert out.total.expected_ms is None
        assert out.total.ms_per_attempt is None
        assert out.total.floor_ms is None
        assert out.clean.expected_ms is None

    def test_two_fits_total_and_clean(self):
        est = ExpDecayEstimator()
        n = 25
        attempts = []
        for i in range(n):
            total = int(12000 * math.exp(-0.1 * i) + 5000)
            clean = int(8000 * math.exp(-0.1 * i) + 3000)
            deaths = 2 if i % 3 == 0 else 0
            attempts.append(make_attempt_record(total, True, deaths=deaths, clean_tail_ms=clean))
        state = est.init_state(attempts[0], priors={})
        for a in attempts[1:]:
            state = est.process_attempt(state, a, attempts)
        out = est.model_output(state, attempts)
        assert out.total.floor_ms > out.clean.floor_ms


class TestExpDecayRebuild:
    def test_rebuild_from_attempts(self):
        est = ExpDecayEstimator()
        attempts = [
            make_attempt_record(12000, True, clean_tail_ms=12000),
            make_incomplete(),
            make_attempt_record(11000, True, clean_tail_ms=11000),
        ]
        state = est.rebuild_state(attempts)
        assert state.n_completed == 2
        assert state.n_attempts == 3

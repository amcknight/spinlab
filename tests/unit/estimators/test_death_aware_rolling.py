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

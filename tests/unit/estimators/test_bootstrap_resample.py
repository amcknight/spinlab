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

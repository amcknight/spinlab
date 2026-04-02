# tests/test_estimator_params.py
"""Tests for estimator tunable params system."""
from spinlab.estimators import ParamDef, Estimator, get_estimator, list_estimators

# Force registration
from spinlab.estimators.kalman import KalmanEstimator  # noqa: F401
from spinlab.estimators.rolling_mean import RollingMeanEstimator  # noqa: F401
try:
    from spinlab.estimators.exp_decay import ExpDecayEstimator  # noqa: F401
except ImportError:
    pass


class TestParamDef:
    def test_create_param_def(self):
        p = ParamDef(
            name="R", display_name="Obs. Noise", default=25.0,
            min_val=0.01, max_val=1000.0, step=0.1,
            description="How noisy individual attempts are.",
        )
        assert p.name == "R"
        assert p.default == 25.0
        assert p.min_val == 0.01

    def test_param_def_to_dict(self):
        p = ParamDef(
            name="R", display_name="Obs. Noise", default=25.0,
            min_val=0.01, max_val=1000.0, step=0.1,
            description="How noisy individual attempts are.",
        )
        d = p.to_dict()
        assert d["name"] == "R"
        assert d["display_name"] == "Obs. Noise"
        assert d["default"] == 25.0
        assert d["min"] == 0.01
        assert d["max"] == 1000.0
        assert d["step"] == 0.1
        assert d["description"] == "How noisy individual attempts are."


class TestDeclaredParamsABC:
    def test_all_estimators_return_list(self):
        for name in list_estimators():
            est = get_estimator(name)
            params = est.declared_params()
            assert isinstance(params, list)
            for p in params:
                assert isinstance(p, ParamDef)

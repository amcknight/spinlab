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


from spinlab.estimators.kalman import KalmanEstimator, KalmanState
from spinlab.models import AttemptRecord


def _attempt(time_ms: int | None, completed: bool) -> AttemptRecord:
    clean = time_ms if completed and time_ms is not None else None
    return AttemptRecord(
        time_ms=time_ms, completed=completed, deaths=0,
        clean_tail_ms=clean, created_at="2026-01-01T00:00:00",
    )


class TestKalmanDeclaredParams:
    def test_returns_params(self):
        est = KalmanEstimator()
        params = est.declared_params()
        assert len(params) == 7
        names = {p.name for p in params}
        assert names == {"D0", "R", "P_D0", "Q_mm", "Q_dd", "R_floor", "R_blend"}

    def test_defaults_match_module_constants(self):
        est = KalmanEstimator()
        params = est.declared_params()
        by_name = {p.name: p for p in params}
        assert by_name["D0"].default == 0.0
        assert by_name["R"].default == 25.0
        assert by_name["Q_mm"].default == 0.1


class TestKalmanParamsAwareRebuild:
    def test_rebuild_with_custom_D0(self):
        est = KalmanEstimator()
        attempts = [_attempt(12000, True), _attempt(11000, True), _attempt(10000, True)]
        state_default = est.rebuild_state(attempts)
        state_custom = est.rebuild_state(attempts, params={"D0": -2.0})
        assert state_default.mu != state_custom.mu

    def test_rebuild_with_custom_R(self):
        est = KalmanEstimator()
        attempts = [_attempt(12000, True), _attempt(11000, True), _attempt(10000, True)]
        state_low_R = est.rebuild_state(attempts, params={"R": 1.0})
        state_high_R = est.rebuild_state(attempts, params={"R": 100.0})
        assert state_low_R.mu != state_high_R.mu

    def test_rebuild_with_no_params_uses_defaults(self):
        est = KalmanEstimator()
        attempts = [_attempt(12000, True), _attempt(11000, True)]
        state_none = est.rebuild_state(attempts, params=None)
        state_empty = est.rebuild_state(attempts, params={})
        assert state_none.mu == state_empty.mu


class TestKalmanRReestimateEveryAttempt:
    def test_r_changes_after_second_attempt(self):
        """R should re-estimate on every completed attempt, not just every 10th."""
        est = KalmanEstimator()
        a1 = _attempt(12000, True)
        state = est.init_state(a1, priors={})
        initial_R = state.R
        for i in range(5):
            a = _attempt(12000 - (i + 1) * 500, True)
            state = est.process_attempt(state, a, [a1])
        assert state.R != initial_R, "R should re-estimate before 10 attempts"


class TestSchedulerParamsWiring:
    def test_rebuild_all_states_passes_params(self, tmp_path):
        """Scheduler.rebuild_all_states should load and pass estimator params."""
        import json
        from spinlab.db import Database
        from spinlab.models import Attempt, Segment
        from spinlab.scheduler import Scheduler

        db = Database(str(tmp_path / "test.db"))
        db.upsert_game("g1", "Game", "any%")
        seg = Segment(
            id="s1", game_id="g1", level_number=1,
            start_type="entrance", start_ordinal=0,
            end_type="checkpoint", end_ordinal=0,
        )
        db.upsert_segment(seg)
        db.create_session("sess1", "g1")
        for t in [12000, 11000, 10000]:
            db.log_attempt(Attempt(
                segment_id="s1", session_id="sess1", completed=True,
                time_ms=t, deaths=0, clean_tail_ms=t,
            ))

        sched = Scheduler(db, "g1")

        # Rebuild with default params
        sched.rebuild_all_states()
        row_default = db.load_model_state("s1", "kalman")
        state_default = json.loads(row_default["state_json"])

        # Save custom params and rebuild
        db.save_allocator_config("estimator_params:kalman", json.dumps({"D0": -2.0}))
        sched.rebuild_all_states()
        row_custom = db.load_model_state("s1", "kalman")
        state_custom = json.loads(row_custom["state_json"])

        # Different D0 should produce different state
        assert state_default["mu"] != state_custom["mu"]


class TestEstimatorParamsAPI:
    def test_get_estimator_params_returns_schema(self, tmp_path):
        from spinlab.db import Database
        from spinlab.dashboard import create_app
        from starlette.testclient import TestClient

        db = Database(str(tmp_path / "test.db"))
        db.upsert_game("g1", "TestGame", "any%")
        app = create_app(db=db, default_category="any%")
        client = TestClient(app)
        app.state.session.game_id = "g1"
        app.state.session.game_name = "TestGame"

        resp = client.get("/api/estimator-params")
        assert resp.status_code == 200
        data = resp.json()
        assert "estimator" in data
        assert "params" in data
        assert isinstance(data["params"], list)

    def test_post_estimator_params_saves(self, tmp_path):
        from spinlab.db import Database
        from spinlab.dashboard import create_app
        from starlette.testclient import TestClient

        db = Database(str(tmp_path / "test.db"))
        db.upsert_game("g1", "TestGame", "any%")
        app = create_app(db=db, default_category="any%")
        client = TestClient(app)
        app.state.session.game_id = "g1"
        app.state.session.game_name = "TestGame"

        resp = client.post("/api/estimator-params", json={"params": {"D0": 1.0}})
        assert resp.status_code == 200

        # Verify it was saved
        raw = db.load_allocator_config("estimator_params:kalman")
        assert raw is not None
        import json
        saved = json.loads(raw)
        assert saved["D0"] == 1.0

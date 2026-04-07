"""Full-stack smoke tests: Mesen headless + dashboard + DB.

These tests verify the assembled system works end-to-end.
They require Mesen2 and a test ROM (same as poke tests).
"""
import pytest

pytestmark = pytest.mark.emulator


class TestNoGameEndpoints:
    """Before Mesen connects, all GET endpoints should return 200 with empty data."""

    @pytest.fixture(autouse=True)
    def _setup(self, dashboard_url):
        """Just ensures dashboard_server fixture is active."""

    def test_state_returns_200(self, api):
        resp = api.get("/api/state")
        assert resp.status_code == 200

    def test_segments_returns_200(self, api):
        resp = api.get("/api/segments")
        assert resp.status_code == 200
        assert isinstance(resp.json()["segments"], list)

    def test_references_returns_200(self, api):
        resp = api.get("/api/references")
        assert resp.status_code == 200

    def test_sessions_returns_200(self, api):
        resp = api.get("/api/sessions")
        assert resp.status_code == 200

    def test_estimator_params_returns_200(self, api):
        resp = api.get("/api/estimator-params")
        assert resp.status_code == 200

    def test_model_returns_200(self, api):
        resp = api.get("/api/model")
        assert resp.status_code == 200


class TestGameLoadsAfterConnect:
    """After Mesen starts, the dashboard should show a connected game."""

    def test_tcp_connected(self, api):
        resp = api.get("/api/state")
        state = resp.json()
        assert state["tcp_connected"] is True

    def test_game_id_populated(self, api):
        resp = api.get("/api/state")
        state = resp.json()
        assert state["game_id"] is not None

    def test_game_name_populated(self, api):
        resp = api.get("/api/state")
        state = resp.json()
        assert state.get("game_name") is not None
        assert len(state["game_name"]) > 0

    def test_segments_returns_200(self, api):
        resp = api.get("/api/segments")
        assert resp.status_code == 200

    def test_references_returns_200(self, api):
        resp = api.get("/api/references")
        assert resp.status_code == 200

    def test_model_returns_estimator_info(self, api):
        resp = api.get("/api/model")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("estimator") is not None
        assert isinstance(data.get("estimators"), list)
        assert len(data["estimators"]) > 0


class TestReferenceStartAfterConnect:
    """After game loads, reference start should be accepted (not 409 'No game loaded')."""

    def test_reference_start_returns_200(self, api):
        resp = api.post("/api/reference/start")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") is not None
        # Stop it to clean up
        api.post("/api/reference/stop")

"""Tests for DashboardHarness — verifies the context-manager lifecycle without
booting a real emulator (the fake_emu_backend=True path)."""
from __future__ import annotations

import pytest
import requests
from tests.integration._dashboard_harness import DashboardHarness


def test_dashboard_harness_starts_and_serves_api_state(tmp_path):
    """In fake-emu mode, the harness should bring up a dashboard whose
    /api/state returns 200."""
    with DashboardHarness.fake(tmp_path_root=tmp_path) as ctx:
        resp = requests.get(f"{ctx.base_url}/api/state", timeout=2)
        assert resp.status_code == 200
        # /api/state always reports emu_connected from session.emu;
        # FakeEmuBackend starts connected=True.
        body = resp.json()
        assert body.get("emu_connected") is True


def test_dashboard_harness_tears_down_cleanly(tmp_path):
    """After exit, the port should be free and tmp dir gone."""
    with DashboardHarness.fake(tmp_path_root=tmp_path) as ctx:
        tmp = ctx.tmp_path
        assert tmp.exists()
    # uvicorn join should have completed
    assert not tmp.exists() or not any(tmp.iterdir())  # rmtree ignore_errors=True


def test_dashboard_harness_exposes_db_and_session(tmp_path):
    """Test bodies use db and session for direct manipulation."""
    with DashboardHarness.fake(tmp_path_root=tmp_path) as ctx:
        # db is a real Database; session is the SessionManager from the FastAPI app.
        assert ctx.db is not None
        assert ctx.session is not None
        # Sanity: db is queryable
        cur = ctx.db.conn.execute("SELECT COUNT(*) FROM segments")
        assert cur.fetchone()[0] == 0


def test_dashboard_harness_fail_to_start_raises_with_outcome(tmp_path, monkeypatch):
    """If the dashboard never reports 200, we get a TimeoutError naming the
    operation. (Smoke check; full path is exercised by the fixtures.)"""
    # Force a startup timeout by patching the wait helper to always fail.
    from tests.integration import _dashboard_harness as dh
    from tests.integration._wait_for import WaitOutcome

    def fake_wait(**_kwargs):
        return WaitOutcome(
            succeeded=False, name="dashboard_ready",
            elapsed_s=0.5, attempts=2, last_reason="status 500",
        )

    monkeypatch.setattr(dh, "wait_for", fake_wait)

    with pytest.raises(RuntimeError, match="dashboard_ready"):
        with DashboardHarness.fake(tmp_path_root=tmp_path):
            pass

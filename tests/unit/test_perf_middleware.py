"""perf middleware logs dur_ms for /api/* requests and skips SSE/static."""
from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from spinlab.dashboard import PERF_LOG_PREFIX, create_app
from spinlab.db import Database


def _client(tmp_path):
    from tests.conftest import make_test_config

    db = Database(tmp_path / "perf.db")
    app = create_app(db=db, config=make_test_config())
    return TestClient(app)


def _perf_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith(f"{PERF_LOG_PREFIX}:")]


def test_api_request_emits_perf_line(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="spinlab.dashboard")
    client = _client(tmp_path)
    resp = client.get("/api/state")
    assert resp.status_code == 200
    recs = _perf_records(caplog)
    assert any("GET /api/state" in r.getMessage() and "dur_ms=" in r.getMessage() for r in recs)


def test_non_api_request_skipped(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="spinlab.dashboard")
    client = _client(tmp_path)
    # Static path that doesn't exist still routes through middleware; 404 is fine.
    client.get("/some-non-api-path")
    assert _perf_records(caplog) == []

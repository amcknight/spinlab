"""Tests for the integration-test diagnostic hook (_collect_diagnostics).

These run as unit tests because they need no live RA — mock funcargs with the
shapes our integration fixtures yield.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.integration.conftest import _collect_diagnostics


@pytest.fixture
def mock_item():
    item = MagicMock()
    item.funcargs = {}
    return item


def test_collect_diagnostics_emits_block_for_dashboard_tuple(mock_item, monkeypatch):
    """A funcarg yielding (base_url, db, _) gets the /api/state + DB block."""
    db = MagicMock()
    db.conn.execute.return_value.fetchone.return_value = (5,)
    mock_item.funcargs["replay_ra_dashboard"] = ("http://x:1", db, None)

    fake_resp = MagicMock()
    fake_resp.json.return_value = {"emu_connected": True}
    monkeypatch.setattr(
        "tests.integration.conftest.http_requests.get",
        lambda url, timeout=2: fake_resp,
    )

    out = _collect_diagnostics(mock_item)
    assert "/api/state" in out
    assert "emu_connected" in out
    assert "active segments" in out


def test_collect_diagnostics_emits_block_for_harness_funcarg(mock_item):
    """A funcarg duck-typing as a harness (.proc, .client, .log_path) gets
    a process-state + log-tail block, even if the fixture isn't the dashboard."""
    harness = MagicMock()
    harness.proc.poll.return_value = None
    harness.proc.pid = 4242
    harness.client.port = 55355
    harness.log_path = MagicMock()
    harness.log_path.exists.return_value = True
    harness.log_path.read_text.return_value = "\n".join(f"line {i}" for i in range(50))

    mock_item.funcargs["ra_harness_love_yourself"] = harness
    out = _collect_diagnostics(mock_item)

    assert "harness: ra_harness_love_yourself" in out
    assert "pid=4242" in out
    assert "port=55355" in out
    assert "proc.poll()=None" in out
    assert "line 49" in out
    assert "line 19" not in out  # well before the 30-line tail


def test_collect_diagnostics_reports_dead_ra_process(mock_item):
    """If proc.poll() returns a non-None exit code, that surfaces in the block."""
    harness = MagicMock()
    harness.proc.poll.return_value = -11
    harness.proc.pid = 4242
    harness.client.port = 55355
    harness.log_path = None

    mock_item.funcargs["ra_harness_love_yourself"] = harness
    out = _collect_diagnostics(mock_item)
    assert "proc.poll()=-11" in out


def test_collect_diagnostics_returns_empty_when_no_funcargs_match(mock_item):
    """If a test has no integration funcargs, the diagnostic block is empty."""
    mock_item.funcargs["unrelated_fixture"] = MagicMock(spec=[])
    out = _collect_diagnostics(mock_item)
    assert "/api/state" not in out
    assert "harness:" not in out

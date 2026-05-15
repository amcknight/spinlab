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


def test_pause_toggle_failure_message_includes_context():
    """Sanity check on the format helper used in the fixture path. Verifies
    the helper exists and produces a message that names the harness, the
    underlying exception, and the harness port/pid."""
    from tests.integration.conftest import _format_pause_toggle_failure

    harness = MagicMock()
    harness.proc.pid = 4242
    harness.client.port = 55355
    msg = _format_pause_toggle_failure(harness, RuntimeError("nci unresponsive"))
    assert "4242" in msg
    assert "55355" in msg
    assert "nci unresponsive" in msg


def test_dashboard_startup_timeout_message_includes_port_and_error():
    """The retry loop's failure helper names the port it tried and the
    most recent connection error."""
    from tests.integration.conftest import _format_dashboard_startup_failure

    msg = _format_dashboard_startup_failure(
        port=18080,
        attempts=40,
        interval_s=0.25,
        last_error=ConnectionError("port not listening"),
    )
    assert "18080" in msg
    assert "10.0" in msg  # 40 × 0.25 = 10.0
    assert "port not listening" in msg


def test_collect_launch_failure_diagnostics_renders_structured_fields(tmp_path):
    """When RAHarness.launch fails, the diagnostic block names the stage,
    pid, port, and startup duration from the typed exception."""
    from tests.integration.conftest import _collect_launch_failure_diagnostics
    from tests.integration.ra_harness import RAHarnessLaunchError

    exc = RAHarnessLaunchError(
        "NCI did not reply",
        stage="nci_ping",
        pid=7777,
        port=55355,
        startup_duration_s=2.5,
        log_path=None,
    )
    out = _collect_launch_failure_diagnostics(exc)
    assert "RAHarnessLaunchError" in out
    assert "stage='nci_ping'" in out
    assert "pid=7777" in out
    assert "port=55355" in out
    assert "startup_duration_s=2.5" in out


def test_collect_launch_failure_diagnostics_tails_preserved_log(tmp_path):
    """When the exception's log_path is a real file, the diagnostic block
    includes its tail."""
    from tests.integration.conftest import _collect_launch_failure_diagnostics
    from tests.integration.ra_harness import RAHarnessLaunchError

    log = tmp_path / "retroarch.log"
    log.write_text("\n".join(f"line {i}" for i in range(50)))

    exc = RAHarnessLaunchError(
        "fake",
        stage="get_status",
        pid=1,
        port=2,
        startup_duration_s=0.1,
        log_path=log,
    )
    out = _collect_launch_failure_diagnostics(exc)
    assert "retroarch.log tail" in out
    assert "line 49" in out
    assert "line 19" not in out  # 30-line tail boundary


def test_collect_launch_failure_diagnostics_handles_missing_log_path():
    """If log_path is None, the block still renders fields, just no log tail."""
    from tests.integration.conftest import _collect_launch_failure_diagnostics
    from tests.integration.ra_harness import RAHarnessLaunchError

    exc = RAHarnessLaunchError(
        "no log",
        stage="path_check",
        pid=None,
        port=None,
        startup_duration_s=None,
        log_path=None,
    )
    out = _collect_launch_failure_diagnostics(exc)
    assert "stage='path_check'" in out
    assert "retroarch.log" not in out

"""Tests for TcpManager async TCP client."""
import asyncio
import json
import pytest

from spinlab.tcp_manager import TcpManager


@pytest.fixture
def tcp_server():
    """Create a real TCP server on a random port for testing."""
    import socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    yield srv, port
    srv.close()


@pytest.mark.asyncio
async def test_connect_and_send(tcp_server):
    srv, port = tcp_server
    mgr = TcpManager("127.0.0.1", port)
    await mgr.connect()
    assert mgr.is_connected

    # Accept on server side
    conn, _ = srv.accept()
    conn.settimeout(2)

    await mgr.send("ping")
    data = conn.recv(1024).decode()
    assert data.strip() == "ping"

    conn.close()
    await mgr.disconnect()
    assert not mgr.is_connected


@pytest.mark.asyncio
async def test_recv_event(tcp_server):
    srv, port = tcp_server
    mgr = TcpManager("127.0.0.1", port)
    await mgr.connect()

    conn, _ = srv.accept()
    event = {"event": "attempt_result", "split_id": "s1", "completed": True, "time_ms": 5000}
    conn.sendall((json.dumps(event) + "\n").encode())

    evt = await mgr.recv_event(timeout=2.0)
    assert evt is not None
    assert evt["event"] == "attempt_result"

    conn.close()
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_connect_refused():
    mgr = TcpManager("127.0.0.1", 59999)  # nothing listening
    connected = await mgr.connect(timeout=0.5)
    assert not connected
    assert not mgr.is_connected


# ---------------------------------------------------------------------------
# Non-JSON handling tests (TDD for ok:/err: prefix support)
# ---------------------------------------------------------------------------


def _make_manager_with_lines(lines: list[str]) -> TcpManager:
    """Return a TcpManager whose _reader yields the given text lines then EOF."""
    from unittest.mock import MagicMock

    manager = TcpManager()
    chunks = [(line.encode("utf-8") + b"\n") for line in lines] + [b""]
    call_iter = iter(chunks)

    async def fake_readline():
        return next(call_iter)

    reader = MagicMock()
    reader.readline = fake_readline

    manager._reader = reader
    manager._writer = MagicMock()
    manager._writer.is_closing.return_value = False
    return manager


@pytest.mark.asyncio
async def test_ok_prefix_no_warning(caplog):
    """ok:-prefixed messages must not produce WARNING-level log entries."""
    import logging

    manager = _make_manager_with_lines(["ok:queued", "ok:practice_loaded"])
    with caplog.at_level(logging.DEBUG, logger="spinlab.tcp_manager"):
        await manager._read_loop()

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == [], f"Unexpected warnings: {[r.message for r in warnings]}"


@pytest.mark.asyncio
async def test_err_prefix_no_warning(caplog):
    """err:-prefixed messages must not produce WARNING-level log entries."""
    import logging

    manager = _make_manager_with_lines(["err:unknown_command"])
    with caplog.at_level(logging.DEBUG, logger="spinlab.tcp_manager"):
        await manager._read_loop()

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == [], f"Unexpected warnings: {[r.message for r in warnings]}"


@pytest.mark.asyncio
async def test_unknown_non_json_warns(caplog):
    """Truly unknown non-JSON text must produce exactly one WARNING."""
    import logging

    manager = _make_manager_with_lines(["something_weird"])
    with caplog.at_level(logging.DEBUG, logger="spinlab.tcp_manager"):
        await manager._read_loop()

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, (
        f"Expected 1 warning, got {len(warnings)}: {[r.message for r in warnings]}"
    )
    assert "something_weird" in warnings[0].message


@pytest.mark.asyncio
async def test_json_events_still_queued():
    """Valid JSON messages must be placed on the events queue."""
    payload = {"type": "transition", "segment": 3}
    manager = _make_manager_with_lines([json.dumps(payload)])
    await manager._read_loop()

    assert not manager.events.empty(), "Expected JSON event on queue"
    event = manager.events.get_nowait()
    assert event == payload

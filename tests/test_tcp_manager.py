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

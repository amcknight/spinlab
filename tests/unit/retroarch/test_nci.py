"""Unit tests for spinlab.retroarch — NCI client and helpers."""
import socket

from spinlab.retroarch import (
    NCICoreFrozen,
    NCIError,
    NCIProtocolError,
    NCITimeout,
)


def test_exception_hierarchy():
    assert issubclass(NCITimeout, NCIError)
    assert issubclass(NCIProtocolError, NCIError)
    assert issubclass(NCICoreFrozen, NCIError)


def test_fake_nci_server_responds(fake_nci_server):
    """Fixture spins up a UDP responder; we can hit it and get a scripted reply."""
    fake_nci_server.handle("VERSION", lambda _: "1.22.2\n")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)
    sock.sendto(b"VERSION", fake_nci_server.address)
    data, _ = sock.recvfrom(4096)
    sock.close()

    assert data.decode() == "1.22.2\n"


def test_transport_round_trip(fake_nci_server):
    from spinlab.retroarch.nci import NCIClient

    fake_nci_server.handle("PING", "PONG\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    assert client._send("PING") == "PONG"


def test_transport_timeout_raises(fake_nci_server):
    import pytest

    from spinlab.retroarch.nci import NCIClient

    # No handler registered; server drops the packet.
    client = NCIClient(
        host=fake_nci_server.address[0],
        port=fake_nci_server.address[1],
        timeout=0.1,
    )
    with pytest.raises(NCITimeout):
        client._send("UNHANDLED_COMMAND")

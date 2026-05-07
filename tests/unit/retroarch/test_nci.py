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

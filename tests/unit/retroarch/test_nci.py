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


def test_version(fake_nci_server):
    from spinlab.retroarch.nci import NCIClient

    fake_nci_server.handle("VERSION", "1.22.2\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    assert client.version() == "1.22.2"


def test_read_ram_parses_data_bytes(fake_nci_server):
    import pytest
    from spinlab.retroarch.nci import NCIClient

    # 4 bytes at $7E0094 returned as: command-echo + addr-echo + 4 hex bytes
    fake_nci_server.handle(
        "READ_CORE_RAM 94 4",
        "READ_CORE_RAM 94 AA BB CC DD\n",
    )
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    assert client.read_ram(0x94, 4) == bytes([0xAA, 0xBB, 0xCC, 0xDD])


def test_read_ram_skips_address_echo(fake_nci_server):
    import pytest
    from spinlab.retroarch.nci import NCIClient

    """Regression: spike's bug was treating the echoed address as a data byte.
    For an unfortunately-shaped address like 0x94 (looks like a byte), the
    parser must not include it in the result.
    """
    fake_nci_server.handle(
        "READ_CORE_RAM 94 1",
        "READ_CORE_RAM 94 7F\n",
    )
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    # Single requested byte returns single byte — NOT [0x94, 0x7F].
    assert client.read_ram(0x94, 1) == bytes([0x7F])


def test_read_ram_protocol_error_on_minus_one(fake_nci_server):
    import pytest
    from spinlab.retroarch.nci import NCIClient

    fake_nci_server.handle(
        "READ_CORE_RAM ffff 1",
        "READ_CORE_RAM ffff -1\n",
    )
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    with pytest.raises(NCIProtocolError, match="-1"):
        client.read_ram(0xFFFF, 1)


def test_read_ram_protocol_error_on_unparseable(fake_nci_server):
    import pytest
    from spinlab.retroarch.nci import NCIClient

    fake_nci_server.handle("READ_CORE_RAM 94 1", "garbage\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    with pytest.raises(NCIProtocolError):
        client.read_ram(0x94, 1)

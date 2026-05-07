"""Unit tests for spinlab.retroarch — NCI client and helpers."""
import socket

import pytest

from spinlab.retroarch import (
    NCICoreFrozen,
    NCIError,
    NCIProtocolError,
    NCITimeout,
    StatusInfo,
)
from spinlab.retroarch.nci import NCIClient


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
    fake_nci_server.handle("PING", "PONG\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    assert client._send("PING") == "PONG"


def test_transport_timeout_raises(fake_nci_server):
    # No handler registered; server drops the packet.
    client = NCIClient(
        host=fake_nci_server.address[0],
        port=fake_nci_server.address[1],
        timeout=0.1,
    )
    with pytest.raises(NCITimeout):
        client._send("UNHANDLED_COMMAND")


def test_version(fake_nci_server):
    fake_nci_server.handle("VERSION", "1.22.2\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    assert client.version() == "1.22.2"


def test_read_ram_parses_data_bytes(fake_nci_server):
    # 4 bytes at $7E0094 returned as: command-echo + addr-echo + 4 hex bytes
    fake_nci_server.handle(
        "READ_CORE_RAM 94 4",
        "READ_CORE_RAM 94 AA BB CC DD\n",
    )
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    assert client.read_ram(0x94, 4) == bytes([0xAA, 0xBB, 0xCC, 0xDD])


def test_read_ram_skips_address_echo(fake_nci_server):
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
    fake_nci_server.handle(
        "READ_CORE_RAM ffff 1",
        "READ_CORE_RAM ffff -1\n",
    )
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    with pytest.raises(NCIProtocolError, match="-1"):
        client.read_ram(0xFFFF, 1)


def test_read_ram_protocol_error_on_unparseable(fake_nci_server):
    fake_nci_server.handle("READ_CORE_RAM 94 1", "garbage\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    with pytest.raises(NCIProtocolError):
        client.read_ram(0x94, 1)


def test_write_ram_sends_correct_command(fake_nci_server):
    received = []

    def capture(cmd):
        received.append(cmd)
        return "WRITE_CORE_RAM 94 1\n"  # RA echoes addr + count on success

    fake_nci_server.handle("WRITE_CORE_RAM", capture)
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    client.write_ram(0x94, bytes([0xAA, 0xBB]))

    assert received[0] == "WRITE_CORE_RAM 94 aa bb"


def test_get_status_playing(fake_nci_server):
    fake_nci_server.handle(
        "GET_STATUS",
        "GET_STATUS PLAYING super_nes,Toothpaste,crc32=41b3c49d\n",
    )
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    status = client.get_status()

    assert status == StatusInfo(
        state="PLAYING",
        system="super_nes",
        game="Toothpaste",
        crc32="41b3c49d",
    )


def test_get_status_contentless(fake_nci_server):
    """No game loaded — still a valid response, just sparse."""
    fake_nci_server.handle("GET_STATUS", "GET_STATUS CONTENTLESS\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    status = client.get_status()

    assert status == StatusInfo(state="CONTENTLESS", system=None, game=None, crc32=None)


@pytest.mark.parametrize(
    "method,args,expected_command",
    [
        ("save_state", (), "SAVE_STATE"),
        ("load_state_slot", (9999,), "LOAD_STATE_SLOT 9999"),
        ("reset", (), "RESET"),
        ("pause_toggle", (), "PAUSE_TOGGLE"),
        ("frame_advance", (), "FRAMEADVANCE"),
        ("quit", (), "QUIT"),
    ],
)
def test_fire_and_forget_commands(fake_nci_server, method, args, expected_command):
    received = []
    fake_nci_server.handle("", lambda cmd: received.append(cmd) or None)
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])

    getattr(client, method)(*args)

    # Give the fake server a moment to process the datagram.
    import time
    time.sleep(0.05)
    assert received == [expected_command]

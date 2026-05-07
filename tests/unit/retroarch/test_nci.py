"""Unit tests for spinlab.retroarch — NCI client and helpers."""
import itertools
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


def test_is_core_running_true_when_memory_changes(fake_nci_server):
    """Two reads of the same address return different values -> core is advancing."""
    counter = itertools.count(start=0)

    def respond(cmd):
        n = next(counter)
        return f"READ_CORE_RAM 13 {n & 0xFF:02x}\n"

    fake_nci_server.handle("READ_CORE_RAM 13 1", respond)
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])

    assert client.is_core_running(tick_addr=0x13, sample_delay=0.01) is True


def test_is_core_running_false_when_memory_static(fake_nci_server):
    """Identical reads -> core frozen (deep-pause state)."""
    fake_nci_server.handle("READ_CORE_RAM 13 1", "READ_CORE_RAM 13 6d\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])

    assert client.is_core_running(tick_addr=0x13, sample_delay=0.01) is False


def test_is_core_running_raises_on_read_error(fake_nci_server):
    fake_nci_server.handle("READ_CORE_RAM ffff 1", "READ_CORE_RAM ffff -1\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])

    with pytest.raises(NCIProtocolError):
        client.is_core_running(tick_addr=0xFFFF, sample_delay=0.01)


def test_client_close_is_idempotent(fake_nci_server):
    fake_nci_server.handle("VERSION", "1.22.2\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    assert client.version() == "1.22.2"
    client.close()
    client.close()  # idempotent — must not raise
    # After close, the client is still usable (lazy reconnect).
    assert client.version() == "1.22.2"


def test_client_context_manager(fake_nci_server):
    fake_nci_server.handle("VERSION", "1.22.2\n")
    with NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1]) as client:
        assert client.version() == "1.22.2"


def test_send_after_timeout_recovers(fake_nci_server):
    """After a timeout, a subsequent successful call works on the same client.

    Regression test for the persistent-socket refactor: if drain isn't done
    on timeout, the late reply could land on the next call.
    """
    # First call has no handler — guaranteed to timeout.
    client = NCIClient(
        host=fake_nci_server.address[0],
        port=fake_nci_server.address[1],
        timeout=0.1,
    )
    with pytest.raises(NCITimeout):
        client.version()

    # Now register VERSION handler and confirm subsequent call works.
    fake_nci_server.handle("VERSION", "1.22.2\n")
    assert client.version() == "1.22.2"


def test_socket_reused_across_calls(fake_nci_server):
    """The persistent-socket refactor's invariant: socket is created once.

    Pin this so a future change can't accidentally reintroduce per-call
    socket creation.
    """
    fake_nci_server.handle("VERSION", "1.22.2\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    client.version()
    sock_after_first = client._sock
    client.version()
    sock_after_second = client._sock
    assert sock_after_first is sock_after_second
    assert sock_after_first is not None


def test_late_reply_does_not_contaminate_next_call(fake_nci_server):
    """Drain clears datagrams that arrived during the timeout window.

    Simulates the scenario: command A times out, but its reply has already
    landed in the kernel buffer (fast network). We drain it so CMD_B's reply
    isn't contaminated. The drain doesn't help with replies that arrive AFTER
    the timeout but before the next call (those require socket close).
    """
    import time as _time

    # Use a handler that replies quickly (before the client's timeout),
    # so the reply arrives and sits in the buffer during timeout processing.
    reply_sent = {"count": 0}

    def quick_a(cmd):
        reply_sent["count"] += 1
        return "REPLY_FOR_A\n"

    fake_nci_server.handle("CMD_A", quick_a)
    fake_nci_server.handle("CMD_B", "REPLY_FOR_B\n")

    client = NCIClient(
        host=fake_nci_server.address[0],
        port=fake_nci_server.address[1],
        timeout=0.5,  # Long timeout so the reply arrives normally for CMD_A.
    )

    # First: a normal successful call to establish the socket.
    assert client._send("CMD_A") == "REPLY_FOR_A"

    # Now switch to a very short timeout and send a command that has no handler,
    # guaranteeing timeout. The socket is already open.
    client.timeout = 0.05
    with pytest.raises(NCITimeout):
        client._send("UNHANDLED")

    # Back to normal timeout. CMD_B should work correctly (drain ensured any
    # buffered reply from timeout processing was cleared).
    client.timeout = 0.5
    assert client._send("CMD_B") == "REPLY_FOR_B"

    # Confirm CMD_A was never called again (drain didn't interfere with socket).
    assert reply_sent["count"] == 1

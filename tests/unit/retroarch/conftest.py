"""Fake NCI server fixture — a UDP responder thread for unit-testing the NCI client."""
from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

# Test commands either echo a scripted response or get explicitly ignored
# (timeout). Default behavior is "no handler -> drop the packet" so
# unhandled commands surface as NCITimeout, matching production.
ResponseFn = Callable[[str], str | None]

# Tight enough that stop() returns within ~50ms; loose enough to avoid busy-waiting.
_POLL_INTERVAL_S = 0.05
# UDP packets in NCI traffic are small (<200 bytes); 4 KB is generous headroom.
_RECV_BUF_BYTES = 4096
# stop() waits at most this long for the server thread to exit before asserting.
_JOIN_TIMEOUT_S = 1.0


@dataclass
class FakeNCIServer:
    address: tuple[str, int]
    _handlers: dict[str, ResponseFn] = field(default_factory=dict)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _sock: socket.socket | None = None

    def handle(self, command_prefix: str, response: ResponseFn | str) -> None:
        """Register a response for any incoming command starting with command_prefix.

        If response is a string, it's returned verbatim. If callable, it receives
        the full incoming command and returns the reply (or None to drop the packet).
        Most-specific (longest) prefix wins. Ties broken by registration
        order (first-registered wins).
        """
        if isinstance(response, str):
            literal = response
            response = lambda _cmd, _lit=literal: _lit  # noqa: E731
        self._handlers[command_prefix] = response

    def _serve(self) -> None:
        assert self._sock is not None
        self._sock.settimeout(_POLL_INTERVAL_S)
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(_RECV_BUF_BYTES)
            except socket.timeout:
                continue
            cmd = data.decode("ascii", errors="replace")
            # Longest-matching-prefix wins so "READ_CORE_RAM 94 4" can be
            # handled distinctly from "READ_CORE_MEMORY 94 4" if both registered.
            handler = None
            best_len = -1
            for prefix, fn in list(self._handlers.items()):
                if cmd.startswith(prefix) and len(prefix) > best_len:
                    handler = fn
                    best_len = len(prefix)
            if handler is None:
                continue  # drop -> client sees timeout
            reply = handler(cmd)
            if reply is None:
                continue
            self._sock.sendto(reply.encode("ascii"), addr)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=_JOIN_TIMEOUT_S)
            assert not self._thread.is_alive(), "fake NCI server thread did not exit"
        if self._sock is not None:
            self._sock.close()


@pytest.fixture
def fake_nci_server():
    """Spin up a fake NCI server on a random localhost UDP port for one test."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))  # ephemeral port
    server = FakeNCIServer(address=sock.getsockname())
    server._sock = sock
    server._thread = threading.Thread(target=server._serve, daemon=True)
    server._thread.start()
    try:
        yield server
    finally:
        server.stop()

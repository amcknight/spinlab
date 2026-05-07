"""Synchronous NCI client for RetroArch's Network Command Interface.

Uses UDP — RetroArch's NCI is fire-and-forget for most commands and one-shot
request/reply for memory reads. We keep the client sync; async callers wrap
calls in `asyncio.to_thread` if they need to.
"""
from __future__ import annotations

import socket

from spinlab.retroarch.exceptions import NCIProtocolError, NCITimeout

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 55355
DEFAULT_TIMEOUT_SEC = 0.5
# NCI replies are short — longest is READ_CORE_RAM returning a few KB of hex
# bytes plus the echoed command + address. 4 KB is generous headroom.
RECV_BUFFER_BYTES = 4096


class NCIClient:
    """Synchronous UDP client for RetroArch's Network Command Interface."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def _send(self, command: str) -> str:
        """Send command and return the reply text (whitespace-stripped).

        Raises NCITimeout if no reply arrives within self.timeout seconds.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        try:
            sock.sendto(command.encode("ascii"), (self.host, self.port))
            data, _ = sock.recvfrom(RECV_BUFFER_BYTES)
        except socket.timeout as exc:
            raise NCITimeout(f"no reply within {self.timeout}s for {command!r}") from exc
        finally:
            sock.close()
        return data.decode("ascii", errors="replace").strip()

    def _send_no_reply(self, command: str) -> None:
        """Send command and don't wait for any reply. For fire-and-forget commands
        like SAVE_STATE that simulate a hotkey press and return nothing.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(command.encode("ascii"), (self.host, self.port))
        finally:
            sock.close()

    def version(self) -> str:
        """Return RetroArch's reported version string (e.g. "1.22.2")."""
        return self._send("VERSION")

    def read_ram(self, addr: int, length: int) -> bytes:
        """Read `length` bytes from WRAM-flat offset `addr`.

        Reply format: "READ_CORE_RAM <addr_hex> <byte0> <byte1> ..." on success,
        or "READ_CORE_RAM <addr_hex> -1 [error]" on failure.

        Raises NCIProtocolError if the reply is malformed or contains -1.
        """
        reply = self._send(f"READ_CORE_RAM {addr:x} {length}")
        parts = reply.split()
        if len(parts) < 2:
            raise NCIProtocolError(f"reply too short: {reply!r}")
        # parts[0] = command echo, parts[1] = address echo, parts[2:] = data bytes.
        data_tokens = parts[2:]
        if not data_tokens:
            raise NCIProtocolError(f"reply has no data bytes: {reply!r}")
        if data_tokens[0] == "-1":
            raise NCIProtocolError(f"RetroArch returned error for read at {addr:#x}: {reply!r}")
        try:
            return bytes(int(t, 16) for t in data_tokens)
        except ValueError as exc:
            raise NCIProtocolError(f"unparseable reply: {reply!r}") from exc

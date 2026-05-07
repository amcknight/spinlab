"""Synchronous NCI client for RetroArch's Network Command Interface.

Uses UDP — RetroArch's NCI is fire-and-forget for most commands and one-shot
request/reply for memory reads. We keep the client sync; async callers wrap
calls in `asyncio.to_thread` if they need to.
"""
from __future__ import annotations

import socket

from spinlab.retroarch.exceptions import NCITimeout

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

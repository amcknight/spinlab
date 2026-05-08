"""Synchronous NCI client for RetroArch's Network Command Interface.

Uses UDP — RetroArch's NCI is fire-and-forget for most commands and one-shot
request/reply for memory reads. We keep the client sync; async callers wrap
calls in `asyncio.to_thread` if they need to.
"""
from __future__ import annotations

import socket
import time
from types import TracebackType
from typing import Self

from spinlab.retroarch.exceptions import NCIProtocolError, NCITimeout
from spinlab.retroarch.responses import StatusInfo

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
        self._sock: socket.socket | None = None

    def _get_socket(self) -> socket.socket:
        """Get or create the persistent UDP socket, lazily bound on first use.

        The socket is created once and reused across calls. After close() is
        called, the next _get_socket() creates a fresh socket (lazy reconnect).
        """
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Always set timeout before use, since it may have changed.
        self._sock.settimeout(self.timeout)
        return self._sock

    def _send(self, command: str) -> str:
        """Send command and return the reply text (whitespace-stripped).

        Raises NCITimeout if no reply arrives within self.timeout seconds.
        """
        sock = self._get_socket()
        try:
            sock.sendto(command.encode("ascii"), (self.host, self.port))
            data, _ = sock.recvfrom(RECV_BUFFER_BYTES)
        except (socket.timeout, ConnectionResetError) as exc:
            # socket.timeout: no reply arrived within self.timeout.
            # ConnectionResetError: Windows surfaces "ICMP port unreachable"
            # responses to UDP sends as 10054 on the next recvfrom. Treat both
            # as "no useful reply" and surface as NCITimeout.
            #
            # Drain any late reply that may arrive while we're handling this
            # timeout — otherwise it would be picked up by the next _send call
            # and silently misattributed to a different command.
            self._drain_socket(sock)
            raise NCITimeout(f"no reply within {self.timeout}s for {command!r}") from exc
        return data.decode("ascii", errors="replace").strip()

    def _drain_socket(self, sock: socket.socket) -> None:
        """Discard any datagrams sitting in the receive buffer.

        Called after a timeout so that a reply that arrived during the timeout
        interval cannot be misattributed to the next command issued on the same socket.
        """
        sock.setblocking(False)
        try:
            while True:
                try:
                    sock.recvfrom(RECV_BUFFER_BYTES)
                except (BlockingIOError, OSError):
                    break
        finally:
            sock.setblocking(True)
            sock.settimeout(self.timeout)

    def _send_no_reply(self, command: str) -> None:
        """Send command and don't wait for any reply. For fire-and-forget commands
        like SAVE_STATE that simulate a hotkey press and return nothing.
        """
        sock = self._get_socket()
        sock.sendto(command.encode("ascii"), (self.host, self.port))

    def version(self) -> str:
        """Return RetroArch's reported version string (e.g. "1.22.2")."""
        return self._send("VERSION")

    def get_status(self) -> StatusInfo:
        """Return parsed emulator state. Raises NCIProtocolError on malformed reply."""
        reply = self._send("GET_STATUS")
        # Format: "GET_STATUS <STATE> [<system>,<game>,crc32=<hex>]"
        parts = reply.split(maxsplit=2)
        if len(parts) < 2:
            raise NCIProtocolError(f"GET_STATUS reply too short: {reply!r}")
        state = parts[1]
        if len(parts) < 3:
            return StatusInfo(state=state)
        # Third field is comma-separated; last entry is "crc32=<hex>".
        bits = parts[2].split(",")
        if len(bits) != 3 or not bits[2].startswith("crc32="):
            raise NCIProtocolError(f"GET_STATUS metadata malformed: {reply!r}")
        return StatusInfo(
            state=state,
            system=bits[0],
            game=bits[1],
            crc32=bits[2].removeprefix("crc32="),
        )

    def get_config_param(self, key: str) -> str:
        """Read a runtime config param, e.g. 'movie_directory', 'savestate_directory'.

        Reply format: GET_CONFIG_PARAM <key> "<value>"

        Raises NCIProtocolError if the reply is malformed.
        """
        reply = self._send(f"GET_CONFIG_PARAM {key}")
        parts = reply.split(maxsplit=2)
        if len(parts) < 3:
            raise NCIProtocolError(f"GET_CONFIG_PARAM reply too short: {reply!r}")
        value = parts[2]
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        return value

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

    def write_ram(self, addr: int, data: bytes) -> None:
        """Write `data` to WRAM-flat offset `addr`. Returns nothing on success.

        Fire-and-forget: live-RA testing showed RetroArch parses WRITE_CORE_RAM
        and applies the write but does not emit a reply over the network port.
        Use is_core_running() or a follow-up read_ram() if you need confirmation
        the write actually landed.
        """
        hex_bytes = " ".join(f"{b:02x}" for b in data)
        self._send_no_reply(f"WRITE_CORE_RAM {addr:x} {hex_bytes}")

    def save_state(self) -> None:
        """Save state to RA's current slot. Increments state_slot if savestate_auto_index is on.

        SpinLab's slot strategy lives in Phase D's state_io module; this method
        is the raw command and does no slot management.
        """
        self._send_no_reply("SAVE_STATE")

    def load_state_slot(self, slot: int) -> None:
        """Load state from a specific slot, ignoring RA's current slot."""
        self._send_no_reply(f"LOAD_STATE_SLOT {slot}")

    def reset(self) -> None:
        """Hard-reset the emulated console."""
        self._send_no_reply("RESET")

    def pause_toggle(self) -> None:
        """Toggle paused state. WARNING: don't call blindly — see is_core_running.

        Phase 2 spike found a 'deep pause' state where PAUSE_TOGGLE could not
        recover the emulator core. Use is_core_running() to confirm state before
        toggling.
        """
        self._send_no_reply("PAUSE_TOGGLE")

    def frame_advance(self) -> None:
        """Advance one frame (only meaningful while paused)."""
        self._send_no_reply("FRAMEADVANCE")

    def bsv_record_toggle(self) -> None:
        """Toggle BSV (libretro deterministic movie) recording on/off.

        Fire-and-forget. RetroArch starts a new .bsv file in movie_directory on
        record-on and finalizes it on record-off. The exact filename is chosen
        by RA; use the recorder's mtime-baseline pattern to discover it.

        NOTE: Phase E smoke test confirms this command's wire format. If the
        command name is wrong on RA 1.22.2 the smoke test fails loudly and we
        investigate alternatives (BSV_RECORD_TOGGLE, hotkey_bsv_record, etc.).
        """
        self._send_no_reply("BSV_RECORD_TOGGLE")

    def bsv_play(self) -> None:
        """Start BSV playback of whatever movie RA currently has loaded.

        Fire-and-forget. Loading the .bsv file itself is out-of-band — typically
        via CLI flag at launch (--bsvplay) or via filesystem placement.

        NOTE: command name is provisional. Smoke test confirms.
        """
        self._send_no_reply("MOVIE_PLAYBACK_TOGGLE")

    def bsv_stop(self) -> None:
        """Stop BSV playback (toggle off)."""
        self._send_no_reply("MOVIE_PLAYBACK_TOGGLE")

    def quit(self) -> None:
        """Tell RetroArch to shut down."""
        self._send_no_reply("QUIT")

    def is_core_running(self, tick_addr: int, sample_delay: float = 0.05) -> bool:
        """Return True if the emulator core is advancing frames.

        Detects the spike-discovered deep-pause state where NCI stays responsive
        but the core thread is frozen — see docs/retroarch-migration/spike-log.md.

        Strategy: read a single byte at `tick_addr` twice with `sample_delay`
        between samples. If the bytes are identical, the core is not advancing.
        Caller picks `tick_addr` (typically a frame counter or fast-changing
        animation register).

        Note: a False result can occur transiently if `tick_addr` happens to
        wrap back to its previous value within `sample_delay`. For deep-pause
        detection, sample_delay should be significantly larger than one frame
        period (16.67ms) — default 0.05s ≈ 3 frames is comfortable.
        """
        a = self.read_ram(tick_addr, 1)
        time.sleep(sample_delay)
        b = self.read_ram(tick_addr, 1)
        return a != b

    def close(self) -> None:
        """Close the persistent socket, if it exists. Idempotent.

        After close(), the next _send/_send_no_reply will lazily create a
        fresh socket (lazy reconnect).
        """
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def __enter__(self) -> Self:
        """Context manager entry."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Context manager exit: close the socket."""
        self.close()

"""Synchronous NCI client for RetroArch's Network Command Interface.

Uses UDP — RetroArch's NCI is fire-and-forget for most commands and one-shot
request/reply for memory reads. We keep the client sync; async callers wrap
calls in `asyncio.to_thread` if they need to.
"""
from __future__ import annotations

import logging
import socket
from types import TracebackType
from typing import Self

from spinlab import log
from spinlab.retroarch.exceptions import NCIProtocolError, NCITimeout
from spinlab.retroarch.responses import StatusInfo

logger = logging.getLogger(__name__)

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
        drained = 0
        try:
            while True:
                try:
                    sock.recvfrom(RECV_BUFFER_BYTES)
                    drained += 1
                except (BlockingIOError, OSError):
                    break
        finally:
            sock.setblocking(True)
            sock.settimeout(self.timeout)
        if drained:
            log.warn(logger, "NCI: drained late datagrams", count=drained)

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
            log.warn(
                logger, "RA read_ram returned -1",
                addr=hex(addr), length=length, reply=reply,
            )
            raise NCIProtocolError(f"RetroArch returned error for read at {addr:#x}: {reply!r}")
        try:
            return bytes(int(t, 16) for t in data_tokens)
        except ValueError as exc:
            raise NCIProtocolError(f"unparseable reply: {reply!r}") from exc

    def write_ram(self, addr: int, data: bytes) -> None:
        """Write `data` to WRAM-flat offset `addr`. Fire-and-forget.

        RetroArch parses WRITE_CORE_RAM and applies the write but does not
        emit a reply. Follow up with read_ram() if confirmation is needed.
        """
        hex_bytes = " ".join(f"{b:02x}" for b in data)
        self._send_no_reply(f"WRITE_CORE_RAM {addr:x} {hex_bytes}")

    def save_state(self) -> None:
        """Save state to RA's current slot. Fire-and-forget.

        Slot management is RAClient's concern; this method is the raw command.
        """
        self._send_no_reply("SAVE_STATE")

    def load_state_slot(self, slot: int) -> None:
        """Load state from a specific slot, ignoring RA's current slot.

        RA echoes the command back as a UDP reply (e.g. "LOAD_STATE_SLOT 9998").
        Without draining it, the echo sits in the socket buffer and is picked
        up by the next ``_send`` call as if it were that command's reply,
        producing ``NCIProtocolError: reply has no data bytes`` on the next
        ``read_ram``. Drain explicitly after sending.
        """
        sock = self._get_socket()
        sock.sendto(f"LOAD_STATE_SLOT {slot}".encode("ascii"), (self.host, self.port))
        # Brief wait so RA has a chance to enqueue the reply before we drain.
        # If RA never replies on some build, the drain is a no-op.
        try:
            sock.settimeout(0.2)
            try:
                sock.recvfrom(RECV_BUFFER_BYTES)
            except (TimeoutError, OSError):
                pass
        finally:
            sock.settimeout(self.timeout)
        # Discard any additional datagrams (defence-in-depth).
        self._drain_socket(sock)

    def reset(self) -> None:
        """Hard-reset the emulated console."""
        self._send_no_reply("RESET")

    def pause_toggle(self) -> None:
        """Toggle paused state."""
        self._send_no_reply("PAUSE_TOGGLE")

    def fast_forward_toggle(self) -> None:
        """Toggle fast-forward state.

        RA runs the emulation as fast as the host CPU allows while
        fast-forward is on. Honoured during PLAY_REPLAY — used to drop the
        end-to-end replay-fixture test from real-time playback (~38s for a
        2273-frame movie at 60fps) to host-bound playback (~3-5s).

        Toggle semantics: every call flips the state. There's no NCI command
        to query the current state, so callers must track whether they
        toggled it on themselves and toggle it back off symmetrically.
        """
        self._send_no_reply("FAST_FORWARD")

    def frame_advance(self) -> None:
        """Advance one frame (only meaningful while paused)."""
        self._send_no_reply("FRAMEADVANCE")

    def record_replay(self) -> None:
        """Start movie recording. Fire-and-forget. Requires content in PLAYING state.

        RA 1.22.2 confirmed command name: RECORD_REPLAY. Writes a .replay<slot>
        file in <savestate_directory>/<core_name>/ when HALT_REPLAY is called.
        """
        self._send_no_reply("RECORD_REPLAY")

    def halt_replay(self) -> None:
        """Stop movie recording or playback. Fire-and-forget.

        RA 1.22.2 confirmed command name: HALT_REPLAY. Works as the universal
        halt for both the recorder and the player.
        """
        self._send_no_reply("HALT_REPLAY")

    def play_replay(self) -> None:
        """Start movie playback of the currently-loaded movie. Fire-and-forget.

        RA 1.22.2 confirmed command name: PLAY_REPLAY. Requires content in
        PLAYING state — RA diverges from the normal timeline and replays recorded
        input. Use halt_replay() to stop.
        """
        self._send_no_reply("PLAY_REPLAY")

    def replay_slot_minus(self) -> None:
        """Decrement RA's runtime replay slot by 1. Fire-and-forget.

        RA's `replay_auto_index = "true"` advances the runtime slot when
        recording, and persists per-game across sessions. At startup RA
        picks up wherever it left off (e.g. slot 64) regardless of cfg
        `replay_slot`. There is no NCI "set slot to N" command, so we
        navigate via SLOT_MINUS to converge to 0 — RA clamps at 0 so
        over-firing is safe.

        Command name is provisional. If RA 1.22.2 doesn't expose it,
        this is a no-op and PLAY_REPLAY's verification will catch the
        downstream failure.
        """
        self._send_no_reply("REPLAY_SLOT_MINUS")

    def replay_slot_plus(self) -> None:
        """Increment RA's runtime replay slot by 1. Fire-and-forget.
        See replay_slot_minus for the broader story.
        """
        self._send_no_reply("REPLAY_SLOT_PLUS")

    def quit(self) -> None:
        """Tell RetroArch to shut down."""
        self._send_no_reply("QUIT")

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

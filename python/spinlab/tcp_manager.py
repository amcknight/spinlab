"""Async TCP client for communicating with the Lua TCP server.

Uses a single reader coroutine that dispatches events to an asyncio.Queue.
This avoids the problem of multiple consumers competing for the same StreamReader.
Both reference capture and practice loop read from the same queue.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

from .protocol import serialize_command

logger = logging.getLogger(__name__)

_KNOWN_NON_JSON = {"pong", "heartbeat"}


class TcpManager:
    """Async wrapper around the Lua TCP socket with event dispatch."""

    def __init__(self, host: str = "127.0.0.1", port: int = 15482) -> None:
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self.on_disconnect: Callable | None = None  # callback when connection drops

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self, timeout: float = 5.0) -> bool:
        """Connect to Lua TCP server. Returns True on success."""
        try:
            self.events = asyncio.Queue()  # fresh queue bound to current loop
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=timeout,
            )
            logger.info("TCP connected to %s:%d", self.host, self.port)
            # Start the single reader coroutine
            self._read_task = asyncio.create_task(self._read_loop())
            return True
        except (OSError, asyncio.TimeoutError) as e:
            logger.debug("TCP connect failed: %s", e)
            self._reader = None
            self._writer = None
            return False

    async def disconnect(self) -> None:
        """Clean shutdown."""
        if self._read_task:
            self._read_task.cancel()
            self._read_task = None
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except OSError:
                pass
            self._writer = None
            self._reader = None
            logger.info("TCP disconnected")
        # Drain any remaining events
        while not self.events.empty():
            try:
                self.events.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def send(self, msg: str) -> None:
        """Send a newline-delimited message."""
        if not self._writer:
            raise ConnectionError("Not connected")
        self._writer.write((msg + "\n").encode("utf-8"))
        await self._writer.drain()

    async def send_command(self, cmd) -> None:
        """Send a typed protocol command (serialized to JSON)."""
        await self.send(serialize_command(cmd))

    async def recv_event(self, timeout: float | None = None) -> dict | None:
        """Wait for the next JSON event from the queue. Returns None on timeout."""
        try:
            if timeout is not None:
                return await asyncio.wait_for(self.events.get(), timeout=timeout)
            return await self.events.get()
        except asyncio.TimeoutError:
            return None

    async def _read_loop(self) -> None:
        """Single reader coroutine: reads lines, parses JSON, puts on queue."""
        if not self._reader:
            return
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    logger.info("TCP: connection closed by remote")
                    break
                text = line.decode("utf-8").strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                    await self.events.put(event)
                except json.JSONDecodeError:
                    if text.startswith("ok:") or text.startswith("err:"):
                        logger.debug("TCP response: %s", text)
                    elif text in _KNOWN_NON_JSON:
                        logger.debug("TCP non-JSON (expected): %s", text)
                    else:
                        logger.warning("Unexpected non-JSON from Lua: %r", text)
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            # Connection dropped — clean up
            self._writer = None
            self._reader = None
            if self.on_disconnect:
                self.on_disconnect()

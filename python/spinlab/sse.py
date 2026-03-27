"""SSE broadcaster — manages subscriber queues and push notifications."""
from __future__ import annotations

import asyncio
from typing import Any, Callable


class SSEBroadcaster:
    """Manages SSE subscriber queues. Decoupled from what state is pushed."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue] = []

    def subscribe(self, maxsize: int = 16) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    @property
    def has_subscribers(self) -> bool:
        return len(self._subscribers) > 0

    async def broadcast(self, state: Any) -> None:
        """Push state snapshot to all subscribers, dropping stale ones."""
        if not self._subscribers:
            return
        dead: list[asyncio.Queue] = []
        for q in self._subscribers:
            try:
                q.put_nowait(state)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(state)
                except asyncio.QueueFull:
                    dead.append(q)
        for q in dead:
            self.unsubscribe(q)

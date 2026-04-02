"""Tests for SSE broadcaster."""
import asyncio

import pytest

from spinlab.sse import SSEBroadcaster


class TestSSEBroadcaster:
    def test_subscribe_and_unsubscribe(self):
        b = SSEBroadcaster()
        assert not b.has_subscribers
        q = b.subscribe()
        assert b.has_subscribers
        b.unsubscribe(q)
        assert not b.has_subscribers

    def test_unsubscribe_unknown_queue_is_noop(self):
        b = SSEBroadcaster()
        q = asyncio.Queue()
        b.unsubscribe(q)  # should not raise

    async def test_broadcast_reaches_all_subscribers(self):
        b = SSEBroadcaster()
        q1 = b.subscribe()
        q2 = b.subscribe()
        await b.broadcast({"mode": "idle"})
        assert q1.get_nowait() == {"mode": "idle"}
        assert q2.get_nowait() == {"mode": "idle"}

    async def test_broadcast_no_subscribers_is_noop(self):
        b = SSEBroadcaster()
        await b.broadcast({"mode": "idle"})  # should not raise

    async def test_full_queue_drops_old_message(self):
        b = SSEBroadcaster()
        q = b.subscribe(maxsize=2)
        await b.broadcast({"n": 1})
        await b.broadcast({"n": 2})
        # Queue is now full. Next broadcast should drop oldest.
        await b.broadcast({"n": 3})
        assert q.get_nowait() == {"n": 2}
        assert q.get_nowait() == {"n": 3}

    async def test_persistently_full_queue_gets_removed(self):
        b = SSEBroadcaster()
        q = b.subscribe(maxsize=1)
        await b.broadcast({"n": 1})
        # Queue is full with 1 item. Broadcast tries to drop+requeue.
        # This should succeed (drop old, put new).
        await b.broadcast({"n": 2})
        assert b.has_subscribers
        assert q.get_nowait() == {"n": 2}

    async def test_multiple_broadcasts_sequential(self):
        b = SSEBroadcaster()
        q = b.subscribe(maxsize=16)
        for i in range(5):
            await b.broadcast({"n": i})
        results = [q.get_nowait() for _ in range(5)]
        assert [r["n"] for r in results] == [0, 1, 2, 3, 4]

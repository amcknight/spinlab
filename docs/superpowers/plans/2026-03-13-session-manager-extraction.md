# Session Manager Extraction & Polish — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract mutable state from `create_app()` into a testable `SessionManager` class, unify the event loop, add SSE push updates, restructure JavaScript into modules, and clean up Lua/Kalman/AHK code.

**Architecture:** A new `SessionManager` class owns all mutable session state (mode, game context, scheduler, practice session, reference capture). The dashboard's two competing background tasks merge into one event loop that feeds events to `SessionManager.route_event()`. SSE replaces polling as the primary update mechanism. Practice sessions receive results via `asyncio.Event` instead of polling the TCP queue.

**Tech Stack:** Python 3.11+ (FastAPI, asyncio), Lua (Mesen2 LuaSocket), vanilla JS (ES modules, EventSource), AutoHotkey v2.0

**Spec:** `docs/superpowers/specs/2026-03-13-session-manager-extraction-design.md`

---

## Chunk 1: SessionManager Core + Event Routing

### Task 1: Scaffold SessionManager with mode transitions

**Files:**
- Create: `python/spinlab/session_manager.py`
- Create: `tests/test_session_manager.py`

- [ ] **Step 1: Write failing test for SessionManager construction and initial state**

```python
# tests/test_session_manager.py
"""Tests for SessionManager state machine."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from spinlab.session_manager import SessionManager


def make_mock_tcp():
    tcp = MagicMock()
    tcp.is_connected = True
    tcp.send = AsyncMock()
    tcp.recv_event = AsyncMock(return_value=None)
    return tcp


def make_mock_db():
    db = MagicMock()
    db.upsert_game = MagicMock()
    db.create_session = MagicMock()
    db.end_session = MagicMock()
    db.create_capture_run = MagicMock()
    db.set_active_capture_run = MagicMock()
    db.get_recent_attempts = MagicMock(return_value=[])
    db.get_all_splits_with_model = MagicMock(return_value=[])
    db.load_model_state = MagicMock(return_value=None)
    db.load_allocator_config = MagicMock(return_value=None)
    db.upsert_split = MagicMock()
    return db


class TestSessionManagerInit:
    def test_initial_state(self):
        sm = SessionManager(
            db=make_mock_db(),
            tcp=make_mock_tcp(),
            rom_dir=None,
            default_category="any%",
        )
        assert sm.mode == "idle"
        assert sm.game_id is None
        assert sm.game_name is None
        assert sm.scheduler is None
        assert sm.practice_session is None
        assert sm.practice_task is None

    def test_get_state_no_game(self):
        sm = SessionManager(
            db=make_mock_db(),
            tcp=make_mock_tcp(),
            rom_dir=None,
            default_category="any%",
        )
        state = sm.get_state()
        assert state["mode"] == "idle"
        assert state["game_id"] is None
        assert state["tcp_connected"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_session_manager.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'spinlab.session_manager'`

- [ ] **Step 3: Implement SessionManager skeleton**

```python
# python/spinlab/session_manager.py
"""SessionManager — owns all mutable session state for the dashboard."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import Database
    from .tcp_manager import TcpManager

logger = logging.getLogger(__name__)


class SessionManager:
    """Central state owner for the SpinLab dashboard.

    Replaces closure-scoped mutable containers in create_app().
    """

    def __init__(
        self,
        db: "Database",
        tcp: "TcpManager",
        rom_dir: Path | None,
        default_category: str = "any%",
    ) -> None:
        self.db = db
        self.tcp = tcp
        self.rom_dir = rom_dir
        self.default_category = default_category

        # Session state
        self.mode: str = "idle"  # "idle" | "reference" | "practice"
        self.game_id: str | None = None
        self.game_name: str | None = None
        self.scheduler = None  # Scheduler | None, lazy-init
        self.practice_session = None  # PracticeSession | None
        self.practice_task: asyncio.Task | None = None

        # Reference capture state
        self.ref_pending: dict[tuple, dict] = {}
        self.ref_splits_count: int = 0
        self.ref_capture_run_id: str | None = None

        # SSE subscribers
        self._sse_subscribers: list[asyncio.Queue] = []

    def get_state(self) -> dict:
        """Full state snapshot for API and SSE."""
        base = {
            "mode": self.mode,
            "tcp_connected": self.tcp.is_connected,
            "game_id": self.game_id,
            "game_name": self.game_name,
            "current_split": None,
            "queue": [],
            "recent": [],
            "session": None,
            "sections_captured": self.ref_splits_count,
            "allocator": None,
            "estimator": None,
        }

        if self.game_id is None:
            return base

        sched = self._get_scheduler()
        base["allocator"] = sched.allocator.name
        base["estimator"] = sched.estimator.name

        if self.mode == "practice" and self.practice_session:
            ps = self.practice_session
            base["session"] = {
                "id": ps.session_id,
                "started_at": ps.started_at,
                "splits_attempted": ps.splits_attempted,
                "splits_completed": ps.splits_completed,
            }
            if ps.current_split_id:
                splits = self.db.get_all_splits_with_model(self.game_id)
                split_map = {s["id"]: s for s in splits}
                if ps.current_split_id in split_map:
                    current_split = split_map[ps.current_split_id]
                    current_split["attempt_count"] = self.db.get_split_attempt_count(
                        ps.current_split_id, ps.session_id
                    )
                    model_row = self.db.load_model_state(ps.current_split_id)
                    if model_row and model_row["state_json"]:
                        from spinlab.estimators.kalman import KalmanState
                        from spinlab.estimators import get_estimator
                        state = KalmanState.from_dict(json.loads(model_row["state_json"]))
                        est = get_estimator(model_row["estimator"])
                        current_split["drift_info"] = est.drift_info(state)
                    base["current_split"] = current_split

            queue_ids = sched.peek_next_n(3)
            if ps.current_split_id:
                queue_ids = [q for q in queue_ids if q != ps.current_split_id][:2]
            splits_all = self.db.get_all_splits_with_model(self.game_id)
            smap = {s["id"]: s for s in splits_all}
            base["queue"] = [smap[sid] for sid in queue_ids if sid in smap]

        base["recent"] = self.db.get_recent_attempts(self.game_id, limit=8)
        return base

    def _get_scheduler(self):
        """Lazy-init scheduler for current game."""
        if self.scheduler is None:
            from spinlab.scheduler import Scheduler
            self.scheduler = Scheduler(self.db, self._require_game())
        return self.scheduler

    def _require_game(self) -> str:
        """Return current game_id or raise."""
        if self.game_id is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=409, detail="No game loaded")
        return self.game_id

    def _clear_ref_state(self) -> None:
        """Clear reference capture state."""
        self.ref_pending.clear()
        self.ref_splits_count = 0
        self.ref_capture_run_id = None
        self.mode = "idle"

    async def switch_game(self, game_id: str, game_name: str) -> None:
        """Switch active game context. Stops any active session first."""
        if self.game_id == game_id:
            return

        if self.practice_session and self.practice_session.is_running:
            self.practice_session.is_running = False

        self._clear_ref_state()
        self.db.upsert_game(game_id, game_name, self.default_category)
        self.game_id = game_id
        self.game_name = game_name
        self.scheduler = None
        self.mode = "idle"
        await self._notify_sse()

    # --- SSE ---
    def subscribe_sse(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=16)
        self._sse_subscribers.append(q)
        return q

    def unsubscribe_sse(self, queue: asyncio.Queue) -> None:
        try:
            self._sse_subscribers.remove(queue)
        except ValueError:
            pass

    async def _notify_sse(self) -> None:
        """Push current state to all SSE subscribers."""
        if not self._sse_subscribers:
            return
        state = self.get_state()
        dead: list[asyncio.Queue] = []
        for q in self._sse_subscribers:
            try:
                q.put_nowait(state)
            except asyncio.QueueFull:
                # Drop oldest, push new
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(state)
                except asyncio.QueueFull:
                    dead.append(q)
        for q in dead:
            self.unsubscribe_sse(q)

    async def shutdown(self) -> None:
        """Clean shutdown: stop sessions, close TCP."""
        await self.stop_practice()
        if self.mode == "reference":
            self._clear_ref_state()
        await self.tcp.disconnect()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_session_manager.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/session_manager.py tests/test_session_manager.py
git commit -m "feat: scaffold SessionManager with state ownership and get_state()"
```

### Task 2: Add event routing to SessionManager

**Files:**
- Modify: `python/spinlab/session_manager.py`
- Modify: `tests/test_session_manager.py`

- [ ] **Step 1: Write failing tests for route_event**

```python
# Add to tests/test_session_manager.py

class TestRouteEvent:
    @pytest.mark.asyncio
    async def test_rom_info_discovers_game(self, tmp_path):
        """rom_info event triggers game discovery via checksum."""
        rom_file = tmp_path / "test_hack.sfc"
        rom_file.write_bytes(b"\x00" * 1024)  # dummy ROM

        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%")

        await sm.route_event({"event": "rom_info", "filename": "test_hack.sfc"})

        assert sm.game_id is not None
        assert sm.game_name is not None
        db.upsert_game.assert_called_once()
        tcp.send.assert_called_once()  # game_context sent back

    @pytest.mark.asyncio
    async def test_rom_info_no_rom_dir(self):
        """rom_info with no rom_dir uses fallback ID."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")

        await sm.route_event({"event": "rom_info", "filename": "test.sfc"})
        # No rom_dir → no game discovery
        assert sm.game_id is None

    @pytest.mark.asyncio
    async def test_game_context_switches_game(self):
        """game_context event triggers switch_game."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")

        await sm.route_event({
            "event": "game_context",
            "game_id": "abc123",
            "game_name": "Test Game",
        })

        assert sm.game_id == "abc123"
        assert sm.game_name == "Test Game"

    @pytest.mark.asyncio
    async def test_level_entrance_in_reference_mode(self):
        """level_entrance buffered during reference mode."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.mode = "reference"
        sm.ref_capture_run_id = "run1"

        await sm.route_event({
            "event": "level_entrance",
            "level": 105,
            "room": 0,
            "state_path": "/path/to/state.mss",
        })

        assert (105, 0) in sm.ref_pending

    @pytest.mark.asyncio
    async def test_level_exit_pairs_with_entrance(self):
        """level_exit in reference mode pairs with pending entrance to create split."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.mode = "reference"
        sm.ref_capture_run_id = "run1"

        # Buffer entrance
        await sm.route_event({
            "event": "level_entrance",
            "level": 105,
            "room": 0,
            "state_path": "/path/to/state.mss",
        })

        # Exit with goal
        await sm.route_event({
            "event": "level_exit",
            "level": 105,
            "room": 0,
            "goal": "normal",
            "elapsed_ms": 5000,
        })

        assert sm.ref_splits_count == 1
        db.upsert_split.assert_called_once()

    @pytest.mark.asyncio
    async def test_level_exit_abort_discards(self):
        """level_exit with goal=abort discards pending entrance."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.mode = "reference"

        await sm.route_event({
            "event": "level_entrance",
            "level": 105,
            "room": 0,
        })
        await sm.route_event({
            "event": "level_exit",
            "level": 105,
            "room": 0,
            "goal": "abort",
        })

        assert sm.ref_splits_count == 0
        db.upsert_split.assert_not_called()

    @pytest.mark.asyncio
    async def test_events_ignored_outside_reference(self):
        """level_entrance/exit ignored when not in reference mode."""
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.mode = "idle"

        await sm.route_event({"event": "level_entrance", "level": 1, "room": 0})
        await sm.route_event({"event": "level_exit", "level": 1, "room": 0, "goal": "normal"})

        assert len(sm.ref_pending) == 0
        assert sm.ref_splits_count == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_session_manager.py::TestRouteEvent -v`
Expected: FAIL — `route_event` not defined

- [ ] **Step 3: Implement route_event**

Add to `SessionManager` in `python/spinlab/session_manager.py`:

```python
    async def route_event(self, event: dict) -> None:
        """Single entry point for all TCP events. Routes by type."""
        evt_type = event.get("event")

        if evt_type == "rom_info":
            await self._handle_rom_info(event)
            return

        if evt_type == "game_context":
            gid = event.get("game_id")
            gname = event.get("game_name", gid or "unknown")
            if gid:
                await self.switch_game(gid, gname)
            return

        if evt_type == "level_entrance" and self.mode == "reference":
            key = (event["level"], event["room"])
            self.ref_pending[key] = event
            await self._notify_sse()
            return

        if evt_type == "level_exit" and self.mode == "reference":
            await self._handle_ref_exit(event)
            return

        if evt_type == "attempt_result" and self.mode == "practice":
            if self.practice_session:
                self.practice_session.receive_result(event)
            await self._notify_sse()
            return

    async def _handle_rom_info(self, event: dict) -> None:
        """Auto-discover game from ROM filename."""
        filename = event.get("filename", "")
        if not self.rom_dir or not filename:
            return

        rom_path = self.rom_dir / filename
        if rom_path.exists():
            from spinlab.romid import rom_checksum, game_name_from_filename
            checksum = rom_checksum(rom_path)
            name = game_name_from_filename(filename)
        else:
            from spinlab.romid import game_name_from_filename
            name = game_name_from_filename(filename)
            checksum = f"file_{name.lower().replace(' ', '_')}"
            logger.warning("ROM not found in rom_dir: %s — using filename as ID", filename)

        await self.switch_game(checksum, name)
        await self.tcp.send(json.dumps({
            "event": "game_context",
            "game_id": checksum,
            "game_name": name,
        }))

    async def _handle_ref_exit(self, event: dict) -> None:
        """Pair level_exit with pending entrance to create a split."""
        key = (event["level"], event["room"])
        goal = event.get("goal", "abort")

        if goal == "abort":
            self.ref_pending.pop(key, None)
            return

        entrance = self.ref_pending.pop(key, None)
        if not entrance:
            return

        self.ref_splits_count += 1
        from .models import Split
        gid = self._require_game()
        split_id = Split.make_id(gid, entrance["level"], entrance["room"], goal)
        split = Split(
            id=split_id,
            game_id=gid,
            level_number=entrance["level"],
            room_id=entrance["room"],
            goal=goal,
            state_path=entrance.get("state_path"),
            reference_time_ms=event.get("elapsed_ms"),
            ordinal=self.ref_splits_count,
            reference_id=self.ref_capture_run_id,
        )
        self.db.upsert_split(split)
        await self._notify_sse()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_session_manager.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/session_manager.py tests/test_session_manager.py
git commit -m "feat: add route_event to SessionManager with rom_info, reference, and practice routing"
```

### Task 3: Add start/stop reference and practice to SessionManager

**Files:**
- Modify: `python/spinlab/session_manager.py`
- Modify: `tests/test_session_manager.py`

- [ ] **Step 1: Write failing tests for start/stop reference and practice**

```python
# Add to tests/test_session_manager.py

class TestReferenceMode:
    @pytest.mark.asyncio
    async def test_start_reference(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"

        result = await sm.start_reference()

        assert result["status"] == "started"
        assert sm.mode == "reference"
        assert sm.ref_capture_run_id is not None
        db.create_capture_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_reference_no_game(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")

        with pytest.raises(Exception):  # HTTPException
            await sm.start_reference()

    @pytest.mark.asyncio
    async def test_start_reference_during_practice(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        sm.mode = "practice"

        result = await sm.start_reference()
        assert result["status"] == "practice_active"

    @pytest.mark.asyncio
    async def test_start_reference_not_connected(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        tcp.is_connected = False
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"

        result = await sm.start_reference()
        assert result["status"] == "not_connected"

    @pytest.mark.asyncio
    async def test_stop_reference(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        await sm.start_reference()

        result = await sm.stop_reference()
        assert result["status"] == "stopped"
        assert sm.mode == "idle"


class TestPracticeMode:
    @pytest.mark.asyncio
    async def test_start_practice(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"

        result = await sm.start_practice()
        assert result["status"] == "started"
        assert sm.mode == "practice"
        assert sm.practice_session is not None

    @pytest.mark.asyncio
    async def test_stop_practice(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"
        await sm.start_practice()

        result = await sm.stop_practice()
        assert result["status"] == "stopped"
        assert sm.mode == "idle"

    @pytest.mark.asyncio
    async def test_start_practice_not_connected(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        tcp.is_connected = False
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")
        sm.game_id = "game1"

        result = await sm.start_practice()
        assert result["status"] == "not_connected"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_session_manager.py::TestReferenceMode tests/test_session_manager.py::TestPracticeMode -v`
Expected: FAIL

- [ ] **Step 3: Implement start/stop methods**

Add to `SessionManager` in `python/spinlab/session_manager.py`:

```python
    async def start_reference(self, run_name: str | None = None) -> dict:
        """Begin reference capture."""
        if self.mode == "practice":
            return {"status": "practice_active"}
        if not self.tcp.is_connected:
            return {"status": "not_connected"}
        gid = self._require_game()
        self._clear_ref_state()
        run_id = f"live_{uuid.uuid4().hex[:8]}"
        name = run_name or f"Live {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')}"
        self.db.create_capture_run(run_id, gid, name)
        self.db.set_active_capture_run(run_id)
        self.ref_capture_run_id = run_id
        self.mode = "reference"
        await self._notify_sse()
        return {"status": "started", "run_id": run_id, "run_name": name}

    async def stop_reference(self) -> dict:
        """End reference capture."""
        if self.mode != "reference":
            return {"status": "not_in_reference"}
        self._clear_ref_state()
        await self._notify_sse()
        return {"status": "stopped"}

    async def start_practice(self) -> dict:
        """Begin practice session."""
        if self.practice_session and self.practice_session.is_running:
            return {"status": "already_running"}
        if not self.tcp.is_connected:
            return {"status": "not_connected"}
        if self.mode == "reference":
            self._clear_ref_state()

        from .practice import PracticeSession
        ps = PracticeSession(tcp=self.tcp, db=self.db, game_id=self._require_game())
        self.practice_session = ps
        self.practice_task = asyncio.create_task(ps.run_loop())
        self.practice_task.add_done_callback(self._on_practice_done)
        self.mode = "practice"
        await self._notify_sse()
        return {"status": "started", "session_id": ps.session_id}

    def _on_practice_done(self, task: asyncio.Task) -> None:
        """Callback when practice task finishes."""
        if self.mode == "practice":
            self.mode = "idle"

    async def stop_practice(self) -> dict:
        """Stop practice session."""
        if self.practice_session and self.practice_session.is_running:
            self.practice_session.is_running = False
            if self.practice_task:
                try:
                    await asyncio.wait_for(self.practice_task, timeout=5)
                except asyncio.TimeoutError:
                    self.practice_task.cancel()
            self.mode = "idle"
            await self._notify_sse()
            return {"status": "stopped"}
        if self.mode == "practice":
            self.mode = "idle"
            return {"status": "stopped"}
        return {"status": "not_running"}

    def on_disconnect(self) -> None:
        """Handle TCP disconnect: stop practice, clear ref state."""
        if self.practice_session and self.practice_session.is_running:
            self.practice_session.is_running = False
        self._clear_ref_state()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_session_manager.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/session_manager.py tests/test_session_manager.py
git commit -m "feat: add start/stop reference and practice to SessionManager"
```

### Task 4: Add SSE support to SessionManager

**Files:**
- Modify: `tests/test_session_manager.py`

- [ ] **Step 1: Write failing tests for SSE subscribe/notify**

```python
# Add to tests/test_session_manager.py

class TestSSE:
    @pytest.mark.asyncio
    async def test_subscribe_receives_notifications(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")

        q = sm.subscribe_sse()
        await sm._notify_sse()

        msg = q.get_nowait()
        assert msg["mode"] == "idle"

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_notifications(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")

        q = sm.subscribe_sse()
        sm.unsubscribe_sse(q)
        await sm._notify_sse()

        assert q.empty()

    @pytest.mark.asyncio
    async def test_full_queue_drops_oldest(self):
        db = make_mock_db()
        tcp = make_mock_tcp()
        sm = SessionManager(db=db, tcp=tcp, rom_dir=None, default_category="any%")

        q = sm.subscribe_sse()
        # Fill queue
        for _ in range(16):
            await sm._notify_sse()
        # Should still accept new
        await sm._notify_sse()
        assert not q.empty()
```

- [ ] **Step 2: Run tests to verify they pass** (SSE was implemented in Task 1 skeleton)

Run: `python -m pytest tests/test_session_manager.py::TestSSE -v`
Expected: PASS (already implemented in skeleton)

- [ ] **Step 3: Commit**

```bash
git add tests/test_session_manager.py
git commit -m "test: add SSE subscribe/notify tests for SessionManager"
```

### Task 5: Wire SessionManager into dashboard, replace closures

**Files:**
- Modify: `python/spinlab/dashboard.py`
- Verify: existing tests still pass

This is the critical integration step. Replace all closure-scoped state (`_mode[0]`, `_scheduler[0]`, etc.) with `SessionManager` delegation.

- [ ] **Step 1: Rewrite create_app to use SessionManager**

Replace the closure state, helper functions, event dispatch loop, reconnect loop, and all endpoints in `dashboard.py` to delegate to `SessionManager`. The endpoints become thin wrappers:

Key changes:
1. Remove all `_mode`, `_scheduler`, `_practice`, `_game_id`, `_game_name` closure containers
2. Create `SessionManager` instance in `create_app()`
3. Replace `_reconnect_loop` + `_event_dispatch_loop` with single `_event_loop`
4. Replace `_switch_game`, `_clear_ref_state`, `_require_game`, `_get_scheduler`, `_current_mode` with `session.*` calls
5. Endpoints delegate to `session.start_reference()`, `session.stop_practice()`, etc.
6. Add `GET /api/events` SSE endpoint
7. Add `POST /api/shutdown` endpoint
8. Expose `session` on `app.state` for testing

```python
# The unified event loop (replaces _reconnect_loop + _event_dispatch_loop):
async def _event_loop(session: SessionManager, tcp: TcpManager):
    while True:
        if not tcp.is_connected:
            await tcp.connect(timeout=2)
            if not tcp.is_connected:
                await asyncio.sleep(2)
                continue
        try:
            event = await tcp.recv_event(timeout=1.0)
            if event:
                await session.route_event(event)
        except Exception:
            logger.exception("Error in event loop")
            await asyncio.sleep(1)
```

```python
# SSE endpoint:
@app.get("/api/events")
async def sse_events():
    from starlette.responses import StreamingResponse

    queue = session.subscribe_sse()

    async def event_stream():
        try:
            while True:
                try:
                    state = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {json.dumps(state)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            session.unsubscribe_sse(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

```python
# Shutdown endpoint:
@app.post("/api/shutdown")
async def api_shutdown():
    await session.shutdown()
    # Signal uvicorn to exit
    import signal
    try:
        signal.raise_signal(signal.SIGINT)
    except (OSError, AttributeError):
        pass
    return {"status": "shutting_down"}
```

- [ ] **Step 2: Update app.state test exposure**

Replace all `app.state._mode`, `app.state._practice`, etc. with `app.state.session`:

```python
app.state.session = session
app.state.tcp = tcp  # keep for TCP-level test access
```

- [ ] **Step 3: Run existing dashboard tests**

Run: `python -m pytest tests/test_dashboard.py -v`
Expected: Some tests will need updates to use `app.state.session` instead of `app.state._mode[0]` etc. Fix test helpers that directly mutate closure state.

- [ ] **Step 4: Fix test helpers to use SessionManager**

In test files, replace patterns like:
- `app.state._mode[0] = "idle"` → `app.state.session.mode = "idle"`
- `app.state._game_id[0] = "test"` → `app.state.session.game_id = "test"`
- `app.state._practice[0]` → `app.state.session.practice_session`
- `app.state._scheduler[0]` → `app.state.session.scheduler`
- `app.state._switch_game(...)` → `asyncio.get_event_loop().run_until_complete(app.state.session.switch_game(...))`

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -v --timeout=30`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/dashboard.py python/spinlab/session_manager.py
git commit -m "refactor: wire SessionManager into dashboard, replace closure state"
```

### Task 6: Fix all test files to use SessionManager API

**Files:**
- Modify: `tests/test_dashboard.py`
- Modify: `tests/test_dashboard_integration.py`
- Modify: `tests/test_dashboard_references.py`
- Modify: `tests/test_multi_game.py`
- Modify: `tests/test_db_dashboard.py`

- [ ] **Step 1: Search-and-replace closure patterns across all test files**

Use editor find/replace across all test files. Key patterns:
- `app.state._mode[0]` → `app.state.session.mode`
- `app.state._game_id[0]` → `app.state.session.game_id`
- `app.state._game_name[0]` → `app.state.session.game_name`
- `app.state._scheduler[0]` → `app.state.session.scheduler`
- `app.state._practice[0]` → `app.state.session.practice_session`
- `app.state._practice_task[0]` → `app.state.session.practice_task`
- `app.state._switch_game(id, name, cat)` → `await app.state.session.switch_game(id, name)` (now async, category param dropped — uses `default_category` from config). Tests calling `_switch_game` must become `@pytest.mark.asyncio` and `await` the call.

- [ ] **Step 2: Run full test suite**

Run: `python -m pytest tests/ -v --timeout=30`
Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add tests/
git commit -m "refactor: update all test files to use SessionManager API"
```

---

## Chunk 2: Practice Session Result Delivery + Scheduler Filter

### Task 7: Add receive_result to PracticeSession (event-driven instead of queue polling)

**Files:**
- Modify: `python/spinlab/practice.py`
- Modify: `tests/test_practice.py`

- [ ] **Step 1: Write failing test for receive_result**

```python
# Add to tests/test_practice.py

class TestReceiveResult:
    @pytest.mark.asyncio
    async def test_receive_result_unblocks_run_one(self):
        """run_one awaits asyncio.Event, receive_result sets it."""
        from spinlab.practice import PracticeSession
        from unittest.mock import MagicMock, AsyncMock

        tcp = MagicMock()
        tcp.is_connected = True
        tcp.send = AsyncMock()
        db = MagicMock()
        db.create_session = MagicMock()
        db.end_session = MagicMock()
        db.log_attempt = MagicMock()
        db.load_allocator_config = MagicMock(return_value=None)
        db.get_all_splits_with_model = MagicMock(return_value=[])
        db.load_model_state = MagicMock(return_value=None)
        db.save_model_state = MagicMock()

        ps = PracticeSession(tcp=tcp, db=db, game_id="test")
        ps.is_running = True

        # Simulate scheduler returning a split
        mock_split = MagicMock()
        mock_split.split_id = "s1"
        mock_split.state_path = "/tmp/test.mss"
        mock_split.goal = "normal"
        mock_split.description = "Test"
        mock_split.reference_time_ms = 5000
        mock_split.estimator_state = None

        ps.scheduler.pick_next = MagicMock(return_value=mock_split)
        ps.scheduler.peek_next_n = MagicMock(return_value=[])
        ps.scheduler.process_attempt = MagicMock()

        # Schedule receive_result after a short delay
        async def deliver_result():
            await asyncio.sleep(0.1)
            ps.receive_result({
                "event": "attempt_result",
                "split_id": "s1",
                "completed": True,
                "time_ms": 4500,
                "goal": "normal",
            })

        asyncio.create_task(deliver_result())
        result = await ps.run_one()

        assert result is True
        assert ps.splits_completed == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_practice.py::TestReceiveResult -v`
Expected: FAIL — `receive_result` not defined

- [ ] **Step 3: Implement receive_result and refactor run_one**

Modify `python/spinlab/practice.py`:

Add to `PracticeSession.__init__`:
```python
        self._result_event = asyncio.Event()
        self._result_data: dict | None = None
```

Add new method:
```python
    def receive_result(self, event: dict) -> None:
        """Called by SessionManager.route_event when attempt_result arrives."""
        self._result_data = event
        self._result_event.set()
```

Modify `run_one` — replace only the queue-polling while loop (the part after `await self.tcp.send(...)`) with event-based waiting. **Keep the retry/skip loop for now** (Task 8 removes it after adding the scheduler filter):

```python
        # Wait for attempt_result via receive_result() (set by SessionManager)
        self._result_event.clear()
        self._result_data = None

        while self.is_running and self.tcp.is_connected:
            try:
                await asyncio.wait_for(self._result_event.wait(), timeout=1.0)
                break
            except asyncio.TimeoutError:
                continue

        if self._result_data and self._result_data.get("event") == "attempt_result":
            self._process_result(self._result_data, cmd)
```

**Important:** Keep the `os` import, the `_skipped` set, and the retry loop in `run_one` intact for now. Only the queue-polling (`await self.tcp.recv_event()`) is replaced with the event pattern. Task 8 removes the retry loop after adding the scheduler-level filter.

- [ ] **Step 4: Update existing test_practice tests**

The existing `test_practice_session_picks_and_sends` test uses `mock_tcp.recv_event` to deliver results. After the refactor, `run_one()` no longer calls `tcp.recv_event` — it uses `self._result_event.wait()`. Update any existing tests that mock `tcp.recv_event` for practice result delivery to instead call `ps.receive_result(event_dict)` (scheduled via `asyncio.create_task` with a short delay, same pattern as the new test).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_practice.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/practice.py tests/test_practice.py
git commit -m "refactor: PracticeSession uses receive_result event instead of queue polling"
```

### Task 8: Add state file filter to Scheduler

**Files:**
- Modify: `python/spinlab/scheduler.py:52-97` (`_load_splits_with_model`)
- Modify: `tests/test_scheduler_kalman.py`

- [ ] **Step 1: Write failing test for state file filter**

```python
# Add to tests/test_scheduler_kalman.py

class TestStateFileFilter:
    def test_pick_next_skips_missing_state_files(self, tmp_path):
        """pick_next only returns splits with existing state files."""
        from spinlab.db import Database
        from spinlab.scheduler import Scheduler
        from spinlab.models import Split

        db = Database(":memory:")

        # Create two splits: one with valid state file, one with missing
        valid_state = tmp_path / "valid.mss"
        valid_state.write_bytes(b"\x00" * 100)

        db.upsert_game("g1", "Test", "any%")
        db.upsert_split(Split(
            id="s1", game_id="g1", level_number=1, room_id=0,
            goal="normal", state_path=str(valid_state),
        ))
        db.upsert_split(Split(
            id="s2", game_id="g1", level_number=2, room_id=0,
            goal="normal", state_path="/nonexistent/path.mss",
        ))
        db.upsert_split(Split(
            id="s3", game_id="g1", level_number=3, room_id=0,
            goal="normal", state_path=None,
        ))

        sched = Scheduler(db, "g1")
        picked = sched.pick_next()

        assert picked is not None
        assert picked.split_id == "s1"

    def test_pick_next_returns_none_when_no_valid_files(self):
        """pick_next returns None when no splits have valid state files."""
        from spinlab.db import Database
        from spinlab.scheduler import Scheduler
        from spinlab.models import Split

        db = Database(":memory:")
        db.upsert_game("g1", "Test", "any%")
        db.upsert_split(Split(
            id="s1", game_id="g1", level_number=1, room_id=0,
            goal="normal", state_path="/nonexistent/path.mss",
        ))

        sched = Scheduler(db, "g1")
        picked = sched.pick_next()

        assert picked is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_scheduler_kalman.py::TestStateFileFilter -v`
Expected: FAIL — `s2` returned (no filtering yet)

- [ ] **Step 3: Add state file filter to _load_splits_with_model**

In `python/spinlab/scheduler.py`, add the filter in `pick_next()` and `peek_next_n()` — NOT in `_load_splits_with_model()` (which is also used by `get_all_model_states()` for the model tab, where we want to show all splits). Add after `splits = self._load_splits_with_model()` in both methods:

```python
        import os
        practicable = [
            s for s in splits
            if s.state_path and os.path.exists(s.state_path)
        ]
```

Then pass `practicable` (instead of `splits`) to `self.allocator.pick_next(practicable)` and `self.allocator.peek_next_n(practicable, n)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_scheduler_kalman.py::TestStateFileFilter -v`
Expected: PASS

- [ ] **Step 5: Simplify PracticeSession.run_one**

Remove the retry loop and `_skipped` set from `run_one()` in `python/spinlab/practice.py`. Replace with direct:

```python
    async def run_one(self) -> bool:
        """Run one pick-send-receive cycle. Returns False if no splits available."""
        picked = self.scheduler.pick_next()
        if picked is None:
            return False

        # ... rest unchanged (build SplitCommand, send, wait for result)
```

- [ ] **Step 6: Run full test suite**

Run: `python -m pytest tests/ -v --timeout=30`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/scheduler.py python/spinlab/practice.py tests/test_scheduler_kalman.py
git commit -m "feat: scheduler filters splits by state file existence, simplify practice retry"
```

---

## Chunk 3: JavaScript Restructure + SSE Client

### Task 9: Split app.js into ES modules

**Files:**
- Create: `python/spinlab/static/api.js`
- Create: `python/spinlab/static/format.js`
- Create: `python/spinlab/static/live.js`
- Create: `python/spinlab/static/model.js`
- Create: `python/spinlab/static/manage.js`
- Modify: `python/spinlab/static/app.js`
- Modify: `python/spinlab/static/index.html`

- [ ] **Step 1: Create format.js**

```javascript
// python/spinlab/static/format.js
export function splitName(s) {
  if (s.description) return s.description;
  let name = 'L' + (s.level_number != null ? s.level_number : '?');
  if (s.goal && s.goal !== 'normal') name += ' (' + s.goal + ')';
  return name;
}

export function formatTime(ms) {
  if (ms == null) return '\u2014';
  const s = ms / 1000;
  return s.toFixed(1) + 's';
}

export function elapsedStr(startedAt) {
  if (!startedAt) return '';
  const start = new Date(startedAt);
  if (!Number.isFinite(start.getTime())) return '0:00';
  const diff = Math.floor((Date.now() - start.getTime()) / 1000);
  const m = Math.floor(diff / 60);
  const s = diff % 60;
  return m + ':' + String(s).padStart(2, '0');
}
```

- [ ] **Step 2: Create api.js with SSE + fetchJSON**

```javascript
// python/spinlab/static/api.js
let toastTimer = null;

function showToast(msg) {
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('visible'), 3000);
}

export async function fetchJSON(url, opts = {}) {
  try {
    const res = await fetch(url, opts);
    return await res.json();
  } catch (e) {
    showToast('Request failed: ' + (e.message || url));
    return null;
  }
}

export async function postJSON(url, body = null) {
  const opts = { method: 'POST' };
  if (body !== null) {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify(body);
  }
  return fetchJSON(url, opts);
}

export function connectSSE(onMessage) {
  const es = new EventSource('/api/events');
  es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      onMessage(data);
    } catch (_) {}
  };
  es.onerror = () => {
    // EventSource auto-reconnects on transient failures.
    // Only fall back to polling on permanent close.
    if (es.readyState === EventSource.CLOSED) {
      startFallbackPoll(onMessage);
    }
  };
  return es;
}

let fallbackInterval = null;
function startFallbackPoll(onMessage) {
  if (fallbackInterval) return;
  fallbackInterval = setInterval(async () => {
    const data = await fetchJSON('/api/state');
    if (data) onMessage(data);
  }, 5000);
}
```

- [ ] **Step 3: Create live.js**

```javascript
// python/spinlab/static/live.js
import { splitName, formatTime, elapsedStr } from './format.js';

export function renderDisconnected() {
  hide('mode-idle', 'mode-reference', 'mode-practice');
  show('mode-disconnected');
}

export function renderIdle(data) {
  hide('mode-disconnected', 'mode-reference', 'mode-practice');
  show('mode-idle');
  updateGameName(data);

  // Disable "Start Practice" if no splits exist
  const btn = document.getElementById('btn-practice-start');
  if (btn) {
    const hasSplits = data.game_id != null;
    btn.disabled = !hasSplits;
    btn.title = hasSplits ? '' : 'No splits available — complete a reference run first';
  }
}

export function renderReference(data) {
  hide('mode-disconnected', 'mode-idle', 'mode-practice');
  show('mode-reference');
  updateGameName(data);
  document.getElementById('ref-sections').textContent =
    'Sections: ' + (data.sections_captured || 0);
}

export function renderPractice(data) {
  hide('mode-disconnected', 'mode-idle', 'mode-reference');
  show('mode-practice');
  updateGameName(data);

  const cs = data.current_split;
  if (cs) {
    document.getElementById('current-goal').textContent = splitName(cs);
    document.getElementById('current-attempts').textContent =
      'Attempt ' + (cs.attempt_count || 0);

    const insight = document.getElementById('insight');
    if (cs.drift_info) {
      const arrow = cs.drift_info.drift < 0 ? '\u2193' : cs.drift_info.drift > 0 ? '\u2191' : '\u2192';
      const rate = Math.abs(cs.drift_info.drift).toFixed(2);
      insight.innerHTML =
        '<span class="drift-' + cs.drift_info.label + '">' +
        arrow + ' ' + rate + ' s/run</span>' +
        ' <span class="dim">(' + cs.drift_info.confidence + ')</span>';
    } else {
      insight.textContent = 'No data yet';
    }
  }

  const queue = document.getElementById('queue');
  queue.innerHTML = '';
  (data.queue || []).forEach(q => {
    const li = document.createElement('li');
    li.textContent = splitName(q);
    queue.appendChild(li);
  });

  const recent = document.getElementById('recent');
  recent.innerHTML = '';
  (data.recent || []).forEach(r => {
    const li = document.createElement('li');
    const time = formatTime(r.time_ms);
    const refTime = r.reference_time_ms ? formatTime(r.reference_time_ms) : '\u2014';
    const cls = r.reference_time_ms && r.time_ms <= r.reference_time_ms ? 'ahead' : 'behind';
    li.innerHTML = '<span class="' + cls + '">' + time + '</span> / ' + refTime +
      ' <span class="dim">' + splitName(r) + '</span>';
    recent.appendChild(li);
  });

  const stats = document.getElementById('session-stats');
  if (data.session) {
    stats.textContent = (data.session.splits_completed || 0) + '/' +
      (data.session.splits_attempted || 0) + ' cleared | ' +
      elapsedStr(data.session.started_at);
  }

  if (data.allocator) {
    document.getElementById('allocator-select').value = data.allocator;
  }

  if (data.session && data.session.started_at) {
    document.getElementById('session-timer').textContent = elapsedStr(data.session.started_at);
  }
}

function updateGameName(data) {
  const el = document.getElementById('game-name');
  el.textContent = data.game_name || '';
}

function show(...ids) { ids.forEach(id => document.getElementById(id).style.display = 'block'); }
function hide(...ids) { ids.forEach(id => document.getElementById(id).style.display = 'none'); }
```

- [ ] **Step 4: Create model.js**

```javascript
// python/spinlab/static/model.js
import { splitName, formatTime } from './format.js';
import { fetchJSON, postJSON } from './api.js';

export async function fetchModel() {
  const data = await fetchJSON('/api/model');
  if (data) updateModel(data);
}

function updateModel(data) {
  const body = document.getElementById('model-body');
  if (!data.splits || !data.splits.length) {
    body.innerHTML = '<tr><td colspan="7" class="dim">No game loaded</td></tr>';
    return;
  }
  body.innerHTML = '';
  data.splits.forEach(s => {
    const tr = document.createElement('tr');
    const driftClass = s.drift_info?.label || 'flat';
    const arrow = s.drift !== null
      ? (s.drift < 0 ? '\u2193' : s.drift > 0 ? '\u2191' : '\u2192')
      : '\u2014';
    tr.className = 'drift-row-' + driftClass;
    let confCell = '\u2014';
    if (s.drift_info && s.drift_info.ci_lower != null) {
      const lo = s.drift_info.ci_lower.toFixed(2);
      const hi = s.drift_info.ci_upper.toFixed(2);
      confCell = '<span class="dim">[' + lo + ', ' + hi + ']</span>';
    }
    tr.innerHTML =
      '<td>' + splitName(s) + '</td>' +
      '<td>' + (s.mu !== null ? s.mu.toFixed(1) : '\u2014') + '</td>' +
      '<td class="drift-' + driftClass + '">' + arrow + ' ' +
        (s.drift !== null ? Math.abs(s.drift).toFixed(2) : '\u2014') + '</td>' +
      '<td>' + confCell + '</td>' +
      '<td>' + (s.marginal_return ? s.marginal_return.toFixed(4) : '\u2014') + '</td>' +
      '<td>' + s.n_completed + '</td>' +
      '<td>' + (s.gold_ms !== null ? formatTime(s.gold_ms) : '\u2014') + '</td>';
    body.appendChild(tr);
  });
  if (data.estimator) {
    document.getElementById('estimator-select').value = data.estimator;
  }
}

export function initModelTab() {
  document.getElementById('allocator-select').addEventListener('change', async (e) => {
    await postJSON('/api/allocator', { name: e.target.value });
  });
  document.getElementById('estimator-select').addEventListener('change', async (e) => {
    await postJSON('/api/estimator', { name: e.target.value });
    fetchModel();
  });
}
```

- [ ] **Step 5: Create manage.js**

```javascript
// python/spinlab/static/manage.js
import { splitName, formatTime } from './format.js';
import { fetchJSON, postJSON } from './api.js';

export async function fetchManage() {
  const refsData = await fetchJSON('/api/references');
  if (!refsData) return;
  const refs = refsData.references || [];
  if (!refs.length) {
    updateManage([], []);
    return;
  }
  const active = refs.find(r => r.active);
  let splits = [];
  if (active) {
    const splitsData = await fetchJSON('/api/references/' + active.id + '/splits');
    splits = splitsData?.splits || [];
  }
  updateManage(refs, splits);
}

function updateManage(refs, splits) {
  const sel = document.getElementById('ref-select');
  sel.innerHTML = '';
  if (!refs.length) {
    const opt = document.createElement('option');
    opt.textContent = 'No game loaded';
    opt.disabled = true;
    sel.appendChild(opt);
    document.getElementById('split-body').innerHTML = '';
    return;
  }
  refs.forEach(r => {
    const opt = document.createElement('option');
    opt.value = r.id;
    opt.textContent = r.name + (r.active ? ' \u25cf' : '');
    if (r.active) opt.selected = true;
    sel.appendChild(opt);
  });

  const body = document.getElementById('split-body');
  body.innerHTML = '';
  splits.forEach(s => {
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td><input class="split-name-input" value="' + (s.description || '') + '" ' +
        'data-id="' + s.id + '" data-field="description"></td>' +
      '<td>' + s.level_number + '</td>' +
      '<td>' + s.goal + '</td>' +
      '<td>' + (s.reference_time_ms ? formatTime(s.reference_time_ms) : '\u2014') + '</td>' +
      '<td><button class="btn-x" data-id="' + s.id + '">\u2715</button></td>';
    body.appendChild(tr);
  });
}

export function initManageTab() {
  document.getElementById('split-body').addEventListener('focusout', async (e) => {
    if (!e.target.classList.contains('split-name-input')) return;
    const id = e.target.dataset.id;
    const field = e.target.dataset.field;
    const value = e.target.value;
    await fetchJSON('/api/splits/' + id, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [field]: value }),
    });
  });

  document.getElementById('split-body').addEventListener('click', async (e) => {
    if (!e.target.classList.contains('btn-x')) return;
    if (!confirm('Remove this split?')) return;
    await fetchJSON('/api/splits/' + e.target.dataset.id, { method: 'DELETE' });
    fetchManage();
  });

  document.getElementById('ref-select').addEventListener('change', async (e) => {
    await postJSON('/api/references/' + e.target.value + '/activate');
    fetchManage();
  });

  document.getElementById('btn-ref-rename').addEventListener('click', async () => {
    const sel = document.getElementById('ref-select');
    const name = prompt('New name:', sel.options[sel.selectedIndex]?.text.replace(' \u25cf', ''));
    if (!name) return;
    await fetchJSON('/api/references/' + sel.value, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    fetchManage();
  });

  document.getElementById('btn-ref-delete').addEventListener('click', async () => {
    if (!confirm('Delete this reference and all its splits?')) return;
    const sel = document.getElementById('ref-select');
    await fetchJSON('/api/references/' + sel.value, { method: 'DELETE' });
    fetchManage();
  });

  document.getElementById('btn-reset').addEventListener('click', async () => {
    if (!confirm('Clear all session data? This cannot be undone.')) return;
    const data = await postJSON('/api/reset');
    document.getElementById('reset-status').textContent =
      data?.status === 'ok' ? 'Data cleared.' : 'Error clearing data.';
  });
}
```

- [ ] **Step 6: Rewrite app.js as entry point**

```javascript
// python/spinlab/static/app.js
import { connectSSE, fetchJSON, postJSON } from './api.js';
import { renderDisconnected, renderIdle, renderReference, renderPractice } from './live.js';
import { fetchModel, initModelTab } from './model.js';
import { fetchManage, initManageTab } from './manage.js';

function updateLive(data) {
  if (!data.tcp_connected) return renderDisconnected();
  switch (data.mode) {
    case 'reference': return renderReference(data);
    case 'practice': return renderPractice(data);
    default: return renderIdle(data);
  }
}

// Tab switching
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'model') fetchModel();
    if (btn.dataset.tab === 'manage') fetchManage();
  });
});

// Mode control buttons
document.getElementById('btn-launch-emu')?.addEventListener('click', async () => {
  const data = await postJSON('/api/emulator/launch');
  if (data?.status === 'error') alert(data.message);
});

document.getElementById('btn-ref-start')?.addEventListener('click', () =>
  postJSON('/api/reference/start'));

document.getElementById('btn-ref-stop')?.addEventListener('click', () =>
  postJSON('/api/reference/stop'));

document.getElementById('btn-practice-start')?.addEventListener('click', () =>
  postJSON('/api/practice/start'));

document.getElementById('btn-practice-stop')?.addEventListener('click', () =>
  postJSON('/api/practice/stop'));

// Init tabs
initModelTab();
initManageTab();

// Connect SSE (primary) with initial poll for first paint
connectSSE(updateLive);
fetchJSON('/api/state').then(data => { if (data) updateLive(data); });
```

- [ ] **Step 7: Update index.html to use ES module**

In `python/spinlab/static/index.html`, change the script tag:

```html
  <script type="module" src="/static/app.js?v=9"></script>
```

Add favicon (inline SVG data URI) to `<head>`:

```html
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#127922;</text></svg>">
```

Add toast container style to `style.css`:

```css
#toast {
  position: fixed;
  bottom: 16px;
  left: 50%;
  transform: translateX(-50%);
  background: #c53030;
  color: #fff;
  padding: 8px 16px;
  border-radius: 4px;
  font-size: 0.85em;
  opacity: 0;
  transition: opacity 0.3s;
  pointer-events: none;
  z-index: 1000;
}
#toast.visible { opacity: 1; }
```

- [ ] **Step 8: Manual smoke test**

Run: `spinlab dashboard` and open `http://localhost:15483` in browser.
Verify: tabs work, SSE updates arrive, no console errors.

- [ ] **Step 9: Commit**

```bash
git add python/spinlab/static/
git commit -m "refactor: split app.js into ES modules, add SSE client and toast errors"
```

---

## Chunk 4: Lua Cleanup

### Task 10: Consolidate Lua practice state into table

**Files:**
- Modify: `lua/spinlab.lua`

- [ ] **Step 1: Replace practice globals with table**

Replace lines 78-85 (the 7 practice globals) with:

```lua
local practice = {
    active = false,
    state = PSTATE_IDLE,
    split = nil,
    start_ms = 0,
    elapsed_ms = 0,
    completed = false,
    result_start_ms = 0,
    auto_advance_ms = 2000,
}

local function practice_reset()
    practice.active = false
    practice.state = PSTATE_IDLE
    practice.split = nil
    practice.start_ms = 0
    practice.elapsed_ms = 0
    practice.completed = false
    practice.result_start_ms = 0
    practice.auto_advance_ms = 2000
end
```

- [ ] **Step 2: Update all references**

Search-and-replace throughout the file:
- `practice_mode` → `practice.active`
- `practice_state` → `practice.state`
- `practice_split` → `practice.split`
- `practice_start_ms` → `practice.start_ms`
- `practice_elapsed_ms` → `practice.elapsed_ms`
- `practice_completed` → `practice.completed`
- `practice_result_start_ms` → `practice.result_start_ms`
- `practice_auto_advance_ms` → `practice.auto_advance_ms`

Replace the inline practice-clear blocks (in `handle_tcp` disconnect, `practice_stop`, `reset`) with `practice_reset()`.

- [ ] **Step 3: Split detect_transitions into pure detection + handlers**

Extract from `detect_transitions(curr)`:

```lua
local function on_level_entrance(curr, state_path)
    level_start_frame = frame_counter
    local event_data = {
        event      = "level_entrance",
        level      = curr.level_num,
        room       = curr.room_num,
        frame      = frame_counter,
        ts_ms      = ts_ms(),
        session    = "passive",
        state_path = state_path or "",
    }
    if JSONL_LOGGING then log_jsonl(event_data) end
    if client and not practice.active then
        client:send(to_json(event_data) .. "\n")
    end
    log("Level entrance: " .. curr.level_num .. " -> " ..
        (state_path and ("queued state save: " .. state_path) or "no game context, save skipped"))
end

local function on_death(curr)
    died_flag = true
    log("Death at level " .. curr.level_num .. " (not logged)")
end

local function on_level_exit(curr)
    local elapsed = math.floor((frame_counter - level_start_frame) / 60.0 * 1000)
    local goal = goal_type(curr)
    local event_data = {
        event      = "level_exit",
        level      = curr.level_num,
        room       = curr.room_num,
        goal       = goal,
        elapsed_ms = elapsed,
        frame      = frame_counter,
        ts_ms      = ts_ms(),
        session    = "passive",
    }
    if JSONL_LOGGING then log_jsonl(event_data) end
    if client and not practice.active then
        client:send(to_json(event_data) .. "\n")
    end
    log("Level exit: " .. curr.level_num .. " goal=" .. goal .. " elapsed=" .. elapsed .. "ms")
end

local function detect_transitions(curr)
    if curr.player_anim == 9 and prev.player_anim ~= 9 then
        on_death(curr)
    end

    if curr.game_mode == 18 and prev.game_mode ~= 18 then
        if not died_flag then
            local state_path
            if not game_id then
                log("No game context yet, skipping state save")
                if client and not practice.active then
                    client:send(to_json({event = "error", message = "No game context — save state skipped"}) .. "\n")
                end
            else
                local state_fname = curr.level_num .. "_" .. curr.room_num .. ".mss"
                state_path = STATE_DIR .. "/" .. game_id .. "/" .. state_fname
                if pending_save then
                    log("WARNING: pending_save overwritten (was: " .. pending_save .. ")")
                end
                pending_save = state_path
            end
            on_level_entrance(curr, state_path)
        else
            died_flag = false
            log("Quick retry at level " .. curr.level_num .. " (not logged as entrance)")
        end
    end

    if curr.exit_mode ~= 0 and prev.exit_mode == 0 then
        on_level_exit(curr)
    end
end
```

- [ ] **Step 4: Extract overlay helper for shared timer rendering**

```lua
local function draw_timer_row(y, elapsed, compare_time, prefix)
    local timer_color
    if compare_time then
        timer_color = (elapsed < compare_time) and 0xFF44FF44 or 0xFFFF4444
    else
        timer_color = 0xFFFFFFFF
    end
    local cmp_str = compare_time and ms_to_display(compare_time) or "?"
    local text = (prefix and (prefix .. "  ") or "") .. ms_to_display(elapsed) .. " / " .. cmp_str
    draw_text(4, y, text, 0x00000000, timer_color)
end
```

Update `draw_practice_overlay` to use `draw_timer_row` for both PLAYING and RESULT states.

- [ ] **Step 5: Manual test in Mesen2**

Load the script in Mesen2, verify:
- Passive mode detects transitions
- Practice mode loads/clears correctly
- Overlay renders properly

- [ ] **Step 6: Commit**

```bash
git add lua/spinlab.lua
git commit -m "refactor: consolidate Lua practice state, extract transition handlers and overlay helper"
```

---

## Chunk 5: Kalman Cleanup + AHK + CLAUDE.md

### Task 11: Replace KalmanState constructors with dataclasses.replace

**Files:**
- Modify: `python/spinlab/estimators/kalman.py`

- [ ] **Step 1: Identify constructors to replace**

In `kalman.py`, these constructors copy most fields from an existing state:
- `_predict` (line 103): changes mu, d, P_* — keep Q_*, gold, n_completed, n_attempts
- `_update` (line 133): changes mu, d, P_* — keep R, Q_*, gold, n_completed, n_attempts
- `_reestimate_R` (line 145): changes R only
- `process_attempt` null case (line 168): changes n_attempts only
- `process_attempt` result (line 181): changes mu, d, P_*, R, gold, n_completed, n_attempts — keep Q_*

- [ ] **Step 2: Replace with dataclasses.replace**

Add `from dataclasses import replace` to imports.

Replace each constructor. Examples:

```python
# _predict — was 6 lines, becomes:
return replace(state,
    mu=mu_pred, d=d_pred,
    P_mm=P_mm_pred, P_md=P_md_pred, P_dm=P_dm_pred, P_dd=P_dd_pred,
)

# _update — was 6 lines, becomes:
return replace(predicted,
    mu=mu_new, d=d_new,
    P_mm=P_mm_new, P_md=P_md_new, P_dm=P_dm_new, P_dd=P_dd_new,
)

# _reestimate_R — was 7 lines, becomes:
return replace(state, R=max(R_blended, R_FLOOR))

# process_attempt null — was 6 lines, becomes:
return replace(state, n_attempts=state.n_attempts + 1)

# process_attempt result — was 6 lines, becomes:
result = replace(updated,
    Q_mm=state.Q_mm, Q_md=state.Q_md, Q_dm=state.Q_dm, Q_dd=state.Q_dd,
    gold=gold, n_completed=n_completed, n_attempts=state.n_attempts + 1,
)
```

- [ ] **Step 3: Run Kalman tests**

Run: `python -m pytest tests/test_kalman.py -v`
Expected: All pass (behavior unchanged)

- [ ] **Step 4: Run full test suite**

Run: `python -m pytest tests/ -v --timeout=30`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/estimators/kalman.py
git commit -m "refactor: use dataclasses.replace in KalmanEstimator, remove boilerplate constructors"
```

### Task 12: Update AHK script

**Files:**
- Modify: `scripts/spinlab.ahk`

- [ ] **Step 1: Update Ctrl+Alt+X to call shutdown endpoint first**

Replace the `^!x` hotkey:

```autohotkey
; Ctrl+Alt+X — graceful shutdown
^!x:: {
    ; Try graceful HTTP shutdown first
    try {
        RunWait 'cmd /c curl -s -X POST http://localhost:15483/api/shutdown',, 'Hide'
        Sleep 1000
    }
    ; Kill Mesen if running
    if ProcessExist("Mesen.exe")
        Run 'taskkill /IM Mesen.exe /F',, "Hide"
    ; Fallback: kill dashboard if HTTP shutdown didn't work
    StopDashboard()
    Flash "SpinLab — stopped"
}
```

- [ ] **Step 2: Update Ctrl+Alt+W to remove Mesen launch**

Replace the `^!w` hotkey:

```autohotkey
; Ctrl+Alt+W — launch dashboard only (Mesen launches from dashboard UI)
^!w:: {
    global dashPID
    existingPID := (dashPID != 0 && ProcessExist(dashPID)) ? dashPID : FindDashPID()
    if (existingPID != 0) {
        dashPID := existingPID
    } else {
        Run 'spinlab dashboard', A_ScriptDir '\..',  'Min', &dashPID
    }
    Flash("SpinLab started", 2000)
}
```

- [ ] **Step 3: Commit**

```bash
git add scripts/spinlab.ahk
git commit -m "feat: AHK uses shutdown endpoint, remove Mesen launch from Ctrl+Alt+W"
```

### Task 13: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add spinlab.ahk to architecture overview**

In the architecture `tree` block, add after `scripts/`:

```
├── scripts/
│   ├── launch.sh           # Launches Mesen2 with Lua script auto-loaded
│   └── spinlab.ahk         # AHK hotkeys: Ctrl+Alt+W (start), Ctrl+Alt+X (stop)
```

Add `session_manager.py` to the Python tree:

```
│       ├── session_manager.py # Central state owner, event routing, SSE
```

- [ ] **Step 2: Fix pip install note**

Change:
> re-run `pip install -e python/` from the worktree root

To:
> re-run `pip install -e .` from the worktree root

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add session_manager.py and spinlab.ahk to architecture overview"
```

### Task 14: Run full test suite and verify

**Files:** None (verification only)

- [ ] **Step 1: Run complete test suite**

Run: `python -m pytest tests/ -v --timeout=30`
Expected: All tests pass

- [ ] **Step 2: Run manual dashboard smoke test**

Run: `spinlab dashboard`
Open: `http://localhost:15483`
Verify:
- SSE connection established (check Network tab for `/api/events`)
- Tab switching works
- No JS console errors
- Favicon appears

- [ ] **Step 3: Stop dashboard**

POST `http://localhost:15483/api/shutdown` or Ctrl+C

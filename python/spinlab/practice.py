"""Practice session loop — runs as async background task in dashboard."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Callable

from .models import Attempt, SplitCommand
from .scheduler import Scheduler

if TYPE_CHECKING:
    from .db import Database
    from .tcp_manager import TcpManager

logger = logging.getLogger(__name__)


class PracticeSession:
    """Manages a practice session: picks splits, sends to Lua, processes results."""

    def __init__(
        self,
        tcp: TcpManager,
        db: Database,
        game_id: str,
        auto_advance_delay_ms: int = 2000,
        on_attempt: Callable | None = None,
    ) -> None:
        self.tcp = tcp
        self.db = db
        self.game_id = game_id
        self.auto_advance_delay_ms = auto_advance_delay_ms
        self.on_attempt = on_attempt

        self.scheduler = Scheduler(db, game_id)
        self.session_id = uuid.uuid4().hex
        self.started_at = datetime.now(UTC).isoformat()

        self.is_running = False
        self.current_split_id: str | None = None
        self.queue: list[str] = []
        self.splits_attempted = 0
        self.splits_completed = 0
        self._skipped: set[str] = set()

    def start(self) -> None:
        self.db.create_session(self.session_id, self.game_id)
        self.is_running = True

    def stop(self) -> None:
        self.is_running = False
        self.db.end_session(
            self.session_id, self.splits_attempted, self.splits_completed
        )

    async def run_one(self) -> bool:
        """Run one pick-send-receive cycle. Returns False if no splits available."""
        import os

        picked = self.scheduler.pick_next()
        if picked is None:
            return False

        # Skip splits with no state file or missing state file
        if not picked.state_path or not os.path.exists(picked.state_path):
            self._skipped.add(picked.split_id)
            active = self.db.get_active_splits(self.game_id)
            if all(s.id in self._skipped for s in active):
                return False
            return True  # skip but continue

        # Compute expected time
        expected_time_ms = None
        if picked.estimator_state and picked.estimator_state.mu > 0:
            expected_time_ms = int(picked.estimator_state.mu * 1000)

        cmd = SplitCommand(
            id=picked.split_id,
            state_path=picked.state_path,
            goal=picked.goal,
            description=picked.description,
            reference_time_ms=picked.reference_time_ms,
            auto_advance_delay_ms=self.auto_advance_delay_ms,
            expected_time_ms=expected_time_ms,
        )

        self.current_split_id = cmd.id
        self.queue = [q for q in self.scheduler.peek_next_n(3) if q != cmd.id][:2]

        await self.tcp.send("practice_load:" + json.dumps(cmd.to_dict()))

        # Wait for attempt_result from the shared event queue
        while self.is_running and self.tcp.is_connected:
            event = await self.tcp.recv_event(timeout=1.0)
            if event is None:
                continue  # timeout, check if still running
            if event.get("event") == "attempt_result":
                self._process_result(event, cmd)
                break

        self.current_split_id = None
        return True

    def _process_result(self, result: dict, cmd: SplitCommand) -> None:
        attempt = Attempt(
            split_id=result["split_id"],
            session_id=self.session_id,
            completed=result["completed"],
            time_ms=result.get("time_ms"),
            goal_matched=(result.get("goal") == cmd.goal) if result.get("completed") else None,
            source="practice",
        )
        self.db.log_attempt(attempt)
        self.scheduler.process_attempt(
            result["split_id"],
            time_ms=result.get("time_ms", 0),
            completed=result["completed"],
        )
        self.splits_attempted += 1
        if result["completed"]:
            self.splits_completed += 1
        if self.on_attempt:
            self.on_attempt(attempt)

    async def run_loop(self) -> None:
        """Run the full practice loop until stopped or no splits."""
        self.start()
        try:
            while self.is_running and self.tcp.is_connected:
                if not await self.run_one():
                    break
        finally:
            try:
                await self.tcp.send("practice_stop")
            except (ConnectionError, OSError):
                pass
            self.stop()

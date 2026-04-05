"""Practice session loop — runs as async background task in dashboard."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Callable

from .allocators import SegmentWithModel
from .models import Attempt, SegmentCommand
from .scheduler import Scheduler

if TYPE_CHECKING:
    from .db import Database
    from .tcp_manager import TcpManager

logger = logging.getLogger(__name__)

SEGMENT_LOAD_TIMEOUT_S = 1.0


class PracticeSession:
    """Manages a practice session: picks segments, sends to Lua, processes results."""

    def __init__(
        self,
        tcp: TcpManager,
        db: Database,
        game_id: str,
        auto_advance_delay_ms: int = 1000,
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
        self.current_segment_id: str | None = None
        self.segments_attempted = 0
        self.segments_completed = 0

        self.initial_expected_total_ms: float | None = None
        self.initial_expected_clean_ms: float | None = None

        self._result_event = asyncio.Event()
        self._result_data: dict | None = None

    def _snapshot_expected_times(
        self, estimator_name: str
    ) -> tuple[float | None, float | None]:
        """Sum expected_ms across practicable segments using the named estimator.

        A segment contributes iff it has a state_path that exists on disk AND
        the estimator produced a non-None expected_ms. Missing clean estimates
        contribute 0 to clean; missing total estimates contribute 0 to total.
        Returns (None, None) if every segment lacked both estimates.
        """
        segments = SegmentWithModel.load_all(self.db, self.game_id, estimator_name)
        total_sum = 0.0
        clean_sum = 0.0
        any_total = False
        any_clean = False
        for seg in segments:
            if not seg.state_path or not os.path.exists(seg.state_path):
                continue
            output = seg.model_outputs.get(estimator_name)
            if output is None:
                continue
            if output.total.expected_ms is not None:
                total_sum += output.total.expected_ms
                any_total = True
            if output.clean.expected_ms is not None:
                clean_sum += output.clean.expected_ms
                any_clean = True
        return (
            total_sum if any_total else None,
            clean_sum if any_clean else None,
        )

    def current_expected_times(self) -> tuple[float | None, float | None]:
        """Current sum of expected_ms across practicable segments, using the
        scheduler's currently selected estimator."""
        return self._snapshot_expected_times(self.scheduler.estimator.name)

    def start(self) -> None:
        self.db.create_session(self.session_id, self.game_id)
        (
            self.initial_expected_total_ms,
            self.initial_expected_clean_ms,
        ) = self._snapshot_expected_times(self.scheduler.estimator.name)
        self.is_running = True

    def stop(self) -> None:
        self.is_running = False
        self.db.end_session(
            self.session_id, self.segments_attempted, self.segments_completed
        )

    def receive_result(self, event: dict) -> None:
        """Called by SessionManager.route_event when attempt_result arrives."""
        self._result_data = event
        self._result_event.set()

    async def run_one(self) -> bool:
        """Run one pick-send-receive cycle. Returns False if no segments available."""
        picked = self.scheduler.pick_next()
        if picked is None:
            return False

        # Compute expected time
        expected_time_ms = None
        sel_out = picked.model_outputs.get(picked.selected_model)
        if sel_out and sel_out.total.expected_ms is not None and sel_out.total.expected_ms > 0:
            expected_time_ms = int(sel_out.total.expected_ms)

        # Build overlay label: use custom description or auto-generate from segment fields
        label = picked.description
        if not label:
            start = "start" if picked.start_type == "entrance" else f"cp{picked.start_ordinal}"
            end = "goal" if picked.end_type == "goal" else f"cp{picked.end_ordinal}"
            label = f"L{picked.level_number} {start} > {end}"

        cmd = SegmentCommand(
            id=picked.segment_id,
            state_path=picked.state_path,
            description=label,
            end_type=picked.end_type,
            expected_time_ms=expected_time_ms,
            auto_advance_delay_ms=self.auto_advance_delay_ms,
        )

        self.current_segment_id = cmd.id

        await self.tcp.send("practice_load:" + json.dumps(cmd.to_dict()))

        # Wait for attempt_result via receive_result() (set by SessionManager)
        self._result_event.clear()
        self._result_data = None

        while self.is_running and self.tcp.is_connected:
            try:
                await asyncio.wait_for(self._result_event.wait(), timeout=SEGMENT_LOAD_TIMEOUT_S)
                break
            except asyncio.TimeoutError:
                continue

        if self._result_data and self._result_data.get("event") == "attempt_result":
            self._process_result(self._result_data, cmd)

        self.current_segment_id = None
        return True

    def _process_result(self, result: dict, cmd: SegmentCommand) -> None:
        attempt = Attempt(
            segment_id=result["segment_id"],
            session_id=self.session_id,
            completed=result["completed"],
            time_ms=result.get("time_ms"),
            deaths=result.get("deaths", 0),
            clean_tail_ms=result.get("clean_tail_ms"),
            source="practice",
        )
        self.db.log_attempt(attempt)
        self.scheduler.process_attempt(
            result["segment_id"],
            time_ms=result.get("time_ms", 0),
            completed=result["completed"],
            deaths=result.get("deaths", 0),
            clean_tail_ms=result.get("clean_tail_ms"),
        )
        self.segments_attempted += 1
        if result["completed"]:
            self.segments_completed += 1
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

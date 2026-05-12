"""StateBuilder — assembles API/SSE state snapshots.

Pure view-model construction. Extracted from SessionManager.get_state()
and _build_practice_state() to separate coordination from presentation.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import Database
    from .session_manager import SessionManager

from .db.attempts import RECENT_ATTEMPTS_DB_LIMIT
from .models import Mode, ModelOutput

logger = logging.getLogger(__name__)


class StateBuilder:
    """Assembles state snapshots for API and SSE consumers."""

    def __init__(self, db: "Database"):
        self.db = db

    def build(self, session: "SessionManager") -> dict:
        """Full state snapshot — replaces SessionManager.get_state().

        Snapshot pattern: read volatile fields (mode, capture phase, active
        run id) into locals once at the top, then use those locals throughout.
        Without this, a mode/run transition concurrent with build() would
        produce a snapshot where (e.g.) ``mode == REFERENCE`` but
        ``capture_run_id`` is None — incoherent to the consumer.
        """
        mode = session.mode
        is_recording = session.capture.is_recording
        active_run_id = session.capture.active_run_id
        game_id = session.game_id

        sections_captured: int | None = None
        if is_recording and active_run_id is not None:
            sections_captured = self.db.count_segments_for_run(
                active_run_id, active_only=True,
            )

        base = {
            "mode": mode.value,
            "emu_connected": session.emu.is_connected,
            "game_id": game_id,
            "game_name": session.game_name,
            "current_segment": None,
            "recent": [],
            "session": None,
            "sections_captured": sections_captured,
            "allocator_weights": None,
            "estimator": None,
        }

        if game_id is None:
            return base

        sched = session.get_scheduler()
        base["allocator_weights"] = sched.all_weights
        base["estimator"] = sched.estimator.name

        if mode == Mode.PRACTICE and session.practice_session:
            self._build_practice_state(base, session, sched)

        if mode == Mode.SPEED_RUN and session.speed_run_session:
            self._build_speed_run_state(base, session)

        if mode in (Mode.REFERENCE, Mode.REPLAY):
            base["capture_run_id"] = active_run_id
        if mode == Mode.REPLAY:
            base["replay"] = {
                "rec_path": session.capture.rec_path,
                "total": session.capture.replay_total,
            }

        paused_run = session.capture.get_paused_state()
        if paused_run:
            base["paused_run"] = paused_run

        if mode == Mode.COLD_FILL:
            cf_state = session.cold_fill.get_state()
            if cf_state:
                base["cold_fill"] = cf_state

        base["recent"] = self.db.get_recent_attempts(
            game_id, limit=RECENT_ATTEMPTS_DB_LIMIT,
            session_id=session.current_session_id,
        )
        return base

    def _build_speed_run_state(self, base: dict, session: "SessionManager") -> None:
        """Populate speed-run-specific fields into state dict."""
        sr = session.speed_run_session
        assert sr is not None  # caller checks before calling
        base["session"] = {
            "id": sr.session_id,
            "started_at": sr.started_at,
            "segments_attempted": sr.segments_recorded,
            "segments_completed": sr.levels_completed,
            "saved_total_ms": None,
            "saved_clean_ms": None,
        }
        if sr.current_level_index < len(sr.levels):
            level = sr.levels[sr.current_level_index]
            base["current_segment"] = {
                "id": level.segments[0]["id"],
                "game_id": sr.game_id,
                "level_number": level.level_number,
                "start_type": "entrance",
                "start_ordinal": 0,
                "end_type": "goal",
                "end_ordinal": 0,
                "description": level.description,
                "attempt_count": 0,
                "model_outputs": {},
                "selected_model": "",
                "state_path": level.entrance_state_path,
            }

    def _build_practice_state(self, base: dict, session: "SessionManager", sched) -> None:
        """Populate practice-specific fields into state dict."""
        ps = session.practice_session
        assert ps is not None  # caller checks before calling
        base["session"] = {
            "id": ps.session_id,
            "started_at": ps.started_at,
            "segments_attempted": ps.segments_attempted,
            "segments_completed": ps.segments_completed,
        }
        cur_total, cur_clean = ps.current_expected_times()
        saved_total = (
            ps.initial_expected_total_ms - cur_total
            if ps.initial_expected_total_ms is not None and cur_total is not None
            else None
        )
        saved_clean = (
            ps.initial_expected_clean_ms - cur_clean
            if ps.initial_expected_clean_ms is not None and cur_clean is not None
            else None
        )
        base["session"]["saved_total_ms"] = saved_total
        base["session"]["saved_clean_ms"] = saved_clean
        if ps.current_segment_id and session.game_id:
            segments = self.db.get_all_segments_with_model(session.game_id)
            seg_map = {s["id"]: s for s in segments}
            if ps.current_segment_id in seg_map:
                current_seg: dict = dict(seg_map[ps.current_segment_id])
                current_seg["attempt_count"] = self.db.get_segment_attempt_count(
                    ps.current_segment_id, ps.session_id
                )
                state_rows = self.db.load_all_model_states_for_segment(ps.current_segment_id)
                model_outputs = {}
                for sr in state_rows:
                    output_json = sr.get("output_json")
                    if output_json:
                        try:
                            model_outputs[sr["estimator"]] = ModelOutput.from_dict(
                                json.loads(output_json)
                            ).to_dict()
                        except (json.JSONDecodeError, KeyError):
                            pass
                current_seg["model_outputs"] = model_outputs
                current_seg["selected_model"] = sched.estimator.name
                base["current_segment"] = current_seg

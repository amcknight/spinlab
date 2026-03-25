"""Scheduler coordinator: wires an estimator + allocator together.

Exposes pick_next(), process_attempt(), peek_next_n() to the orchestrator.
Same interface surface as the old SM-2 scheduler, different internals.
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from spinlab.allocators import SegmentWithModel, get_allocator, list_allocators
from spinlab.allocators.greedy import GreedyAllocator  # ensure registered
from spinlab.allocators.random import RandomAllocator
from spinlab.allocators.round_robin import RoundRobinAllocator
from spinlab.estimators import get_estimator, list_estimators
from spinlab.estimators.kalman import KalmanEstimator  # ensure registered

if TYPE_CHECKING:
    from spinlab.allocators import Allocator
    from spinlab.db import Database
    from spinlab.estimators import Estimator


class Scheduler:
    def __init__(
        self,
        db: "Database",
        game_id: str,
        estimator_name: str = "kalman",
        allocator_name: str = "greedy",
    ) -> None:
        self.db = db
        self.game_id = game_id
        # Load persisted choices from DB, falling back to provided defaults
        saved_alloc = db.load_allocator_config("allocator")
        saved_est = db.load_allocator_config("estimator")
        self.estimator: Estimator = get_estimator(saved_est or estimator_name)
        self.allocator: Allocator = get_allocator(saved_alloc or allocator_name)

    def _sync_config_from_db(self) -> None:
        """Re-read allocator/estimator config from DB.

        Allows dashboard config changes to take effect in a running orchestrator.
        """
        saved_alloc = self.db.load_allocator_config("allocator")
        if saved_alloc and saved_alloc != self.allocator.name:
            self.allocator = get_allocator(saved_alloc)
        saved_est = self.db.load_allocator_config("estimator")
        if saved_est and saved_est != self.estimator.name:
            self.estimator = get_estimator(saved_est)

    def _load_segments_with_model(self) -> list[SegmentWithModel]:
        """Load all active segments and hydrate with estimator state."""
        rows = self.db.get_all_segments_with_model(self.game_id)
        segments = []

        for row in rows:
            state = None
            mr = 0.0
            di = {}
            n_completed = 0
            n_attempts = 0
            gold_ms = None

            if row["state_json"]:
                from spinlab.estimators.kalman import KalmanState

                state = KalmanState.from_dict(json.loads(row["state_json"]))
                mr = self.estimator.marginal_return(state)
                di = self.estimator.drift_info(state)
                n_completed = state.n_completed
                n_attempts = state.n_attempts
                gold_ms = int(state.gold * 1000) if state.gold != float("inf") else None

            segments.append(
                SegmentWithModel(
                    segment_id=row["id"],
                    game_id=row["game_id"],
                    level_number=row["level_number"],
                    start_type=row["start_type"],
                    start_ordinal=row["start_ordinal"],
                    end_type=row["end_type"],
                    end_ordinal=row["end_ordinal"],
                    description=row["description"],
                    strat_version=row["strat_version"],
                    state_path=row["state_path"],
                    active=bool(row["active"]),
                    estimator_state=state,
                    marginal_return=mr,
                    drift_info=di,
                    n_completed=n_completed,
                    n_attempts=n_attempts,
                    gold_ms=gold_ms,
                )
            )
        return segments

    def pick_next(self) -> SegmentWithModel | None:
        """Pick next segment to practice."""
        self._sync_config_from_db()
        segments = self._load_segments_with_model()
        if not segments:
            return None
        practicable = [s for s in segments if s.state_path and os.path.exists(s.state_path)]
        if not practicable:
            return None
        segment_id = self.allocator.pick_next(practicable)
        if segment_id is None:
            return None
        return next((s for s in practicable if s.segment_id == segment_id), None)

    def process_attempt(
        self, segment_id: str, time_ms: int, completed: bool
    ) -> None:
        """Process a completed or incomplete attempt."""
        observed_time = time_ms / 1000.0 if completed else None

        # Load existing state
        row = self.db.load_model_state(segment_id)
        if row and row["state_json"]:
            from spinlab.estimators.kalman import KalmanState

            state = KalmanState.from_dict(json.loads(row["state_json"]))
            state = self.estimator.process_attempt(state, observed_time)
        else:
            if observed_time is not None:
                # First completed attempt — initialize
                all_rows = self.db.load_all_model_states(self.game_id)
                from spinlab.estimators.kalman import KalmanState

                all_states = [
                    KalmanState.from_dict(json.loads(r["state_json"]))
                    for r in all_rows
                    if r["state_json"]
                ]
                priors = self.estimator.get_population_priors(all_states)
                state = self.estimator.init_state(observed_time, priors)
            else:
                # First attempt is incomplete — create minimal state
                from spinlab.estimators.kalman import KalmanState

                state = KalmanState(n_attempts=1)
                self.db.save_model_state(
                    segment_id,
                    self.estimator.name,
                    json.dumps(state.to_dict()),
                    0.0,
                )
                return

        mr = self.estimator.marginal_return(state)
        self.db.save_model_state(
            segment_id, self.estimator.name, json.dumps(state.to_dict()), mr
        )

    def peek_next_n(self, n: int) -> list[str]:
        """Preview next N segment IDs."""
        segments = self._load_segments_with_model()
        practicable = [s for s in segments if s.state_path and os.path.exists(s.state_path)]
        return self.allocator.peek_next_n(practicable, n)

    def get_all_model_states(self) -> list[SegmentWithModel]:
        """Get all segments with model state for dashboard."""
        return self._load_segments_with_model()

    def switch_allocator(self, name: str) -> None:
        self.allocator = get_allocator(name)
        self.db.save_allocator_config("allocator", name)

    def switch_estimator(self, name: str) -> None:
        self.estimator = get_estimator(name)
        self.db.save_allocator_config("estimator", name)

    def rebuild_all_states(self) -> None:
        """Replay all attempts to reconstruct model_state table."""
        segments = self.db.get_all_segments_with_model(self.game_id)
        for row in segments:
            segment_id = row["id"]
            attempts_raw = self.db.get_segment_attempts(segment_id)
            if not attempts_raw:
                continue
            times = [
                a["time_ms"] / 1000.0 if a["completed"] else None
                for a in attempts_raw
            ]
            state = self.estimator.rebuild_state(times)
            mr = self.estimator.marginal_return(state)
            self.db.save_model_state(
                segment_id, self.estimator.name, json.dumps(state.to_dict()), mr
            )

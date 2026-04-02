"""Scheduler coordinator: wires estimators + allocator together.

Runs ALL registered estimators on each attempt. The "active" estimator
selection only affects which ModelOutput the allocator reads.
"""
from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

from spinlab.allocators import SegmentWithModel, get_allocator, list_allocators
from spinlab.allocators.greedy import GreedyAllocator  # ensure registered
from spinlab.allocators.random import RandomAllocator
from spinlab.allocators.round_robin import RoundRobinAllocator
from spinlab.estimators import EstimatorState, get_estimator, list_estimators
from spinlab.estimators.kalman import KalmanEstimator  # noqa: F401 — ensure registered
from spinlab.estimators.rolling_mean import RollingMeanEstimator  # noqa: F401 — ensure registered
try:
    from spinlab.estimators.exp_decay import ExpDecayEstimator  # noqa: F401 — ensure registered
except ImportError:
    logger.warning("exp_decay unavailable (numpy/scipy not installed)")
from spinlab.models import AttemptRecord, ModelOutput

if TYPE_CHECKING:
    from spinlab.allocators import Allocator
    from spinlab.db import Database
    from spinlab.estimators import Estimator


def _attempts_from_rows(rows: list[dict]) -> list[AttemptRecord]:
    return [
        AttemptRecord(
            time_ms=r["time_ms"],
            completed=bool(r["completed"]),
            deaths=r.get("deaths", 0) or 0,
            clean_tail_ms=r.get("clean_tail_ms"),
            created_at=r["created_at"],
        )
        for r in rows
    ]


class Scheduler:
    def __init__(
        self, db: "Database", game_id: str,
        estimator_name: str = "kalman", allocator_name: str = "greedy",
    ) -> None:
        self.db = db
        self.game_id = game_id
        saved_alloc = db.load_allocator_config("allocator")
        saved_est = db.load_allocator_config("estimator")
        self.estimator: Estimator = get_estimator(saved_est or estimator_name)
        self.allocator: Allocator = get_allocator(saved_alloc or allocator_name)

    def _sync_config_from_db(self) -> None:
        saved_alloc = self.db.load_allocator_config("allocator")
        if saved_alloc and saved_alloc != self.allocator.name:
            self.allocator = get_allocator(saved_alloc)
        saved_est = self.db.load_allocator_config("estimator")
        if saved_est and saved_est != self.estimator.name:
            self.estimator = get_estimator(saved_est)

    def pick_next(self) -> SegmentWithModel | None:
        self._sync_config_from_db()
        segments = SegmentWithModel.load_all(self.db, self.game_id, self.estimator.name)
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
        self, segment_id: str, time_ms: int, completed: bool,
        deaths: int = 0, clean_tail_ms: int | None = None,
    ) -> None:
        # If completed with no deaths and no explicit clean_tail_ms, clean_tail = total time
        effective_clean_tail = clean_tail_ms
        if completed and deaths == 0 and clean_tail_ms is None:
            effective_clean_tail = time_ms
        new_attempt = AttemptRecord(
            time_ms=time_ms if completed else None,
            completed=completed, deaths=deaths,
            clean_tail_ms=effective_clean_tail if completed else None,
            created_at="",
        )

        attempt_rows = self.db.get_segment_attempts(segment_id)
        all_attempts = _attempts_from_rows(attempt_rows)
        # Include the new attempt in the list passed to model_output
        all_attempts_with_new = all_attempts + [new_attempt]

        for est in [get_estimator(n) for n in list_estimators()]:
            row = self.db.load_model_state(segment_id, est.name)

            if row and row["state_json"]:
                state = EstimatorState.deserialize(est.name, row["state_json"])
                state = est.process_attempt(state, new_attempt, all_attempts)
            else:
                if completed and time_ms is not None:
                    priors = est.get_priors(self.db, self.game_id)
                    state = est.init_state(new_attempt, priors)
                else:
                    state = est.rebuild_state([new_attempt])
                    output = est.model_output(state, all_attempts_with_new)
                    self.db.save_model_state(
                        segment_id, est.name,
                        json.dumps(state.to_dict()), json.dumps(output.to_dict()),
                    )
                    continue

            output = est.model_output(state, all_attempts_with_new)
            self.db.save_model_state(
                segment_id, est.name,
                json.dumps(state.to_dict()), json.dumps(output.to_dict()),
            )

    def get_all_model_states(self) -> list[SegmentWithModel]:
        return SegmentWithModel.load_all(self.db, self.game_id, self.estimator.name)

    def switch_allocator(self, name: str) -> None:
        self.allocator = get_allocator(name)
        self.db.save_allocator_config("allocator", name)

    def switch_estimator(self, name: str) -> None:
        self.estimator = get_estimator(name)
        self.db.save_allocator_config("estimator", name)

    def rebuild_all_states(self) -> None:
        segments = self.db.get_all_segments_with_model(self.game_id)
        for row in segments:
            segment_id = row["id"]
            attempt_rows = self.db.get_segment_attempts(segment_id)
            if not attempt_rows:
                continue
            all_attempts = _attempts_from_rows(attempt_rows)
            for est in [get_estimator(n) for n in list_estimators()]:
                state = est.rebuild_state(all_attempts)
                output = est.model_output(state, all_attempts)
                self.db.save_model_state(
                    segment_id, est.name,
                    json.dumps(state.to_dict()), json.dumps(output.to_dict()),
                )

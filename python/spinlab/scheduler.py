"""Scheduler coordinator: wires estimators + allocator together.

Runs ALL registered estimators on each attempt. The "active" estimator
selection only affects which ModelOutput the allocator reads.
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from spinlab.allocators import SegmentWithModel, get_allocator, list_allocators
from spinlab.allocators.greedy import GreedyAllocator  # ensure registered
from spinlab.allocators.random import RandomAllocator
from spinlab.allocators.round_robin import RoundRobinAllocator
from spinlab.estimators import EstimatorState, get_estimator, list_estimators
from spinlab.estimators.kalman import KalmanEstimator, KalmanState  # ensure registered
from spinlab.estimators.model_a import ModelAEstimator, ModelAState  # ensure registered
from spinlab.estimators.model_b import ModelBEstimator, ModelBState  # ensure registered
from spinlab.models import AttemptRecord, ModelOutput

if TYPE_CHECKING:
    from spinlab.allocators import Allocator
    from spinlab.db import Database
    from spinlab.estimators import Estimator

# Maps estimator name -> state class for deserialization
_STATE_CLASSES: dict[str, type[EstimatorState]] = {
    "kalman": KalmanState,
    "model_a": ModelAState,
    "model_b": ModelBState,
}


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

    def _all_estimators(self) -> list[Estimator]:
        return [get_estimator(name) for name in list_estimators()]

    def _all_estimators_names(self) -> list[str]:
        return list_estimators()

    def _deserialize_state(self, estimator_name: str, state_json: str) -> EstimatorState:
        d = json.loads(state_json)
        cls = _STATE_CLASSES.get(estimator_name)
        if cls is None:
            raise ValueError(f"No state class for estimator: {estimator_name}")
        return cls.from_dict(d)

    def _load_segments_with_model(self) -> list[SegmentWithModel]:
        rows = self.db.get_all_segments_with_model(self.game_id)
        segments = []

        for row in rows:
            segment_id = row["id"]
            model_outputs: dict[str, ModelOutput] = {}
            n_completed = 0
            n_attempts = 0
            gold_ms = None
            clean_gold_ms = None

            state_rows = self.db.load_all_model_states_for_segment(segment_id)
            for sr in state_rows:
                if sr["output_json"]:
                    try:
                        out = ModelOutput.from_dict(json.loads(sr["output_json"]))
                        model_outputs[sr["estimator"]] = out
                    except (json.JSONDecodeError, KeyError):
                        pass
                if sr["state_json"]:
                    try:
                        sd = json.loads(sr["state_json"])
                        nc = sd.get("n_completed", 0)
                        na = sd.get("n_attempts", 0)
                        if nc > n_completed:
                            n_completed = nc
                            n_attempts = na
                    except (json.JSONDecodeError, KeyError):
                        pass

            # Compute gold_ms and clean_gold from attempt history
            attempt_rows = self.db.get_segment_attempts(segment_id)
            for ar in attempt_rows:
                if ar["completed"] and ar.get("time_ms") is not None:
                    t = ar["time_ms"]
                    if gold_ms is None or t < gold_ms:
                        gold_ms = t
                if ar["completed"] and ar.get("clean_tail_ms") is not None:
                    ct = ar["clean_tail_ms"]
                    if clean_gold_ms is None or ct < clean_gold_ms:
                        clean_gold_ms = ct

            segments.append(SegmentWithModel(
                segment_id=segment_id,
                game_id=row["game_id"],
                level_number=row["level_number"],
                start_type=row["start_type"],
                start_ordinal=row["start_ordinal"],
                end_type=row["end_type"],
                end_ordinal=row["end_ordinal"],
                description=row["description"],
                strat_version=row["strat_version"],
                state_path=row.get("state_path"),
                active=bool(row["active"]),
                model_outputs=model_outputs,
                selected_model=self.estimator.name,
                n_completed=n_completed,
                n_attempts=n_attempts,
                gold_ms=gold_ms,
                clean_gold_ms=clean_gold_ms,
            ))
        return segments

    def pick_next(self) -> SegmentWithModel | None:
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

        for est in self._all_estimators():
            row = self.db.load_model_state(segment_id, est.name)

            if row and row["state_json"]:
                state = self._deserialize_state(est.name, row["state_json"])
                state = est.process_attempt(state, new_attempt, all_attempts)
            else:
                if completed and time_ms is not None:
                    priors = self._get_priors(est)
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

    def _get_priors(self, est: Estimator) -> dict:
        if isinstance(est, KalmanEstimator):
            all_rows = self.db.load_all_model_states(self.game_id)
            kalman_rows = [r for r in all_rows if r["estimator"] == "kalman"]
            all_states = []
            for r in kalman_rows:
                if r["state_json"]:
                    try:
                        all_states.append(KalmanState.from_dict(json.loads(r["state_json"])))
                    except (json.JSONDecodeError, KeyError):
                        pass
            return est.get_population_priors(all_states)
        return {}

    def peek_next_n(self, n: int) -> list[str]:
        segments = self._load_segments_with_model()
        practicable = [s for s in segments if s.state_path and os.path.exists(s.state_path)]
        return self.allocator.peek_next_n(practicable, n)

    def get_all_model_states(self) -> list[SegmentWithModel]:
        return self._load_segments_with_model()

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
            for est in self._all_estimators():
                state = est.rebuild_state(all_attempts)
                output = est.model_output(state, all_attempts)
                self.db.save_model_state(
                    segment_id, est.name,
                    json.dumps(state.to_dict()), json.dumps(output.to_dict()),
                )

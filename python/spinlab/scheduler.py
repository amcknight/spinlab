"""Scheduler coordinator: wires the em_suite sampler + allocator together."""
from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

from spinlab.allocators import Allocator, SegmentWithModel, get_allocator, list_allocators
from spinlab.allocators.mix import MixAllocator
from spinlab.db.attempts import AttemptRow, EventAttemptRow
from spinlab.estimators.em_suite_sampler import EmSuiteSamplerEstimator, SamplerState
from spinlab.models import Attempt, AttemptRecord, AttemptSource
from spinlab.practice_engine.engine import PracticeEngine

if TYPE_CHECKING:
    from spinlab.db import Database
    from spinlab.models import EventAttempt


# Optional segments-v07 silent fit pipeline. The [fits] extra brings
# JAX/NumPyro; without them the import fails and the scheduler skips the
# fit path entirely. This is patched in tests via
# monkeypatch.setattr(scheduler, "_refit_segment", None).
try:
    from spinlab.segments_model import refit_segment as _refit_segment
except ImportError:  # pragma: no cover — only fires without [fits]
    _refit_segment = None  # type: ignore[assignment]

# Minimum number of attempt events before we bother trying a fit. The
# prototype's fit_segment can handle n=1 but produces wide-bands /
# unconverged envelopes that aren't useful and just burn CPU. Matches
# the V1_ESSENCE low_n threshold for "fit results not yet meaningful."
_MIN_EVENTS_FOR_FIT = 5


def _attempts_from_rows(rows: list[AttemptRow]) -> list[AttemptRecord]:
    return [
        AttemptRecord(
            time_ms=r["time_ms"],
            completed=bool(r["completed"]),
            deaths=r.get("deaths", 0) or 0,
            clean_tail_ms=r.get("clean_tail_ms"),
            created_at=r["created_at"],
        )
        for r in rows
        if not r.get("invalidated", False)
    ]


def _events_from_rows(rows: list[EventAttemptRow]) -> list[EventAttempt]:
    """Shared model-ingestion hydration: convert raw event_attempt rows into
    EventAttempt dataclass instances for estimator consumption.

    REPLAY events are excluded from EVERY model view that flows through this
    function — the sampler, the matrix route, the cold-distribution route —
    because replay times are fast-forward-collapsed and would corrupt all of
    them. This is the single sampler-ingestion seam where that filter lives.
    Only the raw ``db.get_segment_event_rows`` call remains unfiltered, for
    any future pure-provenance need that explicitly wants all rows.
    """
    from datetime import datetime

    from spinlab.models import AttemptOutcome, EventAttempt
    out: list[EventAttempt] = []
    for r in rows:
        if AttemptSource(r["source"]) is AttemptSource.REPLAY:
            continue
        out.append(EventAttempt(
            segment_id=r["segment_id"],
            episode_id=r["episode_id"],
            outcome=AttemptOutcome(r["outcome"]),
            time_ms=r["time_ms"],
            session_id=r.get("session_id"),
            capture_run_id=r.get("capture_run_id"),
            source=AttemptSource(r["source"]),
            chosen_allocator=r.get("chosen_allocator"),
            invalidated=bool(r.get("invalidated", 0)),
            is_hot=bool(r["is_hot"]),
            created_at=datetime.fromisoformat(r["created_at"]),
        ))
    return out


# Default RNG seed for the lazily-built PracticeEngine. Keeps the matrix
# reproducible per scheduler instance.
_DEFAULT_PRACTICE_ENGINE_RNG_SEED = 0


class Scheduler:
    def __init__(
        self, db: "Database", game_id: str,
        estimator_name: str = "em_suite_sampler",  # accepted but unused; reserved to avoid a future positional-arg silently doing nothing
        *,
        practice_engine_rollouts: int | None = None,
    ) -> None:
        self.db = db
        self.game_id = game_id
        self.estimator = EmSuiteSamplerEstimator()
        self.allocator: MixAllocator = self._build_mix_from_db()
        self._drop_legacy_allocator_config_key()
        self._engine: PracticeEngine | None = None
        # Fallback to the config-defined default when the caller doesn't override
        # (matches `PracticeEngineConfig.rollouts` default).
        from spinlab.config import DEFAULT_PRACTICE_ENGINE_ROLLOUTS
        self._practice_engine_rollouts: int = (
            practice_engine_rollouts
            if practice_engine_rollouts is not None
            else DEFAULT_PRACTICE_ENGINE_ROLLOUTS
        )

    def _drop_legacy_allocator_config_key(self) -> None:
        if self.db.load_allocator_config("allocator") is not None:
            self.db.delete_allocator_config("allocator")

    def _build_mix_from_db(self) -> MixAllocator:
        raw = self.db.load_allocator_config("allocator_weights")
        if raw:
            weights = json.loads(raw)
            # Add any newly registered allocators missing from saved config
            for name in list_allocators():
                if name not in weights:
                    weights[name] = 0
                    logger.info("Added new allocator %r to saved weights (weight=0)", name)
        else:
            names = list_allocators()
            base = 100 // len(names)
            remainder = 100 - base * len(names)
            weights = {n: base + (1 if i < remainder else 0) for i, n in enumerate(names)}
        self._all_weights = weights
        return self._build_mix(weights)

    @property
    def all_weights(self) -> dict[str, int]:
        """All allocator weights including 0-weight entries (for UI display)."""
        return dict(self._all_weights)

    @staticmethod
    def _build_mix(weights: dict[str, int]) -> MixAllocator:
        entries: list[tuple[Allocator, int | float]] = [
            (get_allocator(name), w) for name, w in weights.items() if w > 0
        ]
        return MixAllocator(entries=entries)

    def _sync_config_from_db(self) -> None:
        raw = self.db.load_allocator_config("allocator_weights")
        if raw:
            saved_weights = json.loads(raw)
            if saved_weights != self._all_weights:
                self.allocator = self._build_mix_from_db()

    @property
    def last_chosen_allocator(self) -> str | None:
        """Name of the sub-allocator that made the most recent pick."""
        return self.allocator.last_chosen_allocator

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

    def record_attempt(self, attempt: Attempt) -> None:
        """Legacy entry point for episode-shaped attempt writers (tests, the
        reference-capture finalizer).

        Persists the attempt via the event-level shim (``log_attempt`` →
        synthesized event rows) and then updates estimator state from the
        just-persisted episode. Production practice and speed-run code no
        longer goes through here — they write per-event rows directly via
        ``PracticeSession.receive_event_attempt`` and end-of-episode
        triggers ``update_state_after_episode``.
        """
        self.db.log_attempt(attempt)
        self.update_state_after_episode(attempt.segment_id)

    def process_attempt(
        self, segment_id: str, time_ms: int, completed: bool,
        deaths: int = 0, clean_tail_ms: int | None = None,
        session_id: str | None = None,
    ) -> None:
        """Back-compat shim — builds an Attempt and calls record_attempt.

        Pre-Phase-0 this was the canonical "update model only" entry point
        and required the caller to invoke ``db.log_attempt`` separately.
        After Phase 0 the DB is the source of truth (events round-trip into
        episodes via the adapter), so the model can no longer be updated
        without the row first being persisted — combining the two operations
        here matches the new model-after-persistence ordering.

        The signature stays positional for the dozens of existing call sites
        in tests, but ``session_id`` becomes required when no draft capture
        run exists (which is the case for ad-hoc tests that don't set up a
        capture_run). The helper auto-creates a session row if missing.
        """
        if session_id is None:
            session_id = "_default_test_session"
        # Idempotent: tests reuse this session across attempts.
        try:
            self.db.create_session(session_id, self.game_id)
        except Exception:
            pass  # session already exists
        attempt = Attempt(
            segment_id=segment_id,
            session_id=session_id,
            completed=completed,
            time_ms=time_ms if completed else None,
            deaths=deaths,
            clean_tail_ms=clean_tail_ms,
        )
        self.record_attempt(attempt)

    @property
    def engine(self) -> PracticeEngine:
        """Lazy practice simulation engine. Built on first access from current SamplerStates."""
        if self._engine is None:
            self._engine = PracticeEngine(
                sampler_states=self._load_all_sampler_states(),
                N=self._practice_engine_rollouts,
                rng_seed=_DEFAULT_PRACTICE_ENGINE_RNG_SEED,
            )
        return self._engine

    def _load_all_sampler_states(self) -> dict[str, SamplerState]:
        """Hydrate all SamplerState objects for this game's segments.

        Rebuilds each state from the event table (the source of truth) rather than
        deserializing the persisted ``state_json``. The JSON can predate the
        sampler's draw-pool fields — saved with EMAs + counters but no pools — and
        ``from_dict`` tolerantly defaults missing pools to empty. The closed-form
        scalar only needs the EMAs, but ``sample_episode`` draws from the pools, so a
        pool-less state makes every draw return None and the rollout matrix excludes
        (or, before the robustness fix, crashed on) the segment. Replaying events
        repopulates pools + EMAs identically to ``update_state_after_episode``.

        A saved em_suite_sampler model_state row is the marker that a segment is
        tracked; segments without one (newly gated, no row yet) are absent.
        """
        rows = self.db.load_all_model_states(self.game_id)
        out: dict[str, SamplerState] = {}
        for r in rows:
            if r["estimator"] != "em_suite_sampler" or not r["state_json"]:
                continue
            seg_id = r["segment_id"]
            attempts = _attempts_from_rows(self.db.get_segment_attempts(seg_id))
            events = _events_from_rows(self.db.get_segment_event_rows(seg_id))
            out[seg_id] = self.estimator.rebuild_state(attempts, events=events)
        return out

    def update_state_after_episode(self, segment_id: str) -> None:
        """Run the em_suite sampler over the latest persisted episode for ``segment_id``.

        Reads the rolled-up episode list from the event-level table and rebuilds
        sampler state from events. Callers must guarantee the new episode is fully
        persisted before invoking this.
        """
        attempt_rows = self.db.get_segment_attempts(segment_id)
        all_attempts = _attempts_from_rows(attempt_rows)
        if not all_attempts:
            return
        event_rows = self.db.get_segment_event_rows(segment_id)
        events = _events_from_rows(event_rows)
        try:
            state = self.estimator.rebuild_state(all_attempts, events=events)
            output = self.estimator.model_output(state, all_attempts, events=events)
            self.db.save_model_state(
                segment_id, self.estimator.name,
                json.dumps(state.to_dict()), json.dumps(output.to_dict()),
            )
            # Invalidate the lazily-built practice engine's column for this
            # segment. Skipped when no engine has been built yet — first access
            # via the ``engine`` property will hydrate from fresh model_state.
            if self._engine is not None:
                self._engine.invalidate(segment_id)
        except Exception:
            logger.exception(
                "update_state_after_episode failed for segment=%s", segment_id,
            )

        # Silent V07 fit. Off the request path but inline (~15ms p50
        # per V1_ESSENCE, well within an end-of-episode budget). Skips
        # cleanly when [fits] isn't installed or the segment has too
        # few events to fit meaningfully.
        self._maybe_refit_segment(segment_id)

    def get_all_model_states(self) -> list[SegmentWithModel]:
        return SegmentWithModel.load_all(self.db, self.game_id, self.estimator.name)

    def set_allocator_weights(self, weights: dict[str, int]) -> None:
        total = sum(weights.values())
        if total != 100:
            raise ValueError(f"Weights must sum to 100, got {total}")
        valid = set(list_allocators())
        for name in weights:
            if name not in valid:
                raise ValueError(f"Unknown allocator: {name!r}. Available: {valid}")
        self.db.save_allocator_config("allocator_weights", json.dumps(weights))
        self._all_weights = dict(weights)
        self.allocator = self._build_mix(weights)

    def rebuild_all_states(self) -> None:
        segments = self.db.get_all_segments_with_model(self.game_id)
        for row in segments:
            segment_id = row["id"]
            attempt_rows = self.db.get_segment_attempts(segment_id)
            if not attempt_rows:
                continue
            all_attempts = _attempts_from_rows(attempt_rows)
            event_rows = self.db.get_segment_event_rows(segment_id)
            events = _events_from_rows(event_rows)
            try:
                state = self.estimator.rebuild_state(all_attempts, events=events)
                output = self.estimator.model_output(state, all_attempts, events=events)
                self.db.save_model_state(
                    segment_id, self.estimator.name,
                    json.dumps(state.to_dict()), json.dumps(output.to_dict()),
                )
            except Exception:
                logger.exception(
                    "rebuild_all_states failed for segment=%s estimator=%s",
                    segment_id, self.estimator.name,
                )

    def _maybe_refit_segment(self, segment_id: str) -> None:
        """Run a streaming v07 refit for ``segment_id`` and persist the payload.

        Reads event-level rows directly (bypassing the episode roll-up)
        because the prototype consumes the raw (outcome, time_ms)
        sequence. Uses the most recent segment_fits row as the warm
        start; falls back to a cold fit on the first call.
        """
        if _refit_segment is None:
            return
        events = self.db.get_segment_event_rows(segment_id)
        if len(events) < _MIN_EVENTS_FOR_FIT:
            return
        attempts = [
            {"outcome": e["outcome"], "time_ms": int(e["time_ms"])}
            for e in events
            if not int(e["invalidated"])
            and AttemptSource(e["source"]) is not AttemptSource.REPLAY
        ]
        if len(attempts) < _MIN_EVENTS_FOR_FIT:
            return
        prev = self.db.load_latest_segment_fit(segment_id, "segment_fit")
        try:
            payload = _refit_segment(
                attempts, segment_id=segment_id, prev_result=prev,
            )
        except Exception:
            logger.exception(
                "segments-v07 refit failed for segment=%s", segment_id,
            )
            return
        self.db.save_segment_fit(segment_id, "segment_fit", payload)

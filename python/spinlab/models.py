"""SpinLab data models."""

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Optional

from pydantic import ConfigDict
from pydantic.dataclasses import dataclass as pydantic_dataclass


class Mode(Enum):
    IDLE = "idle"
    REFERENCE = "reference"
    PRACTICE = "practice"
    REPLAY = "replay"
    FILL_GAP = "fill_gap"
    COLD_FILL = "cold_fill"
    HYPER_PLAY = "hyper_play"


_LEGAL_TRANSITIONS: dict[Mode, set[Mode]] = {
    Mode.IDLE: {Mode.REFERENCE, Mode.PRACTICE, Mode.FILL_GAP, Mode.COLD_FILL, Mode.HYPER_PLAY},
    Mode.REFERENCE: {Mode.IDLE, Mode.REPLAY},
    Mode.PRACTICE: {Mode.IDLE},
    Mode.REPLAY: {Mode.IDLE},
    Mode.FILL_GAP: {Mode.IDLE},
    Mode.COLD_FILL: {Mode.IDLE},
    Mode.HYPER_PLAY: {Mode.IDLE},
}


def transition_mode(current: Mode, target: Mode) -> Mode:
    """Validate and return the target mode, or raise ValueError."""
    if target not in _LEGAL_TRANSITIONS.get(current, set()):
        raise ValueError(f"Illegal mode transition: {current.value} -> {target.value}")
    return target


# Decoded condition values: enum conditions as str labels, bool conditions as bool.
ConditionMap = dict[str, str | bool]


class EndpointType(StrEnum):
    ENTRANCE = "entrance"
    CHECKPOINT = "checkpoint"
    GOAL = "goal"


class Status(StrEnum):
    """Success outcomes from controller actions. Errors are raised as ActionError subclasses."""
    OK = "ok"
    STARTED = "started"
    STOPPED = "stopped"
    NO_GAPS = "no_gaps"


class AttemptSource(StrEnum):
    PRACTICE = "practice"
    REPLAY = "replay"
    REFERENCE = "reference"
    HYPER_PLAY = "hyper_play"


@dataclass
class ActionResult:
    """Typed result from controller actions. Replaces untyped status dicts."""
    status: Status
    new_mode: Mode | None = None
    session_id: str | None = None

    def to_response(self) -> dict:
        """API-safe dict — strips internal fields like new_mode."""
        d: dict = {"status": self.status.value}
        if self.session_id is not None:
            d["session_id"] = self.session_id
        return d


@dataclass
class Segment:
    id: str
    game_id: str
    level_number: int
    start_type: EndpointType
    start_ordinal: int
    end_type: EndpointType
    end_ordinal: int
    description: str = ""
    active: bool = True
    ordinal: int = 0
    capture_run_id: Optional[str] = None
    start_waypoint_id: Optional[str] = None
    end_waypoint_id: Optional[str] = None
    is_primary: bool = True
    capture_session_id: Optional[str] = None

    @staticmethod
    def make_id(game_id: str, level: int, start_type: str, start_ord: int,
                end_type: str, end_ord: int,
                start_waypoint_id: str, end_waypoint_id: str) -> str:
        return (f"{game_id}:{level}:{start_type}.{start_ord}:{end_type}.{end_ord}"
                f":{start_waypoint_id[:8]}:{end_waypoint_id[:8]}")


@dataclass
class Waypoint:
    id: str
    game_id: str
    level_number: int
    endpoint_type: EndpointType
    ordinal: int
    conditions_json: str     # canonical JSON (sorted keys)

    @staticmethod
    def make(game_id: str, level_number: int, endpoint_type: "EndpointType",
             ordinal: int, conditions: ConditionMap) -> "Waypoint":
        canonical = json.dumps(conditions, sort_keys=True, separators=(", ", ": "))
        h = hashlib.sha256(
            f"{game_id}:{level_number}:{endpoint_type}.{ordinal}:{canonical}".encode()
        ).hexdigest()[:16]
        return Waypoint(
            id=h,
            game_id=game_id,
            level_number=level_number,
            endpoint_type=endpoint_type,
            ordinal=ordinal,
            conditions_json=canonical,
        )


@dataclass
class WaypointSaveState:
    waypoint_id: str
    variant_type: str        # 'cold', 'hot'
    state_path: str


@dataclass
class Attempt:
    """An episode-shaped attempt of a segment.

    After the segments-v07 Phase 0 refactor, the on-disk ``attempts`` table is
    event-level (one row per died/survived event). This dataclass retains the
    episode shape as a *convenience input* for test factories and the legacy
    ``Database.log_attempt`` shim, which splits it into N+1 event rows on
    insert and reconstructs it on read via ``Database.get_segment_attempts``.

    Exactly one of ``session_id`` or ``capture_run_id`` is set, enforced by a
    DB CHECK constraint. Practice and speed-run attempts use ``session_id``
    (FK to ``sessions``); reference-seeded attempts use ``capture_run_id``
    (FK to ``capture_runs``). ``source`` distinguishes practice from speed-run
    within the session-attempt category.
    """
    segment_id: str
    completed: bool
    session_id: str | None = None
    capture_run_id: str | None = None
    time_ms: int | None = None
    source: AttemptSource = AttemptSource.PRACTICE
    deaths: int = 0
    clean_tail_ms: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    invalidated: bool = False
    chosen_allocator: str | None = None

    def __post_init__(self) -> None:
        if (self.session_id is None) == (self.capture_run_id is None):
            raise ValueError(
                "Attempt requires exactly one of session_id or capture_run_id"
            )


# Penalty added to the OLD episode-shaped time_ms per death (3.2s, matching the
# PracticeSession default in practice.py). The legacy adapter uses this to
# rebuild episode-total time_ms from raw per-event wall-clock values, so the
# value MUST stay in lockstep with PracticeSession.death_penalty_ms until the
# v07 model takes over and penalties move out of the estimator pipeline.
DEFAULT_DEATH_PENALTY_MS = 3200


class AttemptOutcome(StrEnum):
    DIED = "died"
    SURVIVED = "survived"


@dataclass(frozen=True)
class EventAttempt:
    """A single died-or-survived event within an episode.

    Wire shape for the post-Phase-0 ``attempts`` table. One row per Death or
    LevelExit/Checkpoint event. Episodes group consecutive events by
    ``episode_id`` (minted at ``PracticeTiming.arm`` time and carried through
    every event of that armed attempt). The legacy adapter reconstructs an
    ``Attempt`` from a sorted run of these rows.

    ``time_ms`` is the *raw* wall-clock elapsed since the preceding event in
    the episode (or since arm() for the first event). No death-penalty math —
    the penalty lives in the adapter so the v07 segments model gets the clean
    number directly.
    """
    segment_id: str
    episode_id: str
    outcome: AttemptOutcome
    time_ms: int
    session_id: str | None = None
    capture_run_id: str | None = None
    source: AttemptSource = AttemptSource.PRACTICE
    chosen_allocator: str | None = None
    invalidated: bool = False
    is_hot: bool = False
    # "Science" attempt: counts toward floor/PB but excluded from the model.
    # Mirrors ``invalidated`` but with the opposite floor behavior.
    experimental: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if (self.session_id is None) == (self.capture_run_id is None):
            raise ValueError(
                "EventAttempt requires exactly one of session_id or capture_run_id"
            )


@dataclass
class SegmentCommand:
    """Practice-loop directive: which segment to load next."""
    id: str
    state_path: str
    description: str
    end_type: str              # 'checkpoint' or 'goal'
    expected_time_ms: int | None = None
    auto_advance_delay_ms: int = 1000
    death_penalty_ms: int = 3200

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class CaptureRunStatus(StrEnum):
    DRAFT = "draft"
    SAVED = "saved"


class CaptureRunKind(StrEnum):
    LIVE = "live"
    REPLAY = "replay"


@dataclass
class AttemptRecord:
    """Attempt data flowing through the estimator pipeline."""
    time_ms: int | None          # total time including deaths; None if incomplete
    completed: bool
    deaths: int                  # 0 if clean
    clean_tail_ms: int | None    # time from last death to finish; None if incomplete
    created_at: str              # ISO timestamp


@pydantic_dataclass(config=ConfigDict(extra="allow"))
class Estimate:
    """One coherent set of predictions for a single time series.

    Pydantic dataclass: behaves as a plain @dataclass at runtime (asdict,
    fields, ``to_dict``/``from_dict`` all work) AND emits an OpenAPI schema
    so api_schemas.py can re-export it rather than re-declaring.
    """
    expected_ms: float | None = None
    ms_per_attempt: float | None = None
    floor_ms: float | None = None

    def to_dict(self) -> dict:
        return {
            "expected_ms": self.expected_ms,
            "ms_per_attempt": self.ms_per_attempt,
            "floor_ms": self.floor_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Estimate":
        return cls(
            expected_ms=d.get("expected_ms"),
            ms_per_attempt=d.get("ms_per_attempt"),
            floor_ms=d.get("floor_ms"),
        )


@pydantic_dataclass(config=ConfigDict(extra="allow"))
class DeathExtras:
    """Death-aware extras (deprecated; no producer in the current model layer).

    Kept on the ``ModelOutput.extras`` schema so old persisted rows can still
    deserialize without a migration. The surviving EMA-suite sampler leaves
    ``ModelOutput.extras = None``; this dataclass survives only as a passive
    carrier until Spec #2 redesigns the segment-detail payload.

    Two granularities:
      * Life-level (n_lives_*, p_die_per_life): each EventAttempt is one life.
        Drives total.expected_ms via the geometric formula.
      * Episode-level (n_attempts_*, n_episodes_*, p_die_per_attempt): each
        episode_id is one player attempt. Surfaced for player intuition.

    n_episodes_with_death_eff and n_episodes_completed_eff are NOT
    complementary — an episode can both contain deaths and complete. Their
    sum can exceed n_attempts_effective.
    """
    halflife_attempts: int

    # Episode-level (player intuition)
    n_attempts_effective: float
    n_episodes_with_death_eff: float
    n_episodes_completed_eff: float
    p_die_per_attempt: float | None

    # Life-level (drives geometric formula)
    n_lives_died_effective: float
    n_lives_survived_effective: float
    p_die_per_life: float | None

    # Distributions (life-level samples)
    death_samples: list[tuple[int, float]]
    completion_samples: list[tuple[int, float]]
    expected_death_time_ms: float | None
    expected_completion_time_ms: float | None

    def to_dict(self) -> dict:
        return {
            "halflife_attempts": self.halflife_attempts,
            "n_attempts_effective": self.n_attempts_effective,
            "n_episodes_with_death_eff": self.n_episodes_with_death_eff,
            "n_episodes_completed_eff": self.n_episodes_completed_eff,
            "p_die_per_attempt": self.p_die_per_attempt,
            "n_lives_died_effective": self.n_lives_died_effective,
            "n_lives_survived_effective": self.n_lives_survived_effective,
            "p_die_per_life": self.p_die_per_life,
            "death_samples": [list(s) for s in self.death_samples],
            "completion_samples": [list(s) for s in self.completion_samples],
            "expected_death_time_ms": self.expected_death_time_ms,
            "expected_completion_time_ms": self.expected_completion_time_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DeathExtras":
        return cls(
            halflife_attempts=d["halflife_attempts"],
            n_attempts_effective=d["n_attempts_effective"],
            n_episodes_with_death_eff=d["n_episodes_with_death_eff"],
            n_episodes_completed_eff=d["n_episodes_completed_eff"],
            p_die_per_attempt=d["p_die_per_attempt"],
            n_lives_died_effective=d["n_lives_died_effective"],
            n_lives_survived_effective=d["n_lives_survived_effective"],
            p_die_per_life=d["p_die_per_life"],
            death_samples=[tuple(s) for s in d["death_samples"]],
            completion_samples=[tuple(s) for s in d["completion_samples"]],
            expected_death_time_ms=d["expected_death_time_ms"],
            expected_completion_time_ms=d["expected_completion_time_ms"],
        )


@pydantic_dataclass(config=ConfigDict(extra="allow"))
class ModelOutput:
    """What every estimator produces — predictions for total time and clean tail.

    Pydantic dataclass: see ``Estimate`` for rationale.
    """
    total: Estimate
    clean: Estimate
    extras: DeathExtras | None = None
    # Closed-form practice gain (ms): expected_now - expected_after_one_slope_step.
    # Positive = practicing is predicted to reduce episode time. None when the
    # slope is ungated. Mirrors live_view.practice_gain_ms; surfaced here so the
    # planning table can show a Practice/Trend% column without a second endpoint.
    practice_gain_ms: float | None = None

    def to_dict(self) -> dict:
        return {
            "total": self.total.to_dict(),
            "clean": self.clean.to_dict(),
            "extras": self.extras.to_dict() if self.extras is not None else None,
            "practice_gain_ms": self.practice_gain_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelOutput":
        extras_d = d.get("extras")
        return cls(
            total=Estimate.from_dict(d["total"]),
            clean=Estimate.from_dict(d["clean"]),
            extras=DeathExtras.from_dict(extras_d) if extras_d is not None else None,
            practice_gain_ms=d.get("practice_gain_ms"),
        )

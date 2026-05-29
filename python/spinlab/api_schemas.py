"""Pydantic request/response models for every public API endpoint.

These exist primarily so FastAPI emits a complete OpenAPI schema; that schema
is then consumed by ``openapi-typescript`` to generate ``frontend/src/api-types.ts``.
Keep this file the single source of truth — when a response shape changes,
edit the model here and the frontend types update on the next ``npm run build``.

Conventions
-----------
- Nullable fields use ``T | None``, never ``Optional[T]``, to match the rest of
  the codebase.
- Response models use ``model_config = ConfigDict(extra="allow")``: extra keys
  in a handler's returned dict pass through to the response (rather than being
  silently dropped). Drift surfaces — if ``state_builder`` adds a field but
  ``AppState`` doesn't know about it, the field appears in /api/state, the
  codegen picks it up, and the frontend sees it.
- Action-style POST responses share ``ActionResponse``; richer shapes get
  their own model.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------

class _BaseResponse(BaseModel):
    """Permissive base: extra keys pass through to the response instead of
    being silently filtered. See module docstring for the rationale."""
    model_config = ConfigDict(extra="allow")


# Re-export from models.py so there is a single source of truth. Pydantic v2
# treats Enum / StrEnum as a value type whose OpenAPI schema is a string enum,
# which openapi-typescript emits as a TS string union — identical wire format
# to the prior Literal alias, but with no second definition to drift.
#
# ``Estimate`` and ``ModelOutput`` are pydantic.dataclasses.dataclass over in
# models.py — they ARE dataclasses (asdict/fields/to_dict still work) AND
# Pydantic schema sources, so FastAPI generates OpenAPI definitions from the
# same class the estimator pipeline constructs and state_builder serializes.
# DeathExtras is referenced directly by EstimatorCurves.final_extras below,
# so it's a first-class API contract type. The explicit re-export also
# keeps it in api_schemas.py's namespace transitively via ModelOutput.extras.
from spinlab.models import ConditionMap, DeathExtras, Mode, ModelOutput, Status  # noqa: E402, I001 — kept beside its explanatory block above

CaptureRunStatus = Literal["draft", "saved"]
CaptureRunKind = Literal["live", "replay"]


# ---------------------------------------------------------------------------
# /api/state and SSE payload
# ---------------------------------------------------------------------------

class CurrentSegment(_BaseResponse):
    id: str
    game_id: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int
    description: str
    attempt_count: int
    model_outputs: dict[str, ModelOutput]
    selected_model: str
    state_path: str | None


class RecentAttempt(_BaseResponse):
    id: int
    segment_id: str
    completed: int
    time_ms: int | None
    description: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int
    invalidated: bool  # Pydantic coerces the underlying 0/1 INTEGER column.
    chosen_allocator: str | None


class SessionInfo(_BaseResponse):
    id: str
    started_at: str
    segments_attempted: int
    segments_completed: int
    saved_total_ms: float | None
    saved_clean_ms: float | None


class ReplayState(_BaseResponse):
    rec_path: str | None
    total: int


class PausedRunState(_BaseResponse):
    run_id: str
    segments_captured: int
    session_count: int


class ColdFillState(_BaseResponse):
    current: int
    total: int
    segment_label: str


class AppState(_BaseResponse):
    """Full dashboard state. Returned by GET /api/state and pushed via SSE.

    Contract: every field is always present. Nullable fields hold ``None``
    when not applicable (e.g. ``replay`` is None outside REPLAY mode). The
    frontend therefore never has to handle "missing key" as a third state
    distinct from "present but null".
    """
    mode: Mode
    emu_connected: bool
    game_id: str | None
    game_name: str | None
    current_segment: CurrentSegment | None
    recent: list[RecentAttempt]
    session: SessionInfo | None
    sections_captured: int | None
    allocator_weights: dict[str, int] | None
    estimator: str | None
    capture_run_id: str | None
    replay: ReplayState | None
    paused_run: PausedRunState | None
    cold_fill: ColdFillState | None
    has_active_run: bool


# ---------------------------------------------------------------------------
# Action responses (POST /api/practice/start, /reference/stop, etc.)
# ---------------------------------------------------------------------------

class ActionResponse(_BaseResponse):
    """Generic response for action endpoints. ``status`` is the outcome
    enum; ``session_id`` is set only when an action started a session."""
    status: Status
    session_id: str | None = None


class OkResponse(_BaseResponse):
    status: Status


# ---------------------------------------------------------------------------
# Model tab — /api/model
# ---------------------------------------------------------------------------

class EstimatorInfo(_BaseResponse):
    name: str
    display_name: str


class ModelSegment(_BaseResponse):
    segment_id: str
    description: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int
    selected_model: str
    model_outputs: dict[str, ModelOutput]
    n_completed: int
    n_attempts: int
    gold_ms: int | None = None
    clean_gold_ms: int | None = None


class ModelData(_BaseResponse):
    estimator: str | None = None
    estimators: list[EstimatorInfo] = []
    allocator_weights: dict[str, int] | None = None
    segments: list[ModelSegment] = []


# ---------------------------------------------------------------------------
# Estimator tuning — /api/estimator-params
# ---------------------------------------------------------------------------

class ParamDef(_BaseResponse):
    name: str
    display_name: str
    default: float
    min: float
    max: float
    step: float
    description: str
    value: float


class TuningData(_BaseResponse):
    estimator: str | None = None
    params: list[ParamDef] = []


# ---------------------------------------------------------------------------
# Allocator / estimator config endpoints
# ---------------------------------------------------------------------------

class AllocatorWeightsResponse(_BaseResponse):
    weights: dict[str, int]


class EstimatorSwitchRequest(BaseModel):
    name: str


class EstimatorSwitchResponse(_BaseResponse):
    estimator: str


class EstimatorParamsRequest(BaseModel):
    params: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Segments — /api/segments + history
# ---------------------------------------------------------------------------

class ApiSegment(_BaseResponse):
    id: str
    game_id: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int
    description: str
    active: int
    ordinal: int | None = None
    state_path: str | None = None
    is_primary: bool
    has_cold_state: bool
    start_waypoint_id: str | None = None
    end_waypoint_id: str | None = None
    start_conditions: ConditionMap = {}
    end_conditions: ConditionMap = {}


class SegmentsResponse(_BaseResponse):
    segments: list[ApiSegment]


class SegmentPatchRequest(BaseModel):
    is_primary: bool | None = None
    description: str | None = None
    active: bool | None = None


class SegmentPatchResponse(_BaseResponse):
    ok: bool
    id: str
    is_primary: bool | None = None


class SegmentAttempt(_BaseResponse):
    attempt_number: int
    time_ms: int | None = None
    clean_tail_ms: int | None = None
    deaths: int
    created_at: str


class EstimatorSeries(_BaseResponse):
    expected_ms: list[float | None]
    floor_ms: list[float | None]


class EstimatorCurves(_BaseResponse):
    total: EstimatorSeries
    clean: EstimatorSeries
    # DeathExtras from the estimator's final state (after every completed
    # attempt). None when the estimator doesn't publish death-aware extras
    # (every estimator other than death_aware_rolling today) or when the
    # segment has no completed attempts. Drives the death-histogram panel
    # on the segment detail page.
    final_extras: DeathExtras | None = None


class ColdBin(_BaseResponse):
    """One time bin of the cold-attempt distribution."""

    lo_ms: float
    hi_ms: float
    n_deaths: int        # raw count of cold deaths landing in this bin
    n_completions: int   # raw count of cold completions landing in this bin
    hazard: float | None = None   # empirical hazard in this bin; None when at_risk_w == 0
    at_risk_w: float = 0.0        # weighted at-risk count entering this bin


class ColdDistribution(_BaseResponse):
    """Per-segment cold-attempt distribution payload.

    Feeds the "Cold distribution" panel on the segment-detail page.
    Histogram view reads bin counts; hazard view reads hazard/at_risk_w
    from the same bins.

    Aggregates (mu_*, p_die_*) are cold-only — derived from the same
    cold pool the bins were computed from, NOT from DAR's all-events
    aggregates.
    """

    bins: list[ColdBin]
    n_cold_attempts: int                  # raw cold count after truncation; drives bin layout
    mu_d_ms: float | None                 # weighted mean cold-death time; None when no deaths
    mu_c_ms: float | None                 # weighted mean cold-completion time; None when no completions
    p_die_per_attempt: float | None       # weighted P(any death in attempt); None when n_cold=0
    p_die_per_life: float | None          # weighted P(this life dies); None when no events
    halflife: int = 0                     # halflife used to compute this distribution; echoed for label/debug


class SegmentHistory(_BaseResponse):
    segment_id: str
    description: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int
    attempts: list[SegmentAttempt]
    estimator_curves: dict[str, EstimatorCurves]
    # Name of the currently active estimator (mirrors sched.estimator.name).
    # None when no game is loaded — the segment can still be queried
    # standalone (the route doesn't require a game context to look up a
    # segment by id), but with no scheduler there's no active estimator
    # to name. Frontend uses this to pick which estimator_curves entry's
    # final_extras to render; falls through to the empty state when None.
    selected_model: str | None = None
    # Cold-only distribution for the segment-detail panel (histogram +
    # hazard). None when there are no cold events for this segment.
    cold_distribution: ColdDistribution | None = None


# ---------------------------------------------------------------------------
# References / capture sessions
# ---------------------------------------------------------------------------

class Reference(_BaseResponse):
    id: str
    game_id: str
    name: str
    created_at: str
    status: CaptureRunStatus
    active: int
    kind: CaptureRunKind
    has_replay: bool


class ReferencesResponse(_BaseResponse):
    references: list[Reference]


class CaptureSession(_BaseResponse):
    id: str
    capture_run_id: str
    ordinal: int
    started_at: str
    ended_at: str | None = None
    end_reason: str | None = None
    segment_count: int


class CaptureSessionsResponse(_BaseResponse):
    sessions: list[CaptureSession]


class ReferenceSegment(_BaseResponse):
    id: str
    game_id: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int
    description: str
    active: int
    ordinal: int | None = None
    capture_run_id: str | None = None
    capture_session_id: str | None = None
    session_ordinal: int | None = None
    state_path: str | None = None


class ReferenceSegmentsResponse(_BaseResponse):
    segments: list[ReferenceSegment]


class ReplayExistsResponse(_BaseResponse):
    exists: bool
    path: str | None = None


class ReplayStartRequest(BaseModel):
    ref_id: str
    session_id: str | None = None
    speed: int = 0


class ReferenceFinalizeRequest(BaseModel):
    name: str = "Untitled"


class ReferenceRenameRequest(BaseModel):
    name: str


# ---------------------------------------------------------------------------
# Attempts — PATCH /api/attempts/{id}
# ---------------------------------------------------------------------------

class AttemptPatchRequest(BaseModel):
    invalidated: bool


class AttemptPatchResponse(_BaseResponse):
    ok: bool
    id: int
    invalidated: bool


# ---------------------------------------------------------------------------
# Sessions list — GET /api/sessions
# ---------------------------------------------------------------------------

class SessionRow(_BaseResponse):
    id: str
    game_id: str
    started_at: str
    ended_at: str | None = None
    segments_attempted: int
    segments_completed: int


class SessionsResponse(_BaseResponse):
    sessions: list[SessionRow]


# ---------------------------------------------------------------------------
# ROMs / emulator launch / shutdown
# ---------------------------------------------------------------------------

class RomsResponse(_BaseResponse):
    roms: list[str]
    recently_played: list[str] = []
    recently_added: list[str] = []
    error: str | None = None


class EmulatorLaunchRequest(BaseModel):
    rom: str = ""


class EmulatorLaunchResponse(_BaseResponse):
    status: str


class ShutdownResponse(_BaseResponse):
    status: str

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

from spinlab.protocol import SPEED_NORMAL

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
    # Count of active-run segments whose start has a hot but no cold save state
    # (i.e. cold-fill has something to do). 0 when there's no active run.
    # Drives the cold-capture button's visibility on the frontend.
    segments_missing_cold: int
    # True when a frozen (clean-stopped) practice snapshot persists; drives the
    # idle "frozen session" practice-card state on the frontend.
    has_frozen_session: bool


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
    allocator_weights: dict[str, int] | None = None
    segments: list[ModelSegment] = []


# ---------------------------------------------------------------------------
# Allocator config endpoint
# ---------------------------------------------------------------------------

class AllocatorWeightsResponse(_BaseResponse):
    weights: dict[str, int]


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
    # attempt). None when the segment has no completed attempts. The current
    # EMA-suite sampler does not publish death-aware extras, so this is
    # presently always None — the field is held in the schema until Spec #2
    # redesigns the segment-detail payload.
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
    # Weighted std and log-moments back the Normal/Lognormal PDF overlays on
    # the histogram. Completions only — death-time density is
    # hazard(t) * S(t) / P(die), so a Normal/Lognormal fit there mashes two
    # things together; the hazard view is the principled tool for that side.
    # All None when the underlying sample is empty or non-positive.
    sigma_c_ms: float | None = None       # weighted std of cold-completion times
    mu_log_c: float | None = None         # weighted mean of ln(t_ms) over cold completions
    sigma_log_c: float | None = None      # weighted std of ln(t_ms) over cold completions
    p_die_per_attempt: float | None       # weighted P(any death in attempt); None when n_cold=0
    p_die_per_life: float | None          # weighted P(this life dies); None when no events
    halflife: int = 0                     # halflife used to compute this distribution; echoed for label/debug


class SurgeryAttempt(_BaseResponse):
    id: int
    order: int
    clean_tail_ms: int | None = None
    total_ms: int | None = None
    deaths: int
    created_at: str
    completed: bool
    invalidated: bool
    is_floor: bool


class SurgeryAttemptsResponse(_BaseResponse):
    segment_id: str
    attempts: list[SurgeryAttempt]


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
    # Default to real-time playback for a user-clicked Replay. Callers that want
    # the fast-forward sweep (e.g. the replay-fixture test, or a future "Fast
    # Replay" / DB-regeneration mode) pass SPEED_UNCAPPED (0) explicitly.
    speed: int = SPEED_NORMAL


class GrindStartRequest(BaseModel):
    """Start practice pinned to one segment (GrindOne)."""
    segment_id: str


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


# ---------------------------------------------------------------------------
# EMA-Suite Sampler — per-segment prediction matrix
# See docs/superpowers/specs/2026-05-30-em-suite-sampler-design.md
# ---------------------------------------------------------------------------

class EmSuiteMatrixResponse(_BaseResponse):
    """Per-segment prediction matrix.

    The matrix is upper triangular: matrix[fast_idx][slow_idx] is non-None
    iff fast_idx > slow_idx. The diagonal is reserved for the baseline row
    (no slope; one number per alpha).

    All times are milliseconds. None values mean either insufficient data
    (n_successes / n_deaths / n_attempts < 2) or geometric mean diverges
    (p_die at the suite alpha is ~1).
    """

    segment_id: str
    alpha_grid: list[float]
    baseline: list[float | None]
    matrix: list[list[float | None]]
    n_attempts_total: int
    n_successes: int
    n_deaths: int
    # Per-attempt history for the three underlying quantities. Each value is
    # a 2D array shaped [n_alphas][n_snapshots] where n_snapshots ==
    # n_attempts_total + 1 (snapshot 0 = empty initial state). Times are
    # log(time_ms); the frontend exponentiates for display. Inner cells are
    # None when that alpha has no data yet at that snapshot (denominator 0).
    param_history: dict[str, list[list[float | None]]]
    # Three 10x10 upper-triangular slope matrices keyed by
    # `slope_log_success`, `slope_log_death`, `slope_logit_p`. Cell
    # [fast][slow] is `E_fast - E_slow` in the quantity's working space.
    # Cells are None when the prediction gate fails, when either EMA is
    # undefined, or when fast_idx <= slow_idx.
    slope_matrices: dict[str, list[list[float | None]]]


# ---------------------------------------------------------------------------
# Segment progress — /api/segments/{id}/progress
# ---------------------------------------------------------------------------

class SegmentProgressResponse(_BaseResponse):
    """'Am I improving?' payload for one segment. See segment_progress()."""
    segment_id: str
    ready: bool
    verdict: Literal["faster", "holding", "slower", "not_ready"]
    now_clear_ms: float | None
    baseline_clear_ms: float | None
    death_rate: float
    consistency_ms: float | None
    gap_to_gold_ms: float | None
    pb_ms: float | None
    trend_ms: list[float] = []
    # Gate diagnostics so the view can say "need N more" inline.
    n_successes: int
    n_deaths: int


class LiveSegmentViewResponse(_BaseResponse):
    """Closed-form live-view payload for one segment. See live_segment_view()."""
    segment_id: str
    ready: bool
    expected_episode_ms: float | None
    practice_gain_ms: float | None
    death_rate: float
    floor_ms: float | None
    last_episode_ms: float | None
    last_clean_ms: float | None
    last_deaths: int | None
    last_rank: int | None
    series: list[dict] = []
    n_successes: int
    n_deaths: int
    # Session diffs — None when no active practice session OR either side missing.
    expected_episode_diff_ms: float | None = None
    practice_gain_diff_ms: float | None = None
    floor_diff_ms: float | None = None
    death_rate_diff: float | None = None


class RouteSummaryResponse(_BaseResponse):
    """Closed-form whole-run aggregate for the route bar. See route_summary()."""
    game_id: str
    exp_run_ms: float | None
    exp_deaths: float | None
    n_estimable: int
    n_skipped: int
    # Session-overlay fields — None when no active practice session.
    session_started_at: float | None = None  # epoch seconds
    exp_run_diff_ms: float | None = None
    exp_deaths_diff: float | None = None
    practice_saved_ms: float | None = None
    floor_improvement_ms: float | None = None  # Sum over segments of max(0, baseline_floor - current_floor), positive = improved
    run_series: list[float] = []           # route Exp.Run after each in-session event
    baseline_exp_run_ms: float | None = None  # session-start Exp.Run (the "start" line)
    floor_total_ms: float | None = None    # sum of segment floors (theoretical best run)
    floor_series: list[float | None] = []  # floor-over-time, aligned point-for-point with run_series; None when no segment has a floor yet
    session_ended_at: float | None = None  # epoch seconds; set when the session is frozen
    # R-menu / Practice Pause overlay.
    menu_armed: bool = False               # R held past threshold -> show 'X — Pause' hint
    session_paused_at: float | None = None # epoch seconds the current pause began; None = not paused
    session_pause_offset_sec: float = 0.0  # accumulated completed-pause seconds; route bar subtracts it
    grind_segment_id: str | None = None    # GrindOne: the pinned segment, else None
    # Science / no-record mode overlay.
    experimental: bool = False             # live session in Science mode -> show 'Science' badge


# ---------------------------------------------------------------------------
# Practice Simulation Engine — /api/practice-engine/*
# ---------------------------------------------------------------------------

class PracticeEngineSegmentState(_BaseResponse):
    seg_id: str
    description: str
    level_number: int
    # Endpoint structure so the frontend can format names with the shared
    # segmentName()/shortEndpoint() helpers (identical labels to other tabs).
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int
    # None when the closed-form expected_episode_time_scalar refuses to compute
    # (p_die >= 1 - LOGIT_EPS: geometric mean diverges as p -> 1). Empirically
    # samplable segments in that corner remain gated but show "—" here; previously
    # the route returned a fabricated 0.0 (silent fallback violates model_principles).
    e_sample_0_ms: float | None
    e_sample_1_ms: float | None
    pool_success: int
    pool_death: int
    gold_ms: int | None = None  # backs the "fill from gold" dashboard helper


class PracticeEngineUngated(_BaseResponse):
    seg_id: str
    reason: str
    description: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int


class PracticeEngineState(_BaseResponse):
    gated_segments: list[PracticeEngineSegmentState] = []
    ungated_segments: list[PracticeEngineUngated] = []
    matrix_built_at: str | None = None
    N: int


class PracticeEngineEvaluateRequest(BaseModel):
    policy: Literal["no_reset", "target_paced"]
    policy_kwargs: dict = {}  # cum_splits_ms, slack — optional, validated server-side
    objective: Literal[
        "expected_wall_clock_per_attempt",
        "expected_total_finished_time",
        "q",
        "quantile",
        "p_pb_this_session",
    ]
    objective_ctx: dict = {}  # target_ms, p, session_remaining_ms — validated server-side


class PracticeEnginePerSegmentValue(_BaseResponse):
    seg_id: str
    value: float
    value_per_second: float | None
    e_sample_0_ms: float
    e_sample_1_ms: float


class PracticeEngineTotalTimeSummary(_BaseResponse):
    bins: list[float] = []
    counts: list[int] = []
    mean: float | None = None
    median: float | None = None
    p10: float | None = None
    p90: float | None = None
    finished_pct: float
    aborted_by_segment: dict[str, int] = {}


class PracticeEngineEvaluateResponse(_BaseResponse):
    objective_value: float | None
    per_segment_values: list[PracticeEnginePerSegmentValue] = []
    total_time_summary: PracticeEngineTotalTimeSummary

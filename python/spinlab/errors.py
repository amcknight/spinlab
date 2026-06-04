"""Typed exceptions for controller-layer failures.

Raised by SessionManager / capture controllers when an action cannot proceed.
Translated to HTTPException at the route boundary by create_app's handler.

Each subclass sets:
  http_code: HTTP status to return when caught at the route boundary
  detail:    stable wire-format string (was Status.<name>.value before pruning)
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Mode


class ActionError(Exception):
    """Base for all controller-layer action failures."""
    http_code: int = 500
    detail: str = "internal_error"

    def __init__(self) -> None:
        super().__init__(self.detail)


class NotConnectedError(ActionError):
    http_code = 503
    detail = "not_connected"


class NoGameLoadedError(ActionError):
    """No game is loaded; the requested action needs an active game context."""
    http_code = 409
    detail = "no_game_loaded"


class WrongModeError(ActionError):
    """Action is incompatible with the current Mode.

    Use when an action requires a specific mode (e.g., cold-fill start requires
    IDLE, stop_replay requires REPLAY) and the system is in something else.
    Carries `current_mode` for callers/loggers/tests; the default ActionError
    handler in dashboard.py only serializes `detail`, so the HTTP body is just
    `{"detail": "wrong_mode"}`. A specialized handler can read `current_mode`
    if it wants to surface a human-readable reason in the future.
    """
    http_code = 409
    detail = "wrong_mode"

    def __init__(self, current_mode: "Mode") -> None:
        super().__init__()
        self.current_mode = current_mode


class DraftPendingError(ActionError):
    """A draft reference run is pending save or discard."""
    http_code = 409
    detail = "draft_pending"


class SessionDeleteAfterFinalizeError(ActionError):
    """Cannot delete a capture session after the run has been finalized."""
    http_code = 409
    detail = "session_delete_after_finalize"


class SessionInUseError(ActionError):
    """Cannot delete a capture session while it is being recorded into."""
    http_code = 409
    detail = "session_in_use"


class PracticeActiveError(ActionError):
    http_code = 409
    detail = "practice_active"


class ReferenceActiveError(ActionError):
    http_code = 409
    detail = "reference_active"


class AlreadyRunningError(ActionError):
    http_code = 409
    detail = "already_running"


class AlreadyReplayingError(ActionError):
    http_code = 409
    detail = "already_replaying"


class NotInReferenceError(ActionError):
    http_code = 409
    detail = "not_in_reference"


class NoPausedRunError(ActionError):
    """No paused capture run exists for the requested operation."""
    http_code = 409
    detail = "no_paused_run"


class NotReplayingError(ActionError):
    http_code = 409
    detail = "not_replaying"


class NotRunningError(ActionError):
    http_code = 409
    detail = "not_running"


class MissingSaveStatesError(ActionError):
    http_code = 409
    detail = "missing_save_states"


class NoDraftError(ActionError):
    http_code = 404
    detail = "no_draft"


class NoHotVariantError(ActionError):
    http_code = 404
    detail = "no_hot_variant"


class BackendNotImplementedError(ActionError):
    """A command isn't implemented on the active emulator backend.

    Surfaces cleanly as an HTTP 501 with a clear detail string instead of a
    generic 500.
    """
    http_code = 501
    detail = "backend_not_implemented"


class SnapshotFailedError(ActionError):
    """The practice/hyper-play session snapshot could not be computed.

    Raised by SessionManager.start_practice/start_hyper_play when the
    baseline snapshot build raises. The session is rolled back to IDLE
    before this is raised, so the caller can safely retry.
    """
    http_code = 500
    detail = "snapshot_failed"

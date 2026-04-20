"""Typed exceptions for controller-layer failures.

Raised by SessionManager / capture controllers when an action cannot proceed.
Translated to HTTPException at the route boundary by create_app's handler.

Each subclass sets:
  http_code: HTTP status to return when caught at the route boundary
  detail:    stable wire-format string (was Status.<name>.value before pruning)
"""
from __future__ import annotations


class ActionError(Exception):
    """Base for all controller-layer action failures."""
    http_code: int = 500
    detail: str = "internal_error"

    def __init__(self) -> None:
        super().__init__(self.detail)


class NotConnectedError(ActionError):
    http_code = 503
    detail = "not_connected"


class DraftPendingError(ActionError):
    http_code = 409
    detail = "draft_pending"


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

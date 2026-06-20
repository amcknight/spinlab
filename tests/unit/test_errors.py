"""Tests for the ActionError exception hierarchy."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from spinlab.errors import (
    ActionError,
    AlreadyReplayingError,
    AlreadyRunningError,
    DraftPendingError,
    MissingSaveStatesError,
    NoDraftError,
    NoHotVariantError,
    NoPausedRunError,
    NotConnectedError,
    NotInReferenceError,
    NotReplayingError,
    NotRunningError,
    PracticeActiveError,
    ReferenceActiveError,
    SessionInUseError,
)

ERROR_TABLE = [
    (NotConnectedError, 503, "not_connected"),
    (DraftPendingError, 409, "draft_pending"),
    (PracticeActiveError, 409, "practice_active"),
    (ReferenceActiveError, 409, "reference_active"),
    (AlreadyRunningError, 409, "already_running"),
    (AlreadyReplayingError, 409, "already_replaying"),
    (NotInReferenceError, 409, "not_in_reference"),
    (NoPausedRunError, 409, "no_paused_run"),
    (NotReplayingError, 409, "not_replaying"),
    (NotRunningError, 409, "not_running"),
    (MissingSaveStatesError, 409, "missing_save_states"),
    (NoDraftError, 404, "no_draft"),
    (NoHotVariantError, 404, "no_hot_variant"),
    (SessionInUseError, 409, "session_in_use"),
]


@pytest.mark.parametrize("cls,http_code,detail", ERROR_TABLE)
def test_action_error_attributes(cls, http_code, detail):
    exc = cls()
    assert isinstance(exc, ActionError)
    assert exc.http_code == http_code
    assert exc.detail == detail


def test_detail_codes_unique():
    seen: set[str] = set()
    for cls, _, detail in ERROR_TABLE:
        assert detail not in seen, f"duplicate detail {detail}"
        seen.add(detail)


# --- FastAPI handler integration ---


def _build_app_with_raising_route(exc_factory):
    """Minimal app with the ActionError handler wired up."""
    from spinlab.errors import ActionError

    app = FastAPI()

    @app.exception_handler(ActionError)
    async def _handle(request, exc: ActionError):
        return JSONResponse(status_code=exc.http_code, content={"detail": exc.detail})

    @app.get("/boom")
    def boom():
        raise exc_factory()

    return app


@pytest.mark.parametrize("cls,http_code,detail", [
    (NotConnectedError, 503, "not_connected"),
    (DraftPendingError, 409, "draft_pending"),
    (NoDraftError, 404, "no_draft"),
])
def test_handler_maps_action_error_to_response(cls, http_code, detail):
    """The ActionError handler turns a raised error into its HTTP status + detail
    body through a real request — distinct from test_action_error_attributes,
    which only checks the exception's attributes, not the handler wiring."""
    client = TestClient(_build_app_with_raising_route(cls))
    resp = client.get("/boom")
    assert resp.status_code == http_code
    assert resp.json() == {"detail": detail}


def test_no_paused_run_error_distinct_from_not_in_reference():
    from spinlab.errors import NoPausedRunError, NotInReferenceError
    assert NoPausedRunError is not NotInReferenceError
    err = NoPausedRunError()
    assert err.http_code == 409
    assert err.detail == "no_paused_run"


def test_session_in_use_error_shape():
    from spinlab.errors import SessionInUseError
    err = SessionInUseError()
    assert err.http_code == 409
    assert err.detail == "session_in_use"

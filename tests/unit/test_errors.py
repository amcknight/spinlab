"""Tests for the ActionError exception hierarchy."""
from __future__ import annotations

import pytest

from spinlab.errors import (
    ActionError,
    AlreadyReplayingError,
    AlreadyRunningError,
    DraftPendingError,
    MissingSaveStatesError,
    NoDraftError,
    NoHotVariantError,
    NotConnectedError,
    NotInReferenceError,
    NotReplayingError,
    NotRunningError,
    PracticeActiveError,
    ReferenceActiveError,
)


ERROR_TABLE = [
    (NotConnectedError, 503, "not_connected"),
    (DraftPendingError, 409, "draft_pending"),
    (PracticeActiveError, 409, "practice_active"),
    (ReferenceActiveError, 409, "reference_active"),
    (AlreadyRunningError, 409, "already_running"),
    (AlreadyReplayingError, 409, "already_replaying"),
    (NotInReferenceError, 409, "not_in_reference"),
    (NotReplayingError, 409, "not_replaying"),
    (NotRunningError, 409, "not_running"),
    (MissingSaveStatesError, 409, "missing_save_states"),
    (NoDraftError, 404, "no_draft"),
    (NoHotVariantError, 404, "no_hot_variant"),
]


@pytest.mark.parametrize("cls,http_code,detail", ERROR_TABLE)
def test_action_error_attributes(cls, http_code, detail):
    exc = cls()
    assert isinstance(exc, ActionError)
    assert exc.http_code == http_code
    assert exc.detail == detail


def test_action_error_is_exception():
    with pytest.raises(ActionError):
        raise NotConnectedError()


def test_detail_codes_unique():
    seen: set[str] = set()
    for cls, _, detail in ERROR_TABLE:
        assert detail not in seen, f"duplicate detail {detail}"
        seen.add(detail)


# --- FastAPI handler integration ---

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse


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


def test_handler_maps_not_connected_to_503():
    app = _build_app_with_raising_route(NotConnectedError)
    client = TestClient(app)
    resp = client.get("/boom")
    assert resp.status_code == 503
    assert resp.json() == {"detail": "not_connected"}


def test_handler_maps_draft_pending_to_409():
    app = _build_app_with_raising_route(DraftPendingError)
    client = TestClient(app)
    resp = client.get("/boom")
    assert resp.status_code == 409
    assert resp.json() == {"detail": "draft_pending"}


def test_handler_maps_no_draft_to_404():
    app = _build_app_with_raising_route(NoDraftError)
    client = TestClient(app)
    resp = client.get("/boom")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "no_draft"}

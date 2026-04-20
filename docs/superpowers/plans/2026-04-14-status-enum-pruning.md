# Status Enum Pruning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Status enum's negative/error members with typed exceptions caught at the route boundary, leaving only genuine success states on the enum.

**Architecture:** Introduce a small `ActionError` exception hierarchy in `python/spinlab/errors.py`, with one subclass per error state and an `http_code` class attribute. Register a single FastAPI exception handler in `create_app` that translates any `ActionError` to an `HTTPException`. Controller methods `raise` instead of `return ActionResult(status=Status.X)` for error states. `ActionResult` stays, but `Status` shrinks to `{OK, STARTED, STOPPED}` (plus `NO_GAPS` kept as a success outcome — see Task 7). Routes stop calling `_check_result` and return `result.to_response()` directly.

**Tech Stack:** Python 3.11+, FastAPI, pytest. StrEnum for `Status`, dataclasses for `ActionResult`, plain `Exception` subclasses for errors.

**Scope:** spec §6 from `docs/superpowers/specs/2026-04-13-cleanup-pass-design.md`. Not touching `Mode`, `AttemptSource`, `EventType`, `EndpointType` — only `Status`.

---

## Preflight — Current state

Run these before starting so you see the same state as this plan:

```bash
pytest -q
```
Expected: 608 passed, clean baseline.

```bash
git grep -n "Status\." python/spinlab tests
```

Note the consumers. The plan assumes:
- `python/spinlab/models.py:63-80` defines `Status` (17 members) and `ActionResult`
- `python/spinlab/dashboard.py:26-45` has `_ERROR_STATUS_CODES` dict and `_check_result`
- `python/spinlab/session_manager.py`, `python/spinlab/capture/reference.py`, `python/spinlab/capture/cold_fill.py`, `python/spinlab/capture/draft.py` return `ActionResult(status=Status.<error>)` on failure
- Every route in `python/spinlab/routes/*.py` wraps controller calls in `_check_result(...)`

If the picture is materially different (e.g. a new controller), adapt the plan — do not proceed mechanically.

## Classification

The 17 current values break down as:

| Value | Category | Current HTTP | After |
|-------|----------|--------------|-------|
| `OK` | success | 200 | Keep in Status |
| `STARTED` | success | 200 | Keep in Status |
| `STOPPED` | success | 200 | Keep in Status |
| `NO_GAPS` | success-ish | 200 (no mapping) | **Keep as success** (nothing-to-do outcome) |
| `NOT_CONNECTED` | error | 503 | `NotConnectedError` |
| `DRAFT_PENDING` | error | 409 | `DraftPendingError` |
| `PRACTICE_ACTIVE` | error | 409 | `PracticeActiveError` |
| `REFERENCE_ACTIVE` | error | 409 | `ReferenceActiveError` |
| `ALREADY_RUNNING` | error | 409 | `AlreadyRunningError` |
| `ALREADY_REPLAYING` | error | 409 | `AlreadyReplayingError` |
| `NOT_IN_REFERENCE` | error | 409 | `NotInReferenceError` |
| `NOT_REPLAYING` | error | 409 | `NotReplayingError` |
| `NOT_RUNNING` | error | 409 | `NotRunningError` |
| `NO_DRAFT` | error | 404 | `NoDraftError` |
| `NO_HOT_VARIANT` | error | 404 | `NoHotVariantError` |
| `MISSING_SAVE_STATES` | **bug** (returned 200) | — | `MissingSaveStatesError` (409) — fixes latent bug |
| `SHUTTING_DOWN` | **dead** (no call sites) | — | Delete |

After pruning: `Status` = `{OK, STARTED, STOPPED, NO_GAPS}` — four values, all success outcomes.

## File Structure

```
python/spinlab/
  errors.py            # NEW — ActionError base + subclasses
  models.py            # MODIFY — shrink Status enum
  dashboard.py         # MODIFY — replace _ERROR_STATUS_CODES dict + _check_result
                       #          with a single exception handler
  session_manager.py   # MODIFY — raise instead of return error ActionResults
  capture/reference.py # MODIFY — same
  capture/cold_fill.py # MODIFY — same (except NO_GAPS stays as success return)
  capture/draft.py     # MODIFY — same
  routes/*.py          # MODIFY — drop _check_result wrapper; return result.to_response()

tests/
  unit/test_errors.py               # NEW — exception class + handler tests
  unit/test_models_enums.py         # MODIFY — shrink expected Status set
  unit/test_session_manager.py      # MODIFY — `with pytest.raises(...)` replaces `assert result.status == Status.X`
  unit/capture/test_reference.py    # MODIFY — same
  unit/capture/test_cold_fill.py    # MODIFY — same (NO_GAPS assertion stays)
  unit/capture/test_draft.py        # MODIFY — same
  unit/test_replay.py               # MODIFY — same
  unit/test_speed_run_mode.py       # MODIFY — same
  unit/routes/test_dashboard_references.py  # MODIFY — same or already verifies HTTP codes
  unit/test_dashboard_integration.py        # MODIFY — HTTP-code assertions unchanged
```

---

## Task 1: Add `errors.py` exception hierarchy

**Files:**
- Create: `python/spinlab/errors.py`
- Create: `tests/unit/test_errors.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_errors.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/test_errors.py -v
```
Expected: `ModuleNotFoundError: No module named 'spinlab.errors'` or similar.

- [ ] **Step 3: Write the implementation**

Create `python/spinlab/errors.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify pass**

```bash
pytest tests/unit/test_errors.py -v
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/errors.py tests/unit/test_errors.py
git commit -m "feat: add ActionError exception hierarchy for route-boundary translation"
```

---

## Task 2: Register the FastAPI exception handler in `create_app`

**Files:**
- Modify: `python/spinlab/dashboard.py` — add handler, keep `_check_result` and `_ERROR_STATUS_CODES` in place for now (removed in Task 8)

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_errors.py`:

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient

from spinlab.errors import DraftPendingError, NotConnectedError


def _build_app_with_raising_route(exc_factory):
    """Minimal FastAPI app that raises the given ActionError; lets us verify
    the handler registered by create_app-style wiring without booting a DB."""
    from spinlab.dashboard import register_action_error_handler
    app = FastAPI()
    register_action_error_handler(app)

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
```

- [ ] **Step 2: Run to see it fail**

```bash
pytest tests/unit/test_errors.py::test_handler_maps_not_connected_to_503 -v
```
Expected: `ImportError: cannot import name 'register_action_error_handler'`.

- [ ] **Step 3: Implement `register_action_error_handler`**

In `python/spinlab/dashboard.py`, add this function above `create_app` (keep all existing code in place for now — `_check_result` and `_ERROR_STATUS_CODES` are removed in Task 8):

```python
from .errors import ActionError


def register_action_error_handler(app: FastAPI) -> None:
    """Translate raised ActionError subclasses to HTTPException at the boundary."""
    @app.exception_handler(ActionError)
    async def _handle_action_error(request: Request, exc: ActionError):
        return JSONResponse(status_code=exc.http_code, content={"detail": exc.detail})
```

In `create_app`, add a call to `register_action_error_handler(app)` immediately after the existing `@app.exception_handler(Exception)` block.

- [ ] **Step 4: Run the two tests**

```bash
pytest tests/unit/test_errors.py -v
```
Expected: all green.

- [ ] **Step 5: Run the full suite — should still be green (no behavior change yet)**

```bash
pytest
```
Expected: 608 passed (plus the new test_errors tests).

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/dashboard.py tests/unit/test_errors.py
git commit -m "feat: register ActionError→HTTPException handler in create_app"
```

---

## Task 3: Refactor `capture/draft.py` to raise `NoDraftError`

Start with the smallest controller so the pattern is clear before spreading.

**Files:**
- Modify: `python/spinlab/capture/draft.py` (lines ~65-95)
- Modify: `tests/unit/capture/test_draft.py`

- [ ] **Step 1: Read current state**

Read `python/spinlab/capture/draft.py:65-95`. Identify every `return ActionResult(status=Status.NO_DRAFT)`.

- [ ] **Step 2: Replace returns with raises**

For each `return ActionResult(status=Status.NO_DRAFT)`:

```python
# before
return ActionResult(status=Status.NO_DRAFT)
# after
raise NoDraftError()
```

Add import at top of `capture/draft.py`:

```python
from spinlab.errors import NoDraftError
```

Remove `Status` import if it's no longer used (it likely still is for `Status.OK` returns — keep those).

- [ ] **Step 3: Update tests**

In `tests/unit/capture/test_draft.py`, find every test that asserts `result.status == Status.NO_DRAFT` and replace with a `with pytest.raises(NoDraftError):` block wrapping the call.

Example pattern:

```python
# before
result = await session.save_draft("x")
assert result.status == Status.NO_DRAFT

# after
from spinlab.errors import NoDraftError
with pytest.raises(NoDraftError):
    await session.save_draft("x")
```

Keep any assertions on the success path (`Status.OK`) unchanged.

- [ ] **Step 4: Run capture tests**

```bash
pytest tests/unit/capture/test_draft.py -v
```
Expected: green.

- [ ] **Step 5: Run the full suite**

```bash
pytest
```
Expected: green — route-level tests still pass because the handler registered in Task 2 translates the exception to the same 404 + "no_draft" detail that `_check_result` produces today.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/capture/draft.py tests/unit/capture/test_draft.py
git commit -m "refactor: raise NoDraftError instead of returning Status.NO_DRAFT"
```

---

## Task 4: Refactor `capture/cold_fill.py`

**Files:**
- Modify: `python/spinlab/capture/cold_fill.py`
- Modify: `tests/unit/capture/test_cold_fill.py` (and `test_cold_fill_integration.py` if it asserts the status)

**Note:** `Status.NO_GAPS` **stays a success return**, not converted to an exception. It represents "nothing to do" — a legitimate 200 outcome. Only `Status.NOT_CONNECTED` converts.

- [ ] **Step 1: Replace the single error return**

In `python/spinlab/capture/cold_fill.py` around line 34:

```python
# before
if not self.tcp.is_connected:
    return ActionResult(status=Status.NOT_CONNECTED)
# after
if not self.tcp.is_connected:
    raise NotConnectedError()
```

Add `from spinlab.errors import NotConnectedError` at the top.

Leave `NO_GAPS`, `STARTED`, `OK` returns untouched.

- [ ] **Step 2: Update tests**

In `tests/unit/capture/test_cold_fill.py`:

- Any test asserting `result.status == Status.NOT_CONNECTED` → replace with `with pytest.raises(NotConnectedError):` block.
- Keep `result.status == Status.NO_GAPS` assertions unchanged.

Check `tests/unit/capture/test_cold_fill_integration.py` for the same pattern.

- [ ] **Step 3: Run**

```bash
pytest tests/unit/capture/ -v
```
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/capture/cold_fill.py tests/unit/capture/
git commit -m "refactor: raise NotConnectedError from cold-fill controller; NO_GAPS stays success"
```

---

## Task 5: Refactor `capture/reference.py`

**Files:**
- Modify: `python/spinlab/capture/reference.py`
- Modify: `tests/unit/capture/test_reference.py`

- [ ] **Step 1: Replace all error returns**

In `python/spinlab/capture/reference.py`, find every `return ActionResult(status=Status.<error>)` and convert:

| Old | New |
|-----|-----|
| `Status.DRAFT_PENDING` | `raise DraftPendingError()` |
| `Status.PRACTICE_ACTIVE` | `raise PracticeActiveError()` |
| `Status.ALREADY_REPLAYING` | `raise AlreadyReplayingError()` |
| `Status.NOT_CONNECTED` | `raise NotConnectedError()` |
| `Status.NOT_IN_REFERENCE` | `raise NotInReferenceError()` |
| `Status.REFERENCE_ACTIVE` | `raise ReferenceActiveError()` |
| `Status.NOT_REPLAYING` | `raise NotReplayingError()` |
| `Status.NO_HOT_VARIANT` | `raise NoHotVariantError()` |

Add imports:

```python
from spinlab.errors import (
    AlreadyReplayingError,
    DraftPendingError,
    NoHotVariantError,
    NotConnectedError,
    NotInReferenceError,
    NotReplayingError,
    PracticeActiveError,
    ReferenceActiveError,
)
```

Keep `Status.STARTED`, `Status.STOPPED`, `Status.OK` returns.

- [ ] **Step 2: Update tests**

In `tests/unit/capture/test_reference.py`, replace each `assert result.status == Status.<error>` with `with pytest.raises(<Error>):`.

Pattern:

```python
with pytest.raises(NotInReferenceError):
    await controller.stop_reference()
```

- [ ] **Step 3: Run**

```bash
pytest tests/unit/capture/test_reference.py -v
pytest
```
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/capture/reference.py tests/unit/capture/test_reference.py
git commit -m "refactor: raise typed errors from reference capture controller"
```

---

## Task 6: Refactor `session_manager.py`

**Files:**
- Modify: `python/spinlab/session_manager.py`
- Modify: `tests/unit/test_session_manager.py`
- Modify: `tests/unit/test_speed_run_mode.py`
- Modify: `tests/unit/test_replay.py`

This is the largest controller. Expect 10-15 error-return sites.

- [ ] **Step 1: Replace all error returns**

Same mapping as Task 5, plus:
- `Status.MISSING_SAVE_STATES` → `raise MissingSaveStatesError()` — **this fixes a latent bug** (the route previously returned 200 with `{"status": "missing_save_states"}`; it now correctly returns 409).

Add imports to `session_manager.py`:

```python
from spinlab.errors import (
    AlreadyRunningError,
    DraftPendingError,
    MissingSaveStatesError,
    NotConnectedError,
    NotRunningError,
    # ... whichever are used
)
```

- [ ] **Step 2: Update session-manager tests**

`tests/unit/test_session_manager.py`: replace `assert result.status == Status.X` (for X in error set) with `pytest.raises`. Keep success-path assertions.

- [ ] **Step 3: Update speed-run tests**

In `tests/unit/test_speed_run_mode.py`, lines referenced by the earlier grep: `assert result.status == Status.MISSING_SAVE_STATES` → `with pytest.raises(MissingSaveStatesError):`.

**Important:** If a test was specifically protecting the old (buggy) 200-return behavior for `MISSING_SAVE_STATES`, that test was documenting a bug. Update the test to assert the correct new behavior (raises) and note in the commit message: "fixes latent 200→409 bug for missing save states; previously returned ActionResult with no HTTP mapping, so dashboard 200'd."

- [ ] **Step 4: Update replay tests**

`tests/unit/test_replay.py`: same pattern.

- [ ] **Step 5: Run**

```bash
pytest tests/unit -v
```
Expected: green.

- [ ] **Step 6: Run full suite including emulator**

```bash
pytest
```
Expected: green. The HTTP-code assertions in `tests/unit/test_dashboard_integration.py` and `tests/unit/routes/test_dashboard_references.py` still pass because the route-level handler produces the same codes.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/session_manager.py tests/unit/
git commit -m "refactor: raise typed errors from SessionManager; fix MISSING_SAVE_STATES 200→409"
```

---

## Task 7: Shrink `Status` enum; delete `SHUTTING_DOWN`

**Files:**
- Modify: `python/spinlab/models.py`
- Modify: `tests/unit/test_models_enums.py`

At this point no code references `Status.NOT_CONNECTED`, `Status.DRAFT_PENDING`, etc. Only `OK`, `STARTED`, `STOPPED`, `NO_GAPS` remain.

- [ ] **Step 1: Confirm no remaining references to pruned members**

```bash
git grep -n "Status\.\(NOT_CONNECTED\|DRAFT_PENDING\|PRACTICE_ACTIVE\|REFERENCE_ACTIVE\|ALREADY_RUNNING\|ALREADY_REPLAYING\|NOT_IN_REFERENCE\|NOT_REPLAYING\|NOT_RUNNING\|NO_DRAFT\|NO_HOT_VARIANT\|MISSING_SAVE_STATES\|SHUTTING_DOWN\)" python/ tests/
```

Expected: zero hits in `python/` and `tests/` (hits in `docs/` are historical plans — ignore).

If there are stragglers, fix them before proceeding.

- [ ] **Step 2: Update `Status` enum**

Edit `python/spinlab/models.py`:

```python
class Status(StrEnum):
    """Success outcomes from controller actions. Errors are raised as ActionError subclasses."""
    OK = "ok"
    STARTED = "started"
    STOPPED = "stopped"
    NO_GAPS = "no_gaps"
```

Delete all other members.

- [ ] **Step 3: Update `test_models_enums.py`**

Replace `TestStatus`:

```python
class TestStatus:
    def test_success_statuses(self):
        assert Status.OK == "ok"
        assert Status.STARTED == "started"
        assert Status.STOPPED == "stopped"
        assert Status.NO_GAPS == "no_gaps"

    def test_all_statuses_present(self):
        expected = {"ok", "started", "stopped", "no_gaps"}
        actual = {s.value for s in Status}
        assert expected == actual

    def test_from_string(self):
        assert Status("ok") is Status.OK
        assert Status("no_gaps") is Status.NO_GAPS

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            Status("bogus")
```

Delete `test_error_statuses` — no more error statuses on the enum.

- [ ] **Step 4: Run**

```bash
pytest tests/unit/test_models_enums.py -v
pytest
```
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/models.py tests/unit/test_models_enums.py
git commit -m "refactor: shrink Status enum to success outcomes only; delete SHUTTING_DOWN"
```

---

## Task 8: Remove `_check_result` and `_ERROR_STATUS_CODES`

**Files:**
- Modify: `python/spinlab/dashboard.py` (delete the dict and function)
- Modify: `python/spinlab/routes/practice.py`
- Modify: `python/spinlab/routes/reference.py`
- Modify: `python/spinlab/routes/segments.py`
- Modify: `python/spinlab/routes/system.py`
- Modify: `python/spinlab/routes/speed_run.py`
- Modify: `python/spinlab/routes/model.py` (if it uses `_check_result`)

The handler registered in Task 2 now catches every `ActionError` and produces the same HTTP responses that `_check_result` used to, so the dict and the wrapper are dead weight.

- [ ] **Step 1: Update each route file**

For every `return _check_result(await session.<action>())` or `return _check_result(<result>)`:

```python
# before
return _check_result(await session.start_reference())
# after
result = await session.start_reference()
return result.to_response()
```

Or equivalently, collapse:

```python
return (await session.start_reference()).to_response()
```

Use whichever reads best in each file. Remove `from spinlab.dashboard import _check_result` imports.

- [ ] **Step 2: Delete `_check_result` and `_ERROR_STATUS_CODES`**

In `python/spinlab/dashboard.py`:
- Delete `_ERROR_STATUS_CODES: dict[Status, int] = { ... }` (lines ~26-38)
- Delete `def _check_result(result: ActionResult) -> dict:` (lines ~41-45)
- Remove unused imports (`Status` may no longer be needed in `dashboard.py`)

- [ ] **Step 3: Run full suite**

```bash
pytest
```
Expected: green. Route-level HTTP-code assertions in `tests/unit/test_dashboard_integration.py` and `tests/unit/routes/test_dashboard_references.py` still pass — the exception handler produces the same codes and detail strings.

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/dashboard.py python/spinlab/routes/
git commit -m "refactor: remove _check_result; routes return to_response() directly"
```

---

## Task 9: Final sweep + CLAUDE.md update

- [ ] **Step 1: Confirm Status enum shape**

```bash
git grep -n "class Status" python/spinlab
```
Should show `Status` with exactly 4 members.

- [ ] **Step 2: Confirm no orphaned imports**

```bash
git grep -n "from spinlab.dashboard import _check_result\|Status.NOT_CONNECTED\|Status.NO_DRAFT" python/ tests/
```
Expected: zero hits.

- [ ] **Step 3: Update `CLAUDE.md` if it mentions the Status enum**

```bash
grep -n "Status" CLAUDE.md
```

If CLAUDE.md references the old status-returning error pattern, update to describe raising `ActionError` subclasses instead. If no mention, skip.

- [ ] **Step 4: Run full test suite**

```bash
pytest
```
Expected: green. Exit code 0.

- [ ] **Step 5: Commit if anything changed**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md to describe ActionError pattern"
```

Skip if no docs changed.

---

## Verification

After all tasks:

- `Status` enum has 4 members (`OK`, `STARTED`, `STOPPED`, `NO_GAPS`).
- `python/spinlab/errors.py` exists with 12 `ActionError` subclasses.
- No file imports `_check_result` or `_ERROR_STATUS_CODES`.
- Full `pytest` passes (unit + emulator + frontend, ≥608 tests).
- `tests/unit/test_dashboard_integration.py` still verifies HTTP 409/404/503 responses for the conflict / not-found / not-connected cases — proving the exception handler produces the same wire format as the old `_check_result`.
- Frontend smoke from `test_frontend_smoke.py` still passes (no API contract change).
- Latent bug fixed: POST `/api/speedrun/start` with missing save states now returns 409, not 200.

## Risk

Low blast radius, but touches every controller and every action route. Mitigations:
- The handler registered in Task 2 is live before any controller is converted, so Tasks 3-6 can land incrementally without breaking route-level tests.
- Each task is independently testable and commit-atomic.
- HTTP-code assertions in the integration tests are the backstop — if any route's behavior regresses, `test_dashboard_integration.py` catches it.

## Out of scope

- `Mode`, `AttemptSource`, `EventType`, `EndpointType` enums — only `Status` is pruned.
- `ActionResult.new_mode` field — kept as-is.
- Changing the wire format (`{"detail": "..."}` matches what `HTTPException` produces today).
- Frontend error-handling changes — the JSON shape on error responses is unchanged.

# CF-β: Typed AppState + Typed DB Rows — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drop the `dict | None` and `# type: ignore[return-value]` shims from the SSE/REST boundary and DB layer. Return real Pydantic models from controller snapshot methods, real `AppState` from StateBuilder, and use explicit `cast()` instead of blanket suppressions in DB row helpers.

**Architecture:** Three independent phases that compose. Phase 1 tightens two leaf methods (`ColdFillController.get_state`, `ReferenceController.get_paused_state`) to return their existing api_schemas Pydantic models. Phase 2 replaces every `dict(zip(...)) # type: ignore[return-value]` with `cast(TypedDictName, dict(zip(...)))` — same runtime behavior, explicit type assertion. Phase 3 changes `StateBuilder.build() -> dict` to `-> AppState`, wrapping the final return in `AppState.model_validate(base)` so Pydantic builds and validates the response at the boundary.

**Tech Stack:** Python 3.11+, Pydantic v2 (`pydantic.dataclasses` + `BaseModel`), `sqlite3.Row`, `typing.cast`, FastAPI + `openapi-typescript` codegen. Pyright is the type checker (matches VS Code Pylance).

**Branch:** `improve/typed-boundaries-and-cleanups` (already created; the CF-ε/CF-θ trivial batch is committed on this branch). Continue work here.

**Spec reference:** `docs/superpowers/scans/2026-05-17-improve.md` Top wins → high-leverage → CF-β, plus the merged-list/critique/verify sections for full debunked-claim context.

---

## File Structure

**Modify (Phase 1):**
- `python/spinlab/capture/cold_fill.py` — tighten `get_state() -> ColdFillState | None` (line 127)
- `python/spinlab/capture/reference.py` — tighten `get_paused_state() -> PausedRunState | None` (line 183)

**Modify (Phase 2):**
- `python/spinlab/db/segments.py` — `get_segment_row` (:80-100), `get_all_segments_with_model` (:102-132), `segments_missing_cold` (:134-151)
- `python/spinlab/db/model_state.py` — `load_all_model_states_for_segment` (:61-69), `load_all_model_states` (:71-80), `load_all_model_states_for_game` (:82-96)
- `python/spinlab/db/attempts.py` — `get_recent_attempts` (:70-96), `get_segment_attempts` (:98-106), `get_all_attempts_by_segment` (:108-124)

**Modify (Phase 3):**
- `python/spinlab/state_builder.py` — `build()` return type + final `AppState.model_validate(base)` wrap (line 30, 104)

**Test (existing — should keep passing):**
- `tests/unit/test_state_builder.py` — full coverage of the build() branches
- `tests/unit/test_cold_fill.py` — exercises `get_state()`
- `tests/unit/capture/test_reference_*.py` — exercises `get_paused_state()`
- `tests/unit/test_db_*.py` — DB row helper tests
- `tests/integration/test_frontend_smoke.py` — verifies /api/state shape end-to-end
- `tests/integration/test_replay_fixture.py` — full pipeline

**Test (new, optional — add only if Phase 3 needs end-to-end coverage of the validated shape):**
- `tests/unit/test_state_builder.py::test_build_returns_validated_AppState` — round-trip a built state through Pydantic

---

## Pre-flight

- [ ] **Step 1: Confirm baseline is green**

Run:
```
cd C:/Users/thedo/git/spinlab && python -m pytest 2>&1 | tail -5
```
Expected: `898 passed in ~57s` (or higher if new tests have been added since the trivial batch).

- [ ] **Step 2: Confirm pyright baseline is 0 errors**

Run:
```
cd C:/Users/thedo/git/spinlab && npx pyright python/ 2>&1 | tail -5
```
Expected: `0 errors, 0 warnings, 0 informations`.

- [ ] **Step 3: Confirm the branch is current**

Run:
```
cd C:/Users/thedo/git/spinlab && git status && git log --oneline -3
```
Expected: clean tree, on `improve/typed-boundaries-and-cleanups`, top commit message starts with `improve: trivial batch`.

---

## Phase 1: Tighten controller snapshot return types

### Task 1.1: `ColdFillController.get_state() -> ColdFillState | None`

**Files:**
- Modify: `python/spinlab/capture/cold_fill.py:127-142`
- Test: `tests/unit/test_cold_fill.py` (existing tests should keep passing without changes)

**Why this is safe:** `ColdFillState` already exists in `api_schemas.py:105-108` with the exact three fields this method already returns. `AppState.cold_fill: ColdFillState | None` already declares the field type. StateBuilder line 96-98 does `base["cold_fill"] = cf_state` — Pydantic accepts both a `dict` and a `ColdFillState` instance when validating into `AppState`, so embedding the instance is safe.

- [ ] **Step 1: Read the current implementation**

Read `python/spinlab/capture/cold_fill.py:127-142`. Confirm the returned dict has keys exactly `current`, `total`, `segment_label`.

- [ ] **Step 2: Add the import for `ColdFillState`**

At the top of `python/spinlab/capture/cold_fill.py`, add the import alongside the existing api_schemas-adjacent imports:

```python
from ..api_schemas import ColdFillState
```

Place it next to `from ..models import ActionResult, Mode, Status, WaypointSaveState` (one blank line stays around the import block).

- [ ] **Step 3: Change the return type and constructor**

Replace lines 127-142 of `python/spinlab/capture/cold_fill.py`:

```python
    def get_state(self) -> ColdFillState | None:
        """Return cold-fill progress for state snapshots, or None when no segment is loaded."""
        if not self.current:
            return None
        current_num = self.total - len(self.queue) + 1
        seg = self.queue[0] if self.queue else None
        label = ""
        if seg:
            start = "start" if seg["start_type"] == "entrance" else f"cp{seg['start_ordinal']}"
            end = "goal" if seg["end_type"] == "goal" else f"cp{seg['end_ordinal']}"
            label = seg.get("description") or f"L{seg['level_number']} {start} > {end}"
        return ColdFillState(
            current=current_num,
            total=self.total,
            segment_label=label,
        )
```

- [ ] **Step 4: Run unit tests for cold_fill**

Run:
```
cd C:/Users/thedo/git/spinlab && python -m pytest tests/unit/test_cold_fill.py -v 2>&1 | tail -20
```
Expected: all tests pass. If a test does `assert state["current"] == ...` it will fail because `ColdFillState` is a Pydantic model, not a dict. In that case fix the test to use attribute access (`state.current`) — this is a desired tightening, not a regression.

- [ ] **Step 5: Run pyright**

Run:
```
cd C:/Users/thedo/git/spinlab && npx pyright python/spinlab/capture/cold_fill.py python/spinlab/state_builder.py 2>&1 | tail -10
```
Expected: 0 errors. StateBuilder line 96-98 should still typecheck because `AppState.cold_fill: ColdFillState | None` accepts the instance.

- [ ] **Step 6: Commit**

```
cd C:/Users/thedo/git/spinlab && git add python/spinlab/capture/cold_fill.py tests/unit/test_cold_fill.py && git commit -m "$(cat <<'EOF'
cold_fill: get_state() returns ColdFillState instead of bare dict

Pydantic model already defined in api_schemas.py; this drops the dict|None
return shape in favor of the typed snapshot. StateBuilder embeds the
instance directly — Pydantic coerces in AppState.model_validate.

Part of CF-β: docs/superpowers/scans/2026-05-17-improve.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

(If no test file was modified, drop it from `git add`.)

### Task 1.2: `ReferenceController.get_paused_state() -> PausedRunState | None`

**Files:**
- Modify: `python/spinlab/capture/reference.py:183-195`
- Test: `tests/unit/capture/test_reference_*.py` (existing tests should keep passing)

**Why this is safe:** Same shape match as Task 1.1 — `PausedRunState` at `api_schemas.py:99-102` has exactly `run_id`, `segments_captured`, `session_count`. StateBuilder line 91-93 does `if paused_run: base["paused_run"] = paused_run` — instance is truthy, Pydantic coerces in `AppState`.

- [ ] **Step 1: Add the import**

At the top of `python/spinlab/capture/reference.py`, add (matching the existing import block style):

```python
from ..api_schemas import PausedRunState
```

- [ ] **Step 2: Change the return type and constructor**

Replace lines 183-195 of `python/spinlab/capture/reference.py`:

```python
    def get_paused_state(self) -> PausedRunState | None:
        """Snapshot of the paused run for state_builder. None if no paused run."""
        if not self.paused_run_id:
            return None
        seg_count = self.db.count_segments_for_run(
            self.paused_run_id, active_only=True,
        )
        sessions = self.db.list_capture_sessions_for_run(self.paused_run_id)
        return PausedRunState(
            run_id=self.paused_run_id,
            segments_captured=seg_count,
            session_count=len(sessions),
        )
```

- [ ] **Step 3: Run unit tests for reference**

Run:
```
cd C:/Users/thedo/git/spinlab && python -m pytest tests/unit/capture/ tests/unit/test_session_manager.py -v 2>&1 | tail -20
```
Expected: all pass. If any test does subscript access on `get_paused_state()` result (e.g. `paused["run_id"]`), update it to attribute access (`paused.run_id`).

- [ ] **Step 4: Run pyright**

Run:
```
cd C:/Users/thedo/git/spinlab && npx pyright python/spinlab/capture/reference.py python/spinlab/state_builder.py 2>&1 | tail -10
```
Expected: 0 errors.

- [ ] **Step 5: Commit**

```
cd C:/Users/thedo/git/spinlab && git add python/spinlab/capture/reference.py && git commit -m "$(cat <<'EOF'
reference: get_paused_state() returns PausedRunState instead of bare dict

Mirrors the cold_fill change — typed snapshot, Pydantic model from
api_schemas embeds directly into AppState.

Part of CF-β: docs/superpowers/scans/2026-05-17-improve.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 1.3: Phase 1 validation

- [ ] **Step 1: Run the fast suite**

Run:
```
cd C:/Users/thedo/git/spinlab && python -m pytest -m "not emulator" -q 2>&1 | tail -5
```
Expected: 886+ passed.

- [ ] **Step 2: Rebuild the frontend (regenerates api-types.ts via OpenAPI)**

Run:
```
cd C:/Users/thedo/git/spinlab/frontend && npm run build 2>&1 | tail -10
```
Expected: build succeeds. The codegen will re-emit `frontend/src/api-types.ts` — verify it has not changed (these were already the declared types).

Run:
```
cd C:/Users/thedo/git/spinlab && git diff --stat frontend/src/api-types.ts
```
Expected: 0 changed lines. If there ARE changes, inspect carefully — it means AppState's serialized shape drifted, which would mean the Pydantic field names or types are out of sync with the dict that was being returned before.

---

## Phase 2: Drop `# type: ignore[return-value]` from DB row helpers

For each helper, the change is the same idiom: import `cast`, then replace the `# type: ignore[return-value]` with an explicit `cast(RowType, dict(zip(...)))`. This is functionally identical at runtime — `cast` returns its second argument unchanged — but the type assertion is now named rather than suppressed.

### Task 2.1: `db/segments.py` — three helpers

**Files:**
- Modify: `python/spinlab/db/segments.py:80-100, 102-132, 134-151`

- [ ] **Step 1: Add the `cast` import**

At the top of `python/spinlab/db/segments.py`, change:

```python
from typing import TypedDict
```

to:

```python
from typing import TypedDict, cast
```

- [ ] **Step 2: Replace `get_segment_row`**

In `python/spinlab/db/segments.py`, replace lines 96-100 (the body after `cur.fetchone()`):

```python
        row = cur.fetchone()
        if row is None:
            return None
        actual_cols = [desc[0] for desc in cur.description]
        return cast(SegmentRow, dict(zip(actual_cols, row)))
```

- [ ] **Step 3: Replace `get_all_segments_with_model`**

Replace lines 131-132:

```python
        actual_cols = [desc[0] for desc in cur.description]
        return [cast(SegmentRow, dict(zip(actual_cols, row))) for row in cur.fetchall()]
```

- [ ] **Step 4: Replace `segments_missing_cold`**

Replace lines 149-151:

```python
        cols = ["segment_id", "hot_state_path", "level_number",
                "start_type", "start_ordinal", "end_type", "end_ordinal", "description"]
        return [cast(MissingColdRow, dict(zip(cols, r))) for r in rows]
```

- [ ] **Step 5: Run pyright on the file**

Run:
```
cd C:/Users/thedo/git/spinlab && npx pyright python/spinlab/db/segments.py 2>&1 | tail -10
```
Expected: 0 errors, no `# type: ignore` reported as unused. (If pyright reports `Unnecessary "# type: ignore" comment` you missed one — re-read and remove.)

- [ ] **Step 6: Run unit tests for the DB segment helpers**

Run:
```
cd C:/Users/thedo/git/spinlab && python -m pytest tests/unit/test_db_segments.py tests/unit/db/ -v 2>&1 | tail -15
```
Expected: all pass.

- [ ] **Step 7: Commit**

```
cd C:/Users/thedo/git/spinlab && git add python/spinlab/db/segments.py && git commit -m "$(cat <<'EOF'
db/segments: explicit cast() instead of # type: ignore on row dict construction

Same runtime behavior, but the TypedDict assertion is now named rather
than suppressed — pyright surfaces real shape drift instead of going
silent.

Part of CF-β: docs/superpowers/scans/2026-05-17-improve.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 2.2: `db/model_state.py` — three helpers

**Files:**
- Modify: `python/spinlab/db/model_state.py:61-96`

- [ ] **Step 1: Add the `cast` import**

In `python/spinlab/db/model_state.py`, change:

```python
from typing import TypedDict
```

to:

```python
from typing import TypedDict, cast
```

- [ ] **Step 2: Replace `load_all_model_states_for_segment`**

Replace line 69:

```python
        return [cast(ModelStateRow, dict(zip(cols, row))) for row in cur.fetchall()]
```

- [ ] **Step 3: Replace `load_all_model_states`**

Replace line 80:

```python
        return [cast(ModelStateRow, dict(zip(cols, row))) for row in cur.fetchall()]
```

- [ ] **Step 4: Replace `load_all_model_states_for_game`**

Replace lines 91-96. The current body builds a dict-of-lists; preserve that behavior but cast each row:

```python
        cols = ["segment_id", "estimator", "state_json", "output_json", "updated_at"]
        result: dict[str, list[ModelStateRow]] = defaultdict(list)
        for row in cur.fetchall():
            d = cast(ModelStateRow, dict(zip(cols, row)))
            result[d["segment_id"]].append(d)
        return result
```

(The previous code used `# type: ignore[arg-type]` on the `.append(d)` line because `d` was `dict[str, Any]`. With `cast(ModelStateRow, ...)` that suppression is no longer needed.)

- [ ] **Step 5: Run pyright on the file**

Run:
```
cd C:/Users/thedo/git/spinlab && npx pyright python/spinlab/db/model_state.py 2>&1 | tail -10
```
Expected: 0 errors. If any `Unnecessary "# type: ignore"` appears, remove the stale comment.

- [ ] **Step 6: Run unit tests for model state**

Run:
```
cd C:/Users/thedo/git/spinlab && python -m pytest tests/unit/test_db_model_state.py tests/unit/db/ -v 2>&1 | tail -15
```
Expected: all pass.

- [ ] **Step 7: Commit**

```
cd C:/Users/thedo/git/spinlab && git add python/spinlab/db/model_state.py && git commit -m "$(cat <<'EOF'
db/model_state: explicit cast() instead of # type: ignore on row dict construction

Same change as db/segments: type assertion is now named rather than
suppressed. Also removes the obsolete # type: ignore[arg-type] from
load_all_model_states_for_game.

Part of CF-β: docs/superpowers/scans/2026-05-17-improve.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 2.3: `db/attempts.py` — three helpers

**Files:**
- Modify: `python/spinlab/db/attempts.py:70-124`

- [ ] **Step 1: Add the `cast` import**

In `python/spinlab/db/attempts.py`, change:

```python
from typing import TypedDict
```

to:

```python
from typing import TypedDict, cast
```

- [ ] **Step 2: Replace `get_recent_attempts`**

Replace line 96:

```python
        return [cast(RecentAttemptRow, dict(r)) for r in rows]
```

(Here `r` is a `sqlite3.Row`, and `dict(sqlite3.Row)` produces `dict[str, Any]`. The cast asserts the TypedDict shape.)

- [ ] **Step 3: Replace `get_segment_attempts`**

Replace line 106:

```python
        return [cast(AttemptRow, dict(zip(cols, row))) for row in cur.fetchall()]
```

- [ ] **Step 4: Replace `get_all_attempts_by_segment`**

The previous body has `result: dict[str, list[dict]] = defaultdict(list)`. Tighten that declaration to `list[AttemptRow]` and cast each row. Replace lines 119-124:

```python
        cols = ["segment_id", "completed", "time_ms", "deaths", "clean_tail_ms", "created_at", "invalidated"]
        result: dict[str, list[AttemptRow]] = defaultdict(list)
        for row in cur.fetchall():
            d = cast(AttemptRow, dict(zip(cols, row)))
            result[d["segment_id"]].append(d)
        return result
```

(Note the declared type also gets tightened from `list[dict]` to `list[AttemptRow]` — the function's docstring/return is already `dict[str, list[AttemptRow]]`.)

- [ ] **Step 5: Run pyright on the file**

Run:
```
cd C:/Users/thedo/git/spinlab && npx pyright python/spinlab/db/attempts.py 2>&1 | tail -10
```
Expected: 0 errors.

- [ ] **Step 6: Run unit tests for attempts**

Run:
```
cd C:/Users/thedo/git/spinlab && python -m pytest tests/unit/test_db_attempts.py tests/unit/db/ -v 2>&1 | tail -15
```
Expected: all pass.

- [ ] **Step 7: Commit**

```
cd C:/Users/thedo/git/spinlab && git add python/spinlab/db/attempts.py && git commit -m "$(cat <<'EOF'
db/attempts: explicit cast() instead of # type: ignore on row dict construction

Completes the DB-row sweep — segments + model_state + attempts now use
named TypedDict assertions instead of blanket suppressions.

Part of CF-β: docs/superpowers/scans/2026-05-17-improve.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 2.4: Phase 2 validation

- [ ] **Step 1: Grep for any remaining type-ignore-return-value in the DB layer**

Run (use Grep tool, not bash):

Pattern: `type: ignore\[return-value\]`
Path: `python/spinlab/db/`

Expected: 0 hits. If `get_log_attempt`'s `cur.lastrowid` ignore (line 59) shows up — leave it; that one is a different category (`# type: ignore[return-value]` against `int | None` from sqlite, where the INSERT guarantees a value).

- [ ] **Step 2: Run the fast suite**

Run:
```
cd C:/Users/thedo/git/spinlab && python -m pytest -m "not emulator" -q 2>&1 | tail -5
```
Expected: 886+ passed.

- [ ] **Step 3: Run pyright on all of python/**

Run:
```
cd C:/Users/thedo/git/spinlab && npx pyright python/ 2>&1 | tail -5
```
Expected: `0 errors`. If new errors appear, they will be from a stale `# type: ignore[arg-type]` somewhere downstream — fix or remove the stale comment.

---

## Phase 3: `StateBuilder.build() -> AppState`

This is the highest-value change but also the one with the broadest blast radius. Pacing matters: write the test first, then make the minimal change, then verify the API contract.

### Task 3.1: Write a regression test that build() output validates against AppState

**Files:**
- Test: `tests/unit/test_state_builder.py` (add a new test alongside the existing ones)

- [ ] **Step 1: Read the existing test file structure**

Read `tests/unit/test_state_builder.py` to see how the existing tests assemble a SessionManager + db, and which Mode branches are exercised.

- [ ] **Step 2: Add a regression test**

Append to `tests/unit/test_state_builder.py`:

```python
def test_build_output_validates_as_AppState(db, mock_emu):
    """Regression: build() output should validate cleanly through AppState.

    Catches drift between StateBuilder's dict assembly and the AppState
    contract in api_schemas.py. If a field is added to AppState but build()
    forgets to populate it (or vice versa), this fails.
    """
    from spinlab.api_schemas import AppState
    from spinlab.session_manager import SessionManager
    from spinlab.state_builder import StateBuilder

    session = SessionManager(db=db, emu=mock_emu, data_dir=Path("."), rom_dir=None, category="any%")
    builder = StateBuilder(db=db)

    snapshot = builder.build(session)
    # If snapshot is a dict (pre-Phase 3), validate. If it's already an
    # AppState (post-Phase 3), this is a no-op identity check.
    validated = AppState.model_validate(snapshot)
    assert validated.mode == session.mode
    assert validated.emu_connected == session.emu.is_connected
```

(Reuse whatever fixtures the existing tests use — `db`, `mock_emu`, etc. If the test file doesn't already import `Path`, add `from pathlib import Path`.)

- [ ] **Step 3: Run the new test against the CURRENT (pre-Phase-3) code**

Run:
```
cd C:/Users/thedo/git/spinlab && python -m pytest tests/unit/test_state_builder.py::test_build_output_validates_as_AppState -v 2>&1 | tail -10
```
Expected: PASS. This proves the current `dict` return already round-trips through `AppState.model_validate` cleanly. (If it FAILS, the pre-existing dict assembly is already drifted from AppState — investigate and reconcile before Phase 3 proceeds.)

- [ ] **Step 4: Commit the regression test**

```
cd C:/Users/thedo/git/spinlab && git add tests/unit/test_state_builder.py && git commit -m "$(cat <<'EOF'
test: pin StateBuilder.build() output to AppState shape

Regression test that catches drift between StateBuilder's dict assembly
and the AppState Pydantic contract in api_schemas.py. Passes against the
current dict-return; becomes a tautology once build() returns AppState
directly in the next commit.

Part of CF-β: docs/superpowers/scans/2026-05-17-improve.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 3.2: Change `build()` return type and add the model_validate wrap

**Files:**
- Modify: `python/spinlab/state_builder.py:10, 30, 104`

- [ ] **Step 1: Add the `AppState` import**

In `python/spinlab/state_builder.py`, add to the existing imports:

```python
from .api_schemas import AppState
```

Place it after `from .models import Mode, ModelOutput` (these are sibling first-party imports — keep alphabetical-ish order).

- [ ] **Step 2: Change the `build()` signature**

In `python/spinlab/state_builder.py:30`, change:

```python
    def build(self, session: "SessionManager") -> dict:
```

to:

```python
    def build(self, session: "SessionManager") -> AppState:
```

- [ ] **Step 3: Wrap the early-return for `game_id is None`**

Line 70-71 currently has:

```python
        if game_id is None:
            return base
```

Change to:

```python
        if game_id is None:
            return AppState.model_validate(base)
```

- [ ] **Step 4: Wrap the final return**

Line 104 currently has:

```python
        return base
```

Change to:

```python
        return AppState.model_validate(base)
```

- [ ] **Step 5: Run pyright on state_builder**

Run:
```
cd C:/Users/thedo/git/spinlab && npx pyright python/spinlab/state_builder.py 2>&1 | tail -10
```
Expected: 0 errors. If pyright complains about the `base: dict` declaration, that's expected — `base` stays as a dict internally; only the *return* is `AppState`.

- [ ] **Step 6: Find every caller of `StateBuilder.build()` and confirm they still work**

Run (Grep tool):

Pattern: `\.build\(`
Path: `python/spinlab/`

Inspect each hit. Likely callers: `session_manager.py:get_state()`, `sse.py`, possibly route handlers. Any caller doing `state["key"]` (subscript) on the return value will need updating — Pydantic models use attribute access, not subscript. (`AppState` inherits from `_BaseResponse` → `BaseModel`, which doesn't define `__getitem__`.)

If any caller breaks, the fix is one of:
- (a) Replace `state["key"]` with `state.key`
- (b) Call `state.model_dump()` at the caller to get a dict

Prefer (a). Use (b) only when the caller's downstream consumer genuinely needs a dict (e.g., it's about to be JSON-serialized to a non-FastAPI channel like an SSE message that goes through `json.dumps`).

- [ ] **Step 7: Check SSE broadcast path**

Read `python/spinlab/sse.py` and find `broadcast()`. SSE messages need to be JSON-serializable. If `broadcast` does `json.dumps(payload)`, AppState (a Pydantic model) will fail — `json.dumps` doesn't know how to serialize a BaseModel. Fix at the call site (`session_manager.py:_notify_sse` line 211 does `self.sse.broadcast(self.get_state())`): change to `self.sse.broadcast(self.get_state().model_dump(mode="json"))`. `mode="json"` ensures enums serialize as their `.value` strings — matches the previous dict's `"mode": mode.value` behavior.

- [ ] **Step 8: Confirm the SessionManager.get_state() return path is consistent**

`session_manager.py` likely has `def get_state(self): return self._state_builder.build(self)`. Confirm that's the only place that delegates to build, and that any callers that need a dict get `.model_dump(mode="json")` instead.

If `session_manager.get_state()` itself is annotated `-> dict`, change to `-> AppState`.

- [ ] **Step 9: Run the fast suite**

Run:
```
cd C:/Users/thedo/git/spinlab && python -m pytest -m "not emulator" -q 2>&1 | tail -10
```
Expected: all 886+ pass. The regression test from Task 3.1 should still pass (identity check now). If any test fails on `state["key"]` subscript, update it to attribute access — those tests were reaching into the dict and need to follow the typed contract.

- [ ] **Step 10: Rebuild the frontend and verify api-types.ts has not drifted**

Run:
```
cd C:/Users/thedo/git/spinlab/frontend && npm run build 2>&1 | tail -10
```
Then:
```
cd C:/Users/thedo/git/spinlab && git diff --stat frontend/src/api-types.ts
```
Expected: 0 lines changed. (The OpenAPI schema is generated from AppState — which existed before. The only way api-types.ts changes is if AppState's schema generation differs from before, which it shouldn't.)

- [ ] **Step 11: Run the integration smoke tests**

Run:
```
cd C:/Users/thedo/git/spinlab && python -m pytest tests/integration/test_frontend_smoke.py -v 2>&1 | tail -20
```
Expected: all 6 pass. These tests boot a real FastAPI dashboard and hit `/api/state`, so any serialization break shows up here.

- [ ] **Step 12: Commit**

```
cd C:/Users/thedo/git/spinlab && git add -A && git commit -m "$(cat <<'EOF'
state_builder: build() returns AppState instead of bare dict

Wraps the assembly in AppState.model_validate(base) so the SSE/REST
boundary is typed end to end. SSE broadcasts go through
.model_dump(mode='json') to keep the wire format identical (enums as
.value strings, no Pydantic-class repr leakage).

CurrentSegment, RecentAttempt, ReplayState, PausedRunState, ColdFillState,
SessionInfo, SegmentSnapshot — all already exist in api_schemas; this
change just plumbs them through.

Part of CF-β: docs/superpowers/scans/2026-05-17-improve.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 3.3: Phase 3 final validation

- [ ] **Step 1: Run the full unfiltered suite**

Per CLAUDE.md: full pytest is the merge-readiness gate.

Run:
```
cd C:/Users/thedo/git/spinlab && python -m pytest 2>&1 | tail -15
```
Expected: `898 passed` (plus 1 for the new regression test in Task 3.1 — so 899). No failures, no skips. If emulator tests show launch errors, that's RA still alive from a prior run — wait a few seconds and retry; if the failure persists, surface to the user (per `feedback_red_baseline_habit`).

- [ ] **Step 2: Run pyright on the full python/ tree**

Run:
```
cd C:/Users/thedo/git/spinlab && npx pyright python/ 2>&1 | tail -5
```
Expected: `0 errors, 0 warnings, 0 informations`.

- [ ] **Step 3: Run ruff**

Run:
```
cd C:/Users/thedo/git/spinlab && ruff check python/ 2>&1 | tail -5
```
Expected: pre-existing 5 errors in `api_schemas.py` (the `from spinlab.models import ...` E402/I001/F401 cluster). NO new errors introduced. If new errors appear, fix or revert.

- [ ] **Step 4: Run the frontend test suite**

Run:
```
cd C:/Users/thedo/git/spinlab/frontend && npm test 2>&1 | tail -10
```
Expected: 65 passed.

- [ ] **Step 5: Sanity-check the SSE wire format manually**

Boot the dashboard briefly and curl `/api/state`. Confirm `mode` is a bare string ("idle", not "Mode.IDLE"), `emu_connected` is a bool, nested objects look like the previous dict-shaped responses.

This is a visual / curl-based check, not a scripted one. If something has drifted (e.g. `mode: {value: "idle"}` instead of `mode: "idle"`), the `.model_dump(mode="json")` call in Task 3.2 Step 7 is the place to fix it.

---

## Wrap-up

- [ ] **Step 1: Confirm clean tree**

Run:
```
cd C:/Users/thedo/git/spinlab && git status && git log --oneline -10
```
Expected: clean tree. Commits visible: trivial batch (CF-ε + CF-θ + TS2), then 1.1, 1.2, 2.1, 2.2, 2.3, 3.1, 3.2 (8 new commits over the trivial batch). Order may vary if you batched Phase 2 differently.

- [ ] **Step 2: Hand off back to the user**

The branch `improve/typed-boundaries-and-cleanups` is ready for review or merge. Recommend the user run `git log --oneline main..` to see the full set, then decide whether to PR or fast-forward merge.

If the user wants a PR, suggest: `gh pr create --title "improve: typed boundaries + cleanups (CF-β + trivial batch)" --body "..."` — pointing the body at the scan file for context.

---

## Self-review notes (writing-plans phase)

- **Spec coverage:** All three CF-β scope items covered — Phase 1 (controller snapshots), Phase 2 (DB rows), Phase 3 (StateBuilder). The trivial batch (CF-ε/CF-θ/TS2) already shipped on the branch.
- **Placeholder scan:** No TBDs. Every step has either exact code or an exact command + expected output.
- **Type consistency:** `ColdFillState`, `PausedRunState`, `AppState`, `SegmentRow`, `MissingColdRow`, `ModelStateRow`, `AttemptRow`, `RecentAttemptRow` — all names match the source files I read. `cast` is from `typing`. `.model_validate` and `.model_dump(mode="json")` are Pydantic v2 idioms (confirmed by the existing `pydantic.BaseModel` usage in api_schemas).
- **Risk callouts:**
  - Phase 3 is the load-bearing change — the SSE wire format must not drift. The `mode="json"` arg in `model_dump` is the critical detail (without it, Pydantic emits Enum instances which `json.dumps` chokes on).
  - The Task 3.1 regression test must pass against the pre-Phase-3 code (proves the contract already holds); if it fails, abort and reconcile.
  - `api-types.ts` round-trip via `npm run build` is the second contract check — if the file drifts, the schema generation has changed and the frontend will see a different surface.

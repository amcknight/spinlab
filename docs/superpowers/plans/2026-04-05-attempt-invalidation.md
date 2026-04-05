# Attempt Invalidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user mark individual attempts as invalid so bad-run data (wrong exit, bathroom break, etc.) doesn't pollute estimators. Two paths: an in-emulator hotkey during practice, and a dashboard delete/undo button.

**Architecture:** The `attempts.invalidated` column already exists (Plan 1, Task 5). This plan adds: (1) a `practice.invalidate_combo` config entry, (2) a Lua-side combo detector that fires a new `attempt_invalidated` TCP event, (3) Python handling that marks the current attempt invalidated, (4) a dashboard endpoint + button to toggle invalidation on completed attempts, (5) an estimator filter that excludes invalidated attempts.

**Tech Stack:** Python 3.11+ (config dataclass, FastAPI), Lua (Mesen2 controller polling), TypeScript (frontend button).

**Prerequisite:** Plan 1 (`2026-04-05-segment-conditions-foundation.md`) merged.

---

## File Structure

**Modified files:**
- `python/spinlab/config.py` — add `PracticeConfig` with `invalidate_combo`
- `config.yaml` (user-level) — document new key
- `lua/spinlab.lua` — detect combo in practice mode, send event
- `python/spinlab/session_manager.py` — handle `attempt_invalidated` event, mark DB row
- `python/spinlab/db/attempts.py` — add `set_attempt_invalidated`, `get_last_practice_attempt`, and filtering by `invalidated`
- `python/spinlab/scheduler.py` (`_attempts_from_rows`) — exclude `invalidated` rows
- `python/spinlab/routes/attempts.py` (or wherever attempt routes live; create if missing) — PATCH endpoint for invalidation
- `frontend/src/*.ts` — attempts list UI with invalidate toggle

---

## Task 1: Config — add practice.invalidate_combo

**Files:**
- Modify: `python/spinlab/config.py`
- Create: `tests/test_config.py` (or append if exists)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py (append or create)
from pathlib import Path
from spinlab.config import AppConfig

def test_invalidate_combo_default(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("data: { dir: ./data }\n")
    conf = AppConfig.from_yaml(cfg)
    assert conf.practice.invalidate_combo == ["L", "Select"]

def test_invalidate_combo_custom(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "data: { dir: ./data }\n"
        "practice:\n"
        "  invalidate_combo: [R, Start]\n"
    )
    conf = AppConfig.from_yaml(cfg)
    assert conf.practice.invalidate_combo == ["R", "Start"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v -k invalidate_combo`
Expected: FAIL — `practice` attribute missing.

- [ ] **Step 3: Add PracticeConfig dataclass and wire into AppConfig**

In `python/spinlab/config.py`:

```python
# SNES controller buttons reserved for invalidation combo.
# Chosen to avoid collision with any existing in-emulator controls
# (practice mode uses no controller input by default; this combo is safe).
DEFAULT_INVALIDATE_COMBO = ["L", "Select"]


@dataclass
class PracticeConfig:
    invalidate_combo: list[str] = field(default_factory=lambda: list(DEFAULT_INVALIDATE_COMBO))


@dataclass
class AppConfig:
    network: NetworkConfig
    emulator: EmulatorConfig
    data_dir: Path
    rom_dir: Path | None
    category: str = "any%"
    practice: PracticeConfig = field(default_factory=PracticeConfig)
```

Add to `from_yaml`:

```python
practice_raw = raw.get("practice", {})
practice_cfg = PracticeConfig(
    invalidate_combo=list(practice_raw.get("invalidate_combo", DEFAULT_INVALIDATE_COMBO)),
)
```

And pass `practice=practice_cfg` in the constructor call.

Add `from dataclasses import field` at top.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py -v -k invalidate_combo`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/config.py tests/test_config.py
git commit -m "feat(config): add practice.invalidate_combo"
```

---

## Task 2: DB — set_attempt_invalidated + get_last_practice_attempt

**Files:**
- Modify: `python/spinlab/db/attempts.py`
- Create/append: `tests/test_attempts_invalidation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_attempts_invalidation.py
from spinlab.db import Database
from spinlab.models import Attempt, AttemptSource

def _seed(db):
    db.upsert_game("g", "Game", "any%")
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, start_type, start_ordinal,"
        " end_type, end_ordinal, created_at, updated_at)"
        " VALUES ('s1', 'g', 1, 'entrance', 0, 'goal', 0, '2026-01-01', '2026-01-01')"
    )
    db.conn.commit()

def _attempt(sid="sess1"):
    return Attempt(segment_id="s1", session_id=sid, completed=True,
                   time_ms=1000, source=AttemptSource.PRACTICE)

def test_set_attempt_invalidated():
    db = Database(":memory:")
    _seed(db)
    aid = db.log_attempt(_attempt())
    db.set_attempt_invalidated(aid, True)
    row = db.conn.execute(
        "SELECT invalidated FROM attempts WHERE id = ?", (aid,)
    ).fetchone()
    assert row[0] == 1
    db.set_attempt_invalidated(aid, False)
    row = db.conn.execute(
        "SELECT invalidated FROM attempts WHERE id = ?", (aid,)
    ).fetchone()
    assert row[0] == 0

def test_get_last_practice_attempt():
    db = Database(":memory:")
    _seed(db)
    a1 = db.log_attempt(_attempt(sid="sess1"))
    a2 = db.log_attempt(_attempt(sid="sess1"))
    last = db.get_last_practice_attempt(session_id="sess1")
    assert last is not None
    assert last == a2

def test_get_last_practice_attempt_none_when_empty():
    db = Database(":memory:")
    _seed(db)
    assert db.get_last_practice_attempt(session_id="sess1") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_attempts_invalidation.py -v`
Expected: FAIL — methods undefined.

- [ ] **Step 3: Add methods to AttemptsMixin**

In `python/spinlab/db/attempts.py`:

```python
def set_attempt_invalidated(self, attempt_id: int, invalidated: bool) -> None:
    self.conn.execute(
        "UPDATE attempts SET invalidated = ? WHERE id = ?",
        (int(invalidated), attempt_id),
    )
    self.conn.commit()

def get_last_practice_attempt(self, session_id: str) -> int | None:
    row = self.conn.execute(
        "SELECT id FROM attempts WHERE session_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return row[0] if row else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_attempts_invalidation.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db/attempts.py tests/test_attempts_invalidation.py
git commit -m "feat(db): add set_attempt_invalidated + get_last_practice_attempt"
```

---

## Task 3: Estimator filter — exclude invalidated rows

**Files:**
- Modify: `python/spinlab/scheduler.py` (`_attempts_from_rows`)
- Append: `tests/test_attempts_invalidation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_attempts_invalidation.py (append)
from spinlab.scheduler import _attempts_from_rows

def test_attempts_from_rows_excludes_invalidated():
    rows = [
        {"time_ms": 1000, "completed": 1, "deaths": 0, "clean_tail_ms": 1000, "invalidated": 0},
        {"time_ms": 9999, "completed": 1, "deaths": 0, "clean_tail_ms": 9999, "invalidated": 1},
        {"time_ms": 1100, "completed": 1, "deaths": 0, "clean_tail_ms": 1100, "invalidated": 0},
    ]
    result = _attempts_from_rows(rows)
    assert len(result) == 2
    assert all(r.time_ms != 9999 for r in result)

def test_attempts_from_rows_treats_missing_key_as_valid():
    rows = [
        {"time_ms": 1000, "completed": 1, "deaths": 0, "clean_tail_ms": 1000},
    ]
    result = _attempts_from_rows(rows)
    assert len(result) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_attempts_invalidation.py -v -k _attempts_from_rows`
Expected: FAIL — second row not excluded.

- [ ] **Step 3: Add filter**

In `python/spinlab/scheduler.py`, update `_attempts_from_rows`:

```python
def _attempts_from_rows(rows: list[dict]) -> list[AttemptRecord]:
    return [
        AttemptRecord(
            time_ms=r["time_ms"],
            completed=bool(r["completed"]),
            deaths=r["deaths"],
            clean_tail_ms=r["clean_tail_ms"],
        )
        for r in rows
        if not r.get("invalidated", False)
    ]
```

(Adapt fields to the actual current signature.)

- [ ] **Step 4: Also update the attempts fetch query**

Find `db.get_segment_attempts(segment_id)` in the codebase. The underlying SELECT must return the `invalidated` column (it will, after `SELECT *`, but if the query enumerates columns, add `invalidated`).

Alternatively, filter at the SQL level:

```python
def get_segment_attempts(self, segment_id: str, *,
                        include_invalidated: bool = False) -> list[dict]:
    inv_clause = "" if include_invalidated else "AND invalidated = 0"
    cur = self.conn.execute(
        f"SELECT * FROM attempts WHERE segment_id = ? {inv_clause} "
        "ORDER BY id",
        (segment_id,),
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_attempts_invalidation.py -v`
Expected: PASS.

- [ ] **Step 6: Run fast tests**

Run: `pytest -m "not (emulator or slow)"`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/scheduler.py python/spinlab/db/attempts.py tests/test_attempts_invalidation.py
git commit -m "feat: exclude invalidated attempts from estimator pipeline"
```

---

## Task 4: Lua — detect invalidate combo, send event

**Files:**
- Modify: `lua/spinlab.lua`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_lua_invalidate.py
import pytest
pytestmark = pytest.mark.emulator

def test_invalidate_combo_fires_event(run_lua_test):
    events = run_lua_test(
        rom="smw_test",
        setup_commands=[
            'set_invalidate_combo:["L","Select"]',
            # Simulate entering practice mode here per existing harness pattern.
        ],
        controller_sequence=[  # hypothetical: engage L+Select for 2 frames
            {"L": True, "Select": True, "frames": 2},
        ],
        expected_events=["attempt_invalidated"],
    )
    assert any(e["event"] == "attempt_invalidated" for e in events)
```

Adapt to the existing integration harness. If controller injection isn't wired for practice mode tests, skip this test and verify manually in Task 7; add a comment referencing the smoke test.

- [ ] **Step 2: Run test to verify it fails (or skip if harness doesn't support)**

Run: `pytest tests/integration/test_lua_invalidate.py -v -m emulator`
Expected: FAIL — `set_invalidate_combo` unknown.

- [ ] **Step 3: Add combo config + detection to Lua**

In `lua/spinlab.lua`:

```lua
-- Invalidation combo (SNES button names), set via TCP set_invalidate_combo.
local invalidate_combo = {"L", "Select"}
local invalidate_prev_down = false
local PRACTICE_MODE = "practice"  -- existing constant

local function combo_pressed()
  local input = emu.getInput(0)
  for _, btn in ipairs(invalidate_combo) do
    if not input[btn] then return false end
  end
  return true
end

local function check_invalidate_combo()
  if current_mode ~= PRACTICE_MODE then
    invalidate_prev_down = false
    return
  end
  local down = combo_pressed()
  if down and not invalidate_prev_down then
    send_event({ event = "attempt_invalidated" })
    log("attempt_invalidated: combo pressed")
  end
  invalidate_prev_down = down
end
```

Call `check_invalidate_combo()` from the main frame callback (the same place that currently polls for other input / checks TCP, approximately line 1200-1300 — search for `on_frame` or similar main-loop hook).

Add the TCP command handler:

```lua
["set_invalidate_combo"] = function(arg)
  local ok, decoded = pcall(function() return json.decode(arg) end)
  if ok and type(decoded) == "table" then
    invalidate_combo = decoded
    log("set_invalidate_combo: " .. table.concat(invalidate_combo, "+"))
  end
end,
```

- [ ] **Step 4: Run test (if harness supports)**

Run: `pytest tests/integration/test_lua_invalidate.py -v -m emulator`
Expected: PASS or skipped.

- [ ] **Step 5: Commit**

```bash
git add lua/spinlab.lua tests/integration/test_lua_invalidate.py
git commit -m "feat(lua): detect invalidate combo during practice, fire event"
```

---

## Task 5: Python — send set_invalidate_combo, handle attempt_invalidated event

**Files:**
- Modify: `python/spinlab/session_manager.py` (or whichever owns TCP events + practice session)
- Create: `tests/test_invalidate_flow.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_invalidate_flow.py
from unittest.mock import MagicMock
from spinlab.session_manager import SessionManager

def test_attempt_invalidated_event_marks_last_attempt():
    sm = SessionManager.__new__(SessionManager)  # bypass __init__
    sm.db = MagicMock()
    sm.db.get_last_practice_attempt.return_value = 42
    sm.practice_session_id = "sess1"
    # Assume SessionManager has a dispatch method or _on_event handler; adapt:
    sm._on_event({"event": "attempt_invalidated"})
    sm.db.get_last_practice_attempt.assert_called_once_with(session_id="sess1")
    sm.db.set_attempt_invalidated.assert_called_once_with(42, True)

def test_attempt_invalidated_noop_when_no_recent_attempt():
    sm = SessionManager.__new__(SessionManager)
    sm.db = MagicMock()
    sm.db.get_last_practice_attempt.return_value = None
    sm.practice_session_id = "sess1"
    sm._on_event({"event": "attempt_invalidated"})
    sm.db.set_attempt_invalidated.assert_not_called()
```

(Adapt class/method names to the actual session-manager dispatch mechanism — examine `session_manager.py` to find how events are routed today.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_invalidate_flow.py -v`
Expected: FAIL — unhandled event.

- [ ] **Step 3: Route the new event**

In `session_manager.py` (or its event dispatch location), add a handler:

```python
def _handle_attempt_invalidated(self, event: dict) -> None:
    sid = self.practice_session_id
    if sid is None:
        return
    aid = self.db.get_last_practice_attempt(session_id=sid)
    if aid is None:
        return
    self.db.set_attempt_invalidated(aid, True)
    logger.info("Marked attempt %d as invalidated", aid)
```

Register it in the event dispatch dict/chain alongside existing event handlers.

- [ ] **Step 4: Push set_invalidate_combo to Lua at startup**

After TCP connect (likely near where `set_conditions` is sent per Plan 1 Task 11), add:

```python
import json
combo = self.config.practice.invalidate_combo
self.tcp_client.send(f"set_invalidate_combo:{json.dumps(combo)}\n")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_invalidate_flow.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/session_manager.py tests/test_invalidate_flow.py
git commit -m "feat: handle attempt_invalidated event, push invalidate combo to Lua"
```

---

## Task 6: Dashboard — invalidate toggle endpoint + button

**Files:**
- Modify or Create: `python/spinlab/routes/attempts.py` (find existing attempts routes; if none, create)
- Modify: `frontend/src/*.ts` (wherever recent-attempts are rendered)

- [ ] **Step 1: Locate existing attempts routes**

Run: `grep -rn "attempts" python/spinlab/routes/ --include="*.py"`

Identify whether a router for attempts exists; otherwise add to an existing practice-related route module.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_attempts_route.py
from fastapi.testclient import TestClient
# Use existing app fixture pattern.

def test_patch_attempt_invalidates(client, seeded_attempt_id):
    resp = client.patch(
        f"/api/attempts/{seeded_attempt_id}",
        json={"invalidated": True},
    )
    assert resp.status_code == 200
    # Verify via DB fixture that the row is now invalidated=1

def test_patch_attempt_can_unset(client, seeded_attempt_id):
    client.patch(f"/api/attempts/{seeded_attempt_id}", json={"invalidated": True})
    resp = client.patch(f"/api/attempts/{seeded_attempt_id}", json={"invalidated": False})
    assert resp.status_code == 200
```

(Adapt to fixture style used in existing route tests.)

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_attempts_route.py -v`
Expected: FAIL — 404 or method not allowed.

- [ ] **Step 4: Add PATCH endpoint**

In `python/spinlab/routes/attempts.py`:

```python
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

class AttemptPatch(BaseModel):
    invalidated: bool

@router.patch("/api/attempts/{attempt_id}")
def patch_attempt(attempt_id: int, body: AttemptPatch, db=...):
    db.set_attempt_invalidated(attempt_id, body.invalidated)
    return {"ok": True, "id": attempt_id, "invalidated": body.invalidated}
```

Register the router in the app factory.

- [ ] **Step 5: Add a frontend invalidate button**

In whichever TypeScript module renders the recent-attempts list (grep `frontend/src` for `attempts` or `recent`), add a button per row:

```ts
async function toggleInvalidate(attemptId: number, next: boolean) {
  await fetch(`/api/attempts/${attemptId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ invalidated: next }),
  });
  // Re-fetch list or mutate local state
}
```

Render the button with text "Mark invalid" / "Restore" based on the current `invalidated` flag of the row.

Update the attempts API response type in `frontend/src/types.ts` to include `invalidated: boolean`.

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_attempts_route.py -v`
Expected: PASS.

Run: `cd frontend && npm run typecheck && npm test`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/routes/ frontend/src/ tests/test_attempts_route.py
git commit -m "feat: PATCH /api/attempts/:id to toggle invalidation + dashboard button"
```

---

## Task 7: End-to-end verification

- [ ] **Step 1: Run full fast suite**

Run: `pytest -m "not (emulator or slow)"`
Expected: PASS.

- [ ] **Step 2: Run emulator tests**

Run: `pytest -m emulator`
Expected: PASS.

- [ ] **Step 3: Manual smoke test**

1. Start dashboard + emulator + Lua.
2. Enter practice mode for some segment.
3. Complete an attempt.
4. Press L+Select during the auto-advance pause.
5. Expected: dashboard recent-attempts shows that attempt as "invalid".
6. Click "Restore" on the attempt.
7. Expected: row re-activates; next estimator refresh includes it again.

---

## Self-Review Checklist

- [x] Spec requirement: `practice.invalidate_combo` config key → Task 1
- [x] DB helpers for invalidation + last-attempt lookup → Task 2
- [x] Estimator excludes invalidated attempts → Task 3
- [x] In-emulator combo detection during practice → Task 4
- [x] Python event handling + push combo to Lua → Task 5
- [x] Dashboard PATCH endpoint + button → Task 6

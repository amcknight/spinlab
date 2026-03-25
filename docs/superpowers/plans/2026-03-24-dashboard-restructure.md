# Dashboard Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the SpinLab dashboard — remove Live tab, add replay UI with draft reference lifecycle, widen to 428px, put status in the header bar.

**Architecture:** Backend gains a `draft` column on `capture_runs`, new draft save/discard endpoints, a spinrec existence check, and hard-delete cascade method. Frontend removes `live.js`, adds `header.js`, restructures HTML to two tabs (Model absorbs practice card, Manage absorbs reference controls). SSE state drives a persistent header status chip.

**Tech Stack:** Python/FastAPI backend, SQLite, vanilla JS ES modules, pytest + FastAPI TestClient.

**Spec:** `docs/superpowers/specs/2026-03-24-dashboard-restructure-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `python/spinlab/db.py` | Modify | Add `draft` column, `hard_delete_capture_run()`, modify `list_capture_runs()` to filter drafts and include `has_spinrec` |
| `python/spinlab/session_manager.py` | Modify | Add `draft_run_id`/`draft_segments_count` fields, draft lifecycle in stop_reference/replay events, draft guards, `save_draft()`/`discard_draft()`, modify `get_state()` |
| `python/spinlab/dashboard.py` | Modify | Add draft save/discard/spinrec endpoints, modify replay start to accept `ref_id` |
| `python/spinlab/static/index.html` | Rewrite | Two tabs, header bar with game selector + mode chip, practice card in Model, reference controls in Manage |
| `python/spinlab/static/style.css` | Rewrite | 428px width, header chip, game selector popover, practice card, save/discard prompt |
| `python/spinlab/static/app.js` | Rewrite | Two-tab wiring, SSE → header updates, localStorage game persistence |
| `python/spinlab/static/header.js` | Create | Game selector popover, mode chip rendering, stop button |
| `python/spinlab/static/model.js` | Modify | Add practice card rendering above model table |
| `python/spinlab/static/manage.js` | Modify | Add start reference, replay, save/discard prompt, dropdown locking |
| `python/spinlab/static/live.js` | Delete | Logic distributed to header.js and model.js |
| `python/spinlab/static/api.js` | No change | |
| `python/spinlab/static/format.js` | No change | |
| `tests/test_draft_lifecycle.py` | Create | Draft save/discard, guards, state transitions |
| `tests/test_db_references.py` | Modify | Add tests for hard_delete, draft column, has_spinrec |

---

## Task 1: DB — Add `draft` Column and `hard_delete_capture_run`

**Files:**
- Modify: `python/spinlab/db.py:90-96` (schema), `db.py:470-517` (capture run methods)
- Test: `tests/test_db_references.py`

- [ ] **Step 1: Write failing tests for draft column and hard delete**

In `tests/test_db_references.py`, add a new test class:

```python
class TestDraftColumn:
    def test_create_capture_run_defaults_draft_zero(self, tmp_db):
        """Existing behavior: create_capture_run sets draft=0 (backwards compat)."""
        tmp_db.upsert_game("g1", "Game", "any%")
        tmp_db.create_capture_run("r1", "g1", "Run 1")
        rows = tmp_db.conn.execute(
            "SELECT draft FROM capture_runs WHERE id = 'r1'"
        ).fetchone()
        assert rows[0] == 0

    def test_create_draft_capture_run(self, tmp_db):
        tmp_db.upsert_game("g1", "Game", "any%")
        tmp_db.create_capture_run("r1", "g1", "Run 1", draft=True)
        rows = tmp_db.conn.execute(
            "SELECT draft FROM capture_runs WHERE id = 'r1'"
        ).fetchone()
        assert rows[0] == 1

    def test_list_capture_runs_excludes_drafts(self, tmp_db):
        tmp_db.upsert_game("g1", "Game", "any%")
        tmp_db.create_capture_run("r1", "g1", "Saved", draft=False)
        tmp_db.create_capture_run("r2", "g1", "Draft", draft=True)
        refs = tmp_db.list_capture_runs("g1")
        assert len(refs) == 1
        assert refs[0]["id"] == "r1"

    def test_promote_draft(self, tmp_db):
        tmp_db.upsert_game("g1", "Game", "any%")
        tmp_db.create_capture_run("r1", "g1", "Draft", draft=True)
        tmp_db.promote_draft("r1", "My Run")
        refs = tmp_db.list_capture_runs("g1")
        assert len(refs) == 1
        assert refs[0]["name"] == "My Run"
        assert refs[0]["draft"] == 0


class TestHardDelete:
    def test_hard_delete_removes_everything(self, tmp_db):
        """Hard delete cascades: variants, model_state, attempts, segments, run."""
        from spinlab.models import Segment, SegmentVariant
        tmp_db.upsert_game("g1", "Game", "any%")
        tmp_db.create_capture_run("r1", "g1", "Draft", draft=True)
        seg = Segment(
            id="seg1", game_id="g1", level_number=0x105,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            ordinal=1, reference_id="r1",
        )
        tmp_db.upsert_segment(seg)
        tmp_db.add_variant(SegmentVariant(
            segment_id="seg1", variant_type="cold",
            state_path="/tmp/s.mss", is_default=True,
        ))
        # Add a model_state row
        tmp_db.conn.execute(
            "INSERT INTO model_state (segment_id, estimator, state_json, updated_at) "
            "VALUES ('seg1', 'kalman', '{}', '2026-01-01')"
        )
        # Add an attempt row
        tmp_db.conn.execute(
            "INSERT INTO attempts (segment_id, session_id, completed, time_ms, strat_version, created_at) "
            "VALUES ('seg1', 'sess1', 1, 5000, 1, '2026-01-01')"
        )
        tmp_db.conn.commit()

        tmp_db.hard_delete_capture_run("r1")

        assert tmp_db.conn.execute("SELECT COUNT(*) FROM capture_runs WHERE id='r1'").fetchone()[0] == 0
        assert tmp_db.conn.execute("SELECT COUNT(*) FROM segments WHERE id='seg1'").fetchone()[0] == 0
        assert tmp_db.conn.execute("SELECT COUNT(*) FROM segment_variants WHERE segment_id='seg1'").fetchone()[0] == 0
        assert tmp_db.conn.execute("SELECT COUNT(*) FROM model_state WHERE segment_id='seg1'").fetchone()[0] == 0
        assert tmp_db.conn.execute("SELECT COUNT(*) FROM attempts WHERE segment_id='seg1'").fetchone()[0] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_db_references.py::TestDraftColumn -v && python -m pytest tests/test_db_references.py::TestHardDelete -v`
Expected: FAIL — no `draft` column, no `create_capture_run(draft=)`, no `promote_draft`, no `hard_delete_capture_run`

- [ ] **Step 3: Add draft column to schema and update methods**

In `python/spinlab/db.py`, modify the `capture_runs` table schema (line ~90):

```python
CREATE TABLE IF NOT EXISTS capture_runs (
  id TEXT PRIMARY KEY,
  game_id TEXT NOT NULL REFERENCES games(id),
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  active INTEGER DEFAULT 0,
  draft INTEGER DEFAULT 0
);
```

Add migration in `_init_schema()` after the existing migration block — add the `draft` column if it doesn't exist:

```python
# Add draft column if missing (dashboard restructure)
try:
    self.conn.execute("ALTER TABLE capture_runs ADD COLUMN draft INTEGER DEFAULT 0")
    self.conn.commit()
except sqlite3.OperationalError:
    pass  # Column already exists
```

Modify `create_capture_run` to accept `draft` parameter:

```python
def create_capture_run(self, run_id: str, game_id: str, name: str, draft: bool = False) -> None:
    now = datetime.now(UTC).isoformat()
    self.conn.execute(
        "INSERT INTO capture_runs (id, game_id, name, created_at, active, draft) "
        "VALUES (?, ?, ?, ?, 0, ?)",
        (run_id, game_id, name, now, 1 if draft else 0),
    )
    self.conn.commit()
```

Modify `list_capture_runs` to filter drafts and include `draft` field:

```python
def list_capture_runs(self, game_id: str) -> list[dict]:
    rows = self.conn.execute(
        "SELECT id, game_id, name, created_at, active, draft FROM capture_runs "
        "WHERE game_id = ? AND draft = 0 ORDER BY created_at",
        (game_id,),
    ).fetchall()
    return [dict(r) for r in rows]
```

Add `promote_draft`:

```python
def promote_draft(self, run_id: str, name: str) -> None:
    """Promote a draft capture run to saved: rename and set draft=0."""
    self.conn.execute(
        "UPDATE capture_runs SET draft = 0, name = ? WHERE id = ?",
        (name, run_id),
    )
    self.conn.commit()
```

Add `hard_delete_capture_run`:

```python
def hard_delete_capture_run(self, run_id: str) -> None:
    """Hard delete: remove run, segments, variants, model_state, attempts."""
    seg_ids = [
        r[0] for r in self.conn.execute(
            "SELECT id FROM segments WHERE reference_id = ?", (run_id,)
        ).fetchall()
    ]
    if seg_ids:
        placeholders = ",".join("?" * len(seg_ids))
        self.conn.execute(
            f"DELETE FROM segment_variants WHERE segment_id IN ({placeholders})",
            seg_ids,
        )
        self.conn.execute(
            f"DELETE FROM model_state WHERE segment_id IN ({placeholders})",
            seg_ids,
        )
        self.conn.execute(
            f"DELETE FROM attempts WHERE segment_id IN ({placeholders})",
            seg_ids,
        )
        self.conn.execute(
            f"DELETE FROM segments WHERE reference_id = ?", (run_id,),
        )
    # Always delete the capture_run row, even if it had no segments
    self.conn.execute("DELETE FROM capture_runs WHERE id = ?", (run_id,))
    self.conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_db_references.py -v`
Expected: All pass, including existing tests (backwards compat via `draft=False` default)

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db.py tests/test_db_references.py
git commit -m "feat(db): add draft column, promote_draft, hard_delete_capture_run"
```

---

## Task 2: SessionManager — Draft Lifecycle

**Files:**
- Modify: `python/spinlab/session_manager.py:20-60` (init), `session_manager.py:61-120` (get_state), `session_manager.py:488-551` (start/stop reference, replay)
- Test: `tests/test_draft_lifecycle.py` (create)

- [ ] **Step 1: Write failing tests for draft lifecycle**

Create `tests/test_draft_lifecycle.py`:

```python
"""Tests for draft reference lifecycle in SessionManager."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from spinlab.session_manager import SessionManager


def make_mock_tcp():
    tcp = MagicMock()
    tcp.is_connected = True
    tcp.send = AsyncMock()
    tcp.recv_event = AsyncMock(return_value=None)
    return tcp


def make_mock_db():
    db = MagicMock()
    db.upsert_game = MagicMock()
    db.create_session = MagicMock()
    db.end_session = MagicMock()
    db.create_capture_run = MagicMock()
    db.set_active_capture_run = MagicMock()
    db.get_recent_attempts = MagicMock(return_value=[])
    db.get_all_segments_with_model = MagicMock(return_value=[])
    db.load_model_state = MagicMock(return_value=None)
    db.load_allocator_config = MagicMock(return_value=None)
    db.upsert_segment = MagicMock()
    db.add_variant = MagicMock()
    db.get_active_segments = MagicMock(return_value=[])
    db.promote_draft = MagicMock()
    db.hard_delete_capture_run = MagicMock()
    return db


def make_sm(tmp_path):
    db = make_mock_db()
    tcp = make_mock_tcp()
    sm = SessionManager(db=db, tcp=tcp, rom_dir=tmp_path, default_category="any%", data_dir=tmp_path)
    sm.game_id = "abcdef0123456789"
    sm.game_name = "Test Game"
    return sm, db, tcp


class TestStopReferenceCreatesDraft:
    @pytest.mark.asyncio
    async def test_stop_reference_enters_draft_state(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        await sm.start_reference()
        run_id = sm.ref_capture_run_id
        sm.ref_segments_count = 5

        await sm.stop_reference()
        assert sm.mode == "idle"
        assert sm.draft_run_id == run_id
        assert sm.draft_segments_count == 5

    @pytest.mark.asyncio
    async def test_start_reference_creates_draft_run(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        await sm.start_reference()
        db.create_capture_run.assert_called_once()
        call_kwargs = db.create_capture_run.call_args
        # draft=True should be passed
        assert call_kwargs[1].get("draft") is True or call_kwargs[0][3] is True  # positional or keyword

    @pytest.mark.asyncio
    async def test_start_reference_does_not_set_active(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        await sm.start_reference()
        db.set_active_capture_run.assert_not_called()


class TestReplayCreatesDraft:
    @pytest.mark.asyncio
    async def test_replay_finished_enters_draft_state(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        # Simulate active replay
        sm.mode = "replay"
        sm.ref_capture_run_id = "replay_abc"
        sm.ref_segments_count = 8

        await sm.route_event({"event": "replay_finished", "frames_played": 5000})
        assert sm.mode == "idle"
        assert sm.draft_run_id == "replay_abc"
        assert sm.draft_segments_count == 8

    @pytest.mark.asyncio
    async def test_replay_error_with_segments_enters_draft(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        sm.mode = "replay"
        sm.ref_capture_run_id = "replay_abc"
        sm.ref_segments_count = 3

        await sm.route_event({"event": "replay_error", "message": "fail"})
        assert sm.draft_run_id == "replay_abc"
        assert sm.draft_segments_count == 3

    @pytest.mark.asyncio
    async def test_replay_error_no_segments_auto_discards(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        sm.mode = "replay"
        sm.ref_capture_run_id = "replay_abc"
        sm.ref_segments_count = 0

        await sm.route_event({"event": "replay_error", "message": "fail"})
        assert sm.draft_run_id is None
        db.hard_delete_capture_run.assert_called_once_with("replay_abc")


class TestDraftGuards:
    @pytest.mark.asyncio
    async def test_start_reference_blocked_by_draft(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        sm.draft_run_id = "draft_pending"
        result = await sm.start_reference()
        assert result["status"] == "draft_pending"

    @pytest.mark.asyncio
    async def test_start_replay_blocked_by_draft(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        sm.draft_run_id = "draft_pending"
        result = await sm.start_replay("/data/test.spinrec")
        assert result["status"] == "draft_pending"

    @pytest.mark.asyncio
    async def test_start_practice_blocked_by_draft(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        sm.draft_run_id = "draft_pending"
        result = await sm.start_practice()
        assert result["status"] == "draft_pending"


class TestSaveAndDiscard:
    @pytest.mark.asyncio
    async def test_save_draft(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        sm.draft_run_id = "live_abc"
        sm.draft_segments_count = 5

        result = await sm.save_draft("My Run")
        assert result["status"] == "ok"
        assert sm.draft_run_id is None
        assert sm.draft_segments_count == 0
        db.promote_draft.assert_called_once_with("live_abc", "My Run")
        db.set_active_capture_run.assert_called_once_with("live_abc")

    @pytest.mark.asyncio
    async def test_discard_draft(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        sm.draft_run_id = "live_abc"
        sm.draft_segments_count = 5

        result = await sm.discard_draft()
        assert result["status"] == "ok"
        assert sm.draft_run_id is None
        assert sm.draft_segments_count == 0
        db.hard_delete_capture_run.assert_called_once_with("live_abc")

    @pytest.mark.asyncio
    async def test_save_no_draft_returns_error(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        result = await sm.save_draft("Name")
        assert result["status"] == "no_draft"


class TestGetStateDraft:
    @pytest.mark.asyncio
    async def test_get_state_includes_draft(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        sm.draft_run_id = "live_abc"
        sm.draft_segments_count = 12
        state = sm.get_state()
        assert state["draft"] == {"run_id": "live_abc", "segments_captured": 12}

    @pytest.mark.asyncio
    async def test_get_state_no_draft(self, tmp_path):
        sm, db, tcp = make_sm(tmp_path)
        state = sm.get_state()
        assert state.get("draft") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_draft_lifecycle.py -v`
Expected: FAIL — no `draft_run_id` attribute, no `save_draft`, no `discard_draft`, etc.

- [ ] **Step 3: Implement draft lifecycle in SessionManager**

In `python/spinlab/session_manager.py`:

**Add fields in `__init__`** (after `self.rec_path` around line 53):

```python
# Draft state (persists after recording/replay stops, until save/discard)
self.draft_run_id: str | None = None
self.draft_segments_count: int = 0
```

**Add `_enter_draft_state` helper** (after `_clear_ref_state`):

```python
def _enter_draft_state(self) -> None:
    """Copy ref state to draft fields before clearing."""
    self.draft_run_id = self.ref_capture_run_id
    self.draft_segments_count = self.ref_segments_count
```

**Modify `stop_reference`** — copy to draft before clearing:

```python
async def stop_reference(self) -> dict:
    """End reference capture — enters draft state for save/discard."""
    if self.mode != "reference":
        return {"status": "not_in_reference"}
    if self.tcp.is_connected:
        await self.tcp.send(json.dumps({"event": "reference_stop"}))
    self._enter_draft_state()
    self._clear_ref_state()
    await self._notify_sse()
    return {"status": "stopped"}
```

**Modify `start_reference`** — create as draft, don't set active, add draft guard:

```python
async def start_reference(self, run_name: str | None = None) -> dict:
    """Begin reference capture."""
    if self.draft_run_id:
        return {"status": "draft_pending"}
    if self.mode in ("practice", "replay"):
        return {"status": f"{self.mode}_active"}
    if not self.tcp.is_connected:
        return {"status": "not_connected"}
    gid = self._require_game()
    self._clear_ref_state()
    run_id = f"live_{uuid.uuid4().hex[:8]}"
    name = run_name or f"Live {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"
    self.db.create_capture_run(run_id, gid, name, draft=True)
    self.ref_capture_run_id = run_id
    self.mode = "reference"
    rec_path = str(self._game_rec_dir() / f"{run_id}.spinrec")
    await self.tcp.send(json.dumps({"event": "reference_start", "path": rec_path}))
    await self._notify_sse()
    return {"status": "started", "run_id": run_id, "run_name": name}
```

**Modify `start_replay`** — add draft guard, don't set active:

```python
async def start_replay(self, spinrec_path: str, speed: int = 0) -> dict:
    """Begin replay of a .spinrec file."""
    if self.draft_run_id:
        return {"status": "draft_pending"}
    if self.mode == "practice":
        return {"status": "practice_active"}
    if self.mode == "reference":
        return {"status": "reference_active"}
    if self.mode == "replay":
        return {"status": "already_replaying"}
    if not self.tcp.is_connected:
        return {"status": "not_connected"}

    gid = self._require_game()
    self._clear_ref_state()
    run_id = f"replay_{uuid.uuid4().hex[:8]}"
    name = f"Replay {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"
    self.db.create_capture_run(run_id, gid, name, draft=True)
    self.ref_capture_run_id = run_id

    self.mode = "replay"
    await self.tcp.send(json.dumps({"event": "replay", "path": spinrec_path, "speed": speed}))
    await self._notify_sse()
    return {"status": "started", "run_id": run_id}
```

**Modify `start_practice`** — add draft guard at the top:

```python
async def start_practice(self) -> dict:
    """Begin practice session."""
    if self.draft_run_id:
        return {"status": "draft_pending"}
    # ... rest unchanged
```

**Modify `route_event` replay handlers** — enter draft state:

Replace the `replay_finished` handler:

```python
if evt_type == "replay_finished":
    self._enter_draft_state()
    self._clear_ref_state()
    await self._notify_sse()
    return
if evt_type == "replay_error":
    if self.ref_segments_count > 0:
        self._enter_draft_state()
        self._clear_ref_state()
    else:
        run_id = self.ref_capture_run_id
        self._clear_ref_state()
        if run_id:
            self.db.hard_delete_capture_run(run_id)
    await self._notify_sse()
    return
```

**Add `save_draft` and `discard_draft` methods:**

```python
async def save_draft(self, name: str) -> dict:
    """Promote draft capture run to saved reference."""
    if not self.draft_run_id:
        return {"status": "no_draft"}
    self.db.promote_draft(self.draft_run_id, name)
    self.db.set_active_capture_run(self.draft_run_id)
    self.draft_run_id = None
    self.draft_segments_count = 0
    await self._notify_sse()
    return {"status": "ok"}

async def discard_draft(self) -> dict:
    """Hard-delete draft capture run and all associated data."""
    if not self.draft_run_id:
        return {"status": "no_draft"}
    self.db.hard_delete_capture_run(self.draft_run_id)
    self.draft_run_id = None
    self.draft_segments_count = 0
    await self._notify_sse()
    return {"status": "ok"}
```

**Modify `get_state`** — add draft field (after the replay block around line 117):

```python
if self.draft_run_id:
    base["draft"] = {
        "run_id": self.draft_run_id,
        "segments_captured": self.draft_segments_count,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_draft_lifecycle.py -v`
Expected: All pass

- [ ] **Step 5: Add startup draft recovery**

In `SessionManager.__init__`, after setting `self.draft_segments_count = 0`, add a method call to restore draft state on startup. Add a `_recover_draft` method:

```python
def _recover_draft(self) -> None:
    """On startup, check for orphaned draft capture runs and restore state."""
    if not self.game_id:
        return
    # Query for draft=1 rows for this game
    rows = self.db.conn.execute(
        "SELECT id FROM capture_runs WHERE game_id = ? AND draft = 1 ORDER BY created_at DESC",
        (self.game_id,),
    ).fetchall()
    if not rows:
        return
    # Keep the most recent, hard-delete the rest
    self.draft_run_id = rows[0][0]
    self.draft_segments_count = self.db.conn.execute(
        "SELECT COUNT(*) FROM segments WHERE reference_id = ? AND active = 1",
        (self.draft_run_id,),
    ).fetchone()[0]
    for row in rows[1:]:
        self.db.hard_delete_capture_run(row[0])
```

Call `_recover_draft()` from `switch_game()` after setting `self.game_id`:

```python
async def switch_game(self, game_id: str, game_name: str) -> None:
    # ... existing code ...
    self.game_id = game_id
    self.game_name = game_name
    self.scheduler = None
    self.mode = "idle"
    self._recover_draft()  # <-- add this
    await self._notify_sse()
```

- [ ] **Step 6: Run existing tests to check for regressions**

Run: `python -m pytest tests/test_session_manager.py tests/test_replay.py tests/test_dashboard.py -v`
Expected: Some existing replay tests may need updating (they assert `set_active_capture_run` was called, which no longer happens). Fix any failures:
- `test_replay.py::TestStartReplay::test_sends_replay_command` — may fail if it checked `set_active_capture_run`. Remove that assertion.
- `test_session_manager.py::TestReferenceMode` — may fail on `set_active_capture_run` assertion. Remove it.
- `test_replay.py::TestReplayEvents::test_replay_finished_returns_to_idle` — now needs to also assert `sm.draft_run_id` is set.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/session_manager.py tests/test_draft_lifecycle.py tests/test_replay.py tests/test_session_manager.py
git commit -m "feat(session): draft reference lifecycle — save/discard flow"
```

---

## Task 3: Dashboard API — Draft and Spinrec Endpoints

**Files:**
- Modify: `python/spinlab/dashboard.py:103-114` (replay endpoints), add new endpoints
- Test: `tests/test_dashboard.py` or `tests/test_dashboard_references.py`

- [ ] **Step 1: Write failing tests for new endpoints**

Add to `tests/test_dashboard_references.py` (or create if needed — check what exists):

```python
class TestDraftEndpoints:
    def test_save_draft(self, client):
        # Inject draft state
        client.app.state.session.draft_run_id = "live_abc"
        client.app.state.session.draft_segments_count = 5
        client.app.state.session.save_draft = AsyncMock(return_value={"status": "ok"})

        resp = client.post("/api/references/draft/save", json={"name": "My Run"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_discard_draft(self, client):
        client.app.state.session.draft_run_id = "live_abc"
        client.app.state.session.discard_draft = AsyncMock(return_value={"status": "ok"})

        resp = client.post("/api/references/draft/discard")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestSpinrecEndpoint:
    def test_spinrec_exists(self, client, tmp_path):
        client.app.state.session.game_id = "testgame"
        client.app.state.session.data_dir = tmp_path
        rec_dir = tmp_path / "testgame" / "rec"
        rec_dir.mkdir(parents=True)
        (rec_dir / "ref_abc.spinrec").write_bytes(b"SREC")

        resp = client.get("/api/references/ref_abc/spinrec")
        assert resp.status_code == 200
        assert resp.json()["exists"] is True

    def test_spinrec_not_found(self, client):
        client.app.state.session.game_id = "testgame"
        resp = client.get("/api/references/ref_abc/spinrec")
        assert resp.status_code == 200
        assert resp.json()["exists"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_dashboard_references.py::TestDraftEndpoints tests/test_dashboard_references.py::TestSpinrecEndpoint -v`
Expected: FAIL — endpoints don't exist

- [ ] **Step 3: Add endpoints to dashboard.py**

In `python/spinlab/dashboard.py`, add after the existing replay endpoints (~line 114):

```python
@app.post("/api/references/draft/save")
async def draft_save(req: Request):
    body = await req.json()
    name = body.get("name", "Untitled")
    return await session.save_draft(name)

@app.post("/api/references/draft/discard")
async def draft_discard():
    return await session.discard_draft()

@app.get("/api/references/{ref_id}/spinrec")
def check_spinrec(ref_id: str):
    gid = session.game_id or "unknown"
    rec_path = session.data_dir / gid / "rec" / f"{ref_id}.spinrec"
    if rec_path.is_file():
        return {"exists": True, "path": str(rec_path)}
    return {"exists": False}
```

Modify the replay start endpoint to accept `ref_id` instead of raw path:

```python
@app.post("/api/replay/start")
async def replay_start(req: Request):
    body = await req.json()
    ref_id = body.get("ref_id")
    speed = body.get("speed", 0)
    if not ref_id:
        raise HTTPException(status_code=400, detail="ref_id required")
    gid = session.game_id or "unknown"
    spinrec_path = str(session.data_dir / gid / "rec" / f"{ref_id}.spinrec")
    return await session.start_replay(spinrec_path, speed=speed)
```

Also modify `list_references` to include `has_spinrec`:

```python
@app.get("/api/references")
def list_references():
    gid = session._require_game()
    refs = db.list_capture_runs(gid)
    for ref in refs:
        rec_path = session.data_dir / gid / "rec" / f"{ref['id']}.spinrec"
        ref["has_spinrec"] = rec_path.is_file()
    return {"references": refs}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_dashboard_references.py -v`
Expected: All pass

- [ ] **Step 5: Run full test suite for regressions**

Run: `python -m pytest tests/ -v`
Expected: All pass. If any test used the old `POST /api/replay/start` with `path`, update it to use `ref_id`.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/dashboard.py tests/test_dashboard_references.py
git commit -m "feat(api): draft save/discard, spinrec check, replay-by-ref-id endpoints"
```

---

## Task 4: Frontend — HTML Restructure and CSS

**Files:**
- Rewrite: `python/spinlab/static/index.html`
- Rewrite: `python/spinlab/static/style.css`

This task is pure markup/CSS — no JS logic yet. The goal is the structural skeleton that the JS modules will wire up.

- [ ] **Step 1: Rewrite index.html**

Replace `python/spinlab/static/index.html` with the new two-tab structure:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SpinLab</title>
  <link rel="stylesheet" href="/static/style.css?v=20">
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#127922;</text></svg>">
</head>
<body>
<div id="app">
  <header>
    <div class="header-left">
      <span class="logo">SpinLab</span>
      <button id="game-selector" class="game-selector-btn">
        <span id="game-name">No game</span>
        <span class="caret">&#9662;</span>
      </button>
      <div id="game-popover" class="popover" style="display:none">
        <input id="rom-filter" type="text" placeholder="Filter ROMs..." autocomplete="off">
        <ul id="rom-list"></ul>
      </div>
    </div>
    <div id="mode-chip" class="mode-chip idle">
      <span class="mode-dot"></span>
      <span id="mode-label">Idle</span>
      <button id="mode-stop" class="mode-stop" style="display:none" title="Stop">&times;</button>
    </div>
  </header>

  <nav id="tabs">
    <button class="tab active" data-tab="model">Model</button>
    <button class="tab" data-tab="manage">Manage</button>
  </nav>

  <main>
    <!-- Model Tab -->
    <section id="tab-model" class="tab-content active">
      <!-- Practice card (hidden when not practicing) -->
      <div id="practice-card" style="display:none">
        <div class="card">
          <div class="segment-header">
            <span id="current-goal" class="goal-label"></span>
            <span id="current-attempts" class="dim"></span>
          </div>
          <div id="insight" class="insight-card"></div>
        </div>
        <h3>Up Next</h3>
        <ul id="queue"></ul>
        <h3>Recent</h3>
        <ul id="recent"></ul>
        <div class="practice-footer">
          <span id="session-stats" class="dim"></span>
        </div>
        <div class="allocator-row">
          <label>Allocator:</label>
          <select id="allocator-select">
            <option value="greedy">Greedy</option>
            <option value="random">Random</option>
            <option value="round_robin">Round Robin</option>
          </select>
        </div>
      </div>

      <!-- Model table (always visible) -->
      <div class="model-header">
        <h2>Model State</h2>
        <select id="estimator-select">
          <option value="kalman">Kalman</option>
        </select>
      </div>
      <table id="model-table">
        <thead>
          <tr>
            <th title="Level section being practiced">Segment</th>
            <th title="Expected completion time in seconds">Avg</th>
            <th title="How your time changes per run (negative = improving)">Trend</th>
            <th title="95% confidence interval for the trend">Range</th>
            <th title="Practice value: how much time you save per run here">Value</th>
            <th title="Completed practice attempts">Runs</th>
            <th title="Your fastest completion">Best</th>
          </tr>
        </thead>
        <tbody id="model-body"></tbody>
      </table>
    </section>

    <!-- Manage Tab -->
    <section id="tab-manage" class="tab-content">
      <!-- References section -->
      <div class="manage-section">
        <h3>References</h3>
        <div class="ref-row">
          <select id="ref-select"></select>
          <button id="btn-ref-rename" class="btn-sm" title="Rename">&#9998;</button>
          <button id="btn-ref-delete" class="btn-sm btn-danger-sm" title="Delete">&#10005;</button>
        </div>
        <div class="ref-actions">
          <button id="btn-ref-start" class="btn-primary">Start Reference Run</button>
          <button id="btn-replay" class="btn-primary" disabled>Replay</button>
        </div>
      </div>

      <!-- Draft save/discard prompt (hidden by default) -->
      <div id="draft-prompt" class="manage-section draft-prompt" style="display:none">
        <p id="draft-summary"></p>
        <input id="draft-name" type="text" placeholder="Name this run..." autocomplete="off">
        <div class="draft-actions">
          <button id="btn-draft-save" class="btn-primary">Save</button>
          <button id="btn-draft-discard" class="btn-danger">Discard</button>
        </div>
      </div>

      <!-- Segments table -->
      <div class="manage-section">
        <h3>Segments</h3>
        <table id="segment-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Level</th>
              <th>Segment</th>
              <th>State</th>
              <th></th>
            </tr>
          </thead>
          <tbody id="segment-body"></tbody>
        </table>
      </div>

      <!-- Data section -->
      <div class="manage-section">
        <h3>Data</h3>
        <p class="dim">Clear all session history, attempts, and model state. Keeps segments and game config.</p>
        <button id="btn-reset" class="btn-danger">Clear All Data</button>
        <p id="reset-status" class="dim"></p>
      </div>
    </section>
  </main>

  <script type="module" src="/static/app.js?v=20"></script>
</div>
</body>
</html>
```

- [ ] **Step 2: Rewrite style.css**

Replace `python/spinlab/static/style.css` — key changes: 428px width, header layout, game selector popover, mode chip, practice card, draft prompt. Keep all existing styles that are still used (model table, manage sections, segment table, queue/recent lists, etc). Full CSS:

```css
/* SpinLab dark theme, 428px */
:root {
  --bg: #1a1a2e;
  --surface: #16213e;
  --card: #0f3460;
  --text: #e0e0e0;
  --text-dim: #8888aa;
  --accent: #00d2ff;
  --green: #4caf50;
  --yellow: #ffc107;
  --red: #f44336;
  --orange: #ff9800;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  font-family: 'Consolas', 'Monaco', monospace;
  background: #111;
  color: var(--text);
  display: flex;
  justify-content: center;
  min-height: 100vh;
  overflow-x: hidden;
}

#app {
  width: 428px;
  min-height: 100vh;
  background: var(--bg);
  border-left: 1px solid var(--card);
  border-right: 1px solid var(--card);
}

/* Header */
header {
  background: var(--surface);
  padding: 8px 12px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--card);
  position: relative;
}
.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  position: relative;
}
.logo {
  font-size: 16px;
  color: var(--accent);
  font-weight: 700;
}
.game-selector-btn {
  background: none;
  border: 1px solid transparent;
  color: var(--text-dim);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
  padding: 2px 6px;
  border-radius: 4px;
  display: flex;
  align-items: center;
  gap: 4px;
}
.game-selector-btn:hover {
  border-color: var(--card);
  color: var(--text);
}
.caret { font-size: 10px; }

/* Game selector popover */
.popover {
  position: absolute;
  top: 100%;
  left: 0;
  width: 300px;
  max-height: 400px;
  background: var(--surface);
  border: 1px solid var(--card);
  border-radius: 6px;
  z-index: 100;
  box-shadow: 0 8px 24px rgba(0,0,0,0.5);
  overflow: hidden;
}
.popover input {
  width: 100%;
  padding: 8px 10px;
  background: var(--bg);
  color: var(--text);
  border: none;
  border-bottom: 1px solid var(--card);
  font-family: inherit;
  font-size: 12px;
  outline: none;
}
.popover ul {
  list-style: none;
  max-height: 340px;
  overflow-y: auto;
}
.popover li {
  font-size: 11px;
  padding: 6px 10px;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.popover li:hover {
  background: var(--card);
  color: var(--accent);
}

/* Mode chip */
.mode-chip {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  padding: 3px 8px;
  border-radius: 12px;
  background: var(--bg);
  white-space: nowrap;
}
.mode-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.mode-chip.idle .mode-dot { background: var(--text-dim); }
.mode-chip.disconnected .mode-dot { background: var(--text-dim); border: 1px solid var(--text-dim); background: transparent; }
.mode-chip.recording .mode-dot { background: var(--red); }
.mode-chip.practicing .mode-dot { background: var(--green); }
.mode-chip.replaying .mode-dot { background: var(--accent); }
.mode-chip.draft .mode-dot { background: var(--yellow); }
.mode-stop {
  background: none;
  border: none;
  color: var(--text-dim);
  font-size: 14px;
  cursor: pointer;
  padding: 0 2px;
  line-height: 1;
}
.mode-stop:hover { color: var(--red); }

/* Tabs */
nav#tabs {
  display: flex;
  gap: 0;
  border-bottom: 1px solid var(--card);
}
.tab {
  background: none;
  border: none;
  color: var(--text-dim);
  padding: 8px 16px;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  font-size: 14px;
  font-family: inherit;
}
.tab.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}
.tab-content { display: none; }
.tab-content.active { display: block; }

/* Cards */
.card {
  margin: 6px 8px;
  padding: 8px 10px;
  background: var(--card);
  border-radius: 6px;
}
.segment-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.goal-label {
  font-size: 14px;
  font-weight: 600;
}
.insight-card {
  padding: 8px;
  background: var(--surface);
  border-radius: 4px;
  margin-top: 8px;
}

/* Practice footer */
.practice-footer {
  padding: 6px 8px;
  text-align: center;
}

/* Allocator row */
.allocator-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 8px;
}
.allocator-row select, .model-header select {
  background: var(--surface);
  color: var(--text);
  border: 1px solid var(--card);
  padding: 4px 8px;
  border-radius: 4px;
  font-family: inherit;
}

/* Model */
.model-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 8px;
}
#model-table {
  border-collapse: collapse;
  font-size: 11px;
  width: 100%;
}
#model-table th {
  text-align: left;
  color: var(--text-dim);
  padding: 4px 5px;
  border-bottom: 1px solid var(--card);
  white-space: nowrap;
}
#model-table td {
  padding: 4px 5px;
  border-bottom: 1px solid var(--surface);
  white-space: nowrap;
}

/* Drift colors */
.drift-improving { color: var(--green); }
.drift-regressing { color: var(--red); }
.drift-flat { color: var(--text-dim); }

/* Dim text */
.dim { color: var(--text-dim); }

/* Queue and recent */
h3 {
  font-size: 11px;
  text-transform: uppercase;
  color: var(--text-dim);
  letter-spacing: 0.5px;
  margin: 10px 8px 4px;
}
#queue, #recent {
  list-style: none;
  margin: 0 8px;
}
#queue li, #recent li {
  font-size: 11px;
  padding: 5px 8px;
  margin-bottom: 3px;
  background: var(--surface);
  border: 1px solid var(--card);
  border-radius: 4px;
}

/* Result colors */
.ahead { color: var(--green); }
.behind { color: var(--red); }

/* Manage */
.manage-section {
  margin: 8px;
  padding: 12px;
  background: var(--surface);
  border-radius: 6px;
}
.manage-section h3 { margin: 0 0 8px 0; }
.manage-section p { margin-bottom: 8px; font-size: 12px; }

.ref-row {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-bottom: 8px;
}
.ref-row select {
  flex: 1;
  background: var(--surface);
  color: var(--text);
  border: 1px solid var(--card);
  padding: 4px 8px;
  border-radius: 4px;
  font-family: inherit;
  font-size: 12px;
}
.ref-actions {
  display: flex;
  gap: 6px;
}

/* Draft prompt */
.draft-prompt {
  border: 1px solid var(--yellow);
}
.draft-prompt input {
  width: 100%;
  padding: 6px 8px;
  margin: 8px 0;
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--card);
  border-radius: 4px;
  font-family: inherit;
  font-size: 12px;
}
.draft-prompt input:focus {
  border-color: var(--accent);
  outline: none;
}
.draft-actions {
  display: flex;
  gap: 8px;
}

/* Buttons */
.btn-primary {
  background: var(--accent);
  color: #000;
  border: none;
  padding: 6px 14px;
  border-radius: 4px;
  cursor: pointer;
  font-family: inherit;
  font-size: 12px;
  font-weight: 600;
}
.btn-primary:hover { opacity: 0.85; }
.btn-primary:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-danger {
  background: var(--red);
  color: #fff;
  border: none;
  padding: 6px 14px;
  border-radius: 4px;
  cursor: pointer;
  font-family: inherit;
  font-size: 12px;
}
.btn-danger:hover { opacity: 0.85; }
.btn-sm {
  background: var(--card);
  color: var(--text);
  border: 1px solid var(--text-dim);
  padding: 3px 8px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
}
.btn-danger-sm {
  background: var(--red);
  color: #fff;
  border: none;
  padding: 3px 8px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
}

/* Segment table */
#segment-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11px;
}
#segment-table th {
  text-align: left;
  color: var(--text-dim);
  padding: 4px 5px;
  border-bottom: 1px solid var(--card);
}
#segment-table td {
  padding: 4px 5px;
  border-bottom: 1px solid var(--surface);
}
.segment-name-input {
  background: transparent;
  color: var(--text);
  border: 1px solid transparent;
  padding: 2px 4px;
  width: 100%;
  font-family: inherit;
  font-size: 11px;
}
.segment-name-input:focus {
  border-color: var(--accent);
  outline: none;
}
.btn-x {
  background: none;
  border: none;
  color: var(--red);
  cursor: pointer;
  font-size: 12px;
  padding: 2px 4px;
}
.btn-fill-gap {
  background: none;
  border: none;
  cursor: pointer;
  font-size: 12px;
}

/* Toast */
#toast {
  position: fixed;
  bottom: 16px;
  left: 50%;
  transform: translateX(-50%);
  background: #c53030;
  color: #fff;
  padding: 8px 16px;
  border-radius: 4px;
  font-size: 0.85em;
  opacity: 0;
  transition: opacity 0.3s;
  pointer-events: none;
  z-index: 1000;
}
#toast.visible { opacity: 1; }
```

- [ ] **Step 3: Verify page loads (manual or quick check)**

Run: `python -m spinlab dashboard &` (or just verify the HTML is valid by checking no syntax errors). The page won't be functional yet — JS is still old — but the structure should render.

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/static/index.html python/spinlab/static/style.css
git commit -m "feat(ui): restructure HTML to two tabs, 428px width, header status bar"
```

---

## Task 5: Frontend — header.js (Game Selector + Mode Chip)

**Files:**
- Create: `python/spinlab/static/header.js`
- Modify: `python/spinlab/static/app.js`
- Delete: `python/spinlab/static/live.js`

- [ ] **Step 1: Create header.js**

```javascript
import { fetchJSON, postJSON } from './api.js';

let allRoms = [];
let launchedRom = null;
let popoverOpen = false;

export async function loadRomList() {
  const data = await fetchJSON('/api/roms');
  if (data?.roms) allRoms = data.roms;
}

export function updateHeader(data) {
  // Game name
  const gameEl = document.getElementById('game-name');
  const name = data.game_name || localStorage.getItem('spinlab_game_name') || 'No game';
  gameEl.textContent = name;
  if (data.game_name) {
    localStorage.setItem('spinlab_game_name', data.game_name);
    localStorage.setItem('spinlab_game_id', data.game_id);
  }

  // Mode chip
  const chip = document.getElementById('mode-chip');
  const label = document.getElementById('mode-label');
  const stopBtn = document.getElementById('mode-stop');

  chip.className = 'mode-chip';
  stopBtn.style.display = 'none';

  if (!data.tcp_connected) {
    chip.classList.add('disconnected');
    label.textContent = 'Disconnected';
  } else if (data.draft) {
    chip.classList.add('draft');
    label.textContent = 'Draft \u2014 ' + data.draft.segments_captured + ' segments';
  } else if (data.mode === 'reference') {
    chip.classList.add('recording');
    label.textContent = 'Recording \u2014 ' + (data.sections_captured || 0) + ' segments';
    stopBtn.style.display = '';
  } else if (data.mode === 'practice') {
    chip.classList.add('practicing');
    const seg = data.current_segment;
    label.textContent = 'Practicing' + (seg ? ' \u2014 ' + shortSegName(seg) : '');
    stopBtn.style.display = '';
  } else if (data.mode === 'replay') {
    chip.classList.add('replaying');
    label.textContent = 'Replaying\u2026';
    stopBtn.style.display = '';
  } else {
    chip.classList.add('idle');
    label.textContent = 'Idle';
  }
}

function shortSegName(seg) {
  if (seg.description) return seg.description;
  const start = seg.start_type === 'entrance' ? 'ent' : 'cp.' + seg.start_ordinal;
  const end = seg.end_type === 'goal' ? 'goal' : 'cp.' + seg.end_ordinal;
  return 'L' + seg.level_number + ' ' + start + '\u2192' + end;
}

export function initHeader() {
  const selectorBtn = document.getElementById('game-selector');
  const popover = document.getElementById('game-popover');
  const filter = document.getElementById('rom-filter');

  // Restore last game name
  const lastGame = localStorage.getItem('spinlab_game_name');
  if (lastGame) document.getElementById('game-name').textContent = lastGame;

  selectorBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    popoverOpen = !popoverOpen;
    popover.style.display = popoverOpen ? '' : 'none';
    if (popoverOpen) {
      filter.value = '';
      renderRoms('');
      filter.focus();
      if (!allRoms.length) loadRomList().then(() => renderRoms(''));
    }
  });

  filter.addEventListener('input', (e) => renderRoms(e.target.value));

  // Close on click outside or Escape
  document.addEventListener('click', (e) => {
    if (popoverOpen && !popover.contains(e.target) && e.target !== selectorBtn) {
      popoverOpen = false;
      popover.style.display = 'none';
    }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && popoverOpen) {
      popoverOpen = false;
      popover.style.display = 'none';
    }
  });

  // Stop button
  document.getElementById('mode-stop').addEventListener('click', async () => {
    // Determine what to stop based on current chip class
    const chip = document.getElementById('mode-chip');
    if (chip.classList.contains('recording')) await postJSON('/api/reference/stop');
    else if (chip.classList.contains('practicing')) await postJSON('/api/practice/stop');
    else if (chip.classList.contains('replaying')) await postJSON('/api/replay/stop');
  });
}

function renderRoms(filter) {
  const ul = document.getElementById('rom-list');
  ul.innerHTML = '';
  const lf = filter.toLowerCase();
  const matches = allRoms.filter(r => r.toLowerCase().includes(lf));
  matches.forEach(rom => {
    const li = document.createElement('li');
    li.textContent = rom.replace(/\.(sfc|smc|fig|swc)$/i, '');
    li.addEventListener('click', async () => {
      const res = await postJSON('/api/emulator/launch', { rom });
      if (res?.status === 'error') { alert(res.message); return; }
      launchedRom = rom;
      // Close popover
      popoverOpen = false;
      document.getElementById('game-popover').style.display = 'none';
    });
    ul.appendChild(li);
  });
}
```

- [ ] **Step 2: Rewrite app.js**

Replace `python/spinlab/static/app.js`:

```javascript
import { connectSSE, fetchJSON, postJSON } from './api.js';
import { initHeader, updateHeader, loadRomList } from './header.js';
import { updatePracticeCard, fetchModel, initModelTab } from './model.js';
import { fetchManage, initManageTab } from './manage.js';

function updateFromState(data) {
  updateHeader(data);

  // Update practice card visibility and content
  updatePracticeCard(data);

  // Refresh active secondary tab
  const activeTab = document.querySelector('.tab.active');
  if (activeTab?.dataset.tab === 'model') fetchModel();
  if (activeTab?.dataset.tab === 'manage') fetchManage();
}

// Tab switching
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'model') fetchModel();
    if (btn.dataset.tab === 'manage') fetchManage();
  });
});

// Init
initHeader();
initModelTab();
initManageTab();

// Connect SSE with initial poll
connectSSE(updateFromState);
fetchJSON('/api/state').then(data => { if (data) updateFromState(data); });
```

- [ ] **Step 3: Delete live.js**

```bash
rm python/spinlab/static/live.js
```

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/static/header.js python/spinlab/static/app.js
git rm python/spinlab/static/live.js
git commit -m "feat(ui): header.js with game selector + mode chip, remove live.js"
```

---

## Task 6: Frontend — model.js (Practice Card)

**Files:**
- Modify: `python/spinlab/static/model.js`

- [ ] **Step 1: Update model.js to render practice card**

Replace `python/spinlab/static/model.js`:

```javascript
import { segmentName, formatTime, elapsedStr } from './format.js';
import { fetchJSON, postJSON } from './api.js';

export async function fetchModel() {
  const data = await fetchJSON('/api/model');
  if (data) updateModel(data);
}

function updateModel(data) {
  const body = document.getElementById('model-body');
  if (!data.segments || !data.segments.length) {
    body.innerHTML = '<tr><td colspan="7" class="dim">No game loaded</td></tr>';
    return;
  }
  body.innerHTML = '';
  data.segments.forEach(s => {
    const tr = document.createElement('tr');
    const driftClass = s.drift_info?.label || 'flat';
    const arrow = s.drift !== null
      ? (s.drift < 0 ? '\u2193' : s.drift > 0 ? '\u2191' : '\u2192')
      : '\u2014';
    tr.className = 'drift-row-' + driftClass;
    let confCell = '\u2014';
    if (s.drift_info && s.drift_info.ci_lower != null) {
      const lo = s.drift_info.ci_lower.toFixed(2);
      const hi = s.drift_info.ci_upper.toFixed(2);
      confCell = '<span class="dim">[' + lo + ', ' + hi + ']</span>';
    }
    tr.innerHTML =
      '<td>' + segmentName(s) + '</td>' +
      '<td>' + (s.mu !== null ? s.mu.toFixed(1) : '\u2014') + '</td>' +
      '<td class="drift-' + driftClass + '">' + arrow + ' ' +
        (s.drift !== null ? Math.abs(s.drift).toFixed(2) : '\u2014') + '</td>' +
      '<td>' + confCell + '</td>' +
      '<td>' + (s.marginal_return ? s.marginal_return.toFixed(4) : '\u2014') + '</td>' +
      '<td>' + s.n_completed + '</td>' +
      '<td>' + formatTime(s.gold_ms) + '</td>';
    body.appendChild(tr);
  });
  if (data.estimator) {
    document.getElementById('estimator-select').value = data.estimator;
  }
}

export function updatePracticeCard(data) {
  const card = document.getElementById('practice-card');
  if (data.mode !== 'practice' || !data.current_segment) {
    card.style.display = 'none';
    return;
  }
  card.style.display = '';

  const cs = data.current_segment;
  document.getElementById('current-goal').textContent = segmentName(cs);
  document.getElementById('current-attempts').textContent =
    'Attempt ' + (cs.attempt_count || 0);

  const insight = document.getElementById('insight');
  if (cs.drift_info) {
    const arrow = cs.drift_info.drift < 0 ? '\u2193' : cs.drift_info.drift > 0 ? '\u2191' : '\u2192';
    const rate = Math.abs(cs.drift_info.drift).toFixed(2);
    insight.innerHTML =
      '<span class="drift-' + cs.drift_info.label + '">' +
      arrow + ' ' + rate + ' s/run</span>' +
      ' <span class="dim">(' + cs.drift_info.confidence + ')</span>';
  } else {
    insight.textContent = 'No data yet';
  }

  const queue = document.getElementById('queue');
  queue.innerHTML = '';
  (data.queue || []).forEach(q => {
    const li = document.createElement('li');
    li.textContent = segmentName(q);
    queue.appendChild(li);
  });

  const recent = document.getElementById('recent');
  recent.innerHTML = '';
  (data.recent || []).forEach(r => {
    const li = document.createElement('li');
    const time = formatTime(r.time_ms);
    const cls = r.completed ? 'ahead' : 'behind';
    li.innerHTML = '<span class="' + cls + '">' + time + '</span>' +
      ' <span class="dim">' + segmentName(r) + '</span>';
    recent.appendChild(li);
  });

  const stats = document.getElementById('session-stats');
  if (data.session) {
    stats.textContent = (data.session.segments_completed || 0) + '/' +
      (data.session.segments_attempted || 0) + ' cleared | ' +
      elapsedStr(data.session.started_at);
  }

  if (data.allocator) {
    document.getElementById('allocator-select').value = data.allocator;
  }
}

export function initModelTab() {
  document.getElementById('allocator-select').addEventListener('change', async (e) => {
    await postJSON('/api/allocator', { name: e.target.value });
  });
  document.getElementById('estimator-select').addEventListener('change', async (e) => {
    await postJSON('/api/estimator', { name: e.target.value });
    fetchModel();
  });
}
```

- [ ] **Step 2: Commit**

```bash
git add python/spinlab/static/model.js
git commit -m "feat(ui): practice card in Model tab"
```

---

## Task 7: Frontend — manage.js (Reference Controls, Replay, Draft Prompt)

**Files:**
- Modify: `python/spinlab/static/manage.js`

- [ ] **Step 1: Update manage.js with reference controls, replay, and draft prompt**

Replace `python/spinlab/static/manage.js`:

```javascript
import { segmentName, formatTime } from './format.js';
import { fetchJSON, postJSON } from './api.js';

let lastState = null;

export async function fetchManage() {
  const refsData = await fetchJSON('/api/references');
  if (!refsData) return;
  const refs = refsData.references || [];

  const active = refs.find(r => r.active);
  let segments = [];
  if (active) {
    const segmentsData = await fetchJSON('/api/references/' + active.id + '/segments');
    segments = segmentsData?.segments || [];
  }
  updateManage(refs, segments);
}

function updateManage(refs, segments) {
  const sel = document.getElementById('ref-select');
  const btnStart = document.getElementById('btn-ref-start');
  const btnReplay = document.getElementById('btn-replay');
  const draftPrompt = document.getElementById('draft-prompt');

  // Lock controls during active capture/replay or draft pending
  const busy = lastState && (lastState.mode === 'reference' || lastState.mode === 'replay');
  const hasDraft = lastState?.draft != null;

  sel.disabled = busy || hasDraft;
  btnStart.disabled = busy || hasDraft || !lastState?.tcp_connected;
  document.getElementById('btn-ref-rename').disabled = busy || hasDraft;
  document.getElementById('btn-ref-delete').disabled = busy || hasDraft;

  // Draft prompt
  if (hasDraft) {
    draftPrompt.style.display = '';
    document.getElementById('draft-summary').textContent =
      '\u2713 Captured ' + lastState.draft.segments_captured + ' segments';
  } else {
    draftPrompt.style.display = 'none';
  }

  // Populate dropdown
  sel.innerHTML = '';
  if (!refs.length) {
    const opt = document.createElement('option');
    opt.textContent = 'No references';
    opt.disabled = true;
    sel.appendChild(opt);
    document.getElementById('segment-body').innerHTML = '';
    btnReplay.disabled = true;
    return;
  }
  refs.forEach(r => {
    const opt = document.createElement('option');
    opt.value = r.id;
    opt.textContent = r.name + (r.active ? ' \u25cf' : '');
    if (r.active) opt.selected = true;
    sel.appendChild(opt);
  });

  // Replay button — enabled if selected ref has spinrec
  const selectedRef = refs.find(r => r.id === sel.value);
  btnReplay.disabled = busy || hasDraft || !selectedRef?.has_spinrec || !lastState?.tcp_connected;

  // Segments table
  const body = document.getElementById('segment-body');
  body.innerHTML = '';
  segments.forEach(s => {
    const tr = document.createElement('tr');
    const hasState = s.state_path != null;
    const stateCell = hasState
      ? '<span class="state-ok">\u2705</span>'
      : '<button class="btn-fill-gap" data-id="' + s.id + '">\u274c</button>';
    tr.innerHTML =
      '<td><input class="segment-name-input" value="' + (s.description || '') + '" ' +
        'placeholder="' + segmentName(s) + '" ' +
        'data-id="' + s.id + '" data-field="description"></td>' +
      '<td>' + s.level_number + '</td>' +
      '<td>' + (s.start_type === 'entrance' ? 'entrance' : 'cp.' + s.start_ordinal) +
        ' \u2192 ' + (s.end_type === 'goal' ? 'goal' : 'cp.' + s.end_ordinal) + '</td>' +
      '<td>' + stateCell + '</td>' +
      '<td><button class="btn-x" data-id="' + s.id + '">\u2715</button></td>';
    body.appendChild(tr);
  });
}

export function updateManageState(data) {
  lastState = data;
}

export function initManageTab() {
  // Segment name editing (event delegation)
  document.getElementById('segment-body').addEventListener('focusout', async (e) => {
    if (!e.target.classList.contains('segment-name-input')) return;
    const id = e.target.dataset.id;
    const field = e.target.dataset.field;
    const value = e.target.value;
    await fetchJSON('/api/segments/' + id, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [field]: value }),
    });
  });

  // Segment delete and fill-gap (event delegation)
  document.getElementById('segment-body').addEventListener('click', async (e) => {
    if (e.target.classList.contains('btn-fill-gap')) {
      const id = e.target.dataset.id;
      const data = await postJSON('/api/segments/' + id + '/fill-gap');
      if (data?.status === 'started') {
        e.target.textContent = '\u23f3';
        e.target.disabled = true;
      }
      return;
    }
    if (!e.target.classList.contains('btn-x')) return;
    if (!confirm('Remove this segment?')) return;
    await fetchJSON('/api/segments/' + e.target.dataset.id, { method: 'DELETE' });
    fetchManage();
  });

  // Reference dropdown change
  document.getElementById('ref-select').addEventListener('change', async (e) => {
    await postJSON('/api/references/' + e.target.value + '/activate');
    fetchManage();
  });

  // Rename
  document.getElementById('btn-ref-rename').addEventListener('click', async () => {
    const sel = document.getElementById('ref-select');
    const name = prompt('New name:', sel.options[sel.selectedIndex]?.text.replace(' \u25cf', ''));
    if (!name) return;
    await fetchJSON('/api/references/' + sel.value, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    fetchManage();
  });

  // Delete
  document.getElementById('btn-ref-delete').addEventListener('click', async () => {
    if (!confirm('Delete this reference and all its segments?')) return;
    const sel = document.getElementById('ref-select');
    await fetchJSON('/api/references/' + sel.value, { method: 'DELETE' });
    fetchManage();
  });

  // Start reference run
  document.getElementById('btn-ref-start').addEventListener('click', () =>
    postJSON('/api/reference/start'));

  // Replay
  document.getElementById('btn-replay').addEventListener('click', async () => {
    const sel = document.getElementById('ref-select');
    await postJSON('/api/replay/start', { ref_id: sel.value });
  });

  // Draft save
  document.getElementById('btn-draft-save').addEventListener('click', async () => {
    const name = document.getElementById('draft-name').value.trim();
    if (!name) { document.getElementById('draft-name').focus(); return; }
    await postJSON('/api/references/draft/save', { name });
    document.getElementById('draft-name').value = '';
    fetchManage();
  });

  // Draft discard
  document.getElementById('btn-draft-discard').addEventListener('click', async () => {
    if (!confirm('Discard this capture? This cannot be undone.')) return;
    await postJSON('/api/references/draft/discard');
    fetchManage();
  });

  // Reset
  document.getElementById('btn-reset').addEventListener('click', async () => {
    if (!confirm('Clear all session data? This cannot be undone.')) return;
    const data = await postJSON('/api/reset');
    document.getElementById('reset-status').textContent =
      data?.status === 'ok' ? 'Data cleared.' : 'Error clearing data.';
  });
}
```

- [ ] **Step 2: Update app.js to pass state to manage**

In `app.js`, add the `updateManageState` import and call:

```javascript
import { fetchManage, initManageTab, updateManageState } from './manage.js';

function updateFromState(data) {
  updateHeader(data);
  updatePracticeCard(data);
  updateManageState(data);  // <-- add this line

  const activeTab = document.querySelector('.tab.active');
  if (activeTab?.dataset.tab === 'model') fetchModel();
  if (activeTab?.dataset.tab === 'manage') fetchManage();
}
```

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/static/manage.js python/spinlab/static/app.js
git commit -m "feat(ui): manage tab with reference controls, replay, draft save/discard"
```

---

## Task 8: Integration Testing and Fixes

**Files:**
- All modified files
- Tests: `tests/test_dashboard.py`, `tests/test_dashboard_references.py`

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -v`

Fix any failures. Common issues to expect:
- Old tests referencing `set_active_capture_run` being called in `start_reference` — remove those assertions
- Old tests importing from `live.js` — N/A (backend tests only)
- Tests that use the old replay API with `path` instead of `ref_id`

- [ ] **Step 2: Fix any test failures**

Update assertions in existing tests to match new behavior. Key changes:
- `start_reference` now passes `draft=True` to `create_capture_run`
- `start_reference` no longer calls `set_active_capture_run`
- `start_replay` no longer calls `set_active_capture_run`
- Replay endpoint now expects `ref_id` not `path` — update `tests/test_dashboard_references.py` if it has replay endpoint tests
- `list_capture_runs` now returns a `draft` field in each dict — tests doing exact dict matching need updating
- `POST /api/references` endpoint in `dashboard.py` still creates non-draft refs (for manual creation) — verify it passes `draft=False` explicitly
- `stop_reference` and `replay_finished` now enter draft state instead of just clearing — `test_replay.py::TestReplayEvents::test_replay_finished_returns_to_idle` needs to assert `draft_run_id` is set, not just `mode == "idle"`

- [ ] **Step 3: Manual smoke test**

Start the dashboard: `python -m spinlab dashboard`

Verify:
1. Header shows "No game" or last game name
2. Game selector popover opens/closes correctly
3. Model tab shows model table (practice card hidden)
4. Manage tab shows references, start reference button, replay button
5. Mode chip updates on state changes

- [ ] **Step 4: Commit any test fixes**

```bash
git add -A
git commit -m "fix: update existing tests for dashboard restructure"
```

---

## Task Summary

| Task | Description | Dependencies |
|------|-------------|-------------|
| 1 | DB: draft column + hard_delete | None |
| 2 | SessionManager: draft lifecycle | Task 1 |
| 3 | Dashboard API: draft + spinrec endpoints | Task 2 |
| 4 | Frontend: HTML + CSS restructure | None (parallel with 1-3) |
| 5 | Frontend: header.js | Task 4 |
| 6 | Frontend: model.js practice card | Task 4 |
| 7 | Frontend: manage.js reference/replay/draft | Task 4 |
| 8 | Integration testing and fixes | Tasks 1-7 |

Tasks 1-3 (backend) and Task 4 (HTML/CSS) can be done in parallel. Tasks 5-7 depend on Task 4 but are independent of each other. Task 8 is the integration pass.

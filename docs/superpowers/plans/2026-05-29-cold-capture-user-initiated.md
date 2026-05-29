# User-initiated, run-scoped, escapable Cold Capture — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop auto-entering Cold Capture after a reference run; make it a user-initiated, run-scoped action with Skip and Abort escape hatches; add diagnostic-only instrumentation to the death detector.

**Architecture:** Cold Capture (`cold_fill`) loads each missing-cold segment's hot save state and waits for a death+respawn to capture the cold state. We remove the automatic trigger in `SessionManager.finalize_run`/`save_and_finish_run`, add a run-scoped DB query + active-run resolution, expose start/skip/abort routes, surface controls in the frontend, and add a change-triggered trace to `ColdFillSpawnDetector`. The death-detection *fix* is a separate follow-up; this plan only instruments it.

**Tech Stack:** Python 3.11+ (FastAPI, SQLite, pytest), TypeScript (Vite, vitest, happy-dom).

**Spec:** `docs/superpowers/specs/2026-05-29-cold-capture-user-initiated-design.md`

---

## File structure

**Backend (modify):**
- `python/spinlab/db/capture_runs.py` — add `get_active_capture_run`
- `python/spinlab/db/segments.py` — `segments_missing_cold` gains `run_id`
- `python/spinlab/capture/cold_fill.py` — `start(run_id=)`, `skip()`, `abort()`
- `python/spinlab/session_manager.py` — remove auto-trigger; add `skip_cold_fill`/`abort_cold_fill`
- `python/spinlab/routes/system.py` — run-scoped start; new skip/abort routes
- `python/spinlab/api_schemas.py` — `has_active_run` on AppState
- `python/spinlab/state_builder.py` — populate `has_active_run`
- `python/spinlab/retroarch/cold_fill_detector.py` — change-triggered trace

**Frontend (modify):**
- `frontend/index.html` — Start button (Segments tab); Skip/Exit buttons (header)
- `frontend/src/segments-view.ts` — `coldCaptureButtonEnabled` predicate
- `frontend/src/app.ts` — wire Start button + reactive enable
- `frontend/src/header.ts` — wire Skip/Exit + show/hide in cold_fill
- `frontend/src/api-types.ts` — regenerated (do not hand-edit)

**Tests (modify/create):**
- `tests/unit/db/test_db_references.py`, `tests/unit/db/test_db_segments.py`
- `tests/unit/capture/test_cold_fill.py`, `tests/unit/capture/test_cold_fill_integration.py`
- `tests/unit/test_system_route.py`
- `tests/unit/retroarch/test_cold_fill_detector.py`
- `frontend/src/segments-view.test.ts`, `frontend/src/api-contract.test.ts`, `frontend/src/model-logic.test.ts`

---

## Phase 0: Baseline

### Task 0: Confirm green baseline

- [ ] **Step 1: Run the full suite**

Run: `python -m pytest`
Expected: all pass (no failures, no unexpected skips). If red, STOP and report per CLAUDE.md before touching code.

- [ ] **Step 2: Build the frontend (needed for smoke tests)**

Run: `cd frontend && npm run build && npm test`
Expected: build succeeds, vitest green.

---

## Phase 1: DB layer

### Task 1: `get_active_capture_run`

**Files:**
- Modify: `python/spinlab/db/capture_runs.py`
- Test: `tests/unit/db/test_db_references.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/db/test_db_references.py`:

```python
def test_get_active_capture_run_returns_active_id(tmp_path):
    from spinlab.db import Database
    db = Database(tmp_path / "t.db")
    db.upsert_game("g1", "Game", "any%")
    db.create_capture_run("r1", "g1", "Run 1", kind="live")
    db.create_capture_run("r2", "g1", "Run 2", kind="live")
    db.set_active_capture_run("r2")
    assert db.get_active_capture_run("g1") == "r2"


def test_get_active_capture_run_none_when_no_active(tmp_path):
    from spinlab.db import Database
    db = Database(tmp_path / "t.db")
    db.upsert_game("g1", "Game", "any%")
    db.create_capture_run("r1", "g1", "Run 1", kind="live")
    assert db.get_active_capture_run("g1") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/db/test_db_references.py -k get_active_capture_run -v`
Expected: FAIL — `AttributeError: 'Database' object has no attribute 'get_active_capture_run'`.

- [ ] **Step 3: Implement**

In `python/spinlab/db/capture_runs.py`, after `set_active_capture_run` (~line 98):

```python
    def get_active_capture_run(self, game_id: str) -> str | None:
        """Return the id of the active (active=1) capture run for a game, or None."""
        row = self.conn.execute(
            "SELECT id FROM capture_runs WHERE game_id = ? AND active = 1",
            (game_id,),
        ).fetchone()
        return row[0] if row else None
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/db/test_db_references.py -k get_active_capture_run -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db/capture_runs.py tests/unit/db/test_db_references.py
git commit -m "$(cat <<'EOF'
feat(db): get_active_capture_run(game_id)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 2: Run-scope `segments_missing_cold`

**Files:**
- Modify: `python/spinlab/db/segments.py:149-166`
- Test: `tests/unit/db/test_db_segments.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/db/test_db_segments.py` (use existing helpers in that file for waypoints/segments if present; otherwise construct minimally as below):

```python
def test_segments_missing_cold_scoped_by_run(tmp_path):
    from spinlab.db import Database
    from spinlab.models import Segment, WaypointSaveState, Waypoint
    db = Database(tmp_path / "t.db")
    db.upsert_game("g1", "Game", "any%")
    db.create_capture_run("rA", "g1", "A", kind="live")
    db.create_capture_run("rB", "g1", "B", kind="live")

    # Two checkpoint segments, each with a hot but no cold state.
    def mk(seg_id, run_id, wp_id):
        wp = Waypoint(id=wp_id, game_id="g1", level_number=1,
                      kind="checkpoint", ordinal=1, conditions_json="{}")
        db.upsert_waypoint(wp)
        db.upsert_segment(Segment(
            id=seg_id, game_id="g1", level_number=1,
            start_type="checkpoint", start_ordinal=1, end_type="goal", end_ordinal=0,
            start_waypoint_id=wp_id, end_waypoint_id=wp_id, capture_run_id=run_id,
        ))
        db.add_save_state(WaypointSaveState(wp_id, "hot", f"/{seg_id}.state"))

    mk("segA", "rA", "wpA")
    mk("segB", "rB", "wpB")

    all_gaps = {g["segment_id"] for g in db.segments_missing_cold("g1")}
    assert all_gaps == {"segA", "segB"}                       # whole-game
    scoped = {g["segment_id"] for g in db.segments_missing_cold("g1", run_id="rA")}
    assert scoped == {"segA"}                                 # run-scoped
```

(If `Waypoint`/`upsert_waypoint` signatures differ in this codebase, mirror the construction already used elsewhere in `test_db_segments.py`. The assertion behavior is the contract.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/db/test_db_segments.py -k missing_cold_scoped -v`
Expected: FAIL — `segments_missing_cold() got an unexpected keyword argument 'run_id'`.

- [ ] **Step 3: Implement**

Replace `segments_missing_cold` in `python/spinlab/db/segments.py` (lines 149-166):

```python
    def segments_missing_cold(self, game_id: str,
                              run_id: str | None = None) -> list[MissingColdRow]:
        """Return segments whose start waypoint has hot but not cold save state.

        ``run_id`` scopes to segments whose ``capture_run_id`` matches (None =
        whole game). See the design note on capture_run_id overwrite semantics.
        """
        params: list = [game_id]
        run_clause = ""
        if run_id is not None:
            run_clause = "AND s.capture_run_id = ?"
            params.append(run_id)
        rows = self.conn.execute(
            f"""SELECT s.id AS segment_id, hot.state_path AS hot_state_path,
                      s.level_number, s.start_type, s.start_ordinal,
                      s.end_type, s.end_ordinal, s.description
               FROM segments s
               JOIN waypoint_save_states hot
                 ON hot.waypoint_id = s.start_waypoint_id AND hot.variant_type = 'hot'
               LEFT JOIN waypoint_save_states cold
                 ON cold.waypoint_id = s.start_waypoint_id AND cold.variant_type = 'cold'
               WHERE s.game_id = ? AND s.active = 1 {run_clause}
                 AND cold.waypoint_id IS NULL
               ORDER BY s.ordinal, s.level_number, s.start_ordinal""",
            params,
        ).fetchall()
        cols = ["segment_id", "hot_state_path", "level_number",
                "start_type", "start_ordinal", "end_type", "end_ordinal", "description"]
        return [dict(zip(cols, r)) for r in rows]  # type: ignore[return-value]
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/db/test_db_segments.py -k missing_cold -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db/segments.py tests/unit/db/test_db_segments.py
git commit -m "$(cat <<'EOF'
feat(db): segments_missing_cold accepts optional run_id scope

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 2: ColdFillController (start scope, skip, abort)

### Task 3: `ColdFillController.start(run_id=)`

**Files:**
- Modify: `python/spinlab/capture/cold_fill.py:37-50`
- Test: `tests/unit/capture/test_cold_fill.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/capture/test_cold_fill.py` (follow the file's existing fixture style for `db`/`emu`; the assertion is what matters):

```python
async def test_start_passes_run_id_to_query(monkeypatch, cold_fill_controller):
    cf = cold_fill_controller  # ColdFillController with a connected fake emu
    seen = {}
    def fake_missing(game_id, run_id=None):
        seen["game_id"] = game_id
        seen["run_id"] = run_id
        return []  # no gaps → returns early, fine for this assertion
    monkeypatch.setattr(cf.db, "segments_missing_cold", fake_missing)
    await cf.start("g1", run_id="rA")
    assert seen == {"game_id": "g1", "run_id": "rA"}
```

If `test_cold_fill.py` has no shared `cold_fill_controller` fixture, construct one inline with the same fakes the other tests in that file use (a `Database` on tmp_path and the file's fake/mock emu with `is_connected = True`).

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/capture/test_cold_fill.py -k run_id -v`
Expected: FAIL — `start() got an unexpected keyword argument 'run_id'`.

- [ ] **Step 3: Implement**

In `python/spinlab/capture/cold_fill.py`, change the signature and the query call:

```python
    async def start(self, game_id: str, run_id: str | None = None) -> ActionResult:
        """Begin cold-fill for segments missing cold save states.

        ``run_id`` scopes to a single capture run (None = whole game).
        """
        if not self.emu.is_connected:
            logger.info("cold_fill: skipped — backend not connected")
            raise NotConnectedError()
        gaps = self.db.segments_missing_cold(game_id, run_id=run_id)
        if not gaps:
            logger.info("cold_fill: no gaps found — all segments have cold states")
            return ActionResult(status=Status.NO_GAPS)
        self.queue = list(gaps)
        self.total = len(gaps)
        self.current = None
        logger.info("cold_fill: starting — %d segments need cold states", self.total)
        return await self._load_next()
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/capture/test_cold_fill.py -k run_id -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/capture/cold_fill.py tests/unit/capture/test_cold_fill.py
git commit -m "$(cat <<'EOF'
feat(cold-fill): start() threads run_id into segments_missing_cold

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 4: `ColdFillController.skip()` and `abort()`

**Files:**
- Modify: `python/spinlab/capture/cold_fill.py` (after `handle_spawn`, before `clear`)
- Test: `tests/unit/capture/test_cold_fill.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/capture/test_cold_fill.py`. These exercise queue mechanics directly (no emu needed for `abort`; `skip` calls `_load_next` which sends a load command — use the file's fake emu):

```python
async def test_skip_advances_to_next_segment(cold_fill_controller):
    cf = cold_fill_controller
    cf.queue = [
        {"segment_id": "s1", "hot_state_path": "/h1.state"},
        {"segment_id": "s2", "hot_state_path": "/h2.state"},
    ]
    cf.total = 2
    cf.current = "s1"
    result = await cf.skip()
    assert result.new_mode.value == "cold_fill"   # still filling
    assert cf.current == "s2"
    assert [q["segment_id"] for q in cf.queue] == ["s2"]


async def test_skip_last_segment_drains_to_idle(cold_fill_controller):
    cf = cold_fill_controller
    cf.queue = [{"segment_id": "s1", "hot_state_path": "/h1.state"}]
    cf.total = 1
    cf.current = "s1"
    result = await cf.skip()
    assert result.new_mode.value == "idle"
    assert cf.queue == []
    assert cf.current is None


def test_abort_clears_queue(cold_fill_controller):
    cf = cold_fill_controller
    cf.queue = [{"segment_id": "s1", "hot_state_path": "/h1.state"}]
    cf.total = 1
    cf.current = "s1"
    cf.abort()
    assert cf.queue == []
    assert cf.current is None
    assert cf.total == 0
```

Note: `_load_next` requires `hot_state_path` to point at an existing file or it skips the segment. For `test_skip_advances_to_next_segment`, either create real temp files for `/h1.state`/`/h2.state` via `tmp_path` and use those paths, or assert on `cf.queue` length only. Prefer real temp files so `_load_next` loads `s2`.

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/unit/capture/test_cold_fill.py -k "skip or abort" -v`
Expected: FAIL — `'ColdFillController' object has no attribute 'skip'`.

- [ ] **Step 3: Implement**

In `python/spinlab/capture/cold_fill.py`, add after `handle_spawn` (before `clear`):

```python
    async def skip(self) -> ActionResult:
        """Abandon the current segment without capturing; advance the queue.

        Used when the user can't reproduce a death/respawn for this segment.
        Draining the queue returns a STOPPED/IDLE result (no power-cycle —
        Skip is in the give-up family, unlike a captured completion).
        """
        if not self.queue:
            self.current = None
            self.cold_waypoint_id = None
            return ActionResult(status=Status.STOPPED, new_mode=Mode.IDLE)
        skipped = self.queue.pop(0)
        self._save_state_attempts.pop(skipped["segment_id"], None)
        logger.info("cold_fill: skipped segment=%s", skipped["segment_id"])
        if not self.queue:
            self.current = None
            self.cold_waypoint_id = None
            logger.info("cold_fill: queue drained after skip — done")
            return ActionResult(status=Status.STOPPED, new_mode=Mode.IDLE)
        return await self._load_next()

    def abort(self) -> None:
        """Abandon the whole queue. Caller resets mode → IDLE."""
        logger.info("cold_fill: aborted with %d segment(s) remaining", len(self.queue))
        self.clear()
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/unit/capture/test_cold_fill.py -k "skip or abort" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/capture/cold_fill.py tests/unit/capture/test_cold_fill.py
git commit -m "$(cat <<'EOF'
feat(cold-fill): skip() and abort() queue controls

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 3: SessionManager (remove auto-trigger; skip/abort wrappers)

### Task 5: Remove auto-trigger; add `skip_cold_fill`/`abort_cold_fill`

**Files:**
- Modify: `python/spinlab/session_manager.py:434-454`
- Test: `tests/unit/capture/test_cold_fill_integration.py` (rewrite `test_full_cycle`)

- [ ] **Step 1: Rewrite the integration test (red)**

Replace `test_full_cycle` in `tests/unit/capture/test_cold_fill_integration.py` (lines 104-141). The fixture builds run "run1" with cp1/cp2 hot-only; segments carry `capture_run_id="run1"`. New behavior: finalize → IDLE; then start cold-fill explicitly for the active run.

```python
    async def test_full_cycle(self, sm, db, emu, tmp_path):
        sm.game_id = "g1"
        db.create_capture_run("run1", "g1", "Test Run", kind="live")
        segs, wp_cp1, wp_cp2 = _create_segments_with_hot_only(db, tmp_path=tmp_path)
        sm.capture.paused_run_id = "run1"

        # Finalize no longer auto-enters cold-fill.
        result = await sm.finalize_run("Test Run")
        assert result.status == Status.OK
        assert sm.mode == Mode.IDLE

        # User starts cold-fill for the active run.
        db.set_active_capture_run("run1")
        start = await sm.cold_fill.start("g1", run_id="run1")
        if start.new_mode == Mode.COLD_FILL:
            sm.mode = Mode.COLD_FILL
        assert sm.mode == Mode.COLD_FILL

        cmd = emu.send_command.call_args[0][0]
        assert isinstance(cmd, ColdFillLoadCmd)
        assert cmd.state_path == str(tmp_path / "hot1.mss")
        assert cmd.segment_id == segs[1].id

        await sm.route_event(SpawnEvent(state_path="/cold1.mss"))
        assert sm.mode == Mode.COLD_FILL
        assert db.get_save_state(wp_cp1.id, "cold").state_path == "/cold1.mss"

        await sm.route_event(SpawnEvent(state_path="/cold2.mss"))
        assert sm.mode == Mode.IDLE
        assert db.get_save_state(wp_cp2.id, "cold").state_path == "/cold2.mss"
        assert db.segments_missing_cold("g1", run_id="run1") == []
```

Add a focused regression test in the same file:

```python
    async def test_finalize_does_not_auto_enter_cold_fill(self, sm, db, emu, tmp_path):
        sm.game_id = "g1"
        db.create_capture_run("run1", "g1", "Test Run", kind="live")
        _create_segments_with_hot_only(db, tmp_path=tmp_path)
        sm.capture.paused_run_id = "run1"
        await sm.finalize_run("Test Run")
        assert sm.mode == Mode.IDLE
        emu.send_command.assert_not_called()  # no ColdFillLoadCmd fired
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/capture/test_cold_fill_integration.py -v`
Expected: FAIL — current `finalize_run` still sets `mode == COLD_FILL`, so `test_finalize_does_not_auto_enter_cold_fill` fails (and the rewritten `test_full_cycle` may fail on `emu.send_command` already consumed by the auto-trigger).

- [ ] **Step 3: Implement — remove the auto-trigger**

In `python/spinlab/session_manager.py`, replace `finalize_run` and `save_and_finish_run` (lines 434-454):

```python
    async def finalize_run(self, name: str) -> ActionResult:
        scheduler = self.get_scheduler() if self.game_id else None
        result = await self.capture.finalize_run(name, scheduler=scheduler)
        await self._notify_sse()
        return result

    async def save_and_finish_run(self, name: str) -> ActionResult:
        scheduler = self.get_scheduler() if self.game_id else None
        result = await self.capture.save_and_finish_run(self.mode, name, scheduler=scheduler)
        if result.new_mode is not None:
            self.mode = result.new_mode
        await self._notify_sse()
        return result
```

- [ ] **Step 4: Add skip/abort wrappers**

In `python/spinlab/session_manager.py`, add near the other cold-fill-adjacent methods (e.g., after `start_fill_gap`, ~line 432):

```python
    async def skip_cold_fill(self) -> ActionResult:
        result = await self.cold_fill.skip()
        # Drain → IDLE. NO_GAPS covers the case where _load_next emptied the
        # queue because every remaining segment's hot state file was missing
        # (new_mode is None there), so don't gate solely on new_mode == IDLE.
        if result.new_mode == Mode.IDLE or result.status == Status.NO_GAPS:
            self.mode = Mode.IDLE
        await self._notify_sse()
        return result

    async def abort_cold_fill(self) -> ActionResult:
        self.cold_fill.abort()
        self.mode = Mode.IDLE
        await self._notify_sse()
        return ActionResult(status=Status.STOPPED, new_mode=Mode.IDLE)
```

Ensure `ActionResult`, `Status`, `Mode` are imported in `session_manager.py` (they already are — verify the import line near the top).

- [ ] **Step 5: Run to verify it passes**

Run: `python -m pytest tests/unit/capture/test_cold_fill_integration.py -v`
Expected: PASS.

- [ ] **Step 6: Run the broader capture + session suites to catch fallout**

Run: `python -m pytest tests/unit/capture tests/unit/test_session_manager.py -q` (drop the second path if that file doesn't exist)
Expected: PASS. If any other test asserted auto-trigger, update it to the new IDLE-after-finalize behavior.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/session_manager.py tests/unit/capture/test_cold_fill_integration.py
git commit -m "$(cat <<'EOF'
feat(session): stop auto-entering cold-fill; add skip/abort wrappers

Finalizing a reference run now returns to IDLE. Cold capture is started
explicitly (run-scoped) and can be skipped per-segment or aborted.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 4: Routes

### Task 6: Run-scoped `/cold-fill/start`

**Files:**
- Modify: `python/spinlab/routes/system.py:68-81`
- Test: `tests/unit/test_system_route.py:117-156`

- [ ] **Step 1: Update existing tests + add no-active-run test (red)**

In `tests/unit/test_system_route.py`, the happy-path tests now need an active run. Add a helper near the top of `TestColdFillStart` and use it:

```python
    @staticmethod
    def _with_active_run(client):
        db = client.app.state.db
        db.create_capture_run("r1", GAME_ID, "R1", kind="live")
        db.set_active_capture_run("r1")
```

Update `test_503_when_not_connected`, `test_success_returns_ok_when_cold_fill_started`, and `test_success_returns_no_gaps_when_nothing_to_fill` to call `self._with_active_run(client)` after `_make_client(...)`. (If `client.app.state.db` is not the accessor used elsewhere, use the same attribute the app exposes the Database under — check `create_app`.)

Add a new test:

```python
    def test_400_when_no_active_run(self, db, tmp_path):
        client = _make_client(db, tmp_path=tmp_path)
        # game loaded, mode IDLE, but no active capture run
        resp = client.post("/api/cold-fill/start")
        assert resp.status_code == 400
        assert "active" in resp.json()["detail"].lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/test_system_route.py -k ColdFillStart -v`
Expected: FAIL — `test_400_when_no_active_run` currently 200/503; happy-path tests may now 400 (no active run) before reaching the mock.

- [ ] **Step 3: Implement**

Replace `start_cold_fill` in `python/spinlab/routes/system.py` (lines 68-81):

```python
@router.post("/cold-fill/start", response_model=OkResponse)
async def start_cold_fill(
    session: SessionManager = Depends(get_session),
    db: Database = Depends(get_db),
):
    if not session.game_id:
        raise HTTPException(status_code=400, detail="No game loaded")
    if session.mode != Mode.IDLE:
        raise HTTPException(status_code=409, detail=f"Cannot start cold fill: mode is {session.mode.value}")
    run_id = db.get_active_capture_run(session.game_id)
    if run_id is None:
        raise HTTPException(status_code=400, detail="No active reference run — select one in Manage first")
    try:
        result = await session.cold_fill.start(session.game_id, run_id=run_id)
    except NotConnectedError:
        raise HTTPException(status_code=503, detail="Emulator not connected")
    if result.new_mode == Mode.COLD_FILL:
        session.mode = Mode.COLD_FILL
    await session._notify_sse()
    return {"status": "ok" if result.status != Status.NO_GAPS else "no_gaps"}
```

(`Database` and `get_db` are already imported in `system.py`.)

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/test_system_route.py -k ColdFillStart -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/routes/system.py tests/unit/test_system_route.py
git commit -m "$(cat <<'EOF'
feat(api): /cold-fill/start scopes to the active run (400 if none)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 7: `/cold-fill/skip` and `/cold-fill/abort`

**Files:**
- Modify: `python/spinlab/routes/system.py` (after `start_cold_fill`)
- Test: `tests/unit/test_system_route.py`

- [ ] **Step 1: Write failing tests**

Add a `TestColdFillSkipAbort` class to `tests/unit/test_system_route.py`:

```python
class TestColdFillSkipAbort:
    def test_skip_409_when_not_cold_fill(self, db, tmp_path):
        client = _make_client(db, tmp_path=tmp_path)
        client.app.state.session.mode = Mode.IDLE
        assert client.post("/api/cold-fill/skip").status_code == 409

    def test_abort_409_when_not_cold_fill(self, db, tmp_path):
        client = _make_client(db, tmp_path=tmp_path)
        client.app.state.session.mode = Mode.IDLE
        assert client.post("/api/cold-fill/abort").status_code == 409

    def test_skip_calls_session_and_returns_status(self, db, tmp_path):
        client = _make_client(db, tmp_path=tmp_path)
        s = client.app.state.session
        s.mode = Mode.COLD_FILL
        s.skip_cold_fill = AsyncMock(
            return_value=ActionResult(status=Status.STARTED, new_mode=Mode.COLD_FILL))
        resp = client.post("/api/cold-fill/skip")
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"

    def test_abort_returns_stopped(self, db, tmp_path):
        client = _make_client(db, tmp_path=tmp_path)
        s = client.app.state.session
        s.mode = Mode.COLD_FILL
        s.abort_cold_fill = AsyncMock(
            return_value=ActionResult(status=Status.STOPPED, new_mode=Mode.IDLE))
        resp = client.post("/api/cold-fill/abort")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/unit/test_system_route.py -k SkipAbort -v`
Expected: FAIL — routes return 404 (not registered).

- [ ] **Step 3: Implement**

In `python/spinlab/routes/system.py`, after `start_cold_fill`:

```python
@router.post("/cold-fill/skip", response_model=OkResponse)
async def skip_cold_fill(session: SessionManager = Depends(get_session)):
    if session.mode != Mode.COLD_FILL:
        raise HTTPException(status_code=409, detail=f"Not in cold fill: mode is {session.mode.value}")
    result = await session.skip_cold_fill()
    return {"status": result.status.value}


@router.post("/cold-fill/abort", response_model=OkResponse)
async def abort_cold_fill(session: SessionManager = Depends(get_session)):
    if session.mode != Mode.COLD_FILL:
        raise HTTPException(status_code=409, detail=f"Not in cold fill: mode is {session.mode.value}")
    result = await session.abort_cold_fill()
    return {"status": result.status.value}
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/unit/test_system_route.py -k SkipAbort -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/routes/system.py tests/unit/test_system_route.py
git commit -m "$(cat <<'EOF'
feat(api): /cold-fill/skip and /cold-fill/abort

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 5: AppState `has_active_run`

### Task 8: Add `has_active_run` to AppState + builder

**Files:**
- Modify: `python/spinlab/api_schemas.py:114-135`, `python/spinlab/state_builder.py:54-69`
- Test: `tests/unit/` (state builder test — see Step 1)

- [ ] **Step 1: Write the failing test**

Find the existing state-builder test (search: `grep -rl "StateBuilder\|build(" tests/unit | head`). Add a test asserting the flag. If no dedicated file exists, add `tests/unit/test_state_builder.py`:

```python
def test_has_active_run_reflects_db(tmp_path):
    from spinlab.db import Database
    from spinlab.state_builder import StateBuilder
    from spinlab.session_manager import SessionManager  # or the minimal session the other tests use
    db = Database(tmp_path / "t.db")
    db.upsert_game("g1", "Game", "any%")
    # Build a session pointed at g1 the same way neighboring tests do.
    session = _make_minimal_session(db, game_id="g1")  # reuse helper if present
    sb = StateBuilder(db)

    assert sb.build(session)["has_active_run"] is False
    db.create_capture_run("r1", "g1", "R1", kind="live")
    db.set_active_capture_run("r1")
    assert sb.build(session)["has_active_run"] is True
```

If constructing a `SessionManager` in a unit test is heavy, instead assert via the route: extend `tests/unit/test_system_route.py` with a GET `/api/state` check that `has_active_run` toggles after `set_active_capture_run`. Pick whichever matches existing test ergonomics; the contract is the same.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest -k has_active_run -v`
Expected: FAIL — `KeyError: 'has_active_run'` (builder) or pydantic validation (schema).

- [ ] **Step 3: Implement — schema**

In `python/spinlab/api_schemas.py`, add to `AppState` (after `cold_fill`):

```python
    cold_fill: ColdFillState | None
    has_active_run: bool
```

- [ ] **Step 4: Implement — builder**

In `python/spinlab/state_builder.py`, add to the `base` dict (after `"cold_fill": None,`):

```python
            "cold_fill": None,
            "has_active_run": (
                game_id is not None
                and self.db.get_active_capture_run(game_id) is not None
            ),
```

(`game_id` is read at the top of `build`; this is safe when `game_id is None` because the `and` short-circuits.)

- [ ] **Step 5: Run to verify it passes**

Run: `python -m pytest -k has_active_run -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/api_schemas.py python/spinlab/state_builder.py tests/unit/
git commit -m "$(cat <<'EOF'
feat(api): has_active_run flag on AppState

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 6: Frontend

### Task 9: Start Cold Capture button (Segments tab)

**Files:**
- Modify: `frontend/index.html:180-182`, `frontend/src/segments-view.ts`, `frontend/src/app.ts`
- Test: `frontend/src/segments-view.test.ts`

- [ ] **Step 1: Write the failing test (predicate)**

Add to `frontend/src/segments-view.test.ts`:

```ts
import { coldCaptureButtonEnabled } from "./segments-view";

describe("coldCaptureButtonEnabled", () => {
  it("enabled only when idle and an active run exists", () => {
    expect(coldCaptureButtonEnabled("idle", true)).toBe(true);
    expect(coldCaptureButtonEnabled("idle", false)).toBe(false);
    expect(coldCaptureButtonEnabled("cold_fill", true)).toBe(false);
    expect(coldCaptureButtonEnabled("reference", true)).toBe(false);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npm test -- segments-view`
Expected: FAIL — `coldCaptureButtonEnabled` is not exported.

- [ ] **Step 3: Implement — predicate**

Add to `frontend/src/segments-view.ts`:

```ts
export function coldCaptureButtonEnabled(mode: string, hasActiveRun: boolean): boolean {
  return mode === "idle" && hasActiveRun;
}
```

- [ ] **Step 4: Add the button to the Segments tab HTML**

In `frontend/index.html`, replace the Segments tab section (lines 180-182):

```html
    <!-- Segments Tab -->
    <section id="tab-segments" class="tab-content">
      <div id="segments-toolbar">
        <button id="btn-start-cold-fill" disabled
                title="Select a reference run in Manage first">Start Cold Capture</button>
      </div>
      <div id="segments-view-container"></div>
    </section>
```

- [ ] **Step 5: Wire the button in app.ts**

In `frontend/src/app.ts`:

Change the api import to include `postJSON`:
```ts
import { connectSSE, fetchJSON, formatClientError, postJSON } from "./api";
```
(Confirm `postJSON` is exported from `./api` — it is.)

Import the predicate:
```ts
import { fetchSegments, renderSegmentsView, coldCaptureButtonEnabled } from "./segments-view";
```

Add a reactive updater and call it from `updateFromState` (after `updateManageState(data);`):
```ts
  updateColdCaptureButton(data);
```

```ts
function updateColdCaptureButton(data: AppState): void {
  const btn = document.getElementById("btn-start-cold-fill") as HTMLButtonElement | null;
  if (!btn) return;
  btn.disabled = !coldCaptureButtonEnabled(data.mode, data.has_active_run);
  btn.title = data.has_active_run
    ? "Capture cold states for the active run"
    : "Select a reference run in Manage first";
}
```

Wire the click once at startup (near the other top-level init calls, after `initManageTab();`):
```ts
document.getElementById("btn-start-cold-fill")?.addEventListener("click", async () => {
  const res = await postJSON<{ status?: string }>("/api/cold-fill/start");
  if (res?.status === "no_gaps") {
    // api.ts shows server errors as toasts; surface the benign case inline.
    alert("No missing cold states for the active run.");
  }
});
```

(`data.has_active_run` requires `api-types.ts` regeneration — Task 11. Until then `npm run typecheck` will error on the new field; that is expected and resolved in Task 11.)

- [ ] **Step 6: Run the predicate test**

Run: `cd frontend && npm test -- segments-view`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add frontend/index.html frontend/src/segments-view.ts frontend/src/app.ts frontend/src/segments-view.test.ts
git commit -m "$(cat <<'EOF'
feat(frontend): Start Cold Capture button on Segments tab

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 10: Skip + Exit controls (header)

**Files:**
- Modify: `frontend/index.html:24-28`, `frontend/src/header.ts`
- Test: `frontend/src/header.test.ts` (new)

- [ ] **Step 1: Write the failing test**

Create `frontend/src/header.test.ts`. Mirror the happy-dom setup used in `manage.test.ts` (it sets `document.body.innerHTML` then imports the module). Test that the Skip/Exit buttons are visible only in cold_fill mode:

```ts
import { beforeEach, describe, expect, it } from "vitest";
import { updateHeader } from "./header";
import type { AppState } from "./types";

function baseState(over: Partial<AppState>): AppState {
  return {
    mode: "idle", emu_connected: true, game_id: "g", game_name: "G",
    current_segment: null, recent: [], session: null, sections_captured: null,
    allocator_weights: null, estimator: null, capture_run_id: null,
    replay: null, paused_run: null, cold_fill: null, has_active_run: false,
    ...over,
  } as AppState;
}

beforeEach(() => {
  document.body.innerHTML = `
    <span id="game-name"></span>
    <div id="mode-chip"><span id="mode-label"></span>
      <button id="mode-stop" style="display:none"></button>
      <button id="cold-fill-skip" style="display:none"></button>
      <button id="cold-fill-exit" style="display:none"></button>
    </div>`;
});

describe("cold-fill header controls", () => {
  it("shows skip+exit only in cold_fill", () => {
    updateHeader(baseState({ mode: "cold_fill", cold_fill: { current: 1, total: 2, segment_label: "L1" } }));
    expect((document.getElementById("cold-fill-skip") as HTMLElement).style.display).toBe("");
    expect((document.getElementById("cold-fill-exit") as HTMLElement).style.display).toBe("");
    updateHeader(baseState({ mode: "idle" }));
    expect((document.getElementById("cold-fill-skip") as HTMLElement).style.display).toBe("none");
    expect((document.getElementById("cold-fill-exit") as HTMLElement).style.display).toBe("none");
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npm test -- header`
Expected: FAIL — buttons stay hidden (updateHeader doesn't manage them).

- [ ] **Step 3: Add the buttons to the header HTML**

In `frontend/index.html`, replace lines 24-28:

```html
    <div id="mode-chip" class="mode-chip idle">
      <span class="mode-dot"></span>
      <span id="mode-label">Idle</span>
      <button id="mode-stop" class="mode-stop" style="display:none" title="Stop">&times;</button>
      <button id="cold-fill-skip" class="mode-stop" style="display:none" title="Skip this segment">Skip</button>
      <button id="cold-fill-exit" class="mode-stop" style="display:none" title="Exit cold capture">&times;</button>
    </div>
```

- [ ] **Step 4: Implement — show/hide in updateHeader + wire in initHeader**

In `frontend/src/header.ts`, in `updateHeader`, grab the buttons near `stopBtn` and default-hide them:

```ts
  const stopBtn = document.getElementById("mode-stop") as HTMLElement;
  const skipBtn = document.getElementById("cold-fill-skip") as HTMLElement;
  const exitBtn = document.getElementById("cold-fill-exit") as HTMLElement;

  chip.className = "mode-chip";
  stopBtn.style.display = "none";
  skipBtn.style.display = "none";
  exitBtn.style.display = "none";
```

In the `cold_fill` branch, show them:
```ts
  } else if (data.mode === "cold_fill" && data.cold_fill) {
    chip.classList.add("recording");
    label.textContent =
      "Cold starts — " + data.cold_fill.current + "/" + data.cold_fill.total;
    skipBtn.style.display = "";
    exitBtn.style.display = "";
  } else if (data.mode === "fill_gap") {
```

In `initHeader`, wire clicks (after the existing `stopBtn` handler):
```ts
  document.getElementById("cold-fill-skip")?.addEventListener("click", async () => {
    await postJSON("/api/cold-fill/skip");
  });
  document.getElementById("cold-fill-exit")?.addEventListener("click", async () => {
    await postJSON("/api/cold-fill/abort");
  });
```

(`postJSON` is already imported in `header.ts`.)

- [ ] **Step 5: Run to verify it passes**

Run: `cd frontend && npm test -- header`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/index.html frontend/src/header.ts frontend/src/header.test.ts
git commit -m "$(cat <<'EOF'
feat(frontend): Skip + Exit controls in header during cold capture

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 11: Regenerate types + fix AppState fixtures

**Files:**
- Regenerate: `frontend/src/api-types.ts`
- Modify: `frontend/src/api-contract.test.ts:28,85`, `frontend/src/model-logic.test.ts` (3 fixtures)

- [ ] **Step 1: Regenerate types from the live schema**

Run: `cd frontend && npm run gen-types`
Expected: `frontend/src/api-types.ts` now includes `has_active_run: boolean` in the AppState schema, plus the three `/api/cold-fill/*` operations.

- [ ] **Step 2: Add `has_active_run` to every AppState test fixture (red)**

Run: `cd frontend && npm run typecheck`
Expected: errors in `api-contract.test.ts` and `model-logic.test.ts` — fixtures missing `has_active_run`.

Add `has_active_run: false,` next to each `cold_fill: null,` (and `capture_run_id: null,`) line in:
- `frontend/src/api-contract.test.ts` (2 occurrences)
- `frontend/src/model-logic.test.ts` (3 occurrences)

- [ ] **Step 3: Typecheck + full frontend test**

Run: `cd frontend && npm run typecheck && npm test`
Expected: typecheck clean, all vitest green.

- [ ] **Step 4: Build (regenerates static/ for the backend smoke tests)**

Run: `cd frontend && npm run build`
Expected: build succeeds.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api-types.ts frontend/src/api-contract.test.ts frontend/src/model-logic.test.ts frontend/openapi.json
git commit -m "$(cat <<'EOF'
chore(frontend): regen api-types for cold-fill routes + has_active_run

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 7: Detector instrumentation (diagnostic only)

### Task 12: Change-triggered trace in ColdFillSpawnDetector

**Files:**
- Modify: `python/spinlab/retroarch/cold_fill_detector.py`
- Test: `tests/unit/retroarch/test_cold_fill_detector.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/retroarch/test_cold_fill_detector.py` (mirror how that file builds `MemorySnapshot` fixtures):

```python
def test_trace_logs_on_change_and_is_silent_on_repeat(caplog):
    import logging
    from spinlab.retroarch.cold_fill_detector import ColdFillSpawnDetector
    det = ColdFillSpawnDetector()
    det.activate("seg1")
    snap = _snap(player_anim=0, exit_mode=0, level_start=1)  # use file's snapshot helper
    with caplog.at_level(logging.INFO, logger="spinlab.retroarch.cold_fill_detector"):
        det.step(snap, timestamp_ms=0)
        first = [r for r in caplog.records if "trace" in r.getMessage()]
        det.step(snap, timestamp_ms=16)  # identical signal → no new trace line
        second = [r for r in caplog.records if "trace" in r.getMessage()]
    assert len(first) == 1
    assert len(second) == 1  # unchanged → no additional trace
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/retroarch/test_cold_fill_detector.py -k trace -v`
Expected: FAIL — no trace line emitted.

- [ ] **Step 3: Implement**

In `python/spinlab/retroarch/cold_fill_detector.py`:

Add `self._prev_trace_sig = None` to both `__init__` and `activate` (so a fresh activation re-emits the first line):

```python
    def __init__(self) -> None:
        self._active = False
        self._waiting_spawn = False
        self._segment_id: str | None = None
        self._prev_anim = 0
        self._prev_level_start = 0
        self._prev_exit_mode = 0
        self._prev_trace_sig: tuple | None = None
```

```python
    def activate(self, segment_id: str) -> None:
        self._active = True
        self._waiting_spawn = False
        self._segment_id = segment_id
        self._prev_anim = 0
        self._prev_level_start = 0
        self._prev_exit_mode = 0
        self._prev_trace_sig = None
        logger.info("cold_fill_detector: activated for segment=%s", segment_id)
```

At the very top of `step`, right after the `if not self._active: return None` guard:

```python
    def step(self, curr: MemorySnapshot, timestamp_ms: int) -> SpawnEvent | None:
        if not self._active:
            return None

        # Diagnostic-only trace: emit one compact line whenever the raw death
        # signals change, so a failed-capture reproduction shows exactly which
        # signal the detector did (not) see. No effect on detection logic.
        sig = (curr.player_anim, curr.exit_mode, curr.level_start,
               curr.fanfare, curr.io_port, self._waiting_spawn)
        if sig != self._prev_trace_sig:
            log.info(
                logger, "cold_fill_detector: trace",
                segment_id=self._segment_id, player_anim=curr.player_anim,
                exit_mode=curr.exit_mode, level_start=curr.level_start,
                fanfare=curr.fanfare, io_port=curr.io_port,
                waiting_spawn=self._waiting_spawn, ts=timestamp_ms,
            )
            self._prev_trace_sig = sig

        emitted: SpawnEvent | None = None
        # ... existing logic unchanged ...
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/retroarch/test_cold_fill_detector.py -v`
Expected: PASS (new test + existing tests still green — detection logic untouched).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/cold_fill_detector.py tests/unit/retroarch/test_cold_fill_detector.py
git commit -m "$(cat <<'EOF'
feat(cold-fill): change-triggered diagnostic trace in spawn detector

Diagnostic-only; no change to detection logic. Surfaces the raw death
signals each frame they change, so the next failed-capture repro is
conclusive (see spec: death-detection fix is a follow-up).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 8: Full verification

### Task 13: Full suite + static analysis

- [ ] **Step 1: Build the frontend (static/ must be current for smoke tests)**

Run: `cd frontend && npm run build`
Expected: success.

- [ ] **Step 2: Run the FULL Python suite (unit + emulator + frontend smoke)**

Run: `python -m pytest`
Expected: all pass. Emulator tests must actually run (no `ra_harness launch failed` skips). If any emulator test skips, surface it — do not treat as green (CLAUDE.md).

- [ ] **Step 3: Frontend tests + typecheck**

Run: `cd frontend && npm test && npm run typecheck`
Expected: green, clean.

- [ ] **Step 4: Static analysis on changed Python**

Run: `npx pyright python/spinlab/db/segments.py python/spinlab/capture/cold_fill.py python/spinlab/session_manager.py python/spinlab/routes/system.py python/spinlab/state_builder.py python/spinlab/api_schemas.py python/spinlab/retroarch/cold_fill_detector.py`
Run: `ruff check python/spinlab`
Expected: no new errors introduced.

- [ ] **Step 5: Manual smoke (optional but recommended)**

Restart the dashboard, finalize a short reference run → confirm it returns to IDLE (no auto cold-capture). On the Segments tab, with the run active, click **Start Cold Capture** → confirm it queues only that run's missing-cold segments. In cold capture, confirm **Skip** advances and **✕** exits to IDLE.

- [ ] **Step 6: Final commit (if anything uncommitted)**

```bash
git add -A
git commit -m "$(cat <<'EOF'
chore: finalize user-initiated cold-capture rework

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-review notes (for the implementer)

- **`client.app.state.db` accessor** (Task 6): verify the attribute the app stores the `Database` under in `create_app` — adjust the test helper if it differs.
- **Snapshot/Waypoint constructors** (Tasks 2, 12): mirror the construction already used in the neighboring test files rather than the illustrative signatures here, if they differ.
- **Skip-drain vs natural completion**: skip-to-empty returns to IDLE *without* a `ResetCmd` (give-up family); the captured-completion path in `_handle_spawn` keeps its existing power-cycle. Do not "unify" them.
- **Detector trace**: this is the ONLY detector change. The death-detection *fix* is a separate task driven by the trace output.

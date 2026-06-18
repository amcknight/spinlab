# Science Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Science" record-toggle to practice/grind so strat-hunting/over-optimizing attempts are written but flagged `experimental` — excluded from the expected-time/reliability model, yet still counted for floor/PB (best clean time).

**Architecture:** New `experimental` column on `attempts` (mirrors the existing `invalidated` flag). The live `PracticeSession` carries an `experimental` toggle and stamps it onto each event row at write time. The two estimator-ingestion seams (`attempts_from_rows`, `events_from_rows`) exclude experimental rows — exactly like the model excludes `invalidated`. Floor/gold paths (`compute_golds`, `floor_series_at`) only filter `invalidated`, so experimental rows flow through and set PBs **with no change**. A UI toggle + route-bar badge + R-menu verb drive it.

**Tech Stack:** Python (SQLite migrations, dataclasses), FastAPI, TypeScript/Vite frontend.

---

## Load-bearing decisions (Andrew confirmed the semantics 2026-06-18)

- **Exclude from distribution, include in floor.** Experimental attempts do NOT feed the per-segment expected-time/hazard/reliability estimator, but a fast experimental clean DOES set a new gold/floor (achievability is real even if effort wasn't max). Realized by: add exclusion to the two estimator seams; leave `compute_golds`/`floor_series_at` untouched (they only filter `invalidated`).
- **Flag, don't drop.** Rows persist (nothing lost), flagged `experimental` — composes with the existing `invalidated` infra and honors "highlight, don't auto-drop."
- **Toggle on the session, not a new Mode.** `PracticeSession.experimental` flips mid-session; applies to Grind and normal practice (HyperPlay is a follow-up — it has its own timing class).
- **R-menu button (Task 7): DECIDED 2026-06-18 = R+A** (`A` = `$17`/HELD2, bit `0x80`). Andrew's call ("R+A for now"). Keeps Select free for the speculative future modifier.

---

## Task 1: Add `experimental` column (migration + DB read/write)

**Files:**
- Create: `python/spinlab/db/migrations/0008_attempt_experimental.sql`
- Modify: `python/spinlab/db/attempts.py` (TypedDicts ~38-82; `log_event_attempt` ~233-249; episode roll-up ~126)
- Test: `tests/unit/db/test_attempts.py` (or the nearest existing attempts DB test)

- [ ] **Step 1: Write the migration**

```sql
-- 0008_attempt_experimental.sql
-- "Science"/strat-hunting attempts: written + counted for floor/PB, but
-- excluded from the expected-time/reliability model (effort wasn't max).
-- Mirrors `invalidated` but with the OPPOSITE floor behavior.
ALTER TABLE attempts ADD COLUMN experimental INTEGER NOT NULL DEFAULT 0;
```

- [ ] **Step 2: Failing test — experimental round-trips through the event row**

```python
def test_log_event_attempt_persists_experimental(tmp_path):
    db = Database(tmp_path / "t.db")
    # ... seed game/segment/session as the other attempts tests do ...
    rec = EventAttempt(
        segment_id=seg_id, episode_id="E1", outcome=AttemptOutcome.SURVIVED,
        time_ms=4200, session_id=sess_id, source=AttemptSource.PRACTICE,
        experimental=True,
    )
    db.log_event_attempt(rec)
    rows = db.get_segment_event_rows(seg_id)
    assert rows[0]["experimental"] == 1
```

- [ ] **Step 3: Run it — expect FAIL** (`EventAttempt` has no `experimental`; column missing). Run: `python -m pytest tests/unit/db/test_attempts.py -k experimental -v`

- [ ] **Step 4: Implement** — add `experimental: int` to `AttemptRow` and `EventAttemptRow` TypedDicts; bind `int(event.experimental)` in `log_event_attempt`'s INSERT (add the column to the column list + values); in the episode roll-up (`attempts.py:126` region, alongside `invalidated = any(...)`) add `experimental = any(int(e["experimental"]) for e in events)` and include it in the rolled-up `AttemptRow`.

- [ ] **Step 5: Run — expect PASS.** Then full DB-test module: `python -m pytest tests/unit/db/ -q`

- [ ] **Step 6: Commit** — `git add -A && git commit -m "feat(db): add experimental flag to attempts (migration 0008)"`

---

## Task 2: `experimental` on the Attempt / EventAttempt models

**Files:**
- Modify: `python/spinlab/models.py` (`Attempt` ~143-174; `EventAttempt` ~190-221)
- Test: `tests/unit/test_models.py` (nearest model test)

- [ ] **Step 1: Failing test**

```python
def test_event_attempt_defaults_experimental_false():
    ea = EventAttempt(segment_id="s", episode_id="e",
                      outcome=AttemptOutcome.SURVIVED, time_ms=1, session_id="x")
    assert ea.experimental is False
```

- [ ] **Step 2: Run — expect FAIL** (`experimental` undefined). Run: `python -m pytest tests/unit/test_models.py -k experimental -v`

- [ ] **Step 3: Implement** — add `experimental: bool = False` to both `Attempt` (models.py:167 region) and `EventAttempt` (models.py:213 region), beside `invalidated`.

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit** — `git commit -am "feat(models): experimental field on Attempt/EventAttempt"`

---

## Task 3: Exclude experimental from the estimator; verify floor still counts it

**Files:**
- Modify: `python/spinlab/scheduler.py` (`attempts_from_rows` :49; `events_from_rows` :69)
- Test: `tests/unit/test_scheduler.py` (or `tests/unit/test_scheduler_fallback.py`)

- [ ] **Step 1: Failing test — experimental excluded from model rows, REPLAY-style**

```python
def test_attempts_from_rows_excludes_experimental():
    rows = [
        {"time_ms": 5000, "completed": 1, "deaths": 0, "clean_tail_ms": 5000,
         "created_at": "2026-06-18T00:00:00", "invalidated": 0, "experimental": 0},
        {"time_ms": 3000, "completed": 1, "deaths": 0, "clean_tail_ms": 3000,
         "created_at": "2026-06-18T00:00:01", "invalidated": 0, "experimental": 1},
    ]
    out = attempts_from_rows(rows)
    assert [r.time_ms for r in out] == [5000]  # experimental row dropped
```

- [ ] **Step 2: Run — expect FAIL** (experimental row currently passes). Run: `python -m pytest tests/unit/test_scheduler.py -k experimental -v`

- [ ] **Step 3: Implement** — in `attempts_from_rows` (scheduler.py:49) change the guard to `if not r.get("invalidated", False) and not r.get("experimental", False)`. In `events_from_rows` (scheduler.py:69) extend the skip: `if AttemptSource(r["source"]) is AttemptSource.REPLAY or bool(r.get("experimental", 0)): continue`.

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Failing test — experimental STILL sets gold (floor counts it)**

```python
def test_compute_golds_includes_experimental(tmp_path):
    db = Database(tmp_path / "t.db")
    # seed segment; log a normal clean 5000ms and an EXPERIMENTAL clean 3000ms
    # (both completed, not invalidated) via db.log_event_attempt / log_attempt.
    golds = db.compute_golds(game_id)
    assert golds[seg_id]["clean_gold_ms"] == 3000  # experimental PB wins gold
```

- [ ] **Step 6: Run — expect PASS already** (compute_golds only filters `invalidated`; experimental flows through). This test LOCKS the floor-inclusion behavior so a future change can't silently break it. If it fails, do NOT add an experimental filter to compute_golds — investigate.

- [ ] **Step 7: Commit** — `git commit -am "feat(model): exclude experimental from estimator; lock floor-inclusion"`

---

## Task 4: PracticeSession toggle + stamp at write time

**Files:**
- Modify: `python/spinlab/practice.py` (`__init__` ~123 region; `receive_event_attempt` :244-253; add `toggle_experimental`)
- Test: `tests/unit/test_practice.py`

- [ ] **Step 1: Failing test — toggle flips, and a recorded event carries the flag**

```python
def test_experimental_toggle_stamps_event(practice_db):
    emu = AsyncMock(); emu.is_connected = True; emu.send_command = AsyncMock()
    ps = PracticeSession(emu=emu, db=practice_db, game_id="g",
                         scheduler=Scheduler(practice_db, "g"))
    assert ps.experimental is False
    ps.toggle_experimental()
    assert ps.experimental is True
    ps._last_allocator = None
    ps.receive_event_attempt(EventAttemptEmission(
        segment_id=practice_db._test_seg_id, episode_id="E1",
        outcome="survived", time_ms=4200, timestamp_ms=0))
    rows = practice_db.get_segment_event_rows(practice_db._test_seg_id)
    assert rows[-1]["experimental"] == 1
```

- [ ] **Step 2: Run — expect FAIL** (`experimental`/`toggle_experimental` undefined). Run: `python -m pytest tests/unit/test_practice.py -k experimental -v`

- [ ] **Step 3: Implement** — in `__init__` add `self.experimental: bool = False` (near the pause flags ~123). Add:

```python
def toggle_experimental(self) -> None:
    """Flip Science/no-record mode. Attempts recorded while True are flagged
    experimental: excluded from the expected-time model, still counted for
    floor/PB. Mid-session toggle; affects only attempts written after the flip."""
    self.experimental = not self.experimental
    logger.info("practice: experimental=%s (segment=%s)",
                self.experimental, self.current_segment_id)
```

In `receive_event_attempt`, add `experimental=self.experimental,` to the `EventAttempt(...)` constructor (practice.py:252 region).

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit** — `git commit -am "feat(practice): Science toggle stamps experimental on recorded events"`

---

## Task 5: API toggle route + route-summary `experimental` field

**Files:**
- Modify: `python/spinlab/session_manager.py` (add `toggle_experimental` passthrough); `python/spinlab/routes/practice.py` (new route); `python/spinlab/api_schemas.py` (`RouteSummaryResponse` +field, ~573); `python/spinlab/routes/model.py` (populate, ~372 region)
- Test: `tests/unit/test_session_manager.py`, `tests/unit/test_live_view_routes.py`

- [ ] **Step 1: Failing test — session_manager toggles the live session**

```python
async def test_toggle_experimental_routes_to_session(practice_db, emu):
    sm = make_sm(practice_db, emu); sm.game_id = "g"
    await sm.start_practice()
    sm.toggle_experimental()
    assert sm.practice_session.experimental is True
    await sm.stop_practice()
```

- [ ] **Step 2: Run — expect FAIL.** Run: `python -m pytest tests/unit/test_session_manager.py -k experimental -v`

- [ ] **Step 3: Implement** — `SessionManager.toggle_experimental()`:

```python
def toggle_experimental(self) -> None:
    if self.practice_session is not None and self.practice_session.is_running:
        self.practice_session.toggle_experimental()
```

Route in `routes/practice.py`:

```python
@router.post("/practice/science", response_model=OkResponse)
async def practice_science(session: SessionManager = Depends(get_session)):
    """Toggle Science/no-record mode on the live practice session."""
    session.toggle_experimental()
    return {"status": "ok"}
```

Add `experimental: bool = False` to `RouteSummaryResponse` (api_schemas.py); populate in `routes/model.py` route-summary dict: `"experimental": ps.experimental if (ps is not None and ps.is_running) else False,`. Add `self.experimental = False` to `_FakePausedSession` in `test_live_view_routes.py` (mirrors the grind/is_running stub additions).

- [ ] **Step 4: Run — expect PASS.** Then: `python -m pytest tests/unit/test_live_view_routes.py -q`

- [ ] **Step 5: Commit** — `git commit -am "feat(api): /practice/science toggle + route-summary experimental"`

---

## Task 6: Frontend toggle button + Science badge

**Files:**
- Modify: `frontend/src/model-api.ts` (postScience); `frontend/src/route-bar.ts` (badge, mirror grindBadge ~78); `frontend/src/model.ts` (toggle button in practice controls ~162); `frontend/style.css` (`.rb-science`)
- Regen: `cd frontend && npm run gen-types`
- Test: `frontend/src/route-bar.test.ts`

- [ ] **Step 1: gen-types** — Run: `cd frontend && npm run gen-types` (so `RouteSummary` gains `experimental`).

- [ ] **Step 2: Failing test — badge shows when experimental**

```typescript
it("shows the Science badge when experimental", () => {
  document.body.innerHTML = `<div id="h"></div>`;
  const host = document.getElementById("h")!;
  renderRouteBar(host, { ...SESSION, routeSummary: {
    ...SESSION.routeSummary, experimental: true } });
  expect(host.querySelector(".rb-science")).not.toBeNull();
});
```

- [ ] **Step 3: Run — expect FAIL.** Run: `cd frontend && npm test -- route-bar`

- [ ] **Step 4: Implement** — `postScience()` in model-api.ts (`await postJSON("/api/practice/science")`). In route-bar.ts add `const scienceBadge = rs.experimental ? '<span class="rb-science">\u{1F9EA} Science</span>' : "";` and append to the `rb-title` line beside `grindBadge`. Add a "Science: on/off" toggle button to the practice controls in model.ts wired to `postScience()`. Add `.rb-science { color: var(--orange); margin-left: 0.5rem; font-weight: 600; }` to style.css. `RouteBarData` test fixtures: add `experimental: false` to the shared `SESSION.routeSummary` so existing tests compile.

- [ ] **Step 5: Run — expect PASS.** Then: `cd frontend && npm run typecheck && npm run build && npm test`

- [ ] **Step 6: Commit** — `git commit -am "feat(frontend): Science toggle button + route-bar badge"`

---

## Task 7: R-menu verb — R+A toggles Science

**Files:**
- Modify: `python/spinlab/retroarch/menu_detector.py` (`COMMANDS` ~50-55; add `BUTTON_A`); `python/spinlab/session_manager.py` (`_handle_controller_command` ~416-434)
- Test: `tests/unit/test_menu_detector.py`, `tests/unit/test_session_manager.py`, plus an emulator confirmation test (mirror the existing R+Y `menu_toggle.poke` style)

`A` lives on `$17`/HELD2 at bit `0x80` (same byte as R=`0x10` and X=`0x40`).

- [ ] **Step 1: Failing test — R+A (HELD2, 0x80) dispatches `toggle_science`**

```python
def test_r_a_emits_toggle_science():
    det = ControllerMenuDetector()
    # arm R, then fresh-press A on $17 — assert a ControllerCommandEvent
    # with command == "toggle_science" (mirror the existing R+X pause test,
    # which lives on the same $17 byte).
    ...
```

- [ ] **Step 2: Run — expect FAIL.** Run: `python -m pytest tests/unit/test_menu_detector.py -k science -v`

- [ ] **Step 3: Implement** — define `BUTTON_A = 0x80  # $17` in menu_detector.py and add `(HELD2, BUTTON_A): "toggle_science"` to `COMMANDS`. In `_handle_controller_command` add `elif event.command == "toggle_science": self.toggle_experimental()`.

- [ ] **Step 4: Run — expect PASS** (unit). Then add/confirm the emulator poke test (real-RA byte read + dispatch), mirroring R+Y.

- [ ] **Step 5: Commit** — `git commit -am "feat(menu): R+<btn> = toggle Science mode"`

---

## Self-review notes

- **Spec coverage:** toggle (T4/T5/T7), flag persistence (T1/T2), distribution-exclusion (T3 steps 1-4), floor-inclusion (T3 steps 5-6, locked by test), UI surface (T6). All covered.
- **Type consistency:** `experimental` (snake) everywhere in Python; `experimental` on `RouteSummary` (TS, via codegen). Method `toggle_experimental` (session+practice), command string `"toggle_science"`, route `/practice/science` — names intentionally distinct by layer; keep them exact.
- **Open item:** Task 7 button. Ship Tasks 1-6 first; Task 7 lands once Andrew picks the button.
- **Final gate:** before declaring done, run the FULL suite (`python -m pytest`) + `cd frontend && npm run build && npm test`.

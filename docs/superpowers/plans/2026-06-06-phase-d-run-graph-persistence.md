# Run-Level Graph — Freeze-and-Persist Session (Plan 2) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the live practice view (route bar + run graph + segment summary + segment graph) visible and frozen after a practice/hyper-play stop, instead of tearing the whole `#practice-card` down — so the session-improvement view survives the stop transition.

**Architecture:** The `SessionSnapshot` stops being cleared on a *clean* stop; instead it is stamped with an `ended_at` (frozen) and survives until the next session start or a game switch. Crash paths still clear (a crashed session must not leave a stale baseline). Two new payload signals ride the existing endpoints — `AppState.has_frozen_session` (drives card visibility) and `RouteSummaryResponse.session_ended_at` (drives frozen elapsed). The frontend gains a pure three-state decision (`live` / `frozen` / `hidden`), a `frozen` render path in `live-view.ts` (no 1s tick, elapsed pinned to `ended_at`), and a `(frozen)` badge in the route bar.

**Tech Stack:** Python (FastAPI, pytest), TypeScript (Vite, Vitest), CSS.

**Scope note:** This is Plan 2 of the iter-2 spec (`docs/superpowers/specs/2026-06-06-phase-d-run-graph-and-persistence-design.md`, Part 2 + the persistence half of Part 3). Plan 1 (the run-graph reducer, endpoint fields, component, and mount) already shipped and merged. The composition refinements the spec defers to "the next live look" (segment-graph demotion, click-to-focus, crowding) are **out of scope** here.

**Design decisions locked in by reading the code (do not re-derive):**
- `EventAttempt.created_at` is tz-aware; `SessionSnapshot` is a frozen dataclass (use `dataclasses.replace` to add `ended_at`).
- `_on_practice_done` / `_on_hyper_play_done` fire on **crash** (mode still PRACTICE/HYPER_PLAY) and must KEEP clearing — only `stop_practice` / `stop_hyper_play` (clean stop, mode already flipped) freeze. The existing crash tests (`test_on_practice_done_crash_clears_snapshot`, `test_on_hyper_play_done_crash_clears_snapshot`) must stay green unchanged.
- `_take_session_snapshot()` overwrites unconditionally, so "replace at next start" needs no extra clear.
- The frozen-idle card reuses the existing live-view hosts; the segment identity for the idle segment view comes from a frontend cache of the last-practiced segment (page-reload-while-frozen loses the segment view but keeps the run-level view — acceptable PoC limitation).

---

### Task 1: Snapshot freeze lifecycle (backend)

**Files:**
- Modify: `python/spinlab/estimators/session_snapshot.py:51-56` (`SessionSnapshot`)
- Modify: `python/spinlab/session_manager.py` (`_freeze_session_snapshot`, `stop_practice`, `stop_hyper_play`, `switch_game`)
- Test: `tests/unit/test_session_manager_snapshot.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_session_manager_snapshot.py` (it already defines `_make_sm_with_segments`, `_fake_task`, and imports `pytest`, `time`, `SessionManager`):

```python
def test_freeze_session_snapshot_stamps_ended_at(monkeypatch):
    sm = _make_sm_with_segments(["s0", "s1"])
    monkeypatch.setattr(time, "time", lambda: 1717_000_000.0)
    sm._take_session_snapshot()  # type: ignore[attr-defined]
    assert sm.practice_session_snapshot.ended_at is None
    monkeypatch.setattr(time, "time", lambda: 1717_000_060.0)
    sm._freeze_session_snapshot()  # type: ignore[attr-defined]
    snap = sm.practice_session_snapshot
    assert snap is not None
    assert snap.ended_at == 1717_000_060.0
    assert snap.started_at == 1717_000_000.0  # preserved
    assert set(snap.segments.keys()) == {"s0", "s1"}  # preserved


def test_freeze_session_snapshot_idempotent(monkeypatch):
    sm = _make_sm_with_segments(["s0"])
    monkeypatch.setattr(time, "time", lambda: 1717_000_000.0)
    sm._take_session_snapshot()  # type: ignore[attr-defined]
    monkeypatch.setattr(time, "time", lambda: 1717_000_060.0)
    sm._freeze_session_snapshot()  # type: ignore[attr-defined]
    monkeypatch.setattr(time, "time", lambda: 1717_000_999.0)
    sm._freeze_session_snapshot()  # second freeze must not move ended_at
    assert sm.practice_session_snapshot.ended_at == 1717_000_060.0


def test_freeze_session_snapshot_noop_when_none():
    sm = _make_sm_with_segments(["s0"])
    assert sm.practice_session_snapshot is None
    sm._freeze_session_snapshot()  # type: ignore[attr-defined]
    assert sm.practice_session_snapshot is None


@pytest.mark.asyncio
async def test_stop_practice_freezes_snapshot(monkeypatch):
    """Clean stop must FREEZE (not clear) the snapshot so the idle view persists.
    Exercises the mode==PRACTICE / no-running-session branch of stop_practice,
    which returns without an SSE notify."""
    from spinlab.models import Mode

    sm = _make_sm_with_segments(["s0"])
    sm.practice_session = None
    sm.mode = Mode.PRACTICE
    monkeypatch.setattr(time, "time", lambda: 1717_000_000.0)
    sm._take_session_snapshot()  # type: ignore[attr-defined]
    monkeypatch.setattr(time, "time", lambda: 1717_000_060.0)

    result = await sm.stop_practice()

    assert sm.mode == Mode.IDLE
    assert sm.practice_session_snapshot is not None
    assert sm.practice_session_snapshot.ended_at == 1717_000_060.0


@pytest.mark.asyncio
async def test_stop_hyper_play_freezes_snapshot(monkeypatch):
    from spinlab.models import Mode

    sm = _make_sm_with_segments(["s0"])
    sm.hyper_play_session = None
    sm.mode = Mode.HYPER_PLAY
    monkeypatch.setattr(time, "time", lambda: 1717_000_000.0)
    sm._take_session_snapshot()  # type: ignore[attr-defined]
    monkeypatch.setattr(time, "time", lambda: 1717_000_060.0)

    await sm.stop_hyper_play()

    assert sm.mode == Mode.IDLE
    assert sm.practice_session_snapshot is not None
    assert sm.practice_session_snapshot.ended_at == 1717_000_060.0
```

Note: `_make_sm_with_segments` bypasses `__init__`, so `sm.hyper_play_session` is not set by default — the `test_stop_hyper_play_freezes_snapshot` test sets it to `None` explicitly to hit the `mode == HYPER_PLAY` branch. Same for `sm.practice_session`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_session_manager_snapshot.py -k "freeze or freezes" -v`
Expected: FAIL — `AttributeError: 'SessionManager' object has no attribute '_freeze_session_snapshot'` (and `SessionSnapshot.__init__` has no `ended_at`).

- [ ] **Step 3: Add `ended_at` to `SessionSnapshot`**

In `python/spinlab/estimators/session_snapshot.py`, the dataclass is:
```python
@dataclass(frozen=True)
class SessionSnapshot:
    """Taken at practice/hyper-play start. Read-only thereafter."""
    started_at: float  # epoch seconds (time.time())
    segments: Mapping[str, SegmentBaseline]
    route: RouteBaseline
```
Add one field (default keeps every existing construction site valid):
```python
@dataclass(frozen=True)
class SessionSnapshot:
    """Taken at practice/hyper-play start. Read-only thereafter, except the
    one-shot freeze: on a clean stop the snapshot is replaced with a copy whose
    ended_at is stamped, so the idle 'frozen session' view survives the stop."""
    started_at: float  # epoch seconds (time.time())
    segments: Mapping[str, SegmentBaseline]
    route: RouteBaseline
    ended_at: float | None = None  # epoch seconds; None while live, set on clean stop
```

- [ ] **Step 4: Add `_freeze_session_snapshot` and rewire the clean-stop paths**

In `python/spinlab/session_manager.py`, add the freeze method right after `_clear_session_snapshot` (around line 598):
```python
    def _freeze_session_snapshot(self) -> None:
        """Stamp the live snapshot's ended_at so it survives the stop transition
        for the idle 'frozen session' view. Idempotent; no-op when there is no
        snapshot or it is already frozen. Uses dataclasses.replace because
        SessionSnapshot is frozen."""
        import time as _time
        from dataclasses import replace

        snap = self.practice_session_snapshot
        if snap is None or snap.ended_at is not None:
            return
        self.practice_session_snapshot = replace(snap, ended_at=_time.time())
```

In `stop_practice` (both branches that today call `self._clear_session_snapshot()`), change them to freeze:
```python
    async def stop_practice(self) -> ActionResult:
        if self.practice_session and self.practice_session.is_running:
            self.practice_session.is_running = False
            # Don't await the task — run_loop cleans up (disarm, end_session) in
            # its finally block within one SEGMENT_LOAD_TIMEOUT_S cycle (~1s).
            # Awaiting it was the source of the UI lag.
            self.mode = Mode.IDLE
            self._freeze_session_snapshot()
            await self._notify_sse()
            return ActionResult(status=Status.STOPPED)
        if self.mode == Mode.PRACTICE:
            self.mode = Mode.IDLE
            self._freeze_session_snapshot()
            return ActionResult(status=Status.STOPPED)
        raise NotRunningError()
```

In `stop_hyper_play`, change BOTH `self._clear_session_snapshot()` calls (the running-session branch and the `mode == HYPER_PLAY` branch) to `self._freeze_session_snapshot()`. Leave everything else in that method unchanged.

DO NOT change `_on_practice_done` or `_on_hyper_play_done` — those fire on CRASH (mode still PRACTICE/HYPER_PLAY) and must keep clearing (`self._clear_session_snapshot()`), so a crashed session leaves no stale frozen baseline. The existing crash tests assert this.

In `switch_game`, clear the snapshot to avoid the known stale-window across a game change. Add the clear right after the existing `self.mode = Mode.IDLE` line (around line 205):
```python
        self.mode = Mode.IDLE
        self._clear_session_snapshot()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_session_manager_snapshot.py -v`
Expected: PASS — the new freeze tests pass AND the existing crash-clears tests (`test_on_practice_done_crash_clears_snapshot`, `test_on_hyper_play_done_crash_clears_snapshot`) still pass.

- [ ] **Step 6: Static checks**

Run: `ruff check python/spinlab/session_manager.py python/spinlab/estimators/session_snapshot.py` — clean.
Run: `npx pyright python/spinlab/session_manager.py python/spinlab/estimators/session_snapshot.py` — no new errors.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/estimators/session_snapshot.py python/spinlab/session_manager.py tests/unit/test_session_manager_snapshot.py
git commit -m "feat(session): freeze snapshot on clean stop instead of clearing

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Expose `has_frozen_session` + `session_ended_at` (backend)

**Files:**
- Modify: `python/spinlab/state_builder.py:66-89` (base dict)
- Modify: `python/spinlab/api_schemas.py:116-142` (`AppState`) and `531-543` (`RouteSummaryResponse`)
- Modify: `python/spinlab/routes/model.py` (`get_route_summary` return)
- Test: `tests/unit/test_state_builder.py`, `tests/unit/test_live_view_routes.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_state_builder.py` (it uses real `SessionManager` via `_make_sm(practice_db, mock_emu)`; `practice_db`/`mock_emu` are fixtures):

```python
class TestFrozenSessionSignal:
    def _frozen_snap(self, *, ended_at):
        from spinlab.estimators.session_snapshot import (
            RouteBaseline,
            SessionSnapshot,
        )
        return SessionSnapshot(
            started_at=1717_000_000.0, segments={},
            route=RouteBaseline(exp_run_ms=None, exp_deaths=None),
            ended_at=ended_at,
        )

    def test_no_snapshot_has_frozen_session_false(self, practice_db, mock_emu):
        sm = _make_sm(practice_db, mock_emu)
        assert sm.get_state()["has_frozen_session"] is False

    def test_live_snapshot_has_frozen_session_false(self, practice_db, mock_emu):
        sm = _make_sm(practice_db, mock_emu)
        sm.practice_session_snapshot = self._frozen_snap(ended_at=None)
        assert sm.get_state()["has_frozen_session"] is False

    def test_frozen_snapshot_has_frozen_session_true(self, practice_db, mock_emu):
        sm = _make_sm(practice_db, mock_emu)
        sm.practice_session_snapshot = self._frozen_snap(ended_at=1717_000_060.0)
        assert sm.get_state()["has_frozen_session"] is True
```

Append to `tests/unit/test_live_view_routes.py` a check that a frozen snapshot surfaces `session_ended_at` (the file already has `_client_with_session` / `_ActiveSessionStub` / `SessionSnapshot` / `RouteBaseline` from Plan 1's Task 2). Add a new helper + test that builds a FROZEN snapshot:

```python
def _client_with_frozen_session(tmp_path) -> tuple[TestClient, str, str]:
    db, seg_id, game_id = _seed_db(tmp_path)
    snapshot = SessionSnapshot(
        started_at=0.0, ended_at=60.0, segments={},
        route=RouteBaseline(exp_run_ms=250000.0, exp_deaths=20.0),
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_session] = lambda: _ActiveSessionStub(snapshot)
    return TestClient(app), seg_id, game_id


class TestFrozenLiveSummary:
    def test_live_summary_exposes_session_ended_at_when_frozen(self, tmp_path):
        client, _, game_id = _client_with_frozen_session(tmp_path)
        r = client.get(f"/api/games/{game_id}/live-summary")
        assert r.status_code == 200
        assert r.json()["session_ended_at"] == 60.0

    def test_live_summary_session_ended_at_none_when_live(self, tmp_path):
        # _client_with_session (Plan 1) builds a snapshot with ended_at unset.
        client, _, game_id = _client_with_session(tmp_path)
        r = client.get(f"/api/games/{game_id}/live-summary")
        assert r.status_code == 200
        assert r.json()["session_ended_at"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_state_builder.py -k FrozenSession tests/unit/test_live_view_routes.py -k Frozen -v`
Expected: FAIL — `KeyError: 'has_frozen_session'` / assert on missing `session_ended_at`.

- [ ] **Step 3: Add `has_frozen_session` to the state base dict**

In `python/spinlab/state_builder.py`, inside `build()`'s `base` dict (the block ending around line 89, which must stay before the `if game_id is None: return base` early-return so the field is ALWAYS present), add:
```python
            # True only when a *frozen* snapshot persists (a clean stop stamped
            # ended_at). Distinguishes "idle with a frozen session to show" from
            # "idle, never practiced" for the frontend's practice-card state.
            "has_frozen_session": (
                session.practice_session_snapshot is not None
                and session.practice_session_snapshot.ended_at is not None
            ),
```
Place it next to `"has_active_run": ...`.

- [ ] **Step 4: Extend the schemas**

In `python/spinlab/api_schemas.py`, add to `AppState` (after `segments_missing_cold`):
```python
    # True when a frozen (clean-stopped) practice snapshot persists; drives the
    # idle "frozen session" practice-card state on the frontend.
    has_frozen_session: bool
```

And add to `RouteSummaryResponse` (after `floor_total_ms`, the last field added in Plan 1):
```python
    session_ended_at: float | None = None  # epoch seconds; set when the session is frozen
```

- [ ] **Step 5: Return `session_ended_at` from the route**

In `python/spinlab/routes/model.py`, in `get_route_summary`'s return dict, add (next to `session_started_at`):
```python
        "session_ended_at": snap.ended_at if snap else None,
```

- [ ] **Step 6: Run tests + regenerate types**

Run: `python -m pytest tests/unit/test_state_builder.py tests/unit/test_live_view_routes.py -v`
Expected: PASS, 0 skipped.

Run: `cd frontend && npm run gen-types`
Expected: `frontend/src/api-types.ts` now types `AppState.has_frozen_session: boolean` and `RouteSummaryResponse.session_ended_at: number | null`. (Git-ignored; no commit.)

- [ ] **Step 7: Static checks**

Run: `ruff check python/spinlab/state_builder.py python/spinlab/api_schemas.py python/spinlab/routes/model.py tests/unit/test_state_builder.py tests/unit/test_live_view_routes.py` — clean.
Run: `npx pyright python/spinlab/state_builder.py python/spinlab/routes/model.py python/spinlab/api_schemas.py` — no new errors.

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/state_builder.py python/spinlab/api_schemas.py python/spinlab/routes/model.py tests/unit/test_state_builder.py tests/unit/test_live_view_routes.py
git commit -m "feat(api): expose has_frozen_session + session_ended_at for frozen view

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Frozen render path — route bar badge + live-view tick-skip

**Files:**
- Modify: `frontend/src/route-bar.ts` (`(frozen)` badge)
- Modify: `frontend/src/live-view.ts` (`frozen` inference, pinned elapsed, no tick)
- Modify: `frontend/style.css` (`.rb-frozen`)
- Test: `frontend/src/route-bar.test.ts`, `frontend/src/live-view.test.ts`

- [ ] **Step 1: Write the failing tests**

In `frontend/src/route-bar.test.ts`, add `session_ended_at` to the file's existing `RouteSummary` fixture builder (it already sets the Plan-1 fields `run_series`/`baseline_exp_run_ms`/`floor_total_ms`; add `session_ended_at: null` to the defaults). Then add a test (match the file's existing `describe`/host setup style):

```ts
it("shows a (frozen) badge when the session is frozen", () => {
  const host = document.createElement("div");
  renderRouteBar(host, {
    title: "Beto · any%", gameId: "g", nowSeconds: 1060,
    routeSummary: base({ session_started_at: 1000, session_ended_at: 1060 }),
  });
  expect(host.querySelector(".rb-frozen")).not.toBeNull();
  expect(host.textContent).toContain("(frozen)");
  // Elapsed is pinned by the caller-supplied nowSeconds (= ended_at): 60s.
  expect(host.textContent).toContain("0:01:00");
});

it("shows no (frozen) badge for a live session", () => {
  const host = document.createElement("div");
  renderRouteBar(host, {
    title: "Beto · any%", gameId: "g", nowSeconds: 1030,
    routeSummary: base({ session_started_at: 1000, session_ended_at: null }),
  });
  expect(host.querySelector(".rb-frozen")).toBeNull();
});
```
(Use the file's actual fixture builder name — it may be `base(...)` or inline. If the file builds the `RouteSummary` inline per test, follow that style and just include `session_ended_at`.)

In `frontend/src/live-view.test.ts`, add `session_ended_at` to the mocked `/live-summary` payload as `null` by default (so existing tests stay live), then add a frozen-path test:

```ts
it("frozen summary skips the 1s tick and pins elapsed to ended_at", async () => {
  const api = await import("./api");
  (api.fetchJSON as ReturnType<typeof vi.fn>).mockImplementation(async (url: string) => {
    if (url.includes("/live-summary")) {
      return {
        game_id: "g0", exp_run_ms: 115_000, exp_deaths: 3.5,
        n_estimable: 8, n_skipped: 0,
        session_started_at: 1000, exp_run_diff_ms: null, exp_deaths_diff: null,
        practice_saved_ms: 6200, floor_improvement_ms: null,
        run_series: [120000, 115000], baseline_exp_run_ms: 121200, floor_total_ms: 110000,
        session_ended_at: 1060,
      };
    }
    return {
      segment_id: "s0", ready: true, expected_episode_ms: 21_800, practice_gain_ms: 500,
      death_rate: 0.62, floor_ms: 12_800, last_episode_ms: 16_800, last_clean_ms: 13_600,
      last_deaths: 1, last_rank: 2,
      series: [{ episode_ms: 16800, deaths: 1, clean_ms: 13600, running_floor_ms: 12800 }],
      n_successes: 6, n_deaths: 5,
      expected_episode_diff_ms: null, practice_gain_diff_ms: null,
      floor_diff_ms: null, death_rate_diff: null,
    };
  });
  const spy = vi.spyOn(globalThis, "setInterval");
  const hosts = setupHosts();
  await loadAndRenderLiveView({
    segmentId: "s0", gameId: "g0", segmentName: "L1", title: "Beto · any%", hosts,
  });
  // Frozen: no elapsed-tick interval started.
  expect(spy).not.toHaveBeenCalled();
  // Route bar reflects the frozen elapsed (ended_at - started_at = 60s) and badge.
  expect(hosts.routeBar.textContent).toContain("(frozen)");
  expect(hosts.routeBar.textContent).toContain("0:01:00");
  spy.mockRestore();
  destroyLiveView();
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && npm test -- route-bar live-view`
Expected: FAIL — no `.rb-frozen` element; `setInterval` is called (tick not skipped).

- [ ] **Step 3: Add the `(frozen)` badge to route-bar.ts**

In `frontend/src/route-bar.ts`, inside `renderRouteBar`, compute the badge near the top (after `const rs = data.routeSummary;`):
```ts
  const frozenBadge = rs.session_ended_at != null
    ? ` <span class="rb-frozen">(frozen)</span>`
    : "";
```
Then add it to the title line in the template:
```ts
        <div class="rb-title">${escapeHtml(data.title)}${frozenBadge}</div>
```

- [ ] **Step 4: Add the frozen render path to live-view.ts**

In `frontend/src/live-view.ts`, in `loadAndRenderLiveView`, replace the `if (summary) { ... }` block and the unconditional tick start at the end so the tick is skipped when frozen. The current code is:
```ts
  if (summary) {
    _lastRouteData = {
      title: opts.title, gameId: opts.gameId,
      routeSummary: summary, nowSeconds: Date.now() / 1000,
    };
    renderRouteBar(opts.hosts.routeBar, _lastRouteData);
    renderRunGraph(opts.hosts.runGraph, summary);
  }
  if (live) {
    renderSegmentSummary(opts.hosts.segmentSummary, { name: opts.segmentName, live });
    renderEpisodeGraph(opts.hosts.graph, live);
  }

  _tickHandle = setInterval(tickRouteBar, TICK_INTERVAL_MS);
```
Change to:
```ts
  // Frozen sessions (clean-stopped) carry session_ended_at; pin the elapsed clock
  // to that instant and skip the 1s tick so the idle view stays static.
  let frozen = false;
  if (summary) {
    frozen = summary.session_ended_at != null;
    _lastRouteData = {
      title: opts.title, gameId: opts.gameId,
      routeSummary: summary,
      nowSeconds: frozen ? summary.session_ended_at! : Date.now() / 1000,
    };
    renderRouteBar(opts.hosts.routeBar, _lastRouteData);
    renderRunGraph(opts.hosts.runGraph, summary);
  }
  if (live) {
    renderSegmentSummary(opts.hosts.segmentSummary, { name: opts.segmentName, live });
    renderEpisodeGraph(opts.hosts.graph, live);
  }

  if (!frozen) {
    _tickHandle = setInterval(tickRouteBar, TICK_INTERVAL_MS);
  }
```

- [ ] **Step 5: Add CSS for the frozen badge**

In `frontend/style.css`, next to the `.rb-*` rules (search for `.rb-title`), add:
```css
.rb-frozen { color: var(--text-dim); font-size: 11px; font-weight: 400; }
```

- [ ] **Step 6: Run tests + typecheck**

Run: `cd frontend && npm test -- route-bar live-view && npm run typecheck`
Expected: PASS; typecheck clean.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/route-bar.ts frontend/src/live-view.ts frontend/style.css frontend/src/route-bar.test.ts frontend/src/live-view.test.ts
git commit -m "feat(frontend): frozen route-bar badge + live-view tick-skip

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Three-state practice card (frontend)

**Files:**
- Modify: `frontend/src/model-logic.ts` (pure `practiceCardState`)
- Test: `frontend/src/model-logic.test.ts`
- Modify: `frontend/src/model.ts` (`updatePracticeCard` three states + last-practiced cache)
- Modify: `frontend/style.css` (`[data-frozen]` hide rules)

- [ ] **Step 1: Write the failing test for the pure decision**

Append to `frontend/src/model-logic.test.ts` (mirror its import style — it imports from `./model-logic`):

```ts
import { practiceCardState } from "./model-logic";

describe("practiceCardState", () => {
  const args = (over: Partial<Parameters<typeof practiceCardState>[0]>) => ({
    mode: "idle", hasCurrentSegment: false, hasFrozenSession: false,
    hasLastPracticed: false, hasGameId: true, ...over,
  });

  it("is live while practicing with a current segment", () => {
    expect(practiceCardState(args({ mode: "practice", hasCurrentSegment: true }))).toBe("live");
  });
  it("is live during hyper_play with a current segment", () => {
    expect(practiceCardState(args({ mode: "hyper_play", hasCurrentSegment: true }))).toBe("live");
  });
  it("is hidden while practicing with no current segment yet", () => {
    expect(practiceCardState(args({ mode: "practice", hasCurrentSegment: false }))).toBe("hidden");
  });
  it("is frozen when idle with a frozen session and a remembered segment", () => {
    expect(practiceCardState(args({
      mode: "idle", hasFrozenSession: true, hasLastPracticed: true, hasGameId: true,
    }))).toBe("frozen");
  });
  it("is hidden when frozen-session exists but no remembered segment (e.g. fresh reload)", () => {
    expect(practiceCardState(args({
      mode: "idle", hasFrozenSession: true, hasLastPracticed: false,
    }))).toBe("hidden");
  });
  it("is hidden when idle with no frozen session", () => {
    expect(practiceCardState(args({ mode: "idle", hasFrozenSession: false }))).toBe("hidden");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- model-logic`
Expected: FAIL — `practiceCardState` is not exported.

- [ ] **Step 3: Implement the pure decision**

Append to `frontend/src/model-logic.ts`:
```ts
export type PracticeCardState = "live" | "frozen" | "hidden";

/** Decide the practice card's state. `live` = actively practicing/hyper-playing
 *  a segment; `frozen` = idle but a clean-stopped session persists and we have a
 *  remembered segment to re-render; `hidden` otherwise. */
export function practiceCardState(args: {
  mode: string;
  hasCurrentSegment: boolean;
  hasFrozenSession: boolean;
  hasLastPracticed: boolean;
  hasGameId: boolean;
}): PracticeCardState {
  const isLive = (args.mode === "practice" || args.mode === "hyper_play")
    && args.hasCurrentSegment;
  if (isLive) return "live";
  if (args.hasFrozenSession && args.hasLastPracticed && args.hasGameId) return "frozen";
  return "hidden";
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npm test -- model-logic`
Expected: PASS.

- [ ] **Step 5: Wire the three states into model.ts**

In `frontend/src/model.ts`:

Add to the imports from `./model-logic`:
```ts
import { canStartPractice, canStartHyperPlay, practiceCardState } from "./model-logic";
```

Add a module-level cache near the other module state (next to `_currentSegmentId`):
```ts
let _lastPracticed: { id: string; name: string } | null = null;
```

Replace `updatePracticeCard` (the whole function body, lines ~68-126) with:
```ts
export function updatePracticeCard(data: AppState): void {
  const card = document.getElementById("practice-card") as HTMLElement;
  const cs = data.current_segment;
  const state = practiceCardState({
    mode: data.mode,
    hasCurrentSegment: cs != null,
    hasFrozenSession: data.has_frozen_session,
    hasLastPracticed: _lastPracticed != null,
    hasGameId: data.game_id != null,
  });

  if (state === "hidden") {
    card.style.display = "none";
    card.removeAttribute("data-frozen");
    destroyEmSuitePanel();
    destroyImprovementView();
    destroyLiveView();
    return;
  }

  const hosts = {
    routeBar: document.getElementById("live-route-bar")!,
    runGraph: document.getElementById("live-run-graph")!,
    segmentSummary: document.getElementById("live-segment-summary")!,
    graph: document.getElementById("live-graph-slot")!,
  };

  if (state === "frozen") {
    // Idle with a clean-stopped session: keep the live view visible, rendered
    // from the persisted (frozen) snapshot. CSS [data-frozen] hides the
    // practice-only widgets (recent, session stats, weights, panels).
    card.style.display = "";
    card.dataset.frozen = "true";
    destroyEmSuitePanel();
    destroyImprovementView();
    void loadAndRenderLiveView({
      segmentId: _lastPracticed!.id,
      gameId: data.game_id!,
      segmentName: _lastPracticed!.name,
      title: data.game_name ?? data.game_id!,
      hosts,
    });
    return;
  }

  // state === "live"
  card.style.display = "";
  card.removeAttribute("data-frozen");
  _lastPracticed = { id: cs!.id, name: segmentName(cs!) };
  if (data.game_id) {
    void loadAndRenderLiveView({
      segmentId: cs!.id,
      gameId: data.game_id,
      segmentName: segmentName(cs!),
      title: data.game_name ?? data.game_id,
      hosts,
    });
  }

  renderRecentList(document.getElementById("recent")!, data.recent, patchAttemptInvalidated);
  renderSessionStats(data.session);

  const weightsEl = document.getElementById("allocator-weights") as HTMLElement;
  if (weightsEl) {
    weightsEl.style.display = data.mode === "hyper_play" ? "none" : "";
  }
  if (data.allocator_weights && data.mode !== "hyper_play") {
    _currentWeights = { ...data.allocator_weights };
    renderWeightSlider(data.allocator_weights, (next) => {
      _currentWeights = next;
      postAllocatorWeights(next);
    });
  }

  const improvementHost = document.getElementById("improvement-view") as HTMLElement;
  if (improvementHost) {
    void loadAndRenderImprovementView(cs!.id, improvementHost);
  }

  // EMA-suite panel. Fired per SSE app-state push, so updates per attempt
  // for free. Fire-and-forget — errors render an inline message inside the
  // panel host without blocking the rest of the card.
  const emSuiteHost = document.getElementById("em-suite-panel") as HTMLElement;
  if (emSuiteHost) {
    void loadAndRenderEmSuitePanel(cs!.id, emSuiteHost);
  }
}
```

- [ ] **Step 6: Add `[data-frozen]` CSS to hide practice-only widgets**

In `frontend/style.css`, near the `.lv-*` / `.rg-*` rules, add:
```css
/* Frozen idle card: keep only the live view (route bar + run graph + segment
   summary + segment graph) visible; hide the practice-only widgets. */
#practice-card[data-frozen="true"] > h3,
#practice-card[data-frozen="true"] #recent,
#practice-card[data-frozen="true"] .practice-footer,
#practice-card[data-frozen="true"] .allocator-weights,
#practice-card[data-frozen="true"] .em-suite-panel,
#practice-card[data-frozen="true"] .lv-improvement {
  display: none;
}
```

- [ ] **Step 7: Run tests + typecheck + build**

Run: `cd frontend && npm test && npm run typecheck && npm run build`
Expected: all pass; build writes to `python/spinlab/static/`.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/model-logic.ts frontend/src/model-logic.test.ts frontend/src/model.ts frontend/style.css
git commit -m "feat(frontend): three-state practice card (live/frozen/hidden)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Smoke test + full gate

**Files:**
- Modify: `tests/integration/test_frontend_smoke.py`

- [ ] **Step 1: Read the smoke harness to choose the right assertion**

Read `tests/integration/test_frontend_smoke.py` and its `fake_dashboard_server` / `page` fixtures. Determine whether the harness can drive a real practice start→stop cycle (it serves the built bundle and proxies `/api/**`). If it CANNOT drive a live session (likely — it serves seeded read-only data), DO NOT fake a session. Instead add a DOM-contract assertion that the frozen-hiding CSS selectors resolve to real elements, so the `[data-frozen]` layout cannot silently rot:

```python
@pytest.mark.asyncio(loop_scope="session")
async def test_practice_card_frozen_layout_targets_exist(page):
    pg, _errors = page
    # The frozen idle view (Plan 2) hides practice-only widgets via
    # #practice-card[data-frozen="true"] selectors. Assert those targets exist
    # so the CSS contract can't drift. (Live frozen behavior is verified by the
    # vitest model-logic / live-view tests + manual live look.)
    assert await pg.locator("#practice-card #recent").count() == 1
    assert await pg.locator("#practice-card .practice-footer").count() == 1
    assert await pg.locator("#practice-card .em-suite-panel").count() == 1
    assert await pg.locator("#practice-card .lv-improvement").count() == 1
    assert await pg.locator("#practice-card .lv-run-graph").count() == 1
```

If, on reading, the harness DOES support driving a session, prefer the spec's stronger smoke (after a practice stop the card stays visible with `(frozen)`, a fresh start clears it) and write that instead. Pick exactly one; keep it green and deterministic.

- [ ] **Step 2: Build the bundle and run the smoke subset**

Run: `cd frontend && npm run build`
Then: `python -m pytest -m "not emulator" -k "shell or smoke" -v`
Expected: PASS, 0 skipped.

- [ ] **Step 3: Full gate**

Run: `python -m pytest`
Expected: ALL pass, **0 skipped** (project policy — emulator tests must actually run; a SKIPPED emulator test counts as a FAILURE). The one known cosmetic warning (`RuntimeWarning: invalid value encountered in divide` at `python/spinlab/_segments_v07/api.py:165`) is acceptable. Paste the exact summary line. If anything fails or is skipped, STOP and report — do not commit.

Also run `ruff check python/` (changed files clean) and `npx pyright python/spinlab/session_manager.py python/spinlab/state_builder.py python/spinlab/routes/model.py python/spinlab/api_schemas.py python/spinlab/estimators/session_snapshot.py` — no new errors.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_frontend_smoke.py
git commit -m "test(shell): assert frozen-card layout targets exist on the Play page

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage (Part 2 + Part 3 persistence):**
- Snapshot not cleared on clean stop; `ended_at` stamped → Task 1 (`_freeze_session_snapshot`, `stop_practice`/`stop_hyper_play`). ✓
- Elapsed freezes (`(ended_at or now) − started_at`) → Task 3 (live-view pins `nowSeconds = ended_at`; route-bar already computes `nowSeconds − started_at`). ✓
- Clear/replace only at next start (overwrite) and on game switch → Task 1 (`switch_game` clear; `_take_session_snapshot` overwrites). ✓
- Crash still clears (refinement beyond the spec's loose wording, required by existing tests) → Task 1 (leaves `_on_*_done` clearing). ✓
- AppState signal for "frozen session exists" → Task 2 (`has_frozen_session`). ✓
- Idle `live-summary`/`live` still render frozen via persisted baseline → automatic (snapshot persists; `session_ended_at` added in Task 2). ✓
- `updatePracticeCard` third state (idle-with-frozen stays visible, no tick) → Task 4 (`practiceCardState`) + Task 3 (no tick). ✓
- `(frozen)` label on the spine → Task 3 (route-bar badge). ✓
- Idle layout keeps spine + run graph + segment summary + segment graph; hides practice-only widgets → Task 4 (`[data-frozen]` CSS). ✓
- Tests: vitest (pure card state, frozen tick-skip, frozen badge), pytest (freeze lifecycle, frozen signals), smoke (layout targets), full gate → Tasks 1-5. ✓

**Type consistency:** `SessionSnapshot.ended_at: float | None` → `RouteSummaryResponse.session_ended_at: float | None` → TS `number | null` (read `!= null`); `AppState.has_frozen_session: bool` → TS `boolean`. `practiceCardState(...) -> "live" | "frozen" | "hidden"` consumed by `updatePracticeCard`. `_lastPracticed: { id, name }` matches `loadAndRenderLiveView`'s `segmentId`/`segmentName`. Consistent.

**Deferred (out of scope, per spec "next live look"):** segment-graph demotion, click-to-focus master-detail, practicing-view cull, run-selector promotion. Page-reload-while-frozen loses the segment view (run-level view still renders) — accepted PoC limitation, noted in the header.

**Placeholder scan:** none — every code step shows full code; the one conditional in Task 5 Step 1 is a documented read-then-choose with both concrete alternatives spelled out.

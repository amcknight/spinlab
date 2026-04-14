# Cleanup Bundle — Playwright Smoke, model.ts Split, Overlay + Dead Code, Test Pruning

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute sections 3, 4, 5, 7 of the 2026-04-13 cleanup pass spec as one bundled pass — leaves section 6 (Status enum pruning) as its own separate plan.

**Architecture:** Four independent phases, each landing as its own commit (or small set of commits). Phase A adds a frontend-contract smoke test. Phase B refactors `model.ts` along the existing `model-logic.ts` layering precedent. Phase C deletes the practice-overlay timer and the vestigial `manifest.py` importer. Phase D does a light implementation-detail pruning of the two oversized test files now that Phase 1 (test reorg) and Phase 2 (capture consolidation) have already landed.

**Tech Stack:** Python 3.11+, FastAPI, pytest, Playwright, TypeScript + Vite, Lua (Mesen).

**Out of scope:** Status enum pruning (spec §6) — separate plan. Emulator-driven Playwright, TS codegen from Python — deferred per spec.

---

## Phase A — Frontend Playwright Smoke (spec §3)

**Files:**
- Create: `tests/integration/test_frontend_smoke.py`
- Modify: `tests/integration/conftest.py` — add `fake_game_loaded` fixture
- Reference: `tests/integration/test_smoke.py` (harness), `tests/factories.py` (seeded data)

### Task A1: Scaffold the fake-game-loaded fixture

- [ ] **Step 1: Read the existing conftest**

Read `tests/integration/conftest.py` to find the `dashboard_server` / `dashboard_url` / `api` fixtures and understand how `SystemState` and the DB are wired up. Note the fixture names actually exported.

- [ ] **Step 2: Add the `fake_game_loaded` fixture**

Add to `tests/integration/conftest.py`:

```python
@pytest.fixture
def fake_game_loaded(dashboard_server, db):
    """Test-only: forcibly populate SystemState as if a ROM is loaded.

    Bypasses the TCP/Lua boundary (already covered by test_smoke.py).
    Seeds a minimal game + a couple of segments + a reference + a few attempts
    so every dashboard tab has something to render.
    """
    from tests.factories import seed_basic_game  # add helper if missing
    game_id = seed_basic_game(db)
    session = dashboard_server.session
    session.state.tcp_connected = True
    session.state.game_id = game_id
    session.state.game_name = "FakeGame"
    session.rebuild_game_context()  # use whatever the real wiring calls
    yield game_id
```

If a helper like `seed_basic_game` doesn't exist in `tests/factories.py`, add one that inserts: 1 game, 3 segments, 1 reference, ~10 attempts split across two estimators, 1 recent practice session. Keep it <40 LOC.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/conftest.py tests/factories.py
git commit -m "test: add fake_game_loaded fixture for frontend smoke"
```

### Task A2: Write the Playwright smoke test

- [ ] **Step 1: Ensure frontend build is a precondition**

Confirm `cd frontend && npm run build` populates `python/spinlab/static/`. The existing `@pytest.mark.frontend` tests already require this — do the same here (no `@pytest.mark.emulator`).

- [ ] **Step 2: Write `tests/integration/test_frontend_smoke.py`**

```python
"""Frontend contract smoke: real FE bytes + real backend + fake-loaded game.

Catches FE/BE contract drift (renders reading a dropped field, restructured shape).
No emulator: the TCP/Lua boundary is faked via the `fake_game_loaded` fixture.
"""
import pytest
from playwright.sync_api import sync_playwright, ConsoleMessage

pytestmark = pytest.mark.frontend

TABS = ["practice", "segments", "manage", "model"]


@pytest.fixture
def page(dashboard_url, fake_game_loaded):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context()
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(dashboard_url)
        page.wait_for_load_state("networkidle")
        yield page, errors
        browser.close()


def test_all_tabs_render_without_console_errors(page):
    pg, errors = page
    for tab in TABS:
        pg.click(f'[data-tab="{tab}"]')
        pg.wait_for_load_state("networkidle")
    assert not errors, f"console/page errors: {errors}"


def test_sse_delivers_state_update(page):
    pg, _ = page
    # SSE auto-connects on load; header shows game name once state ticks arrive.
    pg.wait_for_selector("text=FakeGame", timeout=5000)


def test_practice_card_shows_seeded_segment(page):
    pg, _ = page
    pg.click('[data-tab="practice"]')
    # Current-goal element is populated from current_segment in updatePracticeCard.
    # With a fake-loaded game and no active practice session, assert the card
    # renders (hidden or visible) without throwing.
    assert pg.locator("#practice-card").count() == 1


def test_segments_tab_lists_seeded_segments(page):
    pg, _ = page
    pg.click('[data-tab="segments"]')
    pg.wait_for_selector(".segment-row, [data-segment-id]", timeout=5000)
    rows = pg.locator("[data-segment-id]").count()
    assert rows >= 1


def test_manage_tab_shows_reference(page):
    pg, _ = page
    pg.click('[data-tab="manage"]')
    pg.wait_for_load_state("networkidle")
    # Reference name is seeded by seed_basic_game; adjust selector to match manage.ts output.
    assert pg.locator("text=/reference/i").count() >= 1


def test_model_tab_renders_model_table(page):
    pg, _ = page
    pg.click('[data-tab="model"]')
    pg.wait_for_selector("#model-body tr", timeout=5000)
    assert pg.locator("#model-body tr").count() >= 1
```

Adjust selectors (`[data-tab]`, `[data-segment-id]`) to match what the real DOM uses — read `frontend/src/app.ts`, `segments-view.ts`, `manage.ts` for the actual attributes and update selectors before running. Do not assume.

- [ ] **Step 3: Run the test locally**

```bash
cd frontend && npm run build && cd ..
pytest tests/integration/test_frontend_smoke.py -v
```

Expected: all tests pass. If a selector is wrong, read the corresponding FE source and fix the selector — do not weaken the assertion.

- [ ] **Step 4: Run full fast suite to confirm no regression**

```bash
pytest -m "not (emulator or slow)"
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_frontend_smoke.py
git commit -m "test: add frontend contract smoke with fake-loaded game"
```

---

## Phase B — `model.ts` Split (spec §4)

**Files:**
- Create: `frontend/src/model-render.ts`, `frontend/src/model-api.ts`
- Modify: `frontend/src/model.ts` (shrink to orchestration + event binding)
- Reference: `frontend/src/model-logic.ts` (existing layer precedent)

### Task B1: Extract `model-api.ts`

- [ ] **Step 1: Create `frontend/src/model-api.ts`**

Pull the pure fetch+coerce helpers out of `model.ts`:

```ts
import { fetchJSON, postJSON } from "./api";
import type { ModelData, TuningData } from "./types";

export async function fetchModelData(): Promise<ModelData | null> {
  return fetchJSON<ModelData>("/api/model");
}

export async function fetchTuningData(): Promise<TuningData | null> {
  return fetchJSON<TuningData>("/api/estimator-params");
}

export async function postEstimator(name: string): Promise<void> {
  await postJSON("/api/estimator", { name });
}

export async function postTuningParams(params: Record<string, number>): Promise<void> {
  await postJSON("/api/estimator-params", { params });
}

export async function postAllocatorWeights(weights: Record<string, number>): Promise<void> {
  await postJSON("/api/allocator-weights", weights);
}

export async function patchAttemptInvalidated(id: number, invalidated: boolean): Promise<void> {
  await fetch(`/api/attempts/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ invalidated }),
  }).catch(() => { /* next SSE update will reconcile */ });
}
```

- [ ] **Step 2: Update `model.ts` to use `model-api.ts`**

Replace inline `fetchJSON("/api/model")`, `postJSON("/api/allocator-weights", ...)`, etc. with the new imports. Do not yet move render code.

- [ ] **Step 3: Run typecheck + tests**

```bash
cd frontend && npm run typecheck && npm test
```

Expected: green.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/model-api.ts frontend/src/model.ts
git commit -m "refactor(fe): extract model-api.ts"
```

### Task B2: Extract `model-render.ts`

- [ ] **Step 1: Create `frontend/src/model-render.ts`**

Move the pure DOM-building functions from `model.ts`: `renderWeightSlider`, `positionHandle`, `updateSliderVisuals`, `renderLegend`, `renderTuningParams`, plus the two pieces of `updateModel` that build rows, and `updatePracticeCard`'s internal `recent`-list rendering if it factors cleanly.

Keep a narrow seam: render functions take data + DOM nodes, do not call `fetch*`/`postJSON*` and do not own state. If a render function needs to trigger an API call on a user event (e.g. weight slider release), pass a callback in rather than importing from `model-api.ts`.

- [ ] **Step 2: Update `model.ts` to import render functions**

`model.ts` becomes orchestration: `initModelTab`, `fetchModel`, `updateModel` (thin wrapper that calls render), `updatePracticeCard` (thin wrapper), `updatePracticeControls`, `showSegmentDetail`, `hideSegmentDetail`, module-level tuning-debounce state.

Target: `model.ts` under ~200 LOC.

- [ ] **Step 3: Run typecheck + tests + build**

```bash
cd frontend && npm run typecheck && npm test && npm run build
```

Expected: green.

- [ ] **Step 4: Run the frontend smoke from Phase A**

```bash
pytest tests/integration/test_frontend_smoke.py -v
```

Expected: still green — this is exactly what the smoke catches if we broke a render path.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/model-render.ts frontend/src/model.ts
git commit -m "refactor(fe): extract model-render.ts, shrink model.ts to orchestration"
```

---

## Phase C — Overlay Timer Removal + Dead Code Audit (spec §5, §7)

**Files:**
- Modify: `lua/spinlab.lua` — drop practice-overlay timer rows, keep goal label
- Delete: `python/spinlab/manifest.py`
- Modify: `python/spinlab/routes/system.py` — remove `/import-manifest` route
- Modify: `future.txt` — strike the overlay-timer bullet
- Modify: `docs/ARCHITECTURE.md` if it references manifest import

### Task C1: Practice overlay timer removal

- [ ] **Step 1: Edit `lua/spinlab.lua` `draw_practice_overlay` (lines 443-469)**

Remove the timer-row and countdown draws. Keep only the label (goal) draw.

Replacement body (keep indentation, keep guard):

```lua
local function draw_practice_overlay()
  if not practice.active then return end

  local label = practice.segment and practice.segment.description or "?"
  if label == "" then label = "?" end

  draw_text(4, 2, label, 0x00000000, 0xFFFFFFFF)
end
```

Note: **do not** touch `draw_speed_run_overlay` — speed-run mode's timer is load-bearing (it's the whole point). Spec §5 is about the practice overlay's timer specifically.

- [ ] **Step 2: Manual smoke — skip if in a worktree (per CLAUDE.md)**

If in the main checkout:

```bash
spinlab dashboard
```

Load a ROM, start a practice session, confirm the overlay shows only the goal label (no timer, no countdown). If in a worktree, note this as a checkpoint for the user to verify.

- [ ] **Step 3: Update `future.txt`**

Remove the last bullet (`maybe drop the timer form the Overlay?...`).

- [ ] **Step 4: Run emulator tests** (to catch Lua breakage)

```bash
pytest -m emulator
```

Expected: green. If a test asserted specific timer-row text on the overlay, update it — the behavior legitimately changed.

- [ ] **Step 5: Commit**

```bash
git add lua/spinlab.lua future.txt
git commit -m "refactor(lua): drop practice-overlay timer, keep goal label"
```

### Task C2: `manifest.py` audit — delete

- [ ] **Step 1: Verify usage is limited to `/import-manifest`**

```bash
git grep -n "manifest" python/ tests/
```

Expected imports: only `python/spinlab/routes/system.py:129`. The route is `POST /api/import-manifest`, called by no FE code (confirm with `git grep "import-manifest" frontend/`).

- [ ] **Step 2: Check git log for recent meaningful use**

```bash
git log --oneline --follow python/spinlab/manifest.py | head -20
```

Record the last commit that added a feature to this file (not just a mechanical rename) in your commit message. If it's >6 months old and there's no indication users rely on it, proceed with deletion.

- [ ] **Step 3: Delete the file and route**

```bash
git rm python/spinlab/manifest.py
```

Edit `python/spinlab/routes/system.py`: remove the entire `import_manifest` handler (lines ~126-135) and any now-unused imports (`yaml` at that scope, `Path` if only used there).

- [ ] **Step 4: Run tests**

```bash
pytest
```

Expected: green. If a test imports `spinlab.manifest` or hits `/api/import-manifest`, delete that test — it was asserting on dead code.

- [ ] **Step 5: Commit**

```bash
git commit -am "refactor: delete vestigial manifest.py and /import-manifest route"
```

### Task C3: Other dead code — time-boxed sweep (max 30 minutes)

- [ ] **Step 1: Candidate list**

Look at each and either keep-with-docstring or delete:

- `python/spinlab/spinrec.py` — check usage: `git grep "spinrec"`. If only CLI-referenced and unused, note for removal.
- `python/spinlab/vite.py` — check: if only used by dashboard dev flow, keep.
- Any `*_controller.py` or `*_manager.py` not imported anywhere.

```bash
for f in $(git ls-files "python/spinlab/*.py"); do
  mod=$(basename "$f" .py)
  count=$(git grep -l "from spinlab.$mod\|from spinlab import $mod\|spinlab\.$mod" | grep -v "^$f$" | wc -l)
  echo "$count $f"
done | sort -n | head -15
```

- [ ] **Step 2: For any zero-import file, decide keep-or-delete**

If unsure, keep it and add a one-line docstring explaining why it earns its place. Do not refactor beyond deletion — bias is toward "leave it alone."

- [ ] **Step 3: Commit if anything changed**

```bash
git commit -am "refactor: prune confirmed dead modules"
```

Otherwise skip the commit.

---

## Phase D — Test Pruning (spec §8 second pass)

**Files:**
- Modify: `tests/unit/test_dashboard_integration.py` (440 LOC)
- Modify: `tests/unit/test_session_manager.py` if it exists under that path after Phase 1 reorg (spec says `tests/test_session_manager.py` at 543 LOC; locate current path before touching)

### Task D1: Locate the oversized files

- [ ] **Step 1: Find them**

```bash
find tests/ -name "*.py" | xargs wc -l | sort -rn | head -10
```

Identify the two largest unit test files. If neither is over ~400 LOC after the Phase 1 reorg, skip Phase D entirely.

### Task D2: Light implementation-detail pruning

- [ ] **Step 1: Read the file top-to-bottom**

For each oversized file, read through and tag each test mentally as:

- **KEEP** — documents user-visible behavior, API contract, or a previously-broken invariant.
- **KEEP (verbose)** — verbose but documents real behavior. Keep anyway; verbosity is not a deletion criterion.
- **CANDIDATE** — asserts on a private attribute, implementation detail, or internal call ordering that no consumer observes; **AND** needed rewriting anyway after Phase 1/2/B/C renames.

Only **CANDIDATE** tests are deletion candidates.

- [ ] **Step 2: Delete only CANDIDATE tests**

For each:
1. Confirm no other test covers the same behavior.
2. If covered elsewhere, delete.
3. If not covered elsewhere, *update* it instead — write a behavior-level assertion that matches what the original was trying to protect.

Default is keep. When in doubt, keep.

- [ ] **Step 3: Run the full suite**

```bash
pytest
```

Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "test: prune implementation-detail assertions from oversized test files"
```

If no tests were deleted (all were KEEP/KEEP-verbose), skip the commit and note in the final report.

---

## Final Verification

- [ ] **Full pytest:** `pytest` — green across unit, emulator, frontend.
- [ ] **Frontend build:** `cd frontend && npm run build && npm test && npm run typecheck` — green.
- [ ] **Manual smoke:** `spinlab dashboard`, load a ROM, cycle tabs, start practice. Confirm: overlay shows goal only (no timer); all tabs render; no console errors.
- [ ] **Per-phase check:**
  - A: `pytest tests/integration/test_frontend_smoke.py` passes in <10s.
  - B: `frontend/src/model.ts` ≤200 LOC; `model-render.ts` and `model-api.ts` exist.
  - C: `python/spinlab/manifest.py` gone; `/api/import-manifest` returns 404; practice overlay timer gone.
  - D: Two largest test files measurably smaller or unchanged with documented reason.

## Risk Notes

- **Phase A**: selectors must match real DOM; adjust while writing, not by weakening assertions.
- **Phase B**: refactor only — no behavior change. Phase A smoke will catch accidental breakage.
- **Phase C**: `manifest.py` deletion is load-bearing on the assumption that `/api/import-manifest` has no active FE consumer. Verify before deleting. Overlay timer removal is user-visible; flag in commit for Andrew to confirm on-stream.
- **Phase D**: narrow deletion criterion. Bias keep. Do not restructure remaining tests.

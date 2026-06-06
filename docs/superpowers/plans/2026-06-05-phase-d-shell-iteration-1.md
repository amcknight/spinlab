# Phase D Shell — Iteration 1 (Two-Tab Sweep) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dashboard's 4-tab navigation with **2 pages — Play and Setup — joined by a horizontal sweep driven by a tiny edge tab**, regrouping existing content (Play = Model + Simulator; Setup = Manage + Segments) without rewriting any existing component.

**Architecture:** Pure frontend restructure. A new `shell.ts` owns the page-toggle state and edge-tab; `index.html` wraps the four existing `<section>`s into two sliding pages inside a `translateX` track; `app.ts` swaps its 4-tab click wiring for `initShell()` and fires the right per-page fetches on show. The existing `<header>` (game selector + mode chip) remains the always-visible shared spine. The live practice view (`live-view.ts` et al.) is **untouched** — only relocated as a child of the Play page.

**Tech Stack:** TypeScript + Vite, Vitest (pure logic), Playwright via pytest (`tests/integration/test_frontend_smoke.py`), plain CSS (`frontend/style.css`).

**Explicitly OUT of scope (later iterations):** always-on run-aggregate spine, master-detail click-to-focus, unfold-on-stop density, swappable graph slot, second-tab final name, FE3 polish. See `docs/superpowers/specs/2026-06-05-phase-d-shell-design.md`.

**Branch:** `feat/phase-d-shell` (already created; precursor bug-fix + spec already committed there).

---

## File Structure

- **Create** `frontend/src/shell.ts` — page-toggle state (`Page = "play" | "setup"`), pure `nextPage`/`tabLabel` helpers, `initShell(onShow)` wiring the edge tab + `data-page` attribute, `currentPage()` getter.
- **Create** `frontend/src/shell.test.ts` — Vitest for the pure helpers.
- **Modify** `frontend/index.html` — replace `<nav id="tabs">` + four `<section class="tab-content">` with the edge tab + `#sweep-viewport > #sweep-track > (#page-play, #page-setup)`; move the Simulator panel into Play and the Segments container into Setup.
- **Modify** `frontend/src/app.ts` — drop the `.tab` click loop; call `initShell`; route per-page fetches; replace the `.tab.active` check in `updateFromState` with `currentPage()`.
- **Modify** `frontend/style.css` — sweep viewport/track/edge-tab/tint styles; retire `nav#tabs`/`.tab`/`.tab-content` rules.
- **Modify** `tests/integration/test_frontend_smoke.py` — update selectors from tabs to the two pages.

No backend changes. No new types in `api-types.ts`.

---

## Task 1: Shell page-toggle state module (pure, TDD)

**Files:**
- Create: `frontend/src/shell.ts`
- Test: `frontend/src/shell.test.ts`

- [ ] **Step 1: Write the failing test**

```typescript
// frontend/src/shell.test.ts
import { describe, it, expect } from "vitest";
import { nextPage, tabLabel, type Page } from "./shell";

describe("shell page toggle", () => {
  it("toggles play <-> setup", () => {
    expect(nextPage("play")).toBe<Page>("setup");
    expect(nextPage("setup")).toBe<Page>("play");
  });

  it("edge-tab label names the DESTINATION page", () => {
    // On Play, the tab takes you to Setup, so it reads 'Setup'.
    expect(tabLabel("play")).toBe("Setup");
    expect(tabLabel("setup")).toBe("Play");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/shell.test.ts`
Expected: FAIL — `Cannot find module './shell'`.

- [ ] **Step 3: Write the minimal implementation**

```typescript
// frontend/src/shell.ts
export type Page = "play" | "setup";

/** Toggle to the other page. */
export function nextPage(current: Page): Page {
  return current === "play" ? "setup" : "play";
}

/** The edge tab is labelled with the page it takes you TO. */
export function tabLabel(current: Page): string {
  return current === "play" ? "Setup" : "Play";
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/shell.test.ts`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/shell.ts frontend/src/shell.test.ts
git commit -m "feat(shell): pure page-toggle helpers for the two-tab sweep"
```

---

## Task 2: Shell DOM wiring (initShell)

Adds the imperative half of `shell.ts`: reads the current page off a root element's `data-page`, toggles it on edge-tab click, updates the tab label, and invokes an `onShow(page)` callback so the app can fetch per-page data. No test here (DOM wiring is covered by the Playwright smoke in Task 6); keep it tiny.

**Files:**
- Modify: `frontend/src/shell.ts`

- [ ] **Step 1: Append the DOM wiring to `shell.ts`**

```typescript
// --- appended to frontend/src/shell.ts ---

// The root element carries data-page="play|setup"; CSS keys the slide + tint
// off it. #sweep-tab is the edge tab; #sweep-tab-label holds its text.
const ROOT_ID = "sweep-shell";

export function currentPage(): Page {
  const root = document.getElementById(ROOT_ID);
  return (root?.dataset.page as Page) ?? "play";
}

function setPage(page: Page, onShow: (p: Page) => void): void {
  const root = document.getElementById(ROOT_ID);
  const label = document.getElementById("sweep-tab-label");
  if (!root) return;
  root.dataset.page = page;
  if (label) label.textContent = tabLabel(page);
  onShow(page);
}

/** Wire the edge tab. `onShow` fires once for the initial page and again on
 *  every toggle, so the caller can lazily fetch that page's data. */
export function initShell(onShow: (p: Page) => void): void {
  const tab = document.getElementById("sweep-tab");
  tab?.addEventListener("click", () => setPage(nextPage(currentPage()), onShow));
  // Fire once for the page the markup starts on (play).
  setPage(currentPage(), onShow);
}
```

- [ ] **Step 2: Type-check**

Run: `cd frontend && npm run typecheck`
Expected: no errors from `shell.ts` (the HTML ids it references are created in Task 3; typecheck does not resolve DOM ids, so this passes now).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/shell.ts
git commit -m "feat(shell): initShell wires edge tab, data-page toggle, onShow"
```

---

## Task 3: Restructure index.html into two sweeping pages

Replace the `<nav>` + four `<section class="tab-content">` with the sweep shell. **Move the existing markup verbatim** into the new page wrappers — do not edit the inner content of the moved sections.

**Files:**
- Modify: `frontend/index.html`

- [ ] **Step 1: Replace the `<nav id="tabs">…</nav>` block and the `<main>…</main>` open/close with the sweep shell.**

Delete this nav block entirely:

```html
  <nav id="tabs">
    <button class="tab active" data-tab="model">Model</button>
    <button class="tab" data-tab="manage">Manage</button>
    <button class="tab" data-tab="segments">Segments</button>
    <button class="tab" data-tab="practice-engine">Simulator</button>
  </nav>
```

Then change the `<main>` wrapper so the four sections become two pages. Replace the line `<main>` with:

```html
  <div id="sweep-shell" data-page="play">
    <button id="sweep-tab" class="sweep-tab" title="Switch page">
      <span id="sweep-tab-label">Setup</span>
    </button>
    <div id="sweep-viewport">
      <main id="sweep-track">
        <section id="page-play" class="page">
```

- [ ] **Step 2: Inside `#page-play`, keep the former `#tab-model` contents, then append the Simulator panel.**

The former `<section id="tab-model" class="tab-content active"> … </section>` becomes the body of `#page-play`. Remove its own `<section id="tab-model" …>` open/close tags (its children now live directly under `#page-play`). Immediately after the former model contents — i.e. after the `<div id="segment-detail" …></div>` line — insert the Simulator panel (moved out of the old practice-engine tab):

```html
          <!-- Simulator (merged from the former Practice Engine tab) -->
          <div id="practice-engine-panel"></div>
```

- [ ] **Step 3: Close `#page-play`, open `#page-setup`, and move Manage + Segments into it.**

After the Simulator panel, close the Play page and open the Setup page. The former `#tab-manage` contents (References / recording-indicator / paused-run-card / Segments table / Data sections) move in as-is (drop the `<section id="tab-manage" …>` wrapper). Then append the former `#tab-segments` body (`<div id="segments-view-container"></div>`). Structure:

```html
        </section><!-- /#page-play -->

        <section id="page-setup" class="page">
          <!-- former #tab-manage contents (all .manage-section blocks) go here -->
          <!-- ...References, recording-indicator, paused-run-card, Segments table, Data... -->

          <!-- former #tab-segments body -->
          <div id="segments-view-container"></div>
        </section><!-- /#page-setup -->
      </main><!-- /#sweep-track -->
    </div><!-- /#sweep-viewport -->
  </div><!-- /#sweep-shell -->
```

Delete the now-empty former `<section id="tab-segments">` and `<section id="tab-practice-engine">` wrappers. The `<main>` element is now `#sweep-track`; ensure there is exactly one `</main>` (the one shown above) and the old `</main>` is removed.

- [ ] **Step 4: Verify the markup parses and ids are unique.**

Run: `cd frontend && npm run build`
Expected: build succeeds. Then:

Run: `node -e "const h=require('fs').readFileSync('frontend/index.html','utf8'); for (const id of ['sweep-shell','sweep-tab','page-play','page-setup','practice-engine-panel','segments-view-container','practice-card','model-table']) { const n=(h.match(new RegExp('id=\"'+id+'\"','g'))||[]).length; if(n!==1){console.error('BAD',id,n);process.exit(1);} } console.log('ids ok');"`
Expected: `ids ok`.

- [ ] **Step 5: Commit**

```bash
git add frontend/index.html
git commit -m "feat(shell): index.html into Play/Setup sweep pages (Model+Sim / Manage+Segments)"
```

---

## Task 4: Sweep CSS

**Files:**
- Modify: `frontend/style.css`

- [ ] **Step 1: Append the sweep styles** (use the existing palette; tints are subtle).

```css
/* --- Phase D two-tab sweep shell --- */
#sweep-viewport { overflow-x: hidden; }
#sweep-track {
  display: flex;
  width: 200%;
  transition: transform 0.35s cubic-bezier(0.4, 0, 0.2, 1);
}
#sweep-track > .page {
  width: 50%;
  flex: 0 0 50%;
  box-sizing: border-box;
}
/* Slide to the Setup page. */
#sweep-shell[data-page="setup"] #sweep-track { transform: translateX(-50%); }

/* Subtle per-page tint so you always know where you are. */
#sweep-shell[data-page="play"]  { background: #15201a; }
#sweep-shell[data-page="setup"] { background: #1c1622; }

/* Edge tab: clings to the side it sweeps toward; flips across on toggle. */
#sweep-shell { position: relative; }
.sweep-tab {
  position: absolute;
  top: 64px;                /* below the header band; refine placement later */
  z-index: 5;
  cursor: pointer;
  writing-mode: vertical-rl;
  text-orientation: mixed;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.5px;
  padding: 10px 3px;
  border: 1px solid;
  background: transparent;
  transition: all 0.35s ease;
}
#sweep-shell[data-page="play"]  .sweep-tab {
  right: 0; left: auto; border-radius: 6px 0 0 6px; border-right: none;
  color: #c77dff; border-color: #5a4170; background: #2a1e3a;
}
#sweep-shell[data-page="setup"] .sweep-tab {
  left: 0; right: auto; border-radius: 0 6px 6px 0; border-left: none;
  color: #6fcf97; border-color: #3a5a44; background: #15201a;
}
```

- [ ] **Step 2: Remove the dead 4-tab rules.**

Search `frontend/style.css` for `nav#tabs`, `.tab-content`, and standalone `.tab` selectors that styled the old nav. Delete those rule blocks (they reference markup that no longer exists). Leave any `.tab`-named class used elsewhere untouched — grep first:

Run: `cd frontend && grep -n "nav#tabs\|tab-content\|\.tab " style.css`
Delete only the blocks tied to the removed nav.

- [ ] **Step 3: Build and eyeball.**

Run: `cd frontend && npm run build`
Expected: build succeeds.

- [ ] **Step 4: Commit**

```bash
git add frontend/style.css
git commit -m "feat(shell): sweep CSS — sliding track, edge tab, per-page tint"
```

---

## Task 5: Rewire app.ts to the shell

Replace the four-tab click handler and the `.tab.active` lookups with the shell. On show, fetch the page's data.

**Files:**
- Modify: `frontend/src/app.ts`

- [ ] **Step 1: Add the shell import.**

At the top of `frontend/src/app.ts`, add:

```typescript
import { initShell, currentPage } from "./shell";
```

- [ ] **Step 2: Replace the `.tab.active` check inside `updateFromState`.**

Find this block (currently lines ~44-53):

```typescript
  const activeTab = document.querySelector(".tab.active") as HTMLElement | null;
  if (activeTab?.dataset.tab === "model") fetchModel();
  if (
    activeTab?.dataset.tab === "manage" ||
    data.mode === "reference" ||
    data.mode === "replay" ||
    data.mode === "cold_fill"
  ) {
    fetchManage();
  }
```

Replace it with:

```typescript
  // Play hosts the model table; refresh it on every state push while visible.
  if (currentPage() === "play") fetchModel();
  // Manage data must refresh during capture-ish modes regardless of page, plus
  // whenever Setup is the visible page.
  if (
    currentPage() === "setup" ||
    data.mode === "reference" ||
    data.mode === "replay" ||
    data.mode === "cold_fill"
  ) {
    fetchManage();
  }
```

- [ ] **Step 3: Replace the `.tab` click-handler loop with `initShell`.**

Delete this entire block (currently lines ~56-71):

```typescript
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document
      .querySelectorAll(".tab-content")
      .forEach((c) => c.classList.remove("active"));
    (btn as HTMLElement).classList.add("active");
    document
      .getElementById("tab-" + (btn as HTMLElement).dataset.tab)
      ?.classList.add("active");
    if ((btn as HTMLElement).dataset.tab === "model") fetchModel();
    if ((btn as HTMLElement).dataset.tab === "manage") fetchManage();
    if ((btn as HTMLElement).dataset.tab === "segments") fetchAndRenderSegments();
    if ((btn as HTMLElement).dataset.tab === "practice-engine") initPracticeEnginePanel();
  });
});
```

Replace it with:

```typescript
// Two-page sweep. Play hosts Model + Simulator; Setup hosts Manage + Segments.
// onShow fires once for the initial page (play) and again on each toggle.
initShell((page) => {
  if (page === "play") {
    fetchModel();
    initPracticeEnginePanel();
  } else {
    fetchManage();
    fetchAndRenderSegments();
  }
});
```

- [ ] **Step 4: Type-check and build.**

Run: `cd frontend && npm run typecheck && npm run build`
Expected: no type errors; build succeeds. (If `initPracticeEnginePanel` or `fetchAndRenderSegments` show as unused anywhere, they are still used here — confirm no stale references remain to the deleted block.)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/app.ts
git commit -m "feat(shell): app.ts drives the two-page sweep, fetches per page on show"
```

---

## Task 6: Update the frontend smoke tests

The smoke tests navigate via `nav#tabs button.tab[data-tab=...]` and assert `section#tab-{tab}.active`. Repoint them at the two pages.

**Files:**
- Modify: `tests/integration/test_frontend_smoke.py`

- [ ] **Step 1: Add a sweep helper and replace the tab-navigation calls.**

Near the top of the test module (after imports), add:

```python
async def _goto_setup(pg):
    """Sweep from Play to the Setup page."""
    await pg.click("#sweep-tab")
    await pg.wait_for_selector('#sweep-shell[data-page="setup"]', timeout=5000)
```

Then update each test:

- `test_all_tabs_render_without_console_errors` — rename to `test_both_pages_render_without_console_errors`; replace the per-tab loop with: assert Play content is present on load (`#model-table`), then `await _goto_setup(pg)` and assert `#segments-view-container` is present. Keep the console-error assertion.
- `test_practice_card_renders` — remove the `data-tab="model"` click (Play is the default page); the card lives on Play. Keep the `#practice-card` assertion.
- `test_model_tab_renders_model_table` — remove the tab click; assert `#model-body tr` on the default Play page.
- `test_simulator_tab_renders_segment_names_not_undefined` and `test_simulator_ranks_gated_segments` — remove the `data-tab="practice-engine"` click; the Simulator (`#pe-rank-body`) is on the default Play page (its `initPracticeEnginePanel` runs via `initShell`'s initial `onShow("play")`). Assert `#pe-rank-body li` directly.
- `test_segments_tab_lists_seeded_segments` — replace the `data-tab="segments"` click with `await _goto_setup(pg)`; assert `#segments-view-container section.segments-level`.
- `test_manage_tab_shows_reference` — replace the `data-tab="manage"` click with `await _goto_setup(pg)`; replace the `section#tab-manage.active` wait with `#sweep-shell[data-page="setup"]`; keep the `#ref-select option` assertion.

- [ ] **Step 2: Build the frontend, then run the smoke tests.**

Run: `cd frontend && npm run build`
Then: `python -m pytest tests/integration/test_frontend_smoke.py -q`
Expected: all smoke tests pass. (If Playwright Chromium is missing, run `playwright install chromium` first — a skip here is a failure per project policy.)

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_frontend_smoke.py
git commit -m "test(shell): smoke tests navigate the two-page sweep, not four tabs"
```

---

## Task 7: Full verification gate

- [ ] **Step 1: Frontend unit tests.**

Run: `cd frontend && npm test`
Expected: all pass (includes `shell.test.ts`).

- [ ] **Step 2: Frontend typecheck + build.**

Run: `cd frontend && npm run typecheck && npm run build`
Expected: clean.

- [ ] **Step 3: Full Python suite (unit + emulator + frontend smoke).**

Run: `python -m pytest`
Expected: green, zero skips per project policy. If any pre-existing failure appears, STOP and surface it before proceeding (do not commit over a red baseline).

- [ ] **Step 4: Final manual eyeball (dashboard).**

Start the dashboard, confirm: the app opens on Play (Model table + practice card + Simulator panel visible); the edge tab reads "Setup" on the right; clicking it sweeps left to Setup (References / Segments table / Data) and the tab flips to "Play" on the left; sweeping back restores Play. The header (game selector + mode chip) stays put throughout.

- [ ] **Step 5: No commit needed** if Tasks 1-6 were each committed. The branch `feat/phase-d-shell` now carries the iteration-1 shell.

---

## Self-review notes (already applied)

- **Spec coverage:** 2 tabs + sweep + edge tab + tint (Tasks 3-5); Play = Model+Simulator, Setup = Manage+Segments (Task 3); header stays as spine (unchanged); deferred items explicitly excluded.
- **Known rough edges for the next iteration (do not fix here):** the sweep track's height equals the taller page (shorter page shows trailing whitespace); the edge tab's fixed `top: 64px` may overlap content on some pages; run-aggregate spine, master-detail, and unfold-on-stop are not yet present. These are intentional — the point of iteration 1 is to get the shell on screen and re-brainstorm from the running result.
- **Type consistency:** `Page`, `nextPage`, `tabLabel`, `currentPage`, `initShell` names match across `shell.ts`, `shell.test.ts`, and `app.ts`.

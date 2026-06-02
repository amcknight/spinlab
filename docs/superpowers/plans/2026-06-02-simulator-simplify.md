# Simulator Simplify — Implementation Plan (Plan B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Practice Simulator legible at a glance: a unified per-segment list with not-ready segments shown **inline**, a plain **"Practice next"** ranking (no `Value/sec`, no scientific notation, no `Δ`/`Value` columns), and all policy/objective/slack/target knobs collapsed behind **Advanced** so the default view works with zero input.

**Architecture:** Pure frontend reorganization of `frontend/src/practice-engine.ts` + its CSS + the unit/smoke tests that assert its DOM. No backend changes — it consumes the existing `/state` and `/evaluate` payloads. The default objective stays `expected_wall_clock_per_attempt` (needs no ctx), so the simple view auto-computes on open.

**Tech Stack:** TypeScript/Vite, vitest (happy-dom), Playwright smoke. No new deps.

**Spec:** [`docs/superpowers/specs/2026-06-01-practice-ui-overhaul-design.md`](../specs/2026-06-01-practice-ui-overhaul-design.md) §B (run-planning simplified) + §D (readability). Plan B of the A→B→C→D sequence; Plan A already shipped.

---

## What exists now (verified — read `frontend/src/practice-engine.ts` before editing)

- `renderPracticeEnginePanel(container, state)` builds: `<h2>`, a `.pe-help` `<details>`, a `.pe-status` line, an always-visible `.pe-controls` block (policy/objective/slack/target/p/session + Recompute), a `#pe-target-paced-section` (caption + fill-gold + `.pe-segments-input` table), a `#pe-headline`, a `.pe-values` table with `<tbody id="pe-values-body">`, and a separate `.pe-ungated` block (`<h3>Not enough data yet</h3>` + `<ul>`).
- `updatePanelResults(container, response)` sets `#pe-headline` and fills `#pe-values-body` with 6-column rows (Segment/Now/After 1×/Δ/Value/Value/sec, the last via `toExponential(2)`).
- `initPracticeEnginePanel()` renders, calls `applyControlVisibility`, wires change/​input listeners + the Recompute button to `runRecompute`/`scheduleRecompute`, and runs once on open.
- Helpers already present and REUSED as-is: `segmentName`, `formatTime`, `formatSavings`, `OBJECTIVE_LABELS`, `PROB_OBJECTIVES`, `formatObjectiveValue`, `formatObjectiveDelta`, `applyControlVisibility`, `REQUIRED_CTX`, `buildEvaluateRequest`, `fetchState`, `fetchEvaluate`, `runRecompute`, `scheduleRecompute`.
- `per_segment_values` items are `{ seg_id, value, value_per_second (number|null), e_sample_0_ms, e_sample_1_ms }`. `state.ungated_segments` items are `{ seg_id, reason, description, level_number, start_type, start_ordinal, end_type, end_ordinal }` (so `segmentName(u)` works).
- Tests touching this DOM: `frontend/src/practice-engine.test.ts` (asserts `.pe-segments-input tbody tr` count, fill-from-gold input values, ungated text contains the reason) and `tests/integration/test_frontend_smoke.py` (`test_simulator_tab_renders_segment_names_not_undefined` waits `#pe-values-body tr` + asserts `L201 start → cp1` etc.; `test_simulator_recompute_populates_values` clicks `#pe-recompute`, waits `#pe-values-body tr`).

## Target structure (after Plan B)

Default-visible, top to bottom: `<h2>` · `.pe-help` · `.pe-status` · `#pe-headline` · **Practice-next block** (`.pe-ranklist` containing `<ol id="pe-rank-body">` for ranked gated segments, then `<ul id="pe-notready">` for not-ready segments inline). Collapsed: **`<details class="pe-advanced"><summary>Advanced</summary>`** wrapping the existing `.pe-controls` + `#pe-target-paced-section`. The `Recompute` button moves inside Advanced (auto-recompute already covers the common path).

Ranked row (gated): `① L6 start → cp1   +0.5s   22.2s → 21.7s` — rank number, name, expected gain (`formatObjectiveDelta(objName, value)`), and `now → after` in seconds. No raw `value_per_second`, no `Δ`/`Value` headers. Sort by `value_per_second` desc (nulls last). Not-ready row: `L1 start → cp1 — needs ≥2 clears and ≥2 deaths (have 1 / 0)` dimmed.

---

## Task 1: Reorganize `practice-engine.ts` (render + results + wiring) with updated unit tests

**Files:**
- Modify: `frontend/src/practice-engine.ts`
- Modify (tests first): `frontend/src/practice-engine.test.ts`

- [ ] **Step 1: Update the unit tests to the new structure (Red).** Replace the body of `frontend/src/practice-engine.test.ts`'s `describe("renderPracticeEnginePanel", …)` block and add a results test. The MOCK_STATE / MOCK_EVAL objects at the top of the file are unchanged (keep them). Replace the existing `describe("renderPracticeEnginePanel"…)` with:

```typescript
import { renderPracticeEnginePanel, updatePanelResults, buildEvaluateRequest } from "./practice-engine";
```
(update the import line at the top to also import `updatePanelResults`), and replace the renderPracticeEnginePanel describe block with:

```typescript
describe("renderPracticeEnginePanel", () => {
  beforeEach(() => {
    document.body.innerHTML = `<div id="practice-engine-panel"></div>`;
  });

  it("renders the practice-next list container and the status line", () => {
    const container = document.getElementById("practice-engine-panel")!;
    renderPracticeEnginePanel(container, MOCK_STATE);
    expect(container.querySelector("#pe-rank-body")).not.toBeNull();
    expect(container.querySelector(".pe-status")?.textContent ?? "").toContain("ready");
  });

  it("collapses policy/objective controls behind an Advanced details (closed)", () => {
    const container = document.getElementById("practice-engine-panel")!;
    renderPracticeEnginePanel(container, MOCK_STATE);
    const adv = container.querySelector<HTMLDetailsElement>("details.pe-advanced");
    expect(adv).not.toBeNull();
    expect(adv!.open).toBe(false);
    // Controls live inside Advanced, not at top level.
    expect(adv!.querySelector("#pe-policy")).not.toBeNull();
    expect(adv!.querySelector("#pe-objective")).not.toBeNull();
  });

  it("shows not-ready segments inline in the list (not a separate block)", () => {
    const stateWithUngated: PracticeEngineState = {
      ...MOCK_STATE,
      ungated_segments: [{
        seg_id: "s3", reason: "needs more data", description: "", level_number: 3,
        start_type: "entrance", start_ordinal: 0, end_type: "goal", end_ordinal: 0,
      }],
    };
    const container = document.getElementById("practice-engine-panel")!;
    renderPracticeEnginePanel(container, stateWithUngated);
    const notReady = container.querySelector("#pe-notready");
    expect(notReady).not.toBeNull();
    expect(notReady!.textContent).toContain("needs more data");
    expect(container.querySelector(".pe-ungated")).toBeNull(); // old separate block gone
  });

  it("fill-from-gold (inside Advanced) populates cumulative splits", () => {
    const container = document.getElementById("practice-engine-panel")!;
    renderPracticeEnginePanel(container, MOCK_STATE);
    container.querySelector<HTMLButtonElement>("#pe-fill-gold")!.click();
    const s1 = container.querySelector<HTMLInputElement>('input.pe-seg-split[data-seg-id="s1"]')!;
    const s2 = container.querySelector<HTMLInputElement>('input.pe-seg-split[data-seg-id="s2"]')!;
    expect(s1.value).toBe("4200");
    expect(s2.value).toBe(String(4200 + 5800));
  });
});

describe("updatePanelResults", () => {
  beforeEach(() => {
    document.body.innerHTML = `<div id="practice-engine-panel"></div>`;
  });

  it("renders a ranked practice-next list with plain payoff, no scientific notation", () => {
    const container = document.getElementById("practice-engine-panel")!;
    renderPracticeEnginePanel(container, MOCK_STATE);
    updatePanelResults(container, MOCK_EVAL);
    const rows = container.querySelectorAll("#pe-rank-body li");
    expect(rows.length).toBe(2);
    const text = container.querySelector("#pe-rank-body")!.textContent || "";
    expect(text).not.toMatch(/e[+-]\d/i);       // no 5.71e-2
    expect(text).not.toContain("undefined");
    // s1 (value_per_second 200/4500 ≈ 0.0444) ranks above s2 (150/6000 = 0.025).
    expect(rows[0].textContent).toContain("Level 1");
    expect(rows[1].textContent).toContain("Level 2");
  });
});
```

- [ ] **Step 2: Run the unit tests — Red.** `cd frontend && npm test -- practice-engine` → failures (no `#pe-rank-body`, controls not in `details.pe-advanced`, `updatePanelResults` not exported-as-used / old structure).

- [ ] **Step 3: Rewrite `renderPracticeEnginePanel`.** Replace the function body (keep the signature and the `segNameById` capture at the top). New body after the `segNameById` capture:

```typescript
  const header = document.createElement("h2");
  header.textContent = "Practice Simulator";
  container.appendChild(header);

  const help = document.createElement("details");
  help.className = "pe-help";
  help.innerHTML = `
    <summary>What is this? (how to use)</summary>
    <div class="pe-help-body">
      <p>Imagines thousands of full runs from your real per-segment data to rank
      <em>which segment is most worth practicing next</em> and show how your runs
      are likely to go.</p>
      <ul>
        <li><strong>Practice next</strong> lists segments best-first by the expected
        gain from one more practice attempt. Practice the top one.</li>
        <li>A segment needs <strong>≥2 clears and ≥2 deaths</strong> before it can be
        modelled; until then it shows greyed with what it still needs.</li>
        <li><strong>Advanced</strong> lets you change what's measured (objective) and
        the reset rule (policy). The default is average time per attempt.</li>
      </ul>
    </div>
  `;
  container.appendChild(help);

  const gatedCount = state.gated_segments.length;
  const total = gatedCount + state.ungated_segments.length;
  const status = document.createElement("div");
  status.className = "pe-status";
  status.innerHTML = total === 0
    ? "No segments tracked yet — make a reference run and practice."
    : `<strong>${gatedCount}</strong> of ${total} segment${total === 1 ? "" : "s"} `
      + `ready. The rest need ≥2 clears and ≥2 deaths each.`;
  container.appendChild(status);

  const headline = document.createElement("div");
  headline.className = "pe-headline";
  headline.id = "pe-headline";
  headline.textContent = "(computing…)";
  container.appendChild(headline);

  // Practice-next: ranked gated segments (filled by updatePanelResults), then
  // not-ready segments inline (greyed) — one block, no separate "Ungated" header.
  const rank = document.createElement("div");
  rank.className = "pe-ranklist";
  rank.innerHTML = `
    <div class="pe-ranklist-title">Practice next</div>
    <ol id="pe-rank-body"></ol>
    <ul id="pe-notready">
      ${state.ungated_segments.map(u =>
        `<li class="pe-nr"><span class="pe-nr-seg">${segmentName(u)}</span>` +
        `<span class="dim"> — ${u.reason}</span></li>`).join("")}
    </ul>
  `;
  container.appendChild(rank);

  const advanced = document.createElement("details");
  advanced.className = "pe-advanced";
  advanced.innerHTML = `<summary>Advanced</summary>`;
  const advBody = document.createElement("div");
  advBody.className = "pe-advanced-body";
  advanced.appendChild(advBody);
  container.appendChild(advanced);

  const controls = document.createElement("div");
  controls.className = "pe-controls";
  controls.innerHTML = `
    <label>Policy
      <select id="pe-policy">
        <option value="no_reset">no_reset</option>
        <option value="target_paced">target_paced</option>
      </select>
    </label>
    <label>Objective
      <select id="pe-objective">
        <option value="expected_wall_clock_per_attempt">avg time / attempt</option>
        <option value="expected_total_finished_time">avg finished-run time</option>
        <option value="q">chance under target</option>
        <option value="quantile">finish-time quantile</option>
        <option value="p_pb_this_session">PB chance this session</option>
      </select>
    </label>
    <label id="pe-slack-label" title="How far over your split a run may drift before it resets">Slack
      <input id="pe-slack" type="number" step="0.05" value="0" min="0" max="1" />
    </label>
    <label id="pe-target-ms-label">Target (ms)
      <input id="pe-target-ms" type="number" step="100" placeholder="e.g. 12000" />
    </label>
    <label id="pe-p-label">Quantile p
      <input id="pe-p" type="number" step="0.05" min="0" max="1" placeholder="0.5" />
    </label>
    <label id="pe-h-label">Session left (ms)
      <input id="pe-h" type="number" step="60000" placeholder="e.g. 10440000" />
    </label>
    <button id="pe-recompute" title="Recompute now (also runs automatically on change)">Recompute</button>
  `;
  advBody.appendChild(controls);

  const segInputWrap = document.createElement("div");
  segInputWrap.id = "pe-target-paced-section";
  segInputWrap.innerHTML = `
    <p class="pe-caption">Target pace per segment — only used by the
      <code>target_paced</code> policy. A run resets when it falls this far behind.</p>
    <button id="pe-fill-gold" type="button">Fill cum-splits from gold</button>
    <table class="pe-segments-input">
      <thead><tr><th>Segment</th><th>Cumulative split</th><th>Gold</th></tr></thead>
      <tbody>
        ${state.gated_segments.map(seg => `
          <tr data-seg-id="${seg.seg_id}">
            <td>${segmentName(seg)}</td>
            <td><input class="pe-seg-split" type="number" step="100" data-seg-id="${seg.seg_id}" /></td>
            <td class="pe-seg-gold dim" data-gold-ms="${seg.gold_ms ?? ""}">${formatTime(seg.gold_ms)}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
  advBody.appendChild(segInputWrap);

  const fillBtn = segInputWrap.querySelector<HTMLButtonElement>("#pe-fill-gold");
  if (fillBtn) {
    fillBtn.addEventListener("click", () => {
      let cum = 0;
      state.gated_segments.forEach(seg => {
        if (seg.gold_ms !== null && seg.gold_ms !== undefined) {
          cum += seg.gold_ms;
          const input = segInputWrap.querySelector<HTMLInputElement>(
            `.pe-seg-split[data-seg-id="${seg.seg_id}"]`,
          );
          if (input) input.value = String(cum);
        }
      });
    });
  }
```

(Removed: the old standalone `.pe-controls`/`#pe-target-paced-section` appended directly to `container`, the `.pe-values` table, and the `.pe-ungated` block. They're replaced by the Advanced wrapper + the ranked list above.)

- [ ] **Step 4: Rewrite `updatePanelResults`** to fill `#pe-rank-body` (ranked) instead of `#pe-values-body`:

```typescript
export function updatePanelResults(
  container: HTMLElement,
  response: PracticeEngineEvaluateResponse,
): void {
  const objName = container.querySelector<HTMLSelectElement>("#pe-objective")?.value
    ?? "expected_wall_clock_per_attempt";
  const label = OBJECTIVE_LABELS[objName as ObjectiveName] ?? "Objective";
  const headline = container.querySelector<HTMLDivElement>("#pe-headline");
  if (headline) {
    headline.textContent = response.objective_value === null
      ? `${label}: — (not enough data)`
      : `${label}: ${formatObjectiveValue(objName, response.objective_value)}`;
  }
  const body = container.querySelector<HTMLOListElement>("#pe-rank-body");
  if (body) {
    // Best-first by value per second of practice (the ranking metric); nulls last.
    const ranked = [...response.per_segment_values].sort(
      (a, b) => (b.value_per_second ?? -Infinity) - (a.value_per_second ?? -Infinity),
    );
    body.innerHTML = ranked.map((psv, i) => `
      <li>
        <span class="pe-rank-n">${i + 1}</span>
        <span class="pe-rank-seg">${segNameById[psv.seg_id] ?? psv.seg_id}</span>
        <span class="pe-rank-gain" title="Expected gain from one more practice attempt">${formatObjectiveDelta(objName, psv.value)}</span>
        <span class="pe-rank-times dim">${formatTime(psv.e_sample_0_ms)} → ${formatTime(psv.e_sample_1_ms)}</span>
      </li>
    `).join("");
  }
}
```

- [ ] **Step 5: Update `initPracticeEnginePanel` wiring.** It already queries `#pe-recompute`, `#pe-policy`, `#pe-objective`, `#pe-slack`, `#pe-target-ms`, `#pe-p`, `#pe-h`, `.pe-seg-split`, `#pe-fill-gold` via `container.querySelector(...)` — these still resolve because the elements are inside the Advanced `<details>` (still in the DOM, just collapsed). **No change needed** beyond confirming it still compiles. Verify `applyControlVisibility` still works (it queries the same ids). Leave `runRecompute`/`scheduleRecompute` as-is — `runRecompute` now writes into `#pe-rank-body` via the rewritten `updatePanelResults`.

- [ ] **Step 6: Run unit tests — Green.** `cd frontend && npm test -- practice-engine` → all pass. Then `cd frontend && npm test` → full vitest green. Then `npm run typecheck` → clean.

- [ ] **Step 7: Commit.**

```bash
git add frontend/src/practice-engine.ts frontend/src/practice-engine.test.ts
git commit -m "feat(simulator): unified practice-next ranking, inline not-ready, Advanced-collapsed controls"
```

---

## Task 2: CSS for the ranked list + Advanced

**Files:**
- Modify: `frontend/style.css`

- [ ] **Step 1: Append styles** at the end of `frontend/style.css` (match the existing dark theme + `.pe-*` precedent; the old `.pe-values`/`.pe-ungated` rules can stay — they're now unused but harmless; do NOT delete unrelated rules):

```css
/* Practice-next ranked list */
.pe-ranklist { margin: 8px 0; }
.pe-ranklist-title {
  font-size: 11px; text-transform: uppercase; color: var(--text-dim);
  letter-spacing: 0.5px; margin: 6px 0 4px;
}
#pe-rank-body { list-style: none; margin: 0; padding: 0; }
#pe-rank-body li {
  display: flex; align-items: baseline; gap: 8px;
  padding: 4px 5px; border-bottom: 1px solid var(--surface); font-size: 12px;
}
.pe-rank-n {
  color: var(--accent); font-weight: 700; min-width: 1.4em; text-align: right;
}
.pe-rank-seg { flex: 1; color: var(--text); }
.pe-rank-gain { color: var(--green); font-variant-numeric: tabular-nums; }
.pe-rank-times { font-size: 11px; }
#pe-notready { list-style: none; margin: 4px 0 0; padding: 0; }
#pe-notready .pe-nr {
  padding: 3px 5px; border-bottom: 1px solid var(--surface); font-size: 11px;
}
#pe-notready .pe-nr-seg { color: var(--text-dim); }
/* Advanced */
.pe-advanced { margin-top: 10px; }
.pe-advanced > summary { cursor: pointer; color: var(--accent); font-size: 12px; }
.pe-advanced-body { margin-top: 8px; }
```

- [ ] **Step 2: Build to confirm CSS is picked up.** `cd frontend && npm run build` → clean.

- [ ] **Step 3: Commit.**

```bash
git add frontend/style.css
git commit -m "style(simulator): practice-next list + Advanced disclosure"
```

---

## Task 3: Update smoke tests for the new DOM + full verification

**Files:**
- Modify: `tests/integration/test_frontend_smoke.py`

- [ ] **Step 1: Update the two Simulator smoke tests** to the new structure. The ranked list is `#pe-rank-body li` (auto-populates on open); names now render in the ranked rows (gated) and the `#pe-notready` list (ungated). Replace the bodies of `test_simulator_tab_renders_segment_names_not_undefined` and `test_simulator_recompute_populates_values`:

```python
@pytest.mark.asyncio(loop_scope="session")
async def test_simulator_tab_renders_segment_names_not_undefined(page, simulator_seeded):
    """Regression: empty-description segments must resolve to structural names
    (no 'cpundefined'). Gated names appear in the ranked list, ungated inline."""
    pg, errors = page
    await pg.click('nav#tabs button.tab[data-tab="practice-engine"]')
    # Auto-recompute on open populates the ranked list (default objective needs no input).
    await pg.wait_for_selector("#pe-rank-body li", timeout=5000)
    panel_text = await pg.locator("#practice-engine-panel").inner_text()
    assert "undefined" not in panel_text, f"unresolved name fields: {panel_text!r}"
    assert "L201 start → cp1" in panel_text
    assert "L202 cp1 → goal" in panel_text
    # Not-ready segment shown inline (greyed), not a separate block.
    assert "L203 start → goal" in panel_text
    assert await pg.locator(".pe-ungated").count() == 0
    assert not errors, f"console/page errors: {errors}"


@pytest.mark.asyncio(loop_scope="session")
async def test_simulator_ranks_gated_segments(page, simulator_seeded):
    """Regression: the panel auto-ranks gated segments best-first with plain
    payoffs — no scientific notation, no Value/sec column."""
    pg, errors = page
    await pg.click('nav#tabs button.tab[data-tab="practice-engine"]')
    await pg.wait_for_selector("#pe-rank-body li", timeout=5000)
    rows = await pg.locator("#pe-rank-body li").count()
    assert rows >= 1
    body_text = await pg.locator("#pe-rank-body").inner_text()
    import re
    assert not re.search(r"e[+-]\d", body_text), f"scientific notation leaked: {body_text!r}"
    # The Advanced controls are collapsed by default.
    assert await pg.locator("details.pe-advanced").count() == 1
    advanced_open = await pg.locator("details.pe-advanced").evaluate("el => el.open")
    assert advanced_open is False
    assert not errors, f"console/page errors: {errors}"
```

(Note: this renames `test_simulator_recompute_populates_values` → `test_simulator_ranks_gated_segments`; the old `#pe-recompute` click path is gone from the default view since auto-recompute populates the list. The Recompute button still exists inside Advanced but isn't the asserted path.)

- [ ] **Step 2: Build the frontend** (so the smoke harness serves the new bundle): `cd frontend && npm run build`.

- [ ] **Step 3: Run the smoke tests.** `python -m pytest tests/integration/test_frontend_smoke.py -q` → all pass.

- [ ] **Step 4: Frontend checks.** `cd frontend && npm run typecheck && npm test` → clean + green.

- [ ] **Step 5: Full unfiltered gate** (project merge rule): `python -m pytest` → green (requires RetroArch; no live dashboard on NCI 55355).

- [ ] **Step 6: Commit.**

```bash
git add tests/integration/test_frontend_smoke.py
git commit -m "test(simulator): smoke for ranked practice-next list + collapsed Advanced"
```

---

## Self-review notes

- **Spec §B coverage:** not-ready inline ✓ (Task 1 `#pe-notready`), plain "practice next" ranking replacing Value/sec ✓ (Task 1 Step 4), knobs behind Advanced ✓ (Task 1 Step 3). Default objective `expected_wall_clock_per_attempt` needs no ctx → simple view auto-computes.
- **Readability §D:** seconds via `formatTime`; payoff via `formatObjectiveDelta` (no `toExponential`); the smoke test asserts no `e[+-]\d`.
- **No dead behavior:** the `.pe-values`/`.pe-ungated` CSS rules are left in place (harmless) but their DOM is no longer produced; the `pe-num` helpers go unused — acceptable, do not chase unrelated cleanup. If the reviewer prefers, the now-unused `.pe-values`/`.pe-ungated` CSS blocks may be deleted, but that's optional.
- **Reused, not rebuilt:** `runRecompute`/`scheduleRecompute`/`applyControlVisibility`/`REQUIRED_CTX`/`buildEvaluateRequest`/the format helpers are unchanged; only render output + `updatePanelResults`' target element change.
- **Type consistency:** `#pe-rank-body` is an `<ol>` (`HTMLOListElement`); `per_segment_values` fields used (`value`, `value_per_second`, `e_sample_0_ms`, `e_sample_1_ms`) match the existing TS type.

## Out of scope (later)
- The histogram / total-time distribution view (not currently rendered; defer).
- Plan C (alpha memory-window picker) and Plan D (responsive strip↔review).

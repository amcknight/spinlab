# Alpha → Memory Windows — Implementation Plan (Plan C)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the confusing 10×10 EMA-suite alpha matrix (on the Model-tab practice card) with a readable **Now / Baseline memory-window picker** — windows in plain English ("last ~5", "all-time"), defaulting to last-5 / last-20, greying windows you don't have enough attempts for, and showing each chosen window's expected segment time.

**Architecture:** Pure-frontend rewrite of `frontend/src/em-suite-matrix.ts`'s render function (its export name `renderEmSuiteMatrix` is kept so `em-suite-panel.ts` is untouched). The picker derives everything client-side from the existing `/api/segments/{id}/em-suite-matrix` payload (`alpha_grid`, per-α `baseline[]`, `n_attempts_total`). **No backend change. Plan A's improvement view is NOT touched** (per the agreed scope: "just replace the matrix").

**Tech Stack:** TypeScript/Vite, vitest (happy-dom). No new deps, no backend.

**Spec:** [`docs/superpowers/specs/2026-06-01-practice-ui-overhaul-design.md`](../specs/2026-06-01-practice-ui-overhaul-design.md) §C. Plan C of A→B→C→D; A and B already shipped.

---

## What exists now (verified — read `frontend/src/em-suite-matrix.ts` + `em-suite-matrix.test.ts`)

- `em-suite-matrix.ts` exports: `formatMatrixCell(value_ms: number|null): string` ("25.6s" / "—"), `isAlphaPairValid(fastIdx, slowIdx)` (fast>slow), and `renderEmSuiteMatrix(host, data)` which draws a header + a `sample(0)` baseline row + a 10×10 grid.
- `renderEmSuiteMatrix` is called by `em-suite-panel.ts` (`loadAndRenderEmSuitePanel` → `renderEmSuiteMatrix(matrixHost, data)`), per SSE push, inside the practice card. **Keep the export name and signature** so `em-suite-panel.ts` needs no change.
- `EmSuiteMatrixResponse` (TS type, from `./types`) has: `alpha_grid: number[]`, `baseline: (number|null)[]` (per-α `sample(0)` expected ms; index-aligned to `alpha_grid`), `matrix: (number|null)[][]`, `n_attempts_total: number`, `n_successes`, `n_deaths`, `param_history`, `slope_matrices`.
- `ALPHA_GRID` (backend, for reference) = `(0.0, 0.01, 0.02, 0.03, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0)` ascending. Higher α = shorter memory. The frontend reads `data.alpha_grid` — do NOT hardcode it.
- `em-suite-matrix.test.ts` tests only `formatMatrixCell` and `isAlphaPairValid` (keep those tests).
- The existing matrix CSS lives under `.em-suite-matrix*` in `frontend/style.css`.

## Design

- **Window from α:** `α === 0 → "all-time"` (infinite memory); else `round(1/α)` attempts. Labels: `α 0 → "all-time"`, `α 1.0 → "last 1"`, otherwise `"last ~N"`. (e.g. 0.2→"last ~5", 0.05→"last ~20", 0.01→"last ~100".)
- **Defaults:** Now = the α nearest `0.2`; Baseline = the α nearest `0.05` (found by value in `data.alpha_grid`, not hardcoded index).
- **Sufficiency / grey-out:** a window is "distinct" only if its length ≤ your attempts: `α === 0 || round(1/α) <= n_attempts_total`. Insufficient windows aren't *wrong* — they collapse toward the all-time average — so they stay **selectable** but are annotated (e.g. "≈ all-time, only 8 so far"), not disabled. This is the principled answer to "which windows are working": ones short enough that your data fills them.
- **Readout:** for the chosen Now and Baseline, show each window's expected segment time = `formatMatrixCell(baseline[idx])`, with the sufficiency note. The 10×10 grid and the slope heatmaps are not part of this picker (the grid is replaced; the separate `slope_matrices` render in `em-suite-panel.ts` is out of scope for C — left as-is).
- **Interactivity:** changing a dropdown updates the readout from the in-memory payload (no fetch). The panel re-renders per SSE push (existing behaviour), which resets the pickers to defaults each attempt — acceptable for C; picker-state persistence is a Plan-D concern.

---

## Task 1: Rewrite `renderEmSuiteMatrix` as a window picker + helpers + tests

**Files:**
- Modify: `frontend/src/em-suite-matrix.ts`
- Modify: `frontend/src/em-suite-matrix.test.ts`

- [ ] **Step 1 (Red): add tests for the new helpers + picker render.** Append to `frontend/src/em-suite-matrix.test.ts` (keep the existing `formatMatrixCell` / `isAlphaPairValid` describes):

```typescript
import {
  windowLabel, windowAttempts, isWindowSufficient, renderEmSuiteMatrix,
} from "./em-suite-matrix";
import type { EmSuiteMatrixResponse } from "./types";

describe("windowLabel / windowAttempts / isWindowSufficient", () => {
  it("maps alpha to a memory-window label", () => {
    expect(windowLabel(0)).toBe("all-time");
    expect(windowLabel(1)).toBe("last 1");
    expect(windowLabel(0.2)).toBe("last ~5");
    expect(windowLabel(0.05)).toBe("last ~20");
  });
  it("maps alpha to an attempt count (Infinity for all-time)", () => {
    expect(windowAttempts(0)).toBe(Infinity);
    expect(windowAttempts(0.2)).toBe(5);
    expect(windowAttempts(0.01)).toBe(100);
  });
  it("a window is sufficient only when no longer than the attempts seen", () => {
    expect(isWindowSufficient(0, 8)).toBe(true);       // all-time always
    expect(isWindowSufficient(0.2, 8)).toBe(true);     // 5 <= 8
    expect(isWindowSufficient(0.05, 8)).toBe(false);   // 20 > 8
  });
});

const MATRIX: EmSuiteMatrixResponse = {
  segment_id: "s1",
  alpha_grid: [0.0, 0.01, 0.02, 0.03, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0],
  // index-aligned per-alpha sample(0); fill plausible values, nulls allowed.
  baseline: [24000, 23800, 23600, 23400, 24000, 22500, 22000, 21500, 21000, 20800],
  matrix: [],
  n_attempts_total: 8, n_successes: 5, n_deaths: 3,
  param_history: {} as never,
  slope_matrices: {} as never,
};

describe("renderEmSuiteMatrix (window picker)", () => {
  it("renders Now and Baseline pickers defaulting to last~5 / last~20", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderEmSuiteMatrix(host, MATRIX);
    const now = host.querySelector<HTMLSelectElement>("#ems-now")!;
    const base = host.querySelector<HTMLSelectElement>("#ems-baseline")!;
    expect(now).not.toBeNull();
    expect(base).not.toBeNull();
    // Default Now = alpha 0.2 (index 6), Baseline = alpha 0.05 (index 4).
    expect(now.value).toBe("6");
    expect(base.value).toBe("4");
  });

  it("shows each chosen window's expected time in seconds and no 'undefined'", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderEmSuiteMatrix(host, MATRIX);
    const readout = host.querySelector("#ems-readout")!.textContent || "";
    expect(readout).toContain("22.0s");  // Now (alpha 0.2) baseline[6]
    expect(readout).toContain("24.0s");  // Baseline (alpha 0.05) baseline[4]
    expect(readout).not.toContain("undefined");
  });

  it("annotates an insufficient window (longer than attempts seen)", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderEmSuiteMatrix(host, MATRIX);  // n=8; baseline window (20) is insufficient
    const readout = host.querySelector("#ems-readout")!.textContent || "";
    expect(readout.toLowerCase()).toContain("all-time"); // "≈ all-time" note for the 20-window
  });
});
```

- [ ] **Step 2: Run — Red.** `cd frontend && npm test -- em-suite-matrix` → fails (helpers + new render behavior missing).

- [ ] **Step 3 (Green): rewrite `em-suite-matrix.ts`.** Keep the file header comment intent but update it. Keep `formatMatrixCell` and `isAlphaPairValid` exactly as they are. Replace the `renderEmSuiteMatrix` function and add the helpers + default-α constants:

```typescript
// Default memory windows (by alpha value, matched against data.alpha_grid):
// Now ≈ last 5 attempts (current skill), Baseline ≈ last 20 (stable reference).
const DEFAULT_NOW_ALPHA = 0.2;
const DEFAULT_BASELINE_ALPHA = 0.05;

/** Memory window length (attempts) for an alpha. alpha 0 = all-time (Infinity). */
export function windowAttempts(alpha: number): number {
  return alpha <= 0 ? Infinity : Math.round(1 / alpha);
}

/** Plain-English memory-window label for an alpha. */
export function windowLabel(alpha: number): string {
  if (alpha <= 0) return "all-time";
  const n = Math.round(1 / alpha);
  return n === 1 ? "last 1" : `last ~${n}`;
}

/** A window is "distinct" only if it's no longer than the attempts seen;
 * otherwise it collapses toward the all-time average. */
export function isWindowSufficient(alpha: number, nAttempts: number): boolean {
  return alpha <= 0 || windowAttempts(alpha) <= nAttempts;
}

/** Index in alpha_grid of the alpha nearest `target` (defaults pinned by value,
 * not position, so a grid change can't silently shift the default). */
function nearestAlphaIdx(grid: number[], target: number): number {
  let best = 0;
  let bestDist = Infinity;
  grid.forEach((a, i) => {
    const d = Math.abs(a - target);
    if (d < bestDist) { bestDist = d; best = i; }
  });
  return best;
}

function optionsHtml(grid: number[], nAttempts: number, selectedIdx: number): string {
  return grid
    .map((a, i) => {
      const suffix = isWindowSufficient(a, nAttempts) ? "" : " · ≈ all-time";
      const sel = i === selectedIdx ? " selected" : "";
      return `<option value="${i}"${sel}>${windowLabel(a)}${suffix}</option>`;
    })
    .join("");
}

function renderReadout(
  el: HTMLElement, data: EmSuiteMatrixResponse, nowIdx: number, baseIdx: number,
): void {
  const line = (kind: string, idx: number) => {
    const a = data.alpha_grid[idx];
    const ok = isWindowSufficient(a, data.n_attempts_total);
    const note = ok ? "" :
      ` <span class="ems-note">(≈ all-time — only ${data.n_attempts_total} attempt${data.n_attempts_total === 1 ? "" : "s"} so far)</span>`;
    return `<div class="ems-line"><span class="ems-kind">${kind}</span>` +
      `<span class="ems-win">${windowLabel(a)}</span>` +
      `<span class="ems-val">${formatMatrixCell(data.baseline[idx] ?? null)}</span>${note}</div>`;
  };
  el.innerHTML = line("Now", nowIdx) + line("Baseline", baseIdx);
}

export function renderEmSuiteMatrix(
  host: HTMLElement,
  data: EmSuiteMatrixResponse,
): void {
  host.innerHTML = "";
  const wrapper = document.createElement("div");
  wrapper.className = "ems-windows";

  const header = document.createElement("div");
  header.className = "ems-windows__header";
  header.textContent =
    `Skill windows — n=${data.n_attempts_total} (${data.n_successes}S / ${data.n_deaths}D)`;
  wrapper.appendChild(header);

  const nowIdx = nearestAlphaIdx(data.alpha_grid, DEFAULT_NOW_ALPHA);
  const baseIdx = nearestAlphaIdx(data.alpha_grid, DEFAULT_BASELINE_ALPHA);

  const pickers = document.createElement("div");
  pickers.className = "ems-pickers";
  pickers.innerHTML = `
    <label>Now
      <select id="ems-now">${optionsHtml(data.alpha_grid, data.n_attempts_total, nowIdx)}</select>
    </label>
    <label>Baseline
      <select id="ems-baseline">${optionsHtml(data.alpha_grid, data.n_attempts_total, baseIdx)}</select>
    </label>
  `;
  wrapper.appendChild(pickers);

  const readout = document.createElement("div");
  readout.className = "ems-readout";
  readout.id = "ems-readout";
  wrapper.appendChild(readout);

  host.appendChild(wrapper);

  const selById = (id: string) =>
    parseInt(wrapper.querySelector<HTMLSelectElement>(id)!.value, 10);
  const update = () =>
    renderReadout(readout, data, selById("#ems-now"), selById("#ems-baseline"));
  wrapper.querySelector("#ems-now")!.addEventListener("change", update);
  wrapper.querySelector("#ems-baseline")!.addEventListener("change", update);
  update();
}
```

(Removed: the old baseline row + 10×10 grid build. `formatMatrixCell` and `isAlphaPairValid` stay exported — `formatMatrixCell` is reused by the readout; `isAlphaPairValid` remains a tested public helper.)

- [ ] **Step 4: Run — Green.** `cd frontend && npm test -- em-suite-matrix` → all pass; `npm test` → full vitest green; `npm run typecheck` → clean.

- [ ] **Step 5: Commit.**

```bash
git add frontend/src/em-suite-matrix.ts frontend/src/em-suite-matrix.test.ts
git commit -m "feat(em-suite): replace 10x10 matrix with a Now/Baseline memory-window picker"
```

---

## Task 2: CSS for the window picker

**Files:**
- Modify: `frontend/style.css`

- [ ] **Step 1: Append styles** at the end of `frontend/style.css` (theme vars; the old `.em-suite-matrix*` rules can stay — now unused but harmless):

```css
/* EMA-suite memory-window picker (replaces the 10x10 matrix) */
.ems-windows { margin: 6px 0; }
.ems-windows__header {
  font-size: 11px; text-transform: uppercase; color: var(--text-dim);
  letter-spacing: 0.5px; margin-bottom: 6px;
}
.ems-pickers { display: flex; gap: 14px; flex-wrap: wrap; font-size: 11px; color: var(--text-dim); }
.ems-pickers label { display: flex; flex-direction: column; gap: 2px; }
.ems-pickers select {
  background: var(--bg); color: var(--text); border: 1px solid var(--card);
  border-radius: 4px; font-family: inherit; font-size: 11px; padding: 2px 4px;
}
.ems-readout { margin-top: 8px; }
.ems-line { display: flex; align-items: baseline; gap: 8px; font-size: 12px; padding: 2px 0; }
.ems-kind { color: var(--text-dim); min-width: 4.5em; }
.ems-win { color: var(--text); flex: 0 0 auto; }
.ems-val { color: var(--accent); font-variant-numeric: tabular-nums; }
.ems-note { color: var(--text-dim); font-size: 10px; }
```

- [ ] **Step 2: Build.** `cd frontend && npm run build` → clean.

- [ ] **Step 3: Commit.**

```bash
git add frontend/style.css
git commit -m "style(em-suite): memory-window picker styles"
```

---

## Task 3: Verification

**Files:** none (verification only).

- [ ] **Step 1: Frontend checks.** `cd frontend && npm run typecheck && npm test` → clean + green (incl. the new em-suite-matrix tests).

- [ ] **Step 2: Full unfiltered gate** (project rule before merge): `python -m pytest` → green (requires RetroArch; no live dashboard on NCI 55355). No Python changed, so the count should match the prior baseline.

- [ ] **Step 3: (Optional) confirm em-suite-panel still wires.** Grep that `em-suite-panel.ts` still imports/calls `renderEmSuiteMatrix` and that the rename-free signature held: `grep -n "renderEmSuiteMatrix" frontend/src/em-suite-panel.ts`. No change expected.

(No new Python/Playwright smoke test: the em-suite panel only renders in live practice/hyper_play mode, which the fake-backend smoke harness doesn't enter, so there's no headless surface to assert. The picker is covered by the Task-1 vitest. Note this gap rather than fake a test.)

---

## Self-review notes

- **Spec §C coverage:** memory-window labels ✓, Now/Baseline defaults (last-5 / last-20) ✓, grey/annotate insufficient ✓ (`isWindowSufficient`), expected time per window ✓. The 10×10 matrix is gone.
- **No backend, A untouched:** consumes the existing matrix payload's `baseline[]` + `alpha_grid` + `n_attempts_total`; `segment_progress`/`/progress`/`improvement-view.ts` are not modified (agreed scope).
- **No magic numbers:** `DEFAULT_NOW_ALPHA`/`DEFAULT_BASELINE_ALPHA` named with rationale; defaults pinned by α *value* via `nearestAlphaIdx` so a grid change can't silently shift them; the sufficiency rule (`window ≤ attempts`) is principled, not an invented cutoff.
- **Readability §D:** seconds via `formatMatrixCell`; no scientific notation; insufficient windows annotated in plain words rather than hidden.
- **Known limitation (documented, not hidden):** the panel re-renders per SSE push, resetting the pickers to defaults each attempt; picker-state persistence is deferred to Plan D.

## Out of scope (later)
- Wiring the picker to drive Plan A's improvement-view Now/Baseline (deferred to Plan D, after A is live-validated).
- The `slope_matrices` heatmap render in `em-suite-panel.ts` (the spec drops it; do it in D or a follow-up).
- Persisting the picker selection across SSE re-renders.

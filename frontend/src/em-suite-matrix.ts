/**
 * EMA-suite memory-window picker renderer.
 *
 * See docs/superpowers/specs/2026-06-01-practice-ui-overhaul-design.md §C.
 *
 * Renders a Now / Baseline memory-window picker where the user selects
 * a memory window (e.g. "last ~5", "last ~20", "all-time") for each
 * reference point, and sees the expected segment time for each chosen
 * window derived from data.baseline[].
 */
import type { EmSuiteMatrixResponse } from "./types";

const PLACEHOLDER = "—";

/** Format a millisecond value as seconds with one decimal, or em-dash. */
export function formatMatrixCell(value_ms: number | null): string {
  if (value_ms === null || !Number.isFinite(value_ms)) {
    return PLACEHOLDER;
  }
  return `${(value_ms / 1000).toFixed(1)}s`;
}

/** Pair is valid if fast > slow (upper triangle of the matrix). */
export function isAlphaPairValid(fastIdx: number, slowIdx: number): boolean {
  return fastIdx > slowIdx;
}

// Default memory windows (by alpha value, matched against data.alpha_grid):
// Now ~ last 5 attempts (current skill), Baseline ~ last 20 (stable reference).
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
    const a = data.alpha_grid[idx]!;  // idx is always a valid option position
    const ok = isWindowSufficient(a, data.n_attempts_total);
    const note = ok ? "" :
      ` <span class="ems-note">(≈ all-time — only ${data.n_attempts_total} attempt${data.n_attempts_total === 1 ? "" : "s"} so far)</span>`;
    return `<div class="ems-line"><span class="ems-kind">${kind}</span>` +
      `<span class="ems-win">${windowLabel(a)}</span>` +
      `<span class="ems-val">${formatMatrixCell(data.baseline[idx] ?? null)}</span>${note}</div>`;
  };
  el.innerHTML = line("Now", nowIdx) + line("Baseline", baseIdx);
}

/**
 * Render the memory-window picker into a host element. Clears and redraws —
 * safe to call repeatedly with new data.
 */
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

  // Explicitly set .value after appending to DOM so the reflected IDL attribute
  // matches the selected option (some environments need this post-insertion).
  const nowSel = wrapper.querySelector<HTMLSelectElement>("#ems-now")!;
  const baseSel = wrapper.querySelector<HTMLSelectElement>("#ems-baseline")!;
  nowSel.value = String(nowIdx);
  baseSel.value = String(baseIdx);

  const selById = (id: string) =>
    parseInt(wrapper.querySelector<HTMLSelectElement>(id)!.value, 10);
  const update = () =>
    renderReadout(readout, data, selById("#ems-now"), selById("#ems-baseline"));
  nowSel.addEventListener("change", update);
  baseSel.addEventListener("change", update);
  update();
}

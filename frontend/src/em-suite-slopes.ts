/**
 * Per-quantity slope-matrix heatmaps for the EMA-suite sampler.
 *
 * Renders three 10×10 upper-triangular grids — one for each of
 * `slope_log_success`, `slope_log_death`, `slope_logit_p`. Cells are
 * signed-color: strongly negative = green (improving), strongly positive
 * = red (regressing), pale at zero. Color saturation is normalized
 * per-quantity to its visible max-abs so weak signals stay visible when
 * one cell dominates.
 *
 * Cell numeric text shows the raw slope to two decimals in the quantity's
 * working space (log-time or logit-p). Blank cells (diagonal + lower
 * triangle) read empty.
 */
import type { EmSuiteMatrixResponse } from "./types";

const SLOPE_KEYS: Array<{
  key: "slope_log_success" | "slope_log_death" | "slope_logit_p";
  title: string;
  hint: string;
}> = [
  {
    key: "slope_log_success",
    title: "slope · log success time",
    hint: "negative (green) = getting faster",
  },
  {
    key: "slope_log_death",
    title: "slope · log death time",
    hint: "negative (green) = dying earlier (often: getting worse on the early stuff)",
  },
  {
    key: "slope_logit_p",
    title: "slope · logit p(die)",
    hint: "negative (green) = dying less often",
  },
];

// Per-quantity max-abs floor — keeps near-zero matrices from rendering as
// a screaming gradient. The floor is a small magnitude in the working
// space (log-time / logit) below which we say "the trend is essentially
// noise, don't paint it."
const COLOR_FLOOR = 0.05;

// Saturation/lightness for the heatmap fills. Lightness floor stays above
// pure black so the numeric text remains legible.
const HUE_NEG = 130; // green (improving)
const HUE_POS = 0;   // red (regressing)
const SAT_PERCENT = 70;
const LIGHT_BASE = 25;
const LIGHT_RANGE = 25;

function cellColor(value: number | null, maxAbs: number): string {
  if (value == null || !Number.isFinite(value) || maxAbs <= 0) {
    return "#222";
  }
  const t = Math.min(1, Math.abs(value) / maxAbs);
  const hue = value < 0 ? HUE_NEG : HUE_POS;
  const light = LIGHT_BASE + LIGHT_RANGE * t;
  return `hsl(${hue}, ${SAT_PERCENT}%, ${light}%)`;
}

function formatSlopeCell(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return "";
  return value.toFixed(2);
}

function maxAbsOver(grid: (number | null)[][]): number {
  let m = 0;
  for (const row of grid) {
    for (const v of row) {
      if (v == null || !Number.isFinite(v)) continue;
      const a = Math.abs(v);
      if (a > m) m = a;
    }
  }
  return Math.max(m, COLOR_FLOOR);
}

function renderOneGrid(
  host: HTMLElement,
  title: string,
  hint: string,
  alphaGrid: number[],
  grid: (number | null)[][],
): void {
  const wrapper = document.createElement("div");
  wrapper.className = "em-suite-slopes__panel";

  const header = document.createElement("div");
  header.className = "em-suite-slopes__title";
  header.textContent = title;
  wrapper.appendChild(header);

  const sub = document.createElement("div");
  sub.className = "em-suite-slopes__hint";
  sub.textContent = hint;
  wrapper.appendChild(sub);

  const maxAbs = maxAbsOver(grid);

  const tbl = document.createElement("div");
  tbl.className = "em-suite-slopes__grid";
  tbl.style.gridTemplateColumns = `auto repeat(${alphaGrid.length}, 1fr)`;

  // Top header row.
  const corner = document.createElement("span");
  corner.className = "em-suite-slopes__corner";
  corner.textContent = "fast \\ slow";
  tbl.appendChild(corner);
  for (const alpha of alphaGrid) {
    const head = document.createElement("span");
    head.className = "em-suite-slopes__col-header";
    head.textContent = String(alpha);
    tbl.appendChild(head);
  }

  for (let fastIdx = 0; fastIdx < alphaGrid.length; fastIdx++) {
    const rowLabel = document.createElement("span");
    rowLabel.className = "em-suite-slopes__row-header";
    rowLabel.textContent = String(alphaGrid[fastIdx]);
    tbl.appendChild(rowLabel);
    for (let slowIdx = 0; slowIdx < alphaGrid.length; slowIdx++) {
      const cell = document.createElement("span");
      cell.className = "em-suite-slopes__cell";
      const value = grid[fastIdx]?.[slowIdx] ?? null;
      const isBlank = slowIdx >= fastIdx;
      if (isBlank) {
        cell.classList.add("em-suite-slopes__cell--blank");
      } else {
        cell.style.background = cellColor(value, maxAbs);
        cell.textContent = formatSlopeCell(value);
      }
      tbl.appendChild(cell);
    }
  }
  wrapper.appendChild(tbl);
  host.appendChild(wrapper);
}

/**
 * Render three slope-matrix heatmaps into `host`. Safe to call repeatedly:
 * clears and rebuilds. No teardown needed — pure DOM.
 */
export function renderSlopeMatrices(
  host: HTMLElement,
  data: EmSuiteMatrixResponse,
): void {
  host.innerHTML = "";
  host.classList.add("em-suite-slopes");

  for (const { key, title, hint } of SLOPE_KEYS) {
    const grid = data.slope_matrices[key] ?? [];
    if (grid.length === 0) continue;
    renderOneGrid(host, title, hint, data.alpha_grid, grid);
  }
}

// Exposed for unit testing.
export const __test__ = { cellColor, formatSlopeCell, maxAbsOver };

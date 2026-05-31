/**
 * EMA-suite per-segment prediction matrix renderer.
 *
 * See docs/superpowers/specs/2026-05-30-em-suite-sampler-design.md.
 *
 * Renders an upper-triangular 10x10 grid where rows are alpha_fast and
 * columns are alpha_slow. The cell value is the expected (mean) episode
 * time in seconds. A baseline row above the grid shows sample(0) per
 * alpha (no slope applied).
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

/**
 * Render the matrix into a host element. Clears and redraws — safe to
 * call repeatedly with new data.
 */
export function renderEmSuiteMatrix(
  host: HTMLElement,
  data: EmSuiteMatrixResponse,
): void {
  host.innerHTML = "";

  const wrapper = document.createElement("div");
  wrapper.className = "em-suite-matrix";

  const header = document.createElement("div");
  header.className = "em-suite-matrix__header";
  header.textContent =
    `EMA-suite matrix — n=${data.n_attempts_total} ` +
    `(${data.n_successes}S / ${data.n_deaths}D)`;
  wrapper.appendChild(header);

  // Baseline row (sample(0), no slope) — one value per alpha
  const baselineRow = document.createElement("div");
  baselineRow.className = "em-suite-matrix__baseline";
  const baselineLabel = document.createElement("span");
  baselineLabel.className = "em-suite-matrix__label";
  baselineLabel.textContent = "sample(0)";
  baselineRow.appendChild(baselineLabel);
  for (const value of data.baseline) {
    const cell = document.createElement("span");
    cell.className = "em-suite-matrix__cell em-suite-matrix__cell--baseline";
    cell.textContent = formatMatrixCell(value);
    baselineRow.appendChild(cell);
  }
  wrapper.appendChild(baselineRow);

  // Main grid: matrix[fastIdx][slowIdx]
  const grid = document.createElement("div");
  grid.className = "em-suite-matrix__grid";
  grid.style.gridTemplateColumns = `auto repeat(${data.alpha_grid.length}, 1fr)`;

  // Top header row: alpha_slow column labels
  const corner = document.createElement("span");
  corner.className = "em-suite-matrix__corner";
  corner.textContent = "fast \\ slow";
  grid.appendChild(corner);
  for (const alpha of data.alpha_grid) {
    const head = document.createElement("span");
    head.className = "em-suite-matrix__col-header";
    head.textContent = String(alpha);
    grid.appendChild(head);
  }

  for (let fastIdx = 0; fastIdx < data.alpha_grid.length; fastIdx++) {
    const rowLabel = document.createElement("span");
    rowLabel.className = "em-suite-matrix__row-header";
    rowLabel.textContent = String(data.alpha_grid[fastIdx]);
    grid.appendChild(rowLabel);
    for (let slowIdx = 0; slowIdx < data.alpha_grid.length; slowIdx++) {
      const cell = document.createElement("span");
      cell.className = "em-suite-matrix__cell";
      if (!isAlphaPairValid(fastIdx, slowIdx)) {
        cell.classList.add("em-suite-matrix__cell--blank");
        cell.textContent = "";
      } else {
        const cellValue = data.matrix[fastIdx]?.[slowIdx] ?? null;
        cell.textContent = formatMatrixCell(cellValue);
      }
      grid.appendChild(cell);
    }
  }

  wrapper.appendChild(grid);
  host.appendChild(wrapper);
}

/**
 * Per-attempt time series of the three EMA-suite quantities.
 *
 * Renders three stacked Chart.js line charts (p_die, T_s, T_d), each with
 * 10 lines — one per α decay rate, gradient-colored from slow (cool) to
 * fast (warm). x-axis is the attempt index (snapshot number in the
 * backend's param_history payload).
 *
 * Backend ships log(time_ms) for the two time quantities (matches the
 * EMA's internal accumulation space); this renderer exponentiates for
 * display. p_die is shipped as the raw EMA in [0, 1].
 */
import {
  Chart,
  LineController,
  LineElement,
  PointElement,
  LinearScale,
  LogarithmicScale,
  CategoryScale,
  Legend,
  Tooltip,
} from "chart.js";
import type { EmSuiteMatrixResponse } from "./types";

Chart.register(
  LineController, LineElement, PointElement,
  LinearScale, LogarithmicScale, CategoryScale, Legend, Tooltip,
);

// Slow α (idx 0) → blue. Fast α (idx 9) → red. HSL hue from 240° to 0°
// at fixed saturation/lightness so the gradient stays perceptually even.
const HUE_SLOW = 240;
const HUE_FAST = 0;
const SAT_PERCENT = 70;
const LIGHT_PERCENT = 50;
const LINE_WIDTH_PX = 1.5;

function alphaColor(idx: number, n: number): string {
  const t = n <= 1 ? 0 : idx / (n - 1);
  const hue = HUE_SLOW + (HUE_FAST - HUE_SLOW) * t;
  return `hsl(${hue}, ${SAT_PERCENT}%, ${LIGHT_PERCENT}%)`;
}

interface ChartSpec {
  title: string;
  yScale: "linear" | "logarithmic";
  yLabel: string;
  // Backend rows are per-α; transform converts a backend cell value to the
  // display value (exp for log-times, identity for p_die).
  transform: (raw: number | null) => number | null;
}

const P_DIE_SPEC: ChartSpec = {
  title: "p_die",
  yScale: "linear",
  yLabel: "p(die)",
  transform: (v) => v,
};
const T_S_SPEC: ChartSpec = {
  title: "success time (ms)",
  yScale: "logarithmic",
  yLabel: "ms",
  transform: (v) => (v == null ? null : Math.exp(v)),
};
const T_D_SPEC: ChartSpec = {
  title: "death time (ms)",
  yScale: "logarithmic",
  yLabel: "ms",
  transform: (v) => (v == null ? null : Math.exp(v)),
};

function buildOneChart(
  canvas: HTMLCanvasElement,
  spec: ChartSpec,
  alphaGrid: number[],
  perAlphaRows: (number | null)[][],
): Chart {
  const nSnapshots = perAlphaRows[0]?.length ?? 0;
  // x-axis labels: snapshot 0 (initial state) through n_attempts. Show as
  // attempt index; snapshot 0 reads "0" (pre-data baseline).
  const labels = Array.from({ length: nSnapshots }, (_, k) => String(k));

  const datasets = perAlphaRows.map((row, idx) => ({
    label: `α=${alphaGrid[idx]}`,
    data: row.map(spec.transform),
    borderColor: alphaColor(idx, alphaGrid.length),
    backgroundColor: alphaColor(idx, alphaGrid.length),
    borderWidth: LINE_WIDTH_PX,
    pointRadius: 0,
    tension: 0.1,
    spanGaps: false, // honest gaps where α has no data yet
  }));

  return new Chart(canvas, {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      scales: {
        x: { title: { display: true, text: "attempt #" } },
        y: {
          type: spec.yScale,
          title: { display: true, text: spec.yLabel },
        },
      },
      plugins: {
        title: { display: true, text: spec.title },
        legend: { display: false }, // 10 lines is too many for a legend
        tooltip: {
          mode: "nearest",
          intersect: false,
        },
      },
    },
  });
}

/**
 * Render the three-quantity param-history panel into `host`. Returns the
 * three Chart instances so the caller can destroy() them on teardown.
 *
 * Safe to call repeatedly: clears `host` and rebuilds. Any prior charts
 * must be destroyed by the caller BEFORE re-rendering to avoid Chart.js
 * holding references to detached canvases.
 */
export function renderParamHistory(
  host: HTMLElement,
  data: EmSuiteMatrixResponse,
): Chart[] {
  host.innerHTML = "";
  host.classList.add("em-suite-params");

  const charts: Chart[] = [];
  const specs: Array<[ChartSpec, (number | null)[][]]> = [
    [P_DIE_SPEC, data.param_history["p_die"] ?? []],
    [T_S_SPEC, data.param_history["log_success_time"] ?? []],
    [T_D_SPEC, data.param_history["log_death_time"] ?? []],
  ];

  for (const [spec, rows] of specs) {
    const panel = document.createElement("div");
    panel.className = "em-suite-params__chart";
    const canvas = document.createElement("canvas");
    panel.appendChild(canvas);
    host.appendChild(panel);
    if (rows.length === 0) continue;
    charts.push(buildOneChart(canvas, spec, data.alpha_grid, rows));
  }
  return charts;
}

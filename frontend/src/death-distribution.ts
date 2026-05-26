// Fixed bin count. Twenty is enough to see distribution shape in a
// segment detail card, few enough to read at a glance, and predictable
// (no Freedman-Diaconis surprises across segments).
export const BIN_COUNT = 20;

// One-second rounding for the upper edge so x-axis labels land on
// whole seconds without manual tick configuration.
const HI_ROUND_MS = 1000;

export interface Bin {
  // Left edge (inclusive) and right edge (exclusive) of this bin, in ms.
  // The topmost bin's right edge is inclusive (clamped).
  lo_ms: number;
  hi_ms: number;
  deaths: number;
  completions: number;
}

export interface BinSummary {
  bins: Bin[];
  lo: number;
  hi: number;
}

/**
 * Bucket death and completion samples into shared bins by time_ms.
 * Sample weights are ignored — bar heights are raw counts. Weighted
 * statistics (means) are surfaced via marker overlays, not bar height.
 *
 * The range always starts at 0; the upper edge is the max sample value
 * rounded up to the nearest second. Empty input returns BIN_COUNT
 * zero-counts with lo=hi=0.
 */
export function binSamples(
  deaths: [number, number][],
  completions: [number, number][],
): BinSummary {
  const empty = (): Bin[] => Array.from({ length: BIN_COUNT }, (_, i) => ({
    lo_ms: 0, hi_ms: 0, deaths: 0, completions: 0,
  }));

  if (deaths.length === 0 && completions.length === 0) {
    return { bins: empty(), lo: 0, hi: 0 };
  }

  let maxMs = 0;
  for (const [t] of deaths) if (t > maxMs) maxMs = t;
  for (const [t] of completions) if (t > maxMs) maxMs = t;
  const hi = Math.ceil(maxMs / HI_ROUND_MS) * HI_ROUND_MS;
  const lo = 0;
  const width = (hi - lo) / BIN_COUNT;

  const bins: Bin[] = Array.from({ length: BIN_COUNT }, (_, i) => ({
    lo_ms: lo + i * width,
    hi_ms: lo + (i + 1) * width,
    deaths: 0,
    completions: 0,
  }));

  const placeIdx = (t: number): number => {
    if (width === 0) return 0;
    let idx = Math.floor((t - lo) / width);
    if (idx >= BIN_COUNT) idx = BIN_COUNT - 1;
    if (idx < 0) idx = 0;
    return idx;
  };

  for (const [t] of deaths) bins[placeIdx(t)]!.deaths += 1;
  for (const [t] of completions) bins[placeIdx(t)]!.completions += 1;

  return { bins, lo, hi };
}

import {
  Chart,
  BarController,
  BarElement,
  LinearScale,
  CategoryScale,
  Legend,
  Tooltip,
} from "chart.js";
import { formatTime } from "./format";
import type { components } from "./api-types";

Chart.register(BarController, BarElement, LinearScale, CategoryScale, Legend, Tooltip);

type DeathExtras = components["schemas"]["DeathExtras"];

// Colors chosen to match the conventional red/green of failure/success
// without being saturated enough to fight the existing line-chart palette.
const DEATH_COLOR = "rgba(255, 100, 100, 0.55)";
const DEATH_LINE = "rgba(255, 100, 100, 0.95)";
const COMPLETION_COLOR = "rgba(100, 200, 100, 0.55)";
const COMPLETION_LINE = "rgba(100, 200, 100, 0.95)";

// Offset the label a few pixels right of the marker line so the glyphs
// don't sit on top of the stroke.
const MARKER_LABEL_X_OFFSET_PX = 3;

// Drop the label down from the chart's top edge so it clears the legend
// and any top gridline.
const MARKER_LABEL_Y_OFFSET_PX = 12;

// Small-but-legible default; matches the visual weight of the line chart's
// axis labels above without competing with them.
const MARKER_LABEL_FONT = "11px sans-serif";

// Both the vertical mean-marker line and the bar borders are 1px; if they
// ever diverge, split into two constants.
const MARKER_LINE_WIDTH_PX = 1;

interface MarkerPluginOptions {
  death_ms: number | null;
  completion_ms: number | null;
}

// Inline Chart.js plugin: draws two vertical lines + labels on top of the
// bar chart for the weighted-mean death/completion times. The full chartjs
// plugin-annotation dep is overkill for two lines.
const deathMarkersPlugin = {
  id: "deathMarkers",
  afterDatasetsDraw(chart: Chart) {
    const opts = (chart.options.plugins as Record<string, unknown> | undefined)
      ?.deathMarkers as MarkerPluginOptions | undefined;
    if (!opts) return;
    const { ctx, chartArea, scales } = chart;
    const xScale = scales["x"];
    if (!xScale) return;
    const draw = (ms: number | null, color: string, label: string) => {
      if (ms == null) return;
      const x = xScale.getPixelForValue(ms);
      if (!Number.isFinite(x)) return;
      if (x < chartArea.left || x > chartArea.right) return;
      ctx.save();
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.lineWidth = MARKER_LINE_WIDTH_PX;
      ctx.beginPath();
      ctx.moveTo(x, chartArea.top);
      ctx.lineTo(x, chartArea.bottom);
      ctx.stroke();
      ctx.font = MARKER_LABEL_FONT;
      ctx.fillText(label, x + MARKER_LABEL_X_OFFSET_PX, chartArea.top + MARKER_LABEL_Y_OFFSET_PX);
      ctx.restore();
    };
    draw(opts.death_ms, DEATH_LINE, "μ_d");        // μ_d
    draw(opts.completion_ms, COMPLETION_LINE, "μ_c"); // μ_c
  },
};
Chart.register(deathMarkersPlugin);

let _histChart: Chart | null = null;

function buildHeader(extras: DeathExtras | null): HTMLElement {
  const header = document.createElement("div");
  header.className = "death-distribution-header";
  const title = document.createElement("h4");
  title.textContent = "Death distribution";
  header.appendChild(title);

  if (extras === null) return header;

  const stats = document.createElement("div");
  stats.className = "death-distribution-stats dim";
  const parts: string[] = [];
  parts.push(`halflife: ${extras.halflife_attempts} ep`);
  if (extras.p_die_per_life != null) {
    parts.push(`p(die|life): ${extras.p_die_per_life.toFixed(2)}`);
  }
  if (extras.p_die_per_attempt != null) {
    parts.push(`p(die|attempt): ${extras.p_die_per_attempt.toFixed(2)}`);
  }
  stats.textContent = parts.join("   ");
  header.appendChild(stats);
  return header;
}

function buildEmptyState(): HTMLElement {
  const msg = document.createElement("p");
  msg.className = "dim";
  msg.textContent =
    "No death data — selected estimator doesn’t publish death distributions.";
  return msg;
}

export function renderDeathDistribution(
  container: HTMLElement,
  extras: DeathExtras | null,
): void {
  destroyDeathDistribution();

  const section = document.createElement("section");
  section.className = "death-distribution";
  container.appendChild(section);

  if (extras === null) {
    section.appendChild(buildHeader(null));
    section.appendChild(buildEmptyState());
    return;
  }

  section.appendChild(buildHeader(extras));

  if (extras.death_samples.length === 0 && extras.completion_samples.length === 0) {
    section.appendChild(buildEmptyState());
    return;
  }

  const { bins } = binSamples(extras.death_samples, extras.completion_samples);

  const chartWrap = document.createElement("div");
  chartWrap.className = "chart-wrapper";
  const canvas = document.createElement("canvas");
  canvas.id = "death-histogram";
  chartWrap.appendChild(canvas);
  section.appendChild(chartWrap);

  // Bin centers (ms) so the bar x-position is meaningful on a linear scale.
  const labels = bins.map((b) => (b.lo_ms + b.hi_ms) / 2);
  const deathData = bins.map((b) => b.deaths);
  const completionData = bins.map((b) => b.completions);

  _histChart = new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          label: "deaths",
          data: deathData,
          backgroundColor: DEATH_COLOR,
          borderColor: DEATH_LINE,
          borderWidth: MARKER_LINE_WIDTH_PX,
        },
        {
          label: "completions",
          data: completionData,
          backgroundColor: COMPLETION_COLOR,
          borderColor: COMPLETION_LINE,
          borderWidth: MARKER_LINE_WIDTH_PX,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: {
          title: { display: true, text: "Time" },
          ticks: {
            callback: (_v, idx) => formatTime(labels[idx]!),
          },
        },
        y: {
          title: { display: true, text: "Samples" },
          ticks: { precision: 0 },
        },
      },
      plugins: {
        legend: { position: "top" },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y}`,
          },
        },
        // Chart.js doesn't type custom plugin options; merge as `unknown`
        // so legend/tooltip above keep their inferred types.
        ...({
          deathMarkers: {
            death_ms: extras.expected_death_time_ms,
            completion_ms: extras.expected_completion_time_ms,
          },
        } as Record<string, unknown>),
      },
    },
  });
}

export function destroyDeathDistribution(): void {
  if (_histChart) {
    _histChart.destroy();
    _histChart = null;
  }
}

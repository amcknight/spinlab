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
import type { ColdDistribution } from "./types";

Chart.register(BarController, BarElement, LinearScale, CategoryScale, Legend, Tooltip);

// Conventional failure/success colors at moderate opacity so overlapping
// regions blend visibly.
const DEATH_COLOR = "rgba(255, 100, 100, 0.55)";
const DEATH_LINE = "rgba(255, 100, 100, 0.95)";
const COMPLETION_COLOR = "rgba(100, 200, 100, 0.55)";
const COMPLETION_LINE = "rgba(100, 200, 100, 0.95)";

// Pixel offset for the marker label so it doesn't sit on top of the line.
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
    draw(opts.death_ms, DEATH_LINE, "μ_d");           // μ_d
    draw(opts.completion_ms, COMPLETION_LINE, "μ_c");  // μ_c
  },
};
Chart.register(deathMarkersPlugin);

export function renderColdHistogram(
  canvas: HTMLCanvasElement,
  dist: ColdDistribution,
): Chart {
  const labels = dist.bins.map((b) => formatTime(b.lo_ms));
  const deaths = dist.bins.map((b) => b.n_deaths);
  const completions = dist.bins.map((b) => b.n_completions);

  return new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          label: "Deaths",
          data: deaths,
          backgroundColor: DEATH_COLOR,
          borderColor: DEATH_LINE,
          borderWidth: MARKER_LINE_WIDTH_PX,
        },
        {
          label: "Completions",
          data: completions,
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
            callback: (_v, idx) => formatTime(dist.bins[idx]!.lo_ms),
          },
        },
        y: {
          beginAtZero: true,
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
            death_ms: dist.mu_d_ms,
            completion_ms: dist.mu_c_ms,
          },
        } as Record<string, unknown>),
      },
    },
  });
}

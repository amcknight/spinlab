import {
  Chart,
  BarController,
  BarElement,
  LineController,
  LineElement,
  PointElement,
  LinearScale,
  CategoryScale,
  Legend,
  Tooltip,
} from "chart.js";
import { formatTime } from "./format";
import type { ColdDistribution } from "./types";

Chart.register(
  BarController, BarElement,
  LineController, LineElement, PointElement,
  LinearScale, CategoryScale, Legend, Tooltip,
);

// Conventional failure/success colors at moderate opacity so overlapping
// regions blend visibly.
const DEATH_COLOR = "rgba(255, 100, 100, 0.55)";
const DEATH_LINE = "rgba(255, 100, 100, 0.95)";
const COMPLETION_COLOR = "rgba(100, 200, 100, 0.55)";
const COMPLETION_LINE = "rgba(100, 200, 100, 0.95)";

// PDF overlays: dashed = Normal, solid = Lognormal. Same hue family as the
// underlying histogram so death/completion curves read at a glance.
const NORMAL_DASH: [number, number] = [6, 4];
const PDF_LINE_WIDTH_PX = 2;

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

// Normal PDF evaluated at x, scaled by total_count * bin_width so the curve
// sits on the histogram's count scale rather than a [0,1] density scale.
function normalOverlay(
  centers: number[], binWidth: number, total: number,
  mu: number | null | undefined, sigma: number | null | undefined,
): (number | null)[] {
  if (mu == null || sigma == null || sigma <= 0 || total <= 0 || binWidth <= 0) {
    return centers.map(() => null);
  }
  const norm = total * binWidth / (sigma * Math.sqrt(2 * Math.PI));
  return centers.map((x) => {
    const z = (x - mu) / sigma;
    return norm * Math.exp(-0.5 * z * z);
  });
}

// Lognormal PDF on the same count scale. Undefined at x ≤ 0 (returns null
// there) since ln(x) blows up.
function lognormalOverlay(
  centers: number[], binWidth: number, total: number,
  muLog: number | null | undefined, sigmaLog: number | null | undefined,
): (number | null)[] {
  if (muLog == null || sigmaLog == null || sigmaLog <= 0 || total <= 0 || binWidth <= 0) {
    return centers.map(() => null);
  }
  const norm = total * binWidth / (sigmaLog * Math.sqrt(2 * Math.PI));
  return centers.map((x) => {
    if (x <= 0) return null;
    const z = (Math.log(x) - muLog) / sigmaLog;
    return (norm / x) * Math.exp(-0.5 * z * z);
  });
}

export function renderColdHistogram(
  canvas: HTMLCanvasElement,
  dist: ColdDistribution,
): Chart {
  const labels = dist.bins.map((b) => formatTime(b.lo_ms));
  const deaths = dist.bins.map((b) => b.n_deaths);
  const completions = dist.bins.map((b) => b.n_completions);

  // PDF overlays are evaluated at bin centers (ms) and scaled by
  // total_count * bin_width to live on the same count scale as the bars.
  const centers = dist.bins.map((b) => (b.lo_ms + b.hi_ms) / 2);
  const binWidth = dist.bins.length > 0
    ? dist.bins[0]!.hi_ms - dist.bins[0]!.lo_ms : 0;
  const totalCompletions = completions.reduce((s, n) => s + n, 0);

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
        // Parametric PDF overlays — only on the completion side. Death-time
        // density is hazard(t) * S(t) / P(die), so a Normal/Lognormal curve-
        // fit there is a shape mash-up; the hazard view is the principled
        // tool for that side. Dashed = Normal, solid = Lognormal.
        {
          type: "line",
          label: "Normal (survival)",
          data: normalOverlay(centers, binWidth, totalCompletions, dist.mu_c_ms, dist.sigma_c_ms),
          borderColor: COMPLETION_LINE,
          backgroundColor: "transparent",
          borderWidth: PDF_LINE_WIDTH_PX,
          borderDash: NORMAL_DASH,
          pointRadius: 0,
          tension: 0.2,
        } as any,
        {
          type: "line",
          label: "Lognormal (survival)",
          data: lognormalOverlay(centers, binWidth, totalCompletions, dist.mu_log_c, dist.sigma_log_c),
          borderColor: COMPLETION_LINE,
          backgroundColor: "transparent",
          borderWidth: PDF_LINE_WIDTH_PX,
          pointRadius: 0,
          tension: 0.2,
        } as any,
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

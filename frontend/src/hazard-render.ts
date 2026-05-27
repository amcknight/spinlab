import {
  Chart, BarController, BarElement,
  LinearScale, CategoryScale, Legend, Tooltip,
} from "chart.js";
import { formatTime } from "./format";
import type { ColdDistribution } from "./types";

Chart.register(BarController, BarElement, LinearScale, CategoryScale, Legend, Tooltip);

// Yellow matches the spec's mockup; high enough contrast on dark bg
// without competing with the histogram's red/green.
const HAZARD_RGB = "255, 241, 118";

export function renderHazard(
  canvas: HTMLCanvasElement, dist: ColdDistribution,
): Chart {
  const labels = dist.bins.map((b) => formatTime(b.lo_ms));
  const data = dist.bins.map((b) => b.hazard ?? null);  // null preserved; chart.js skips
  const denom = dist.bins.length > 0 ? dist.bins[0]!.at_risk_w : 0;
  const bg = dist.bins.map((b) => {
    const opacity = denom > 0 ? Math.max(0, Math.min(1, b.at_risk_w / denom)) : 0;
    return `rgba(${HAZARD_RGB}, ${opacity})`;
  });

  return new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "Hazard rate",
        data,
        backgroundColor: bg,
        borderWidth: 0,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        y: { min: 0, max: 1, title: { display: true, text: "Hazard rate" } },
        x: { title: { display: true, text: "Time" } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const b = dist.bins[ctx.dataIndex]!;
              const h = b.hazard == null ? "n/a" : b.hazard.toFixed(2);
              return `hazard: ${h} · at_risk: ${b.at_risk_w.toFixed(1)} (effective)`;
            },
          },
        },
      },
    },
  });
}

import {
  Chart,
  LineController,
  LineElement,
  PointElement,
  LinearScale,
  CategoryScale,
  Legend,
  Tooltip,
} from "chart.js";
import { fetchJSON } from "./api";
import { formatTime } from "./format";
import type { SegmentHistory } from "./types";

Chart.register(LineController, LineElement, PointElement, LinearScale, CategoryScale, Legend, Tooltip);

/** Colors for estimator curves — visually distinct, accessible on dark bg. */
const ESTIMATOR_COLORS = ["#4fc3f7", "#ff8a65", "#81c784", "#ba68c8", "#fff176"];

type SeriesMode = "total" | "clean";

interface ChartDataset {
  label: string;
  data: (number | null)[];
  borderColor: string;
  backgroundColor: string;
  borderWidth: number;
  pointRadius: number;
  tension: number;
}

export function buildChartDatasets(history: SegmentHistory, mode: SeriesMode): ChartDataset[] {
  const datasets: ChartDataset[] = [];

  // Raw attempt points
  const rawData = history.attempts.map((a) => {
    const ms = mode === "total" ? a.time_ms : a.clean_tail_ms;
    return ms != null ? ms / 1000 : null;
  });
  datasets.push({
    label: "Attempts",
    data: rawData,
    borderColor: "rgba(255, 255, 255, 0.5)",
    backgroundColor: "rgba(255, 255, 255, 0.7)",
    borderWidth: 2,
    pointRadius: 4,
    tension: 0,
  });

  // Estimator curves
  const estimatorNames = Object.keys(history.estimator_curves);
  estimatorNames.forEach((name, i) => {
    const curves = history.estimator_curves[name]!;
    const series = mode === "total" ? curves.total : curves.clean;
    datasets.push({
      label: name,
      data: series.expected_ms.map((v) => (v != null ? v / 1000 : null)),
      borderColor: ESTIMATOR_COLORS[i % ESTIMATOR_COLORS.length]!,
      backgroundColor: "transparent",
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.3,
    });
  });

  return datasets;
}

let _chart: Chart | null = null;
let _history: SegmentHistory | null = null;
let _mode: SeriesMode = "total";

export async function renderSegmentDetail(
  container: HTMLElement,
  segmentId: string,
  onBack: () => void,
): Promise<void> {
  container.innerHTML = "";

  // Header with back button
  const header = document.createElement("div");
  header.className = "detail-header";
  const backBtn = document.createElement("button");
  backBtn.className = "btn-back";
  backBtn.textContent = "\u2190 Back";
  backBtn.addEventListener("click", onBack);
  header.appendChild(backBtn);
  const title = document.createElement("span");
  title.className = "detail-title";
  title.textContent = "Loading...";
  header.appendChild(title);
  container.appendChild(header);

  // Toggle buttons
  const toggleRow = document.createElement("div");
  toggleRow.className = "detail-toggle";
  const totalBtn = document.createElement("button");
  totalBtn.textContent = "Total";
  totalBtn.className = "toggle-btn active";
  const cleanBtn = document.createElement("button");
  cleanBtn.textContent = "Clean Tail";
  cleanBtn.className = "toggle-btn";
  toggleRow.appendChild(totalBtn);
  toggleRow.appendChild(cleanBtn);
  container.appendChild(toggleRow);

  // Canvas inside a sized wrapper (Chart.js needs a block-level container with
  // an explicit height when maintainAspectRatio is false, otherwise it grows forever)
  const chartWrap = document.createElement("div");
  chartWrap.className = "chart-wrapper";
  const canvas = document.createElement("canvas");
  canvas.id = "segment-chart";
  chartWrap.appendChild(canvas);
  container.appendChild(chartWrap);

  // Fetch data
  const history = await fetchJSON<SegmentHistory>(
    `/api/segments/${encodeURIComponent(segmentId)}/history`,
  );
  if (!history) {
    title.textContent = "Failed to load";
    return;
  }

  _history = history;
  _mode = "total";
  title.textContent = history.description || segmentId;

  if (history.attempts.length === 0) {
    const msg = document.createElement("p");
    msg.className = "dim";
    msg.textContent = "No completed attempts yet";
    container.appendChild(msg);
    return;
  }

  // Build chart
  const labels = history.attempts.map((a) => String(a.attempt_number));
  _chart = new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: buildChartDatasets(history, "total"),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        y: {
          title: { display: true, text: "Time (s)" },
          ticks: {
            callback: (v) => formatTime(Number(v) * 1000),
          },
        },
        x: {
          title: { display: true, text: "Attempt #" },
        },
      },
      plugins: {
        legend: { position: "top" },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const v = ctx.parsed.y;
              return ctx.dataset.label + ": " + formatTime(v != null ? v * 1000 : null);
            },
          },
        },
      },
    },
  });

  // Wire toggle
  totalBtn.addEventListener("click", () => {
    if (_mode === "total") return;
    _mode = "total";
    totalBtn.classList.add("active");
    cleanBtn.classList.remove("active");
    updateChart();
  });
  cleanBtn.addEventListener("click", () => {
    if (_mode === "clean") return;
    _mode = "clean";
    cleanBtn.classList.add("active");
    totalBtn.classList.remove("active");
    updateChart();
  });
}

function updateChart(): void {
  if (!_chart || !_history) return;
  _chart.data.datasets = buildChartDatasets(_history, _mode);
  _chart.update();
}

export function destroySegmentDetail(): void {
  if (_chart) {
    _chart.destroy();
    _chart = null;
  }
  _history = null;
  _mode = "total";
}

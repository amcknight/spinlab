import { segmentName, formatTime, elapsedStr, formatSavings } from "./format";
import { fetchJSON, postJSON } from "./api";
import { selectedEstimate, currentEstimate, formatTrend, canStartPractice, canStartSpeedRun } from "./model-logic";
import type { AppState, ModelData, TuningData, SessionInfo } from "./types";
import { renderSegmentDetail, destroySegmentDetail } from "./segment-detail";

const ALLOCATOR_COLORS: Record<string, string> = {
  greedy: "#4caf50",
  random: "#2196f3",
  round_robin: "#ff9800",
};
const ALLOCATOR_LABELS: Record<string, string> = {
  greedy: "Greedy",
  random: "Random",
  round_robin: "Round Robin",
};
const ALLOCATOR_ORDER = ["greedy", "random", "round_robin"];

let _currentWeights: Record<string, number> | null = null;
let _tuningParams: TuningData | null = null;
let _tuningDebounce: ReturnType<typeof setTimeout> | null = null;
let _currentSegmentId: string | null = null;
const TUNING_DEBOUNCE_MS = 200;

function debouncedApply(): void {
  if (_tuningDebounce) clearTimeout(_tuningDebounce);
  _tuningDebounce = setTimeout(() => {
    applyTuningParams();
  }, TUNING_DEBOUNCE_MS);
}

function renderWeightSlider(weights: Record<string, number>): void {
  _currentWeights = { ...weights };
  const slider = document.getElementById("weight-slider");
  const legend = document.getElementById("weight-legend");
  if (!slider || !legend) return;

  slider.innerHTML = "";
  legend.innerHTML = "";

  const entries = ALLOCATOR_ORDER.filter((k) => k in weights);

  entries.forEach((name) => {
    const seg = document.createElement("div");
    seg.className = "weight-segment";
    seg.style.flex = String(weights[name]);
    seg.style.background = ALLOCATOR_COLORS[name] ?? "#666";
    seg.dataset.allocator = name;
    slider.appendChild(seg);
  });

  const totalWidth = () => slider.getBoundingClientRect().width;
  for (let i = 0; i < entries.length - 1; i++) {
    const handle = document.createElement("div");
    handle.className = "weight-handle";
    handle.dataset.index = String(i);
    positionHandle(handle, entries, weights);
    slider.appendChild(handle);

    handle.addEventListener("mousedown", (e: MouseEvent) => {
      e.preventDefault();
      handle.classList.add("dragging");
      const left = entries[i]!;
      const right = entries[i + 1]!;
      const startX = e.clientX;
      const startLeftW = weights[left]!;
      const startRightW = weights[right]!;
      const pxPerPercent = totalWidth() / 100;

      const onMove = (ev: MouseEvent) => {
        const dx = ev.clientX - startX;
        const dp = Math.round(dx / pxPerPercent);
        const newLeft = Math.max(0, Math.min(startLeftW + startRightW, startLeftW + dp));
        const newRight = startLeftW + startRightW - newLeft;
        weights[left] = newLeft;
        weights[right] = newRight;
        updateSliderVisuals(entries, weights, slider, legend);
      };
      const onUp = () => {
        handle.classList.remove("dragging");
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        _currentWeights = { ...weights };
        postJSON("/api/allocator-weights", weights);
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  }

  renderLegend(entries, weights, legend);
}

function positionHandle(
  handle: HTMLElement,
  entries: string[],
  weights: Record<string, number>,
): void {
  let cumulative = 0;
  const idx = parseInt(handle.dataset.index!);
  for (let i = 0; i <= idx; i++) cumulative += weights[entries[i]!]!;
  handle.style.left = cumulative + "%";
}

function updateSliderVisuals(
  entries: string[],
  weights: Record<string, number>,
  slider: HTMLElement,
  legend: HTMLElement,
): void {
  const segments = slider.querySelectorAll(".weight-segment") as NodeListOf<HTMLElement>;
  entries.forEach((name, i) => {
    if (segments[i]) segments[i].style.flex = String(weights[name]);
  });
  const handles = slider.querySelectorAll(".weight-handle") as NodeListOf<HTMLElement>;
  handles.forEach((h) => positionHandle(h, entries, weights));
  renderLegend(entries, weights, legend);
}

function renderLegend(
  entries: string[],
  weights: Record<string, number>,
  legend: HTMLElement,
): void {
  legend.innerHTML = "";
  entries.forEach((name) => {
    const item = document.createElement("span");
    item.className = "weight-legend-item";
    const dot = document.createElement("span");
    dot.className = "weight-dot";
    dot.style.background = ALLOCATOR_COLORS[name] ?? "#666";
    item.appendChild(dot);
    item.appendChild(
      document.createTextNode((ALLOCATOR_LABELS[name] ?? name) + " " + weights[name] + "%"),
    );
    legend.appendChild(item);
  });
}

export async function fetchModel(): Promise<void> {
  const data = await fetchJSON<ModelData>("/api/model");
  if (data) updateModel(data);
}

function updateModel(data: ModelData): void {
  const body = document.getElementById("model-body")!;
  if (!data.segments || !data.segments.length) {
    body.innerHTML = '<tr><td colspan="6" class="dim">No game loaded</td></tr>';
    return;
  }
  body.innerHTML = "";
  data.segments.forEach((s) => {
    const tr = document.createElement("tr");
    const est = selectedEstimate(s);

    const nameTd = document.createElement("td");
    const nameLink = document.createElement("a");
    nameLink.href = "#";
    nameLink.textContent = segmentName(s);
    nameLink.addEventListener("click", (e) => {
      e.preventDefault();
      showSegmentDetail(s.segment_id);
    });
    nameTd.appendChild(nameLink);

    const restHtml =
      "<td>" + formatTime(est?.expected_ms ?? null) + "</td>" +
      "<td>" + (formatTrend(est) ?? "\u2014") + "</td>" +
      "<td>" + formatTime(est?.floor_ms ?? null) + "</td>" +
      "<td>" + s.n_completed + "</td>" +
      "<td>" + formatTime(s.gold_ms) + "</td>";

    tr.innerHTML = restHtml;
    tr.prepend(nameTd);
    body.appendChild(tr);
  });

  const estSelect = document.getElementById("estimator-select") as HTMLSelectElement | null;
  if (estSelect && data.estimators) {
    const current = data.estimator || estSelect.value;
    estSelect.innerHTML = "";
    data.estimators.forEach((e) => {
      const opt = document.createElement("option");
      opt.value = e.name;
      opt.textContent = e.display_name;
      estSelect.appendChild(opt);
    });
    estSelect.value = current;
  }
}

function showSegmentDetail(segmentId: string): void {
  _currentSegmentId = segmentId;
  // Hide model content
  (document.getElementById("model-table") as HTMLElement).style.display = "none";
  (document.querySelector(".model-header") as HTMLElement).style.display = "none";
  (document.getElementById("tuning-panel") as HTMLElement).style.display = "none";
  (document.getElementById("practice-controls") as HTMLElement).style.display = "none";
  const practiceCard = document.getElementById("practice-card") as HTMLElement;
  practiceCard.dataset.wasVisible = practiceCard.style.display;
  practiceCard.style.display = "none";

  // Show detail
  const detail = document.getElementById("segment-detail") as HTMLElement;
  detail.style.display = "";
  renderSegmentDetail(detail, segmentId, hideSegmentDetail);
}

function hideSegmentDetail(): void {
  _currentSegmentId = null;
  destroySegmentDetail();

  // Restore model content
  (document.getElementById("model-table") as HTMLElement).style.display = "";
  (document.querySelector(".model-header") as HTMLElement).style.display = "";
  (document.getElementById("tuning-panel") as HTMLElement).style.display = "";
  (document.getElementById("practice-controls") as HTMLElement).style.display = "";
  const practiceCard = document.getElementById("practice-card") as HTMLElement;
  practiceCard.style.display = practiceCard.dataset.wasVisible || "none";

  // Hide detail
  (document.getElementById("segment-detail") as HTMLElement).style.display = "none";

  // Refresh model data
  fetchModel();
}

export function updateSavingsPanel(session: SessionInfo | null): void {
  const panel = document.getElementById("savings-panel") as HTMLElement | null;
  if (!panel) return;

  const totalStr = session ? formatSavings(session.saved_total_ms) : null;
  const cleanStr = session ? formatSavings(session.saved_clean_ms) : null;

  if (totalStr === null && cleanStr === null) {
    panel.style.display = "none";
    return;
  }
  panel.style.display = "";

  const totalEl = document.getElementById("savings-total")!;
  const cleanEl = document.getElementById("savings-clean")!;

  if (totalStr !== null) {
    totalEl.textContent = totalStr + " total";
    totalEl.className =
      "savings-value " + ((session!.saved_total_ms ?? 0) >= 0 ? "positive" : "negative");
  } else {
    totalEl.textContent = "";
    totalEl.className = "savings-value";
  }

  if (cleanStr !== null) {
    cleanEl.textContent = cleanStr + " clean";
    cleanEl.className =
      "savings-value " + ((session!.saved_clean_ms ?? 0) >= 0 ? "positive" : "negative");
  } else {
    cleanEl.textContent = "";
    cleanEl.className = "savings-value";
  }
}

export function updatePracticeCard(data: AppState): void {
  const card = document.getElementById("practice-card") as HTMLElement;
  if ((data.mode !== "practice" && data.mode !== "speed_run") || !data.current_segment) {
    card.style.display = "none";
    return;
  }
  card.style.display = "";
  updateSavingsPanel(data.session);

  const cs = data.current_segment;
  document.getElementById("current-goal")!.textContent = segmentName(cs);
  document.getElementById("current-attempts")!.textContent =
    "Attempt " + (cs.attempt_count || 0);

  const insight = document.getElementById("insight")!;
  const est = currentEstimate(cs);
  const trend = formatTrend(est);
  if (trend) {
    insight.innerHTML = "<span>" + trend + "</span>";
  } else {
    insight.textContent = "No data yet";
  }

  const recent = document.getElementById("recent")!;
  recent.innerHTML = "";
  (data.recent || []).forEach((r) => {
    const li = document.createElement("li");
    if (r.invalidated) {
      li.classList.add("invalidated");
    }
    const time = formatTime(r.time_ms);
    const cls = r.completed ? "ahead" : "behind";
    const btnLabel = r.invalidated ? "Restore" : "Mark invalid";
    const btn = document.createElement("button");
    btn.className = "invalidate-btn";
    btn.textContent = btnLabel;
    btn.addEventListener("click", () => {
      fetch(`/api/attempts/${r.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ invalidated: !r.invalidated }),
      }).catch(() => {
        // Silently ignore network errors; next SSE update will reflect truth.
      });
    });
    li.innerHTML =
      '<span class="' + cls + '">' + time + "</span>" +
      ' <span class="dim">' + segmentName(r) + "</span>";
    li.appendChild(btn);
    recent.appendChild(li);
  });

  const stats = document.getElementById("session-stats");
  if (stats && data.session) {
    stats.textContent =
      (data.session.segments_completed || 0) +
      "/" +
      (data.session.segments_attempted || 0) +
      " cleared | " +
      elapsedStr(data.session.started_at);
  }

  const weightsEl = document.getElementById("allocator-weights") as HTMLElement;
  if (weightsEl) {
    weightsEl.style.display = data.mode === "speed_run" ? "none" : "";
  }
  if (data.allocator_weights && data.mode !== "speed_run") {
    renderWeightSlider(data.allocator_weights);
  }
}

export function updatePracticeControls(data: AppState): void {
  const startBtn = document.getElementById("btn-practice-start") as HTMLButtonElement;
  const stopBtn = document.getElementById("btn-practice-stop") as HTMLElement;
  const srStartBtn = document.getElementById("btn-speedrun-start") as HTMLButtonElement;
  const srStopBtn = document.getElementById("btn-speedrun-stop") as HTMLElement;
  const isPracticing = data.mode === "practice";
  const isSpeedRun = data.mode === "speed_run";

  startBtn.style.display = isPracticing || isSpeedRun ? "none" : "";
  startBtn.disabled = !canStartPractice(data);
  stopBtn.style.display = isPracticing ? "" : "none";

  srStartBtn.style.display = isPracticing || isSpeedRun ? "none" : "";
  srStartBtn.disabled = !canStartSpeedRun(data);
  srStopBtn.style.display = isSpeedRun ? "" : "none";
}

async function fetchTuningParams(): Promise<void> {
  const data = await fetchJSON<TuningData>("/api/estimator-params");
  if (!data) return;
  _tuningParams = data;
  renderTuningParams(data);
}

function renderTuningParams(data: TuningData): void {
  const container = document.getElementById("tuning-params");
  if (!container) return;
  container.innerHTML = "";
  const actions = document.querySelector(".tuning-actions") as HTMLElement | null;
  if (!data.params || data.params.length === 0) {
    container.innerHTML = '<p class="tuning-empty">No tunable parameters</p>';
    if (actions) actions.style.display = "none";
    return;
  }
  if (actions) actions.style.display = "";
  data.params.forEach((p) => {
    const row = document.createElement("div");
    row.className = "tuning-row";
    row.innerHTML =
      '<span class="tuning-label">' + p.display_name + "</span>" +
      '<input type="range" class="tuning-slider" ' +
      'data-param="' + p.name + '" ' +
      'min="' + p.min + '" max="' + p.max + '" step="' + p.step + '" ' +
      'value="' + p.value + '">' +
      '<input type="number" class="tuning-value" ' +
      'data-param="' + p.name + '" ' +
      'min="' + p.min + '" max="' + p.max + '" step="' + p.step + '" ' +
      'value="' + p.value + '">';
    container.appendChild(row);

    const slider = row.querySelector(".tuning-slider") as HTMLInputElement;
    const input = row.querySelector(".tuning-value") as HTMLInputElement;
    slider.addEventListener("input", () => {
      input.value = slider.value;
      debouncedApply();
    });
    input.addEventListener("input", () => {
      slider.value = input.value;
      debouncedApply();
    });
  });
}

function collectTuningParams(): Record<string, number> {
  const params: Record<string, number> = {};
  document.querySelectorAll<HTMLInputElement>("#tuning-params .tuning-slider").forEach((slider) => {
    params[slider.dataset.param!] = parseFloat(slider.value);
  });
  return params;
}

async function applyTuningParams(): Promise<void> {
  const params = collectTuningParams();
  await postJSON("/api/estimator-params", { params });
  fetchModel();
}

async function resetTuningDefaults(): Promise<void> {
  if (!_tuningParams) return;
  _tuningParams.params.forEach((p) => {
    const slider = document.querySelector<HTMLInputElement>(
      '.tuning-slider[data-param="' + p.name + '"]',
    );
    const input = document.querySelector<HTMLInputElement>(
      '.tuning-value[data-param="' + p.name + '"]',
    );
    if (slider) slider.value = String(p.default);
    if (input) input.value = String(p.default);
  });
  await applyTuningParams();
}

export function initModelTab(): void {
  document.getElementById("estimator-select")!.addEventListener("change", async (e) => {
    await postJSON("/api/estimator", { name: (e.target as HTMLSelectElement).value });
    fetchModel();
    fetchTuningParams();
  });
  document.getElementById("btn-practice-start")!.addEventListener("click", () =>
    postJSON("/api/practice/start"),
  );
  document.getElementById("btn-practice-stop")!.addEventListener("click", () =>
    postJSON("/api/practice/stop"),
  );
  document.getElementById("btn-speedrun-start")!.addEventListener("click", () =>
    postJSON("/api/speedrun/start"),
  );
  document.getElementById("btn-speedrun-stop")!.addEventListener("click", () =>
    postJSON("/api/speedrun/stop"),
  );

  const toggle = document.getElementById("tuning-toggle");
  const panel = document.getElementById("tuning-panel");
  const body = document.getElementById("tuning-body") as HTMLElement | null;
  if (toggle && panel && body) {
    toggle.addEventListener("click", () => {
      panel.classList.toggle("collapsed");
      body.style.display = panel.classList.contains("collapsed") ? "none" : "";
    });
  }
  document.getElementById("btn-tuning-reset")?.addEventListener("click", resetTuningDefaults);

  fetchTuningParams();
}

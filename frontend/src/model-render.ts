import { segmentName, formatTime, elapsedStr, formatSavings } from "./format";
import { selectedEstimate, currentEstimate, formatTrend } from "./model-logic";
import type { AppState, ModelData, TuningData, SessionInfo } from "./types";

export function renderSavingsPanel(session: SessionInfo | null): void {
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

export const ALLOCATOR_COLORS: Record<string, string> = {
  greedy: "#4caf50",
  random: "#2196f3",
  round_robin: "#ff9800",
  least_played: "#ab47bc",
};
export const ALLOCATOR_LABELS: Record<string, string> = {
  greedy: "Greedy",
  random: "Random",
  round_robin: "Round Robin",
  least_played: "Least Played",
};
export const ALLOCATOR_ORDER = ["greedy", "random", "round_robin", "least_played"];

export function renderWeightSlider(
  weights: Record<string, number>,
  onCommit: (next: Record<string, number>) => void,
): void {
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
        onCommit({ ...weights });
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

export function renderModelTable(
  data: ModelData,
  onSegmentClick: (segmentId: string) => void,
): void {
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
      onSegmentClick(s.segment_id);
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

export function renderRecentList(
  ul: HTMLElement,
  recent: AppState["recent"],
  onToggleInvalidated: (id: number, invalidated: boolean) => void,
): void {
  ul.innerHTML = "";
  (recent || []).forEach((r) => {
    const li = document.createElement("li");
    if (r.invalidated) {
      li.classList.add("invalidated");
    }
    const time = formatTime(r.time_ms);
    const allocColor = r.chosen_allocator ? ALLOCATOR_COLORS[r.chosen_allocator] ?? null : null;
    const cls = r.completed ? "ahead" : "behind";
    const btnLabel = r.invalidated ? "Restore" : "Mark invalid";
    const btn = document.createElement("button");
    btn.className = "invalidate-btn";
    btn.textContent = btnLabel;
    btn.addEventListener("click", () => {
      onToggleInvalidated(r.id, !r.invalidated);
    });
    const timeSpan = '<span class="' + cls + '"'
      + (allocColor ? ' style="color:' + allocColor + '"' : '')
      + '>' + time + "</span>";
    li.innerHTML =
      timeSpan +
      ' <span class="dim">' + segmentName(r) + "</span>";
    li.appendChild(btn);
    ul.appendChild(li);
  });
}

export function renderPracticeInsight(cs: AppState["current_segment"]): void {
  if (!cs) return;
  document.getElementById("current-goal")!.textContent = segmentName(cs);
  document.getElementById("current-attempts")!.textContent =
    "Attempt " + (cs.attempt_count || 0);

  const insight = document.getElementById("insight")!;
  const est = currentEstimate(cs);
  const expectedStr = formatTime(est?.expected_ms ?? null);
  const trend = formatTrend(est);
  if (expectedStr) {
    const parts = ["Expected: " + expectedStr];
    if (trend) parts.push(trend);
    insight.innerHTML = "<span>" + parts.join(" · ") + "</span>";
  } else if (trend) {
    insight.innerHTML = "<span>" + trend + "</span>";
  } else {
    insight.textContent = "No data yet";
  }
}

export function renderSessionStats(session: AppState["session"]): void {
  const stats = document.getElementById("session-stats");
  if (stats && session) {
    stats.textContent =
      (session.segments_completed || 0) +
      "/" +
      (session.segments_attempted || 0) +
      " cleared | " +
      elapsedStr(session.started_at);
  }
}

export function renderTuningParams(
  data: TuningData,
  onParamChange: () => void,
): void {
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
      onParamChange();
    });
    input.addEventListener("input", () => {
      slider.value = input.value;
      onParamChange();
    });
  });
}

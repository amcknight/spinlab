import { canStartPractice, canStartSpeedRun } from "./model-logic";
import type { AppState, ModelData, TuningData, SessionInfo } from "./types";
import { renderSegmentDetail, destroySegmentDetail } from "./segment-detail";
import {
  fetchModelData,
  fetchTuningData,
  postEstimator,
  postTuningParams,
  postAllocatorWeights,
  patchAttemptInvalidated,
  postPracticeStart,
  postPracticeStop,
  postSpeedrunStart,
  postSpeedrunStop,
} from "./model-api";
import {
  renderWeightSlider,
  renderModelTable,
  renderRecentList,
  renderPracticeInsight,
  renderSessionStats,
  renderTuningParams,
  renderSavingsPanel,
} from "./model-render";

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

export async function fetchModel(): Promise<void> {
  const data = await fetchModelData();
  if (data) updateModel(data);
}

function updateModel(data: ModelData): void {
  renderModelTable(data, showSegmentDetail);
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
  renderSavingsPanel(session);
}

export function updatePracticeCard(data: AppState): void {
  const card = document.getElementById("practice-card") as HTMLElement;
  if ((data.mode !== "practice" && data.mode !== "speed_run") || !data.current_segment) {
    card.style.display = "none";
    return;
  }
  card.style.display = "";
  updateSavingsPanel(data.session);

  renderPracticeInsight(data.current_segment);
  renderRecentList(document.getElementById("recent")!, data.recent, patchAttemptInvalidated);
  renderSessionStats(data.session);

  const weightsEl = document.getElementById("allocator-weights") as HTMLElement;
  if (weightsEl) {
    weightsEl.style.display = data.mode === "speed_run" ? "none" : "";
  }
  if (data.allocator_weights && data.mode !== "speed_run") {
    _currentWeights = { ...data.allocator_weights };
    renderWeightSlider(data.allocator_weights, (next) => {
      _currentWeights = next;
      postAllocatorWeights(next);
    });
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
  const data = await fetchTuningData();
  if (!data) return;
  _tuningParams = data;
  renderTuningParams(data, debouncedApply);
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
  await postTuningParams(params);
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
    await postEstimator((e.target as HTMLSelectElement).value);
    fetchModel();
    fetchTuningParams();
  });
  document.getElementById("btn-practice-start")!.addEventListener("click", () => postPracticeStart());
  document.getElementById("btn-practice-stop")!.addEventListener("click", () => postPracticeStop());
  document.getElementById("btn-speedrun-start")!.addEventListener("click", () => postSpeedrunStart());
  document.getElementById("btn-speedrun-stop")!.addEventListener("click", () => postSpeedrunStop());

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

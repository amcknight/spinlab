import { canStartPractice, canStartHyperPlay } from "./model-logic";
import type { AppState, ModelData, SessionInfo } from "./types";
import {
  loadAndRenderEmSuitePanel,
  destroyEmSuitePanel,
} from "./em-suite-panel";
import { renderSegmentDetail, destroySegmentDetail } from "./segment-detail";
import {
  fetchModelData,
  postAllocatorWeights,
  patchAttemptInvalidated,
  postPracticeStart,
  postPracticeStop,
  postHyperPlayStart,
  postHyperPlayStop,
} from "./model-api";
import {
  renderWeightSlider,
  renderModelTable,
  renderRecentList,
  renderPracticeInsight,
  renderSessionStats,
  renderSavingsPanel,
} from "./model-render";

let _currentWeights: Record<string, number> | null = null;
let _currentSegmentId: string | null = null;

export async function fetchModel(): Promise<void> {
  const data = await fetchModelData();
  if (data) updateModel(data);
}

function updateModel(data: ModelData): void {
  renderModelTable(data, showSegmentDetail);
}

function showSegmentDetail(segmentId: string): void {
  _currentSegmentId = segmentId;
  (document.getElementById("model-table") as HTMLElement).style.display = "none";
  (document.querySelector(".model-header") as HTMLElement).style.display = "none";
  (document.getElementById("practice-controls") as HTMLElement).style.display = "none";
  const practiceCard = document.getElementById("practice-card") as HTMLElement;
  practiceCard.dataset.wasVisible = practiceCard.style.display;
  practiceCard.style.display = "none";

  const detail = document.getElementById("segment-detail") as HTMLElement;
  detail.style.display = "";
  renderSegmentDetail(detail, segmentId, hideSegmentDetail);
}

function hideSegmentDetail(): void {
  _currentSegmentId = null;
  destroySegmentDetail();

  (document.getElementById("model-table") as HTMLElement).style.display = "";
  (document.querySelector(".model-header") as HTMLElement).style.display = "";
  (document.getElementById("practice-controls") as HTMLElement).style.display = "";
  const practiceCard = document.getElementById("practice-card") as HTMLElement;
  practiceCard.style.display = practiceCard.dataset.wasVisible || "none";

  (document.getElementById("segment-detail") as HTMLElement).style.display = "none";

  fetchModel();
}

export function updateSavingsPanel(session: SessionInfo | null): void {
  renderSavingsPanel(session);
}

export function updatePracticeCard(data: AppState): void {
  const card = document.getElementById("practice-card") as HTMLElement;
  if ((data.mode !== "practice" && data.mode !== "hyper_play") || !data.current_segment) {
    card.style.display = "none";
    destroyEmSuitePanel();
    return;
  }
  card.style.display = "";
  updateSavingsPanel(data.session);

  renderPracticeInsight(data.current_segment);
  renderRecentList(document.getElementById("recent")!, data.recent, patchAttemptInvalidated);
  renderSessionStats(data.session);

  const weightsEl = document.getElementById("allocator-weights") as HTMLElement;
  if (weightsEl) {
    weightsEl.style.display = data.mode === "hyper_play" ? "none" : "";
  }
  if (data.allocator_weights && data.mode !== "hyper_play") {
    _currentWeights = { ...data.allocator_weights };
    renderWeightSlider(data.allocator_weights, (next) => {
      _currentWeights = next;
      postAllocatorWeights(next);
    });
  }

  // EMA-suite panel. Fired per SSE app-state push, so updates per attempt
  // for free. Fire-and-forget — errors render an inline message inside the
  // panel host without blocking the rest of the card.
  const emSuiteHost = document.getElementById("em-suite-panel") as HTMLElement;
  if (emSuiteHost) {
    void loadAndRenderEmSuitePanel(data.current_segment.id, emSuiteHost);
  }
}

export function updatePracticeControls(data: AppState): void {
  const startBtn = document.getElementById("btn-practice-start") as HTMLButtonElement;
  const stopBtn = document.getElementById("btn-practice-stop") as HTMLElement;
  const srStartBtn = document.getElementById("btn-hyperplay-start") as HTMLButtonElement;
  const srStopBtn = document.getElementById("btn-hyperplay-stop") as HTMLElement;
  const isPracticing = data.mode === "practice";
  const isHyperPlay = data.mode === "hyper_play";

  startBtn.style.display = isPracticing || isHyperPlay ? "none" : "";
  startBtn.disabled = !canStartPractice(data);
  stopBtn.style.display = isPracticing ? "" : "none";

  srStartBtn.style.display = isPracticing || isHyperPlay ? "none" : "";
  srStartBtn.disabled = !canStartHyperPlay(data);
  srStopBtn.style.display = isHyperPlay ? "" : "none";
}

export function initModelTab(): void {
  document.getElementById("btn-practice-start")!.addEventListener("click", () => postPracticeStart());
  document.getElementById("btn-practice-stop")!.addEventListener("click", () => postPracticeStop());
  document.getElementById("btn-hyperplay-start")!.addEventListener("click", () => postHyperPlayStart());
  document.getElementById("btn-hyperplay-stop")!.addEventListener("click", () => postHyperPlayStop());
}

import { canStartPractice, canStartHyperPlay, practiceCardState } from "./model-logic";
import type { AppState, ModelData } from "./types";
import {
  loadAndRenderEmSuitePanel,
  destroyEmSuitePanel,
} from "./em-suite-panel";
import { loadAndRenderImprovementView, destroyImprovementView } from "./improvement-view";
import { loadAndRenderLiveView, destroyLiveView } from "./live-view";
import { renderSegmentDetail, destroySegmentDetail } from "./segment-detail";
import { segmentName } from "./format";
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
  renderSessionStats,
} from "./model-render";

let _currentWeights: Record<string, number> | null = null;
let _currentSegmentId: string | null = null;
let _lastPracticed: { id: string; name: string } | null = null;
// Latest has_active_run from app-state. The model table re-renders on its own
// /api/model fetch (no AppState in hand), so the empty-state choice reads this.
let _hasActiveRun = false;

export async function fetchModel(): Promise<void> {
  const data = await fetchModelData();
  if (data) updateModel(data);
}

function updateModel(data: ModelData): void {
  renderModelTable(data, showSegmentDetail, _hasActiveRun);
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

export function updatePracticeCard(data: AppState): void {
  // Tracked for the model-table empty state, which re-renders on its own fetch.
  _hasActiveRun = data.has_active_run;
  const card = document.getElementById("practice-card") as HTMLElement;
  const cs = data.current_segment;
  const state = practiceCardState({
    mode: data.mode,
    hasCurrentSegment: cs != null,
    hasFrozenSession: data.has_frozen_session,
    hasLastPracticed: _lastPracticed != null,
    hasGameId: data.game_id != null,
  });

  if (state === "hidden") {
    card.style.display = "none";
    card.removeAttribute("data-frozen");
    destroyEmSuitePanel();
    destroyImprovementView();
    destroyLiveView();
    return;
  }

  const hosts = {
    routeBar: document.getElementById("live-route-bar")!,
    runGraph: document.getElementById("live-run-graph")!,
    segmentSummary: document.getElementById("live-segment-summary")!,
    graph: document.getElementById("live-graph-slot")!,
  };

  if (state === "frozen") {
    // Idle with a clean-stopped session: keep the live view visible, rendered
    // from the persisted (frozen) snapshot. CSS [data-frozen] hides the
    // practice-only widgets (recent, session stats, weights, panels).
    card.style.display = "";
    card.dataset.frozen = "true";
    destroyEmSuitePanel();
    destroyImprovementView();
    void loadAndRenderLiveView({
      segmentId: _lastPracticed!.id,
      gameId: data.game_id!,
      segmentName: _lastPracticed!.name,
      title: data.game_name ?? data.game_id!,
      hosts,
    });
    return;
  }

  // state === "live"
  card.style.display = "";
  card.removeAttribute("data-frozen");
  _lastPracticed = { id: cs!.id, name: segmentName(cs!) };
  if (data.game_id) {
    void loadAndRenderLiveView({
      segmentId: cs!.id,
      gameId: data.game_id,
      segmentName: segmentName(cs!),
      title: data.game_name ?? data.game_id,
      hosts,
    });
  }

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

  const improvementHost = document.getElementById("improvement-view") as HTMLElement;
  if (improvementHost) {
    void loadAndRenderImprovementView(cs!.id, improvementHost);
  }

  // EMA-suite panel. Fired per SSE app-state push, so updates per attempt
  // for free. Fire-and-forget — errors render an inline message inside the
  // panel host without blocking the rest of the card.
  const emSuiteHost = document.getElementById("em-suite-panel") as HTMLElement;
  if (emSuiteHost) {
    void loadAndRenderEmSuitePanel(cs!.id, emSuiteHost);
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

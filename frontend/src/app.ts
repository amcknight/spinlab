import { connectSSE, fetchJSON } from "./api";
import { initHeader, updateHeader } from "./header";
import {
  updatePracticeCard,
  updatePracticeControls,
  fetchModel,
  initModelTab,
} from "./model";
import { fetchManage, initManageTab, updateManageState } from "./manage";
import { fetchSegments, renderSegmentsView } from "./segments-view";
import type { AppState } from "./types";

let _currentGameId: string | null = null;

function updateFromState(data: AppState): void {
  _currentGameId = data.game_id;
  updateHeader(data);
  updatePracticeCard(data);
  updatePracticeControls(data);
  updateManageState(data);

  const activeTab = document.querySelector(".tab.active") as HTMLElement | null;
  if (activeTab?.dataset.tab === "model") fetchModel();
  if (
    activeTab?.dataset.tab === "manage" ||
    data.mode === "reference" ||
    data.mode === "replay" ||
    data.mode === "cold_fill"
  ) {
    fetchManage();
  }
}

// Tab switching
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document
      .querySelectorAll(".tab-content")
      .forEach((c) => c.classList.remove("active"));
    (btn as HTMLElement).classList.add("active");
    document
      .getElementById("tab-" + (btn as HTMLElement).dataset.tab)
      ?.classList.add("active");
    if ((btn as HTMLElement).dataset.tab === "model") fetchModel();
    if ((btn as HTMLElement).dataset.tab === "manage") fetchManage();
    if ((btn as HTMLElement).dataset.tab === "segments") fetchAndRenderSegments();
  });
});

async function fetchAndRenderSegments(): Promise<void> {
  const container = document.getElementById("segments-view-container") as HTMLElement;
  if (!_currentGameId) {
    container.innerHTML = '<p class="dim">No game loaded</p>';
    return;
  }
  try {
    const segs = await fetchSegments(_currentGameId);
    if (!segs.length) {
      container.innerHTML = '<p class="dim">No segments</p>';
      return;
    }
    renderSegmentsView(container, segs);
  } catch (err) {
    container.textContent = String(err);
  }
}

// Init
initHeader();
initModelTab();
initManageTab();

// Connect SSE with initial poll
connectSSE(updateFromState);
fetchJSON<AppState>("/api/state").then((data) => {
  if (data) updateFromState(data);
});

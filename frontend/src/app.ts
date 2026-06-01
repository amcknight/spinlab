import { connectSSE, fetchJSON, formatClientError, postJSON } from "./api";
import { initHeader, updateHeader } from "./header";
import {
  updatePracticeCard,
  updatePracticeControls,
  fetchModel,
  initModelTab,
} from "./model";
import { fetchManage, initManageTab, updateManageState } from "./manage";
import { fetchSegments, renderSegmentsView, coldCaptureButtonEnabled } from "./segments-view";
import type { AppState } from "./types";

let _currentGameId: string | null = null;

function updateColdCaptureButton(data: AppState): void {
  const btn = document.getElementById("btn-start-cold-fill") as HTMLButtonElement | null;
  if (!btn) return;
  btn.disabled = !coldCaptureButtonEnabled(data.mode, data.has_active_run, data.emu_connected);
  btn.title = data.has_active_run
    ? "Capture cold states for the active run"
    : "Select a reference run in Manage first";
}

function updateFromState(data: AppState): void {
  _currentGameId = data.game_id;
  updateHeader(data);
  updatePracticeCard(data);
  updatePracticeControls(data);
  updateManageState(data);
  updateColdCaptureButton(data);

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
    container.textContent = `Failed to load segments: ${formatClientError(err)}`;
  }
}

initHeader();
initModelTab();
initManageTab();

document.getElementById("btn-start-cold-fill")?.addEventListener("click", async () => {
  const res = await postJSON<{ status?: string }>("/api/cold-fill/start");
  if (res?.status === "no_gaps") {
    alert("No missing cold states for the active run.");
  }
});

connectSSE(updateFromState);
fetchJSON<AppState>("/api/state").then((data) => {
  if (data) updateFromState(data);
});

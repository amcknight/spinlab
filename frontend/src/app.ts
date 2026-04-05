import { connectSSE, fetchJSON } from "./api";
import { initHeader, updateHeader } from "./header";
import {
  updatePracticeCard,
  updatePracticeControls,
  fetchModel,
  initModelTab,
} from "./model";
import { fetchManage, initManageTab, updateManageState } from "./manage";
import type { AppState } from "./types";

function updateFromState(data: AppState): void {
  updateHeader(data);
  updatePracticeCard(data);
  updatePracticeControls(data);
  updateManageState(data);

  const activeTab = document.querySelector(".tab.active") as HTMLElement | null;
  if (activeTab?.dataset.tab === "model") fetchModel();
  if (
    activeTab?.dataset.tab === "manage" ||
    data.mode === "reference" ||
    data.mode === "replay"
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
  });
});

// Init
initHeader();
initModelTab();
initManageTab();

// Connect SSE with initial poll
connectSSE(updateFromState);
fetchJSON<AppState>("/api/state").then((data) => {
  if (data) updateFromState(data);
});

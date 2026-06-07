import { connectSSE, fetchJSON, fetchStateWithRetry, formatClientError, postJSON } from "./api";
import { initHeader, updateHeader } from "./header";
import { initRunSelector, updateRunSelector } from "./run-selector";
import {
  updatePracticeCard,
  updatePracticeControls,
  fetchModel,
  initModelTab,
} from "./model";
import { fetchManage, initManageTab, updateManageState } from "./manage";
import {
  fetchSegments,
  renderSegmentsView,
  coldCaptureButtonEnabled,
  coldCaptureButtonVisible,
} from "./segments-view";
import { initPracticeEnginePanel } from "./practice-engine";
import { initShell, currentPage } from "./shell";
import { emptyStateMessage } from "./format";
import type { AppState } from "./types";

let _currentGameId: string | null = null;
// Latest has_active_run from app-state. Segments view re-renders on page show
// / state push (no AppState in hand at that call site), so it reads this.
let _hasActiveRun = false;

function updateColdCaptureButton(data: AppState): void {
  const btn = document.getElementById("btn-start-cold-fill") as HTMLButtonElement | null;
  if (!btn) return;
  // Hide entirely unless there's actually cold-fill work for the active run —
  // a disabled-but-visible button when there's nothing to capture is noise.
  const visible = coldCaptureButtonVisible(data.segments_missing_cold);
  btn.style.display = visible ? "" : "none";
  if (!visible) return;
  btn.disabled = !coldCaptureButtonEnabled(data.mode, data.has_active_run, data.emu_connected);
  btn.textContent = `Start Cold Capture (${data.segments_missing_cold})`;
  btn.title = data.emu_connected
    ? "Capture the missing cold states for the active run"
    : "Connect the emulator to capture cold states";
}

function updateFromState(data: AppState): void {
  _currentGameId = data.game_id;
  _hasActiveRun = data.has_active_run;
  updateHeader(data);
  updatePracticeCard(data);
  updatePracticeControls(data);
  updateManageState(data);
  updateRunSelector(data);
  updateColdCaptureButton(data);

  // Play hosts the model table; refresh it on every state push while visible.
  if (currentPage() === "play") fetchModel();
  // Manage data must refresh during capture-ish modes regardless of page, plus
  // whenever Setup is the visible page.
  if (
    currentPage() === "setup" ||
    data.mode === "reference" ||
    data.mode === "replay" ||
    data.mode === "cold_fill"
  ) {
    fetchManage();
  }
}

// Two-page sweep. Play hosts Model + Simulator; Setup hosts Manage + Segments.
// onShow fires once for the initial page (play) and again on each toggle.
initShell((page) => {
  if (page === "play") {
    fetchModel();
    initPracticeEnginePanel(_hasActiveRun);
  } else {
    fetchManage();
    fetchAndRenderSegments();
  }
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
      // No active run gets its own copy; a run with zero traversed segments
      // keeps the generic "No segments".
      const msg = emptyStateMessage(_hasActiveRun, "No segments");
      container.innerHTML = '<p class="dim">' + msg + "</p>";
      return;
    }
    renderSegmentsView(container, segs);
  } catch (err) {
    container.textContent = `Failed to load segments: ${formatClientError(err)}`;
  }
}

// Re-fetch state and re-render the visible page. Used after a run-selector
// mutation (activate/rename/delete) so all pages reflect the new active run —
// updateFromState fans out to fetchModel/fetchManage/segments by page.
async function refreshAll(): Promise<void> {
  const data = await fetchJSON<AppState>("/api/state");
  if (data) updateFromState(data);
}

initHeader();
initRunSelector(refreshAll);
initModelTab();
initManageTab();

document.getElementById("btn-start-cold-fill")?.addEventListener("click", async () => {
  const res = await postJSON<{ status?: string }>("/api/cold-fill/start");
  if (res?.status === "no_gaps") {
    alert("No missing cold states for the active run.");
  }
});

// Fetch initial state with retry first (the Vite dev server outlives a
// cold backend on launch), THEN open the SSE stream — opening SSE only once
// the backend has answered avoids it racing the not-yet-bound backend too.
fetchStateWithRetry().then((data) => {
  if (data) updateFromState(data);
  connectSSE(updateFromState);
});

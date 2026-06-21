import { fetchJSON, postJSON } from "./api";
import type { AppState, CaptureSession, Reference } from "./types";

let lastState: AppState | null = null;
// Id of the currently-active reference run, tracked from /api/references so the
// Replay / Fast Replay buttons can target it (the old #ref-select dropdown that
// used to carry the selection is gone — the title-bar selector owns it now).
let activeRefId: string | null = null;

export async function fetchManage(): Promise<void> {
  const refsData = await fetchJSON<{ references: Reference[] }>("/api/references");
  if (!refsData) return;
  updateManage(refsData.references);
}

function updateManage(refs: Reference[]): void {
  const btnStart = document.getElementById("btn-ref-start") as HTMLButtonElement;
  const btnReplay = document.getElementById("btn-replay") as HTMLButtonElement;
  const pausedRunCard = document.getElementById("paused-run-card") as HTMLElement;

  const busy =
    lastState != null &&
    (lastState.mode === "reference" || lastState.mode === "replay" ||
     lastState.mode === "cold_fill" || lastState.mode === "fill_gap");
  const pausedRun = lastState?.paused_run || null;
  const recording = lastState?.mode === "reference";

  // Replay targets the ACTIVE run (the title-bar selector owns run selection
  // now that the old #ref-select dropdown is gone).
  const activeRef = refs.find((r) => r.active);
  activeRefId = activeRef?.id ?? null;

  btnStart.disabled = busy || pausedRun != null || !lastState?.emu_connected;

  if (pausedRun) {
    pausedRunCard.style.display = "";
    document.getElementById("paused-run-summary")!.textContent =
      `${pausedRun.segments_captured} segments captured across ${pausedRun.session_count} sessions`;
    renderSessionsList(pausedRun.run_id);
  } else {
    pausedRunCard.style.display = "none";
  }

  (document.getElementById("btn-resume") as HTMLButtonElement).disabled =
    !pausedRun || recording || !lastState?.emu_connected;
  (document.getElementById("btn-save-and-finish") as HTMLButtonElement).disabled =
    !pausedRun;
  (document.getElementById("btn-discard-run") as HTMLButtonElement).disabled =
    !pausedRun;

  const recIndicator = document.getElementById("recording-indicator") as HTMLElement;
  if (recording) {
    recIndicator.style.display = "";
    document.getElementById("recording-seg-count")!.textContent =
      String(lastState?.sections_captured ?? 0);
  } else {
    recIndicator.style.display = "none";
  }

  const hasReplayable = !!activeRef?.has_replay;
  btnReplay.disabled =
    busy || pausedRun != null || !hasReplayable || !lastState?.emu_connected;
  const btnFastReplay = document.getElementById("btn-fast-replay") as HTMLButtonElement | null;
  if (btnFastReplay) btnFastReplay.disabled = btnReplay.disabled;

  const cfBanner = document.getElementById("cold-fill-banner") as HTMLElement | null;
  if (cfBanner) {
    if (lastState?.mode === "cold_fill" && lastState?.cold_fill) {
      const cf = lastState.cold_fill;
      cfBanner.innerHTML =
        '<div class="cold-fill-status">' +
        "<strong>Capturing cold starts</strong> — " +
        "Die to continue (" + cf.current + "/" + cf.total + ")" +
        (cf.segment_label ? " — " + cf.segment_label : "") +
        "</div>";
      cfBanner.style.display = "block";
    } else {
      cfBanner.style.display = "none";
    }
  }
}

async function renderSessionsList(runId: string): Promise<void> {
  const data = await fetchJSON<{ sessions: CaptureSession[] }>(
    `/api/capture_sessions?run_id=${encodeURIComponent(runId)}`,
  );
  const body = document.getElementById("sessions-body")!;
  body.innerHTML = "";
  (data?.sessions || []).forEach((s) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${s.ordinal}</td>` +
      `<td>${s.started_at}</td>` +
      `<td>${s.ended_at || "(open)"}</td>` +
      `<td>${s.end_reason || ""}</td>` +
      `<td>${s.segment_count}</td>` +
      `<td><button data-sess-id="${s.id}" class="btn-del-sess btn-sm btn-danger-sm">Delete</button></td>`;
    body.appendChild(tr);
  });
  body.querySelectorAll(".btn-del-sess").forEach((btn) =>
    btn.addEventListener("click", async (e) => {
      const sid = (e.currentTarget as HTMLElement).getAttribute("data-sess-id")!;
      if (!confirm("Delete this session and its segments?")) return;
      await fetch(`/api/capture_sessions/${sid}`, { method: "DELETE" });
      await fetchManage();
    }),
  );
}

export function updateManageState(data: AppState): void {
  lastState = data;
}

export function initManageTab(): void {
  document.getElementById("btn-ref-start")!.addEventListener("click", () => {
    if (!lastState?.emu_connected) return;
    postJSON("/api/reference/start");
  });

  document.getElementById("btn-replay")!.addEventListener("click", async () => {
    if (!activeRefId) return;
    await postJSON("/api/replay/start", { ref_id: activeRefId });
  });

  // Fast Replay: uncapped (fast-forward) playback for regenerating
  // states/events quickly. SPEED_UNCAPPED mirrors python/spinlab/protocol.py;
  // the plain Replay button omits speed and gets SPEED_NORMAL server-side.
  const SPEED_UNCAPPED = 0;
  document.getElementById("btn-fast-replay")?.addEventListener("click", async () => {
    if (!activeRefId) return;
    await postJSON("/api/replay/start", { ref_id: activeRefId, speed: SPEED_UNCAPPED });
  });

  document.getElementById("btn-resume")?.addEventListener("click", async () => {
    await fetch("/api/reference/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
  });

  document.getElementById("btn-save-and-finish")?.addEventListener("click", async () => {
    const nameInput = document.getElementById("finalize-name") as HTMLInputElement;
    const name = nameInput?.value?.trim() || "Untitled";
    await fetch("/api/reference/save_and_finish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
  });

  document.getElementById("btn-discard-run")?.addEventListener("click", async () => {
    if (!confirm("Discard this run? All sessions and segments will be deleted.")) return;
    await fetch("/api/reference/discard_run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
  });

  document.getElementById("btn-reset")!.addEventListener("click", async () => {
    if (!confirm("Clear all session data? This cannot be undone.")) return;
    const data = await postJSON<{ status?: string }>("/api/reset");
    document.getElementById("reset-status")!.textContent =
      data?.status === "ok" ? "Data cleared." : "Error clearing data.";
  });
}

import { segmentName, shortEndpoint, formatTime } from "./format";
import { fetchJSON, postJSON } from "./api";
import type { AppState, CaptureSession, Reference, ReferenceSegment } from "./types";

let lastState: AppState | null = null;

export async function fetchManage(): Promise<void> {
  const refsData = await fetchJSON<{ references: Reference[] }>("/api/references");
  if (!refsData) return;
  const refs = refsData.references || [];

  let segments: ReferenceSegment[] = [];
  const captureId = lastState?.capture_run_id;
  if (captureId) {
    const segData = await fetchJSON<{ segments: ReferenceSegment[] }>(
      "/api/references/" + captureId + "/segments",
    );
    segments = segData?.segments || [];
  } else {
    const active = refs.find((r) => r.active);
    if (active) {
      const segData = await fetchJSON<{ segments: ReferenceSegment[] }>(
        "/api/references/" + active.id + "/segments",
      );
      segments = segData?.segments || [];
    }
  }
  updateManage(refs, segments);
}

function updateManage(refs: Reference[], segments: ReferenceSegment[]): void {
  const sel = document.getElementById("ref-select") as HTMLSelectElement;
  const btnStart = document.getElementById("btn-ref-start") as HTMLButtonElement;
  const btnReplay = document.getElementById("btn-replay") as HTMLButtonElement;
  const pausedRunCard = document.getElementById("paused-run-card") as HTMLElement;

  const busy =
    lastState != null &&
    (lastState.mode === "reference" || lastState.mode === "replay" ||
     lastState.mode === "cold_fill" || lastState.mode === "fill_gap");
  const pausedRun = lastState?.paused_run || null;
  const recording = lastState?.mode === "reference";

  const noRefs = refs.length === 0;
  sel.disabled = busy || pausedRun != null;
  btnStart.disabled = busy || pausedRun != null || !lastState?.tcp_connected;
  (document.getElementById("btn-ref-rename") as HTMLButtonElement).disabled =
    busy || pausedRun != null || noRefs;
  (document.getElementById("btn-ref-delete") as HTMLButtonElement).disabled =
    busy || pausedRun != null || noRefs;

  if (pausedRun) {
    pausedRunCard.style.display = "";
    document.getElementById("paused-run-summary")!.textContent =
      `${pausedRun.segments_captured} segments captured across ${pausedRun.session_count} sessions`;
    renderSessionsList(pausedRun.run_id);
  } else {
    pausedRunCard.style.display = "none";
  }

  (document.getElementById("btn-resume") as HTMLButtonElement).disabled =
    !pausedRun || recording || !lastState?.tcp_connected;
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

  sel.innerHTML = "";
  if (!refs.length) {
    const opt = document.createElement("option");
    opt.textContent = "No references";
    opt.disabled = true;
    sel.appendChild(opt);
    document.getElementById("segment-body")!.innerHTML = "";
    btnReplay.disabled = true;
    return;
  }
  refs.forEach((r) => {
    const opt = document.createElement("option");
    opt.value = r.id;
    opt.textContent = r.name + (r.active ? " ●" : "");
    if (r.active) opt.selected = true;
    sel.appendChild(opt);
  });

  const selectedRef = refs.find((r) => r.id === sel.value);
  // Either a Mesen-era .spinrec or an RA-era .replay enables replay — the
  // orchestrator's _on_replay translates suffixes either way.
  const hasReplayable =
    !!(selectedRef?.has_spinrec || selectedRef?.has_replay);
  btnReplay.disabled =
    busy || pausedRun != null || !hasReplayable || !lastState?.tcp_connected;

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

  const body = document.getElementById("segment-body")!;
  body.innerHTML = "";
  segments.forEach((s) => {
    const tr = document.createElement("tr");
    const hasState = s.state_path != null;
    const stateCell = hasState
      ? '<span class="state-ok">✅</span>'
      : '<button class="btn-fill-gap" data-id="' + s.id + '">❌</button>';
    tr.innerHTML =
      '<td>' + (s.session_ordinal ?? '—') + '</td>' +
      '<td><input class="segment-name-input" value="' +
      (s.description || "") +
      '" ' +
      'placeholder="' + segmentName(s) + '" ' +
      'data-id="' + s.id + '" data-field="description"></td>' +
      "<td>" + s.level_number + "</td>" +
      "<td>" +
      shortEndpoint(s.start_type, s.start_ordinal) +
      " → " +
      shortEndpoint(s.end_type, s.end_ordinal) +
      "</td>" +
      "<td>" + stateCell + "</td>" +
      '<td><button class="btn-x" data-id="' + s.id + '">✕</button></td>';
    body.appendChild(tr);
  });
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
  document.getElementById("segment-body")!.addEventListener("focusout", async (e) => {
    const target = e.target as HTMLElement;
    if (!target.classList.contains("segment-name-input")) return;
    const input = target as HTMLInputElement;
    const id = input.dataset.id;
    const field = input.dataset.field;
    const value = input.value;
    await fetchJSON("/api/segments/" + id, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [field!]: value }),
    });
  });

  document.getElementById("segment-body")!.addEventListener("click", async (e) => {
    const target = e.target as HTMLElement;
    if (target.classList.contains("btn-fill-gap")) {
      const id = target.dataset.id;
      const data = await postJSON<{ status?: string }>("/api/segments/" + id + "/fill-gap");
      if (data?.status === "started") {
        target.textContent = "⏳";
        (target as HTMLButtonElement).disabled = true;
      }
      return;
    }
    if (!target.classList.contains("btn-x")) return;
    if (!confirm("Remove this segment?")) return;
    await fetchJSON("/api/segments/" + target.dataset.id, { method: "DELETE" });
    fetchManage();
  });

  document
    .getElementById("ref-select")!
    .addEventListener("change", async (e) => {
      await postJSON(
        "/api/references/" + (e.target as HTMLSelectElement).value + "/activate",
      );
      fetchManage();
    });

  document.getElementById("btn-ref-rename")!.addEventListener("click", async () => {
    const sel = document.getElementById("ref-select") as HTMLSelectElement;
    const name = prompt(
      "New name:",
      sel.options[sel.selectedIndex]?.text.replace(" ●", ""),
    );
    if (!name) return;
    await fetchJSON("/api/references/" + sel.value, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    fetchManage();
  });

  document.getElementById("btn-ref-delete")!.addEventListener("click", async () => {
    if (!confirm("Delete this reference and all its segments?")) return;
    const sel = document.getElementById("ref-select") as HTMLSelectElement;
    await fetchJSON("/api/references/" + sel.value, { method: "DELETE" });
    fetchManage();
  });

  document.getElementById("btn-ref-start")!.addEventListener("click", () => {
    if (!lastState?.tcp_connected) return;
    postJSON("/api/reference/start");
  });

  document.getElementById("btn-replay")!.addEventListener("click", async () => {
    const sel = document.getElementById("ref-select") as HTMLSelectElement;
    await postJSON("/api/replay/start", { ref_id: sel.value });
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

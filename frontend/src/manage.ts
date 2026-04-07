import { segmentName, formatTime } from "./format";
import { fetchJSON, postJSON } from "./api";
import type { AppState, Reference, ReferenceSegment } from "./types";

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
  const draftPrompt = document.getElementById("draft-prompt") as HTMLElement;

  const busy =
    lastState != null &&
    (lastState.mode === "reference" || lastState.mode === "replay");
  const hasDraft = lastState?.draft != null;

  const noRefs = refs.length === 0;
  sel.disabled = busy || hasDraft;
  btnStart.disabled = busy || hasDraft || !lastState?.tcp_connected;
  (document.getElementById("btn-ref-rename") as HTMLButtonElement).disabled =
    busy || hasDraft || noRefs;
  (document.getElementById("btn-ref-delete") as HTMLButtonElement).disabled =
    busy || hasDraft || noRefs;

  if (hasDraft && lastState?.draft) {
    draftPrompt.style.display = "";
    document.getElementById("draft-summary")!.textContent =
      "\u2713 Captured " + lastState.draft.segments_captured + " segments";
  } else {
    draftPrompt.style.display = "none";
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
    opt.textContent = r.name + (r.active ? " \u25cf" : "");
    if (r.active) opt.selected = true;
    sel.appendChild(opt);
  });

  const selectedRef = refs.find((r) => r.id === sel.value);
  btnReplay.disabled =
    busy || hasDraft || !selectedRef?.has_spinrec || !lastState?.tcp_connected;

  const cfBanner = document.getElementById("cold-fill-banner") as HTMLElement | null;
  if (cfBanner) {
    if (lastState?.mode === "cold_fill" && lastState?.cold_fill) {
      const cf = lastState.cold_fill;
      cfBanner.innerHTML =
        '<div class="cold-fill-status">' +
        "<strong>Capturing cold starts</strong> \u2014 " +
        "Die to continue (" + cf.current + "/" + cf.total + ")" +
        (cf.segment_label ? " \u2014 " + cf.segment_label : "") +
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
      ? '<span class="state-ok">\u2705</span>'
      : '<button class="btn-fill-gap" data-id="' + s.id + '">\u274c</button>';
    tr.innerHTML =
      '<td><input class="segment-name-input" value="' +
      (s.description || "") +
      '" ' +
      'placeholder="' + segmentName(s) + '" ' +
      'data-id="' + s.id + '" data-field="description"></td>' +
      "<td>" + s.level_number + "</td>" +
      "<td>" +
      (s.start_type === "entrance" ? "start" : "cp" + s.start_ordinal) +
      " \u2192 " +
      (s.end_type === "goal" ? "goal" : "cp" + s.end_ordinal) +
      "</td>" +
      "<td>" + stateCell + "</td>" +
      '<td><button class="btn-x" data-id="' + s.id + '">\u2715</button></td>';
    body.appendChild(tr);
  });
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
        target.textContent = "\u23f3";
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
      sel.options[sel.selectedIndex]?.text.replace(" \u25cf", ""),
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

  document.getElementById("btn-draft-save")!.addEventListener("click", async () => {
    const input = document.getElementById("draft-name") as HTMLInputElement;
    const name = input.value.trim();
    if (!name) {
      input.focus();
      return;
    }
    await postJSON("/api/references/draft/save", { name });
    input.value = "";
    fetchManage();
  });

  document.getElementById("btn-draft-discard")!.addEventListener("click", async () => {
    if (!confirm("Discard this capture? This cannot be undone.")) return;
    await postJSON("/api/references/draft/discard");
    fetchManage();
  });

  document.getElementById("btn-reset")!.addEventListener("click", async () => {
    if (!confirm("Clear all session data? This cannot be undone.")) return;
    const data = await postJSON<{ status?: string }>("/api/reset");
    document.getElementById("reset-status")!.textContent =
      data?.status === "ok" ? "Data cleared." : "Error clearing data.";
  });
}

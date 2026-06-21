import { shortEndpoint, segmentName } from "./format";
import type { ApiSegment } from "./types";

export function coldCaptureButtonEnabled(mode: string, hasActiveRun: boolean, emuConnected: boolean): boolean {
  return mode === "idle" && hasActiveRun && emuConnected;
}

/** Whether the cold-capture button should be shown at all. Hidden unless the
 * active run actually has cold states left to capture — a count of 0 (no run,
 * or every gap already filled, possibly from another run) means there's
 * nothing to do, so the button stays out of the way. */
export function coldCaptureButtonVisible(segmentsMissingCold: number): boolean {
  return segmentsMissingCold > 0;
}

export function groupByLevel(segs: ApiSegment[]): Record<string, ApiSegment[]> {
  const out: Record<string, ApiSegment[]> = {};
  for (const s of segs) {
    const key = String(s.level_number);
    const bucket = out[key];
    if (bucket !== undefined) {
      bucket.push(s);
    } else {
      out[key] = [s];
    }
  }
  for (const key of Object.keys(out)) {
    const bucket = out[key];
    if (bucket !== undefined) {
      bucket.sort((a, b) => (a.ordinal ?? 0) - (b.ordinal ?? 0));
    }
  }
  const ordered: Record<string, ApiSegment[]> = {};
  for (const key of Object.keys(out).sort((a, b) => Number(a) - Number(b))) {
    const bucket = out[key];
    if (bucket !== undefined) ordered[key] = bucket;
  }
  return ordered;
}

export function formatConditions(conds: Record<string, string | boolean>): string {
  const keys = Object.keys(conds);
  if (keys.length === 0) return "—";
  return keys.map(k => `${k}=${conds[k]}`).join(", ");
}

export async function patchDescription(segmentId: string, description: string): Promise<void> {
  const resp = await fetch(`/api/segments/${encodeURIComponent(segmentId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ description }),
  });
  if (!resp.ok) throw new Error(`patch failed: ${resp.status}`);
}

export async function patchIsPrimary(segmentId: string, isPrimary: boolean): Promise<void> {
  const resp = await fetch(`/api/segments/${encodeURIComponent(segmentId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ is_primary: isPrimary }),
  });
  if (!resp.ok) throw new Error(`patch failed: ${resp.status}`);
}

export function renderSegmentsView(container: HTMLElement, segs: ApiSegment[]): void {
  const grouped = groupByLevel(segs);
  container.innerHTML = "";
  for (const level of Object.keys(grouped)) {
    const section = document.createElement("section");
    section.className = "segments-level";
    const h = document.createElement("h3");
    h.textContent = `Level ${level}`;
    section.appendChild(h);
    const table = document.createElement("table");
    table.className = "segments-table";
    table.innerHTML =
      "<thead><tr><th></th><th>Segment</th><th>Name</th><th>Primary</th><th>Cold</th></tr></thead>";
    const tbody = document.createElement("tbody");
    for (const seg of grouped[level] ?? []) {
      appendSegmentRows(tbody, seg);
    }
    table.appendChild(tbody);
    section.appendChild(table);
    container.appendChild(section);
  }
}

function appendSegmentRows(tbody: HTMLElement, seg: ApiSegment): void {
  const segLabel = shortEndpoint(seg.start_type, seg.start_ordinal) +
    " → " + shortEndpoint(seg.end_type, seg.end_ordinal);

  const row = document.createElement("tr");
  row.className = "seg-row";

  // Expander
  const expTd = document.createElement("td");
  const exp = document.createElement("button");
  exp.className = "seg-expander";
  exp.type = "button";
  exp.textContent = "▸"; // right-pointing triangle ▸
  expTd.appendChild(exp);
  row.appendChild(expTd);

  // Segment label
  const segTd = document.createElement("td");
  segTd.textContent = segLabel;
  row.appendChild(segTd);

  // Editable name
  const nameTd = document.createElement("td");
  const nameInput = document.createElement("input");
  nameInput.className = "segment-name-input";
  nameInput.value = seg.description || "";
  nameInput.placeholder = segmentName(seg);
  nameInput.addEventListener("focusout", async () => {
    try { await patchDescription(seg.id, nameInput.value); }
    catch (err) { alert(String(err)); }
  });
  nameTd.appendChild(nameInput);
  row.appendChild(nameTd);

  // Primary checkbox (existing behavior)
  const primaryTd = document.createElement("td");
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = seg.is_primary;
  cb.addEventListener("change", async () => {
    cb.disabled = true;
    try { await patchIsPrimary(seg.id, cb.checked); seg.is_primary = cb.checked; }
    catch (err) { cb.checked = seg.is_primary; alert(String(err)); }
    finally { cb.disabled = false; }
  });
  primaryTd.appendChild(cb);
  row.appendChild(primaryTd);

  // Cold cell (filled in Task 3 with the Fill button; placeholder for now)
  const coldTd = document.createElement("td");
  coldTd.className = "seg-cold";
  coldTd.textContent = seg.has_cold_state ? "✅" : "❌";
  row.appendChild(coldTd);

  tbody.appendChild(row);

  // Detail row
  const detail = document.createElement("tr");
  detail.className = "seg-detail";
  detail.style.display = "none";
  const detailTd = document.createElement("td");
  detailTd.colSpan = 5;
  const conds = formatConditions(seg.start_conditions);
  const session = seg.session_ordinal == null ? "—" : String(seg.session_ordinal);
  detailTd.innerHTML =
    `<span class="seg-detail-item">Conditions: ${conds}</span>` +
    `<span class="seg-detail-item">Session: ${session}</span>`;
  detail.appendChild(detailTd);
  tbody.appendChild(detail);

  exp.addEventListener("click", () => {
    const open = detail.style.display !== "none";
    detail.style.display = open ? "none" : "";
    exp.textContent = open ? "▸" : "▾";
  });
}

export async function fetchSegments(gameId: string): Promise<ApiSegment[]> {
  const resp = await fetch(`/api/segments?game_id=${encodeURIComponent(gameId)}`);
  if (!resp.ok) throw new Error(`fetch failed: ${resp.status}`);
  const data = await resp.json();
  return data.segments;
}

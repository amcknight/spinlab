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
  // One table for all levels — a single column header at the top, with each
  // level introduced by a compact full-width divider row. (Was one table +
  // <h3> per level, which repeated the header and ate vertical space.)
  const table = document.createElement("table");
  table.className = "segments-table";
  table.innerHTML =
    "<thead><tr><th></th><th>Segment</th><th>Name</th><th>Primary</th><th>Cold</th></tr></thead>";
  const tbody = document.createElement("tbody");
  for (const level of Object.keys(grouped)) {
    const divider = document.createElement("tr");
    divider.className = "seg-level-divider";
    const dividerTd = document.createElement("td");
    dividerTd.colSpan = 5;
    dividerTd.textContent = `Level ${level}`;
    divider.appendChild(dividerTd);
    tbody.appendChild(divider);
    for (const seg of grouped[level] ?? []) {
      appendSegmentRows(tbody, seg);
    }
  }
  table.appendChild(tbody);
  container.appendChild(table);
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

  // Cold cell: present -> marker; missing -> Fill button
  const coldTd = document.createElement("td");
  coldTd.className = "seg-cold";
  if (seg.has_cold_state) {
    // present cold state is muted (the signal is what's MISSING)
    coldTd.textContent = "✅";
    coldTd.classList.add("dim");
    coldTd.title = "cold state captured";
  } else {
    const fill = document.createElement("button");
    fill.className = "btn-fill-gap";
    fill.type = "button";
    fill.textContent = "❌ Fill";
    fill.title = "cold state missing — capture it";
    fill.addEventListener("click", async () => {
      try {
        const res = await startFillGap(seg.id);
        if (res.status === "started") { fill.textContent = "⏳"; fill.disabled = true; }
      } catch (err) {
        alert(String(err));
      }
    });
    coldTd.appendChild(fill);
  }
  row.appendChild(coldTd);

  tbody.appendChild(row);

  // Detail row
  const detail = document.createElement("tr");
  detail.className = "seg-detail";
  detail.style.display = "none";
  const detailTd = document.createElement("td");
  detailTd.colSpan = 5;
  // Build detail items as DOM nodes with textContent — keeps the row uniformly
  // injection-proof (no innerHTML) and consistent across all fields.
  const detailItem = (text: string): HTMLSpanElement => {
    const span = document.createElement("span");
    span.className = "seg-detail-item";
    span.textContent = text;
    return span;
  };
  const session = seg.session_ordinal == null ? "—" : String(seg.session_ordinal);
  detailTd.appendChild(detailItem(`Conditions: ${formatConditions(seg.start_conditions)}`));
  detailTd.appendChild(detailItem(`Session: ${session}`));
  if (seg.state_path) {
    detailTd.appendChild(detailItem(`state: ${seg.state_path}`));
  }
  const delBtn = document.createElement("button");
  delBtn.className = "btn-x";
  delBtn.type = "button";
  delBtn.textContent = "Delete";
  delBtn.addEventListener("click", async () => {
    if (!confirm("Remove this segment?")) return;
    try {
      await deleteSegment(seg.id);
      row.remove();
      detail.remove();
    } catch (err) {
      alert(String(err));
    }
  });
  detailTd.appendChild(delBtn);
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

export async function deleteSegment(segmentId: string): Promise<void> {
  const resp = await fetch(`/api/segments/${encodeURIComponent(segmentId)}`, { method: "DELETE" });
  if (!resp.ok) throw new Error(`delete failed: ${resp.status}`);
}

export async function startFillGap(segmentId: string): Promise<{ status?: string }> {
  const resp = await fetch(`/api/segments/${encodeURIComponent(segmentId)}/fill-gap`, { method: "POST" });
  if (!resp.ok) throw new Error(`fill-gap failed: ${resp.status}`);
  return resp.json();
}

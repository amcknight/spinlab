import { shortEndpoint } from "./format";
import type { ApiSegment } from "./types";

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
    table.innerHTML =
      "<thead><tr><th>Segment</th><th>Conditions</th><th>Primary</th></tr></thead>";
    const tbody = document.createElement("tbody");
    for (const seg of grouped[level] ?? []) {
      const tr = document.createElement("tr");
      const segLabel = shortEndpoint(seg.start_type, seg.start_ordinal) +
        " \u2192 " + shortEndpoint(seg.end_type, seg.end_ordinal);
      const conds = formatConditions(seg.start_conditions);

      const segTd = document.createElement("td");
      segTd.textContent = segLabel;
      tr.appendChild(segTd);

      const condTd = document.createElement("td");
      condTd.className = "dim";
      condTd.textContent = conds;
      tr.appendChild(condTd);

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
      tr.appendChild(primaryTd);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    section.appendChild(table);
    container.appendChild(section);
  }
}

export async function fetchSegments(gameId: string): Promise<ApiSegment[]> {
  const resp = await fetch(`/api/segments?game_id=${encodeURIComponent(gameId)}`);
  if (!resp.ok) throw new Error(`fetch failed: ${resp.status}`);
  const data = await resp.json();
  return data.segments;
}

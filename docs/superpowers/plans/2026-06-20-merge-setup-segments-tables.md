# Merge Setup Segments Tables Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the two Setup-page segment tables (the `manage.ts` editor and the `segments-view.ts` grouped view) into one Route-scoped, level-grouped table with a per-row expander for rarely-needed columns.

**Architecture:** The merged table builds on the *existing* `/api/segments` endpoint, which is already scoped to the active reference run and already returns `is_primary`, `has_cold_state`, and `start_conditions`. We add one field (`session_ordinal`) to `ApiSegment`, extend `segments-view.ts`'s `renderSegmentsView` into a fully editable + expandable table (Name, Primary, Cold/Fill, Delete, and a chevron-toggled detail row), move that single table into the Setup "Segments" section, strip the duplicate editor out of `manage.ts`, and delete the now-dead editor endpoint.

**Tech Stack:** Python 3.11 + FastAPI + Pydantic + SQLite (backend); TypeScript + Vite + Vitest + happy-dom (frontend). Frontend API types are codegen'd from FastAPI's OpenAPI schema via `npm run gen-types`.

## Global Constraints

- **No magic numbers / no fudge factors.** Every numeric constant gets a named file-level variable with a rationale comment. (No new numerics are expected in this plan.)
- **No defensive `.get(key, default)` on contract-guaranteed fields.** `ApiSegment` fields are contract-guaranteed; direct-index them in the frontend so a missing field surfaces as a bug.
- **TDD, Red-Green.** Every code change starts with a failing test. Keep only tests that document behavior or catch regressions.
- **Run the FULL suite before declaring done:** `python -m pytest` (unit + emulator + frontend) — not `-m "not emulator"`. `SKIPPED` counts as a failure. Frontend smoke tests require `cd frontend && npm run build` first.
- **Frontend types are generated, never hand-edited.** After any backend schema change, run `cd frontend && npm run gen-types` to refresh `frontend/src/api-types.ts`.
- **ASCII only in plan code blocks** (smart quotes break copied TS/Python).
- **Branch:** `feat/merge-setup-segments` (already checked out).

---

## File Structure

**Backend:**
- `python/spinlab/api_schemas.py` — add `session_ordinal` to `ApiSegment` (~line 230).
- `python/spinlab/db/segments.py` — `get_all_segments_with_model` query gains a `LEFT JOIN capture_sessions` + `cs.ordinal AS session_ordinal` (~line 143).
- `python/spinlab/routes/segments.py` — pass `session_ordinal` into `ApiSegment` (~line 53).
- Cleanup (Task 5): `python/spinlab/routes/reference.py` (delete `get_reference_segments`), `python/spinlab/db/capture_runs.py` (delete `get_segments_by_reference`), `python/spinlab/api_schemas.py` (delete `ReferenceSegment` + `ReferenceSegmentsResponse`).

**Frontend:**
- `frontend/src/segments-view.ts` — extend `renderSegmentsView` into the merged editable/expandable table; add `patchDescription`, `deleteSegment`, `startFillGap` helpers.
- `frontend/src/segments-view.test.ts` — new tests for the merged table.
- `frontend/src/manage.ts` — remove `#segment-body` render loop (in `updateManage`), the segments fetch in `fetchManage`, and the `#segment-body` listeners in `initManageTab`.
- `frontend/src/manage.test.ts` — drop the `#segment-body` DOM stub line (no behavior tests reference it).
- `frontend/index.html` — move the single segments container into the "Segments" section; delete the bottom `#segments-view-container` and the old `#segment-table`.
- `frontend/style.css` — styles for the merged table (editable name, chevron, detail row, fill/delete buttons). Reuse existing `.segment-name-input`, `.btn-x`, `.btn-fill-gap`.

**Tests touched by the backend change:**
- `tests/unit/routes/test_dashboard_*` / wherever `/api/segments` is asserted — add a `session_ordinal` assertion (Task 1 finds the exact file).

---

## Task 1: Add `session_ordinal` to `ApiSegment`

**Files:**
- Modify: `python/spinlab/api_schemas.py` (`ApiSegment`, ~line 220-237)
- Modify: `python/spinlab/db/segments.py` (`get_all_segments_with_model`, ~line 143-162)
- Modify: `python/spinlab/routes/segments.py` (`api_segments`, ~line 53-71)
- Test: `tests/unit/db/test_db_segments.py` (or the existing `get_all_segments_with_model` test file — Step 1 locates it)

**Interfaces:**
- Produces: `ApiSegment.session_ordinal: int | None` — the ordinal of the capture session that owns the segment row (display-only; `None` when no owning session). The frontend detail row reads it.

- [ ] **Step 1: Locate the existing DB test for `get_all_segments_with_model`**

Run: `grep -rn "get_all_segments_with_model" tests/`
Expected: at least one test file (likely `tests/unit/db/test_db_segments.py`). Use that file for the next step. If none exists, create `tests/unit/db/test_db_segments.py`.

- [ ] **Step 2: Write the failing test for `session_ordinal` in the query result**

Add to the located test file. Mirror the existing setup helpers in that file (they already create a game, a capture run, a capture session, and segments with attempts). The key assertion: a segment whose `capture_session_id` points at a session with `ordinal = N` comes back with `session_ordinal == N`.

```python
def test_get_all_segments_with_model_includes_session_ordinal(db):
    # Reuse the file's existing fixture helpers to create a game + capture run +
    # capture session (ordinal 1) + one segment owned by that session, with a
    # non-invalidated attempt stamping run membership. (Match the helper names
    # already used by neighboring tests in this file.)
    game_id, run_id, seg_id = _setup_run_with_one_segment(db, session_ordinal=1)

    rows = db.get_all_segments_with_model(game_id, primary_only=False, run_id=run_id)

    assert len(rows) == 1
    assert rows[0]["session_ordinal"] == 1
```

If the file has no reusable helper, build the rows inline using the same `db.*` calls the recorder uses (create_capture_run, create_capture_session, upsert segment, insert a non-invalidated attempt). Do NOT bypass with raw SQL inserts that skip the attempt row — run-scoping needs the attempt.

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/unit/db/test_db_segments.py::test_get_all_segments_with_model_includes_session_ordinal -v`
Expected: FAIL with `KeyError: 'session_ordinal'`.

- [ ] **Step 4: Add the join + column to the query**

In `python/spinlab/db/segments.py`, `get_all_segments_with_model`, extend the SELECT list and add the join. The capture_sessions join key is `s.capture_session_id`.

```python
        cur = self.conn.execute(
            f"""SELECT s.id, s.game_id, s.level_number, s.start_type, s.start_ordinal,
                       s.end_type, s.end_ordinal, s.description,
                       s.active, s.ordinal, s.is_primary,
                       s.start_waypoint_id, s.end_waypoint_id,
                       cs.ordinal AS session_ordinal,
                       (SELECT wss.state_path FROM waypoint_save_states wss
                        WHERE wss.waypoint_id = s.start_waypoint_id
                        ORDER BY CASE wss.variant_type
                                   WHEN 'cold' THEN 0
                                   WHEN 'hot'  THEN 1
                                   ELSE 2
                                 END
                        LIMIT 1) AS state_path
                FROM segments s
                LEFT JOIN capture_sessions cs ON s.capture_session_id = cs.id
                WHERE s.game_id = ? AND s.active = 1 {primary_clause} {run_clause}
                ORDER BY s.ordinal, s.level_number""",
            params,
        )
```

- [ ] **Step 5: Run the DB test to verify it passes**

Run: `python -m pytest tests/unit/db/test_db_segments.py::test_get_all_segments_with_model_includes_session_ordinal -v`
Expected: PASS.

- [ ] **Step 6: Add the field to the `ApiSegment` schema**

In `python/spinlab/api_schemas.py`, in `ApiSegment` (after `ordinal`, near line 230):

```python
    ordinal: int | None = None
    session_ordinal: int | None = None
    state_path: str | None = None
```

- [ ] **Step 7: Pass `session_ordinal` through the route**

In `python/spinlab/routes/segments.py`, `api_segments`, in the `ApiSegment(...)` construction (~line 53), add the field. `session_ordinal` is contract-guaranteed in the row (the query always selects it), so index directly:

```python
        out.append(ApiSegment(
            id=r["id"],
            game_id=r["game_id"],
            level_number=r["level_number"],
            start_type=r["start_type"],
            start_ordinal=r["start_ordinal"],
            end_type=r["end_type"],
            end_ordinal=r["end_ordinal"],
            description=r["description"],
            active=r["active"],
            ordinal=r["ordinal"],
            session_ordinal=r["session_ordinal"],
            state_path=r["state_path"],
            is_primary=bool(r.get("is_primary", 1)),
            has_cold_state=has_cold,
            start_waypoint_id=swid,
            end_waypoint_id=ewid,
            start_conditions=json.loads(start_wp.conditions_json) if start_wp else {},
            end_conditions=json.loads(end_wp.conditions_json) if end_wp else {},
        ))
```

- [ ] **Step 8: Regenerate frontend types**

Run: `cd frontend && npm run gen-types`
Expected: `frontend/src/api-types.ts` updated; `git diff frontend/src/api-types.ts` shows `session_ordinal?: number | null;` added to `ApiSegment`.

- [ ] **Step 9: Run the backend suite for the touched modules**

Run: `python -m pytest tests/unit/db tests/unit/routes -q`
Expected: PASS (no regressions). If a route test asserts the exact `/api/segments` payload shape and now fails on the new key, update it to include `session_ordinal`.

- [ ] **Step 10: Commit**

```bash
git add python/spinlab/api_schemas.py python/spinlab/db/segments.py python/spinlab/routes/segments.py frontend/src/api-types.ts tests/
git commit -m "feat(segments): expose session_ordinal on ApiSegment for merged Setup table

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Merged table — editable Name + chevron detail row

**Files:**
- Modify: `frontend/src/segments-view.ts` (`renderSegmentsView`, add `patchDescription`)
- Test: `frontend/src/segments-view.test.ts`

**Interfaces:**
- Consumes: `ApiSegment` (now with `session_ordinal`), `segmentName` from `./format`, `formatConditions` (existing), `patchIsPrimary` (existing).
- Produces:
  - `patchDescription(segmentId: string, description: string): Promise<void>` — PATCH `/api/segments/{id}` with `{ description }`.
  - `renderSegmentsView(container, segs)` now renders, per level section, a table whose header is `Segment | Name | Primary | Cold` plus a leading expander column. Each segment renders a base `<tr class="seg-row">` and a hidden detail `<tr class="seg-detail">` toggled by a `.seg-expander` button. The detail row shows Conditions and Session #.

- [ ] **Step 1: Write the failing test for the editable Name input**

```ts
describe("renderSegmentsView merged table", () => {
  it("renders an editable name input with description as value and segment label as placeholder", () => {
    const container = document.createElement("div");
    const segs = [
      { id: "a", level_number: 1, ordinal: 1, start_type: "entrance", start_ordinal: 0,
        end_type: "goal", end_ordinal: 0, start_conditions: {}, end_conditions: {},
        is_primary: true, has_cold_state: true, description: "Yoshi spot", session_ordinal: 2 },
    ] as any[];
    renderSegmentsView(container, segs);
    const input = container.querySelector("input.segment-name-input") as HTMLInputElement;
    expect(input).not.toBeNull();
    expect(input.value).toBe("Yoshi spot");
    expect(input.placeholder.length).toBeGreaterThan(0);
  });
});
```

- [ ] **Step 2: Write the failing test for the chevron-toggled detail row**

```ts
  it("hides the detail row until the expander is clicked, then shows Conditions and Session #", () => {
    const container = document.createElement("div");
    const segs = [
      { id: "a", level_number: 1, ordinal: 1, start_type: "entrance", start_ordinal: 0,
        end_type: "goal", end_ordinal: 0, start_conditions: { powerup: "cape" },
        end_conditions: {}, is_primary: true, has_cold_state: true,
        description: "", session_ordinal: 2 },
    ] as any[];
    renderSegmentsView(container, segs);
    const detail = container.querySelector("tr.seg-detail") as HTMLElement;
    expect(detail.style.display).toBe("none");
    (container.querySelector(".seg-expander") as HTMLElement).click();
    expect(detail.style.display).not.toBe("none");
    expect(detail.textContent).toContain("powerup=cape");
    expect(detail.textContent).toContain("2"); // session ordinal
  });
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd frontend && npx vitest run src/segments-view.test.ts`
Expected: FAIL (no `input.segment-name-input`, no `tr.seg-detail`).

- [ ] **Step 4: Add `patchDescription` and rewrite `renderSegmentsView`**

In `frontend/src/segments-view.ts`. Add the import for `segmentName`, add `patchDescription`, and replace `renderSegmentsView`'s row construction. Header becomes `Segment | Name | Primary | Cold` with a leading blank expander column. Each segment emits a base row + a hidden detail row.

```ts
import { shortEndpoint, segmentName } from "./format";
import type { ApiSegment } from "./types";

export async function patchDescription(segmentId: string, description: string): Promise<void> {
  const resp = await fetch(`/api/segments/${encodeURIComponent(segmentId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ description }),
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
  exp.textContent = "▸"; // right-pointing triangle
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
```

Note: `seg-detail-item` content here is text the test asserts on (`powerup=cape`, the session ordinal). Keep `formatConditions`, `groupByLevel`, `patchIsPrimary`, `coldCaptureButton*`, and `fetchSegments` exactly as they are.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd frontend && npx vitest run src/segments-view.test.ts`
Expected: the two new tests PASS. The pre-existing `renders a Cold column with checkmark...` test now fails (it asserts `✓`/`✗` and a `Cold` header position) — that is expected; it is superseded. Update it in the next step rather than leaving it red.

- [ ] **Step 6: Update the superseded Cold-column test**

Replace the old `renders a Cold column with checkmark when has_cold_state...` test to assert the new markup: a `.seg-cold` cell showing `✅` for `has_cold_state: true`. (The Fill button for the false case lands in Task 3 — for now assert the true case shows `✅`.)

```ts
  it("shows a cold-present marker when has_cold_state is true", () => {
    const container = document.createElement("div");
    const segs = [
      { id: "a", level_number: 1, ordinal: 1, start_type: "entrance", start_ordinal: 0,
        end_type: "goal", end_ordinal: 0, start_conditions: {}, end_conditions: {},
        is_primary: true, has_cold_state: true, description: "", session_ordinal: 1 },
    ] as any[];
    renderSegmentsView(container, segs);
    expect(container.querySelector(".seg-cold")?.textContent).toBe("✅");
  });
```

- [ ] **Step 7: Run the full frontend test file**

Run: `cd frontend && npx vitest run src/segments-view.test.ts`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/segments-view.ts frontend/src/segments-view.test.ts
git commit -m "feat(setup): editable name + chevron detail row in merged segments table

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Merged table — Cold/Fill action + Delete in detail row

**Files:**
- Modify: `frontend/src/segments-view.ts` (`appendSegmentRows`; add `deleteSegment`, `startFillGap`)
- Test: `frontend/src/segments-view.test.ts`

**Interfaces:**
- Produces:
  - `deleteSegment(segmentId: string): Promise<void>` — DELETE `/api/segments/{id}`.
  - `startFillGap(segmentId: string): Promise<{ status?: string }>` — POST `/api/segments/{id}/fill-gap`, returns parsed JSON.
  - Cold cell renders `✅` when `has_cold_state`, else a `button.btn-fill-gap` labelled "Fill". The detail row gains a `button.btn-x` Delete.

- [ ] **Step 1: Write the failing test for the Fill button**

```ts
  it("renders a Fill button (not a checkmark) when has_cold_state is false", () => {
    const container = document.createElement("div");
    const segs = [
      { id: "a", level_number: 1, ordinal: 1, start_type: "entrance", start_ordinal: 0,
        end_type: "goal", end_ordinal: 0, start_conditions: {}, end_conditions: {},
        is_primary: false, has_cold_state: false, description: "", session_ordinal: 1 },
    ] as any[];
    renderSegmentsView(container, segs);
    const btn = container.querySelector(".seg-cold .btn-fill-gap") as HTMLButtonElement;
    expect(btn).not.toBeNull();
    expect(btn.textContent).toContain("Fill");
  });
```

- [ ] **Step 2: Write the failing test for `deleteSegment` and `startFillGap` endpoints**

```ts
import { deleteSegment, startFillGap } from "./segments-view";

describe("segment action helpers", () => {
  it("deleteSegment issues DELETE to /api/segments/{id}", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) });
    vi.stubGlobal("fetch", fetchMock);
    await deleteSegment("seg1");
    expect(fetchMock).toHaveBeenCalledWith("/api/segments/seg1", { method: "DELETE" });
  });

  it("startFillGap POSTs to /api/segments/{id}/fill-gap", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ status: "started" }) });
    vi.stubGlobal("fetch", fetchMock);
    const out = await startFillGap("seg1");
    expect(fetchMock).toHaveBeenCalledWith("/api/segments/seg1/fill-gap", { method: "POST" });
    expect(out.status).toBe("started");
  });
});
```

Add `import { vi } from "vitest";` to the existing import line if not present.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd frontend && npx vitest run src/segments-view.test.ts`
Expected: FAIL (`deleteSegment`/`startFillGap` not exported; no `.btn-fill-gap`).

- [ ] **Step 4: Add the helpers and the Cold/Delete markup**

In `frontend/src/segments-view.ts`, add the helpers:

```ts
export async function deleteSegment(segmentId: string): Promise<void> {
  const resp = await fetch(`/api/segments/${encodeURIComponent(segmentId)}`, { method: "DELETE" });
  if (!resp.ok) throw new Error(`delete failed: ${resp.status}`);
}

export async function startFillGap(segmentId: string): Promise<{ status?: string }> {
  const resp = await fetch(`/api/segments/${encodeURIComponent(segmentId)}/fill-gap`, { method: "POST" });
  if (!resp.ok) throw new Error(`fill-gap failed: ${resp.status}`);
  return resp.json();
}
```

In `appendSegmentRows`, replace the Cold cell block with the conditional Fill button, and add a Delete button to the detail row:

```ts
  // Cold cell: present -> marker; missing -> Fill button
  const coldTd = document.createElement("td");
  coldTd.className = "seg-cold";
  if (seg.has_cold_state) {
    coldTd.textContent = "✅";
  } else {
    const fill = document.createElement("button");
    fill.className = "btn-fill-gap";
    fill.type = "button";
    fill.textContent = "❌ Fill";
    fill.addEventListener("click", async () => {
      const res = await startFillGap(seg.id);
      if (res.status === "started") { fill.textContent = "⏳"; fill.disabled = true; }
    });
    coldTd.appendChild(fill);
  }
  row.appendChild(coldTd);
```

And in the detail row, after the Conditions/Session spans, append a Delete button:

```ts
  const delBtn = document.createElement("button");
  delBtn.className = "btn-x";
  delBtn.type = "button";
  delBtn.textContent = "Delete";
  delBtn.addEventListener("click", async () => {
    if (!confirm("Remove this segment?")) return;
    await deleteSegment(seg.id);
    row.remove();
    detail.remove();
  });
  detailTd.appendChild(delBtn);
```

(Add a `seg.state_path` diagnostic span here too if `state_path` is present:
`if (seg.state_path) detailTd.insertAdjacentHTML("beforeend", \`<span class="seg-detail-item">state: ${seg.state_path}</span>\`);` — placed before the Delete button.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd frontend && npx vitest run src/segments-view.test.ts`
Expected: PASS.

- [ ] **Step 6: Add styles for the merged table**

In `frontend/style.css`, near the existing `#segment-table` block (~line 511), add (and leave the existing `.segment-name-input`, `.btn-x`, `.btn-fill-gap` rules in place — they are reused):

```css
/* Merged Setup segments table */
.segments-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.segments-table th {
  text-align: left; color: var(--text-dim);
  padding: 4px 5px; border-bottom: 1px solid var(--card);
}
.segments-table td { padding: 4px 5px; border-bottom: 1px solid var(--surface); }
.seg-expander {
  background: none; border: none; color: var(--text-dim);
  cursor: pointer; font-size: 11px; padding: 0 2px;
}
.seg-detail td { color: var(--text-dim); background: var(--surface); }
.seg-detail-item { margin-right: 16px; }
```

- [ ] **Step 7: Build and run the full frontend suite**

Run: `cd frontend && npm run build && npm test`
Expected: build succeeds, all frontend tests PASS.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/segments-view.ts frontend/src/segments-view.test.ts frontend/style.css
git commit -m "feat(setup): cold-fill button and delete action in merged segments table

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Wire the single merged table into Setup; remove the `manage.ts` editor

**Files:**
- Modify: `frontend/index.html` (Setup "Segments" section + bottom container)
- Modify: `frontend/src/manage.ts` (`updateManage`, `fetchManage`, `initManageTab`)
- Modify: `frontend/src/manage.test.ts` (DOM stub)

**Interfaces:**
- Consumes: `renderSegmentsView` / `fetchSegments` (Task 2-3), `fetchAndRenderSegments` in `app.ts` (already targets `#segments-view-container`).

- [ ] **Step 1: Restructure the Setup HTML**

In `frontend/index.html`, in the "Segments" section (currently lines ~137-158), replace the `<table id="segment-table">...</table>` with the container the merged renderer fills:

```html
          <!-- Segments table (merged: run-scoped, level-grouped, editable) -->
          <div class="manage-section">
            <h3>Segments</h3>
            <div id="segments-toolbar">
              <button id="btn-start-cold-fill" class="btn-primary" style="display:none"
                      title="Capture the missing cold states for the active run">Start Cold Capture</button>
            </div>
            <div id="cold-fill-banner" style="display:none"></div>
            <div id="segments-view-container"></div>
          </div>
```

Then DELETE the old standalone container near the bottom of the Setup section (currently line ~168-169):

```html
          <!-- former Segments tab — populated by fetchAndRenderSegments on Setup show -->
          <div id="segments-view-container"></div>
```

(There must be exactly ONE `#segments-view-container` after this step — in the Segments section.)

- [ ] **Step 2: Remove the segment-table render + fetch from `manage.ts`**

In `frontend/src/manage.ts`:
- In `fetchManage`, delete the block that fetches `/api/references/{...}/segments` and the `segments` variable, and call `updateManage(refs)` with refs only. Keep the `/api/references` fetch (replay targeting needs it).
- Change `updateManage(refs, segments)` to `updateManage(refs)` and delete the `const body = document.getElementById("segment-body")!; ...; segments.forEach(...)` block at the end (currently ~lines 101-125).
- Remove the now-unused `ReferenceSegment` import and the `segments` parameter type.

The resulting `fetchManage`:

```ts
export async function fetchManage(): Promise<void> {
  const refsData = await fetchJSON<{ references: Reference[] }>("/api/references");
  if (!refsData) return;
  updateManage(refsData.references);
}
```

- [ ] **Step 3: Remove the `#segment-body` listeners from `initManageTab`**

In `frontend/src/manage.ts`, delete the two `document.getElementById("segment-body")!.addEventListener(...)` blocks (the `focusout` name PATCH and the `click` fill-gap/delete handler, currently ~lines 160-189). Their behavior now lives in `segments-view.ts`'s inline per-row listeners. Keep all the button handlers (ref-start, replay, resume, etc.).

- [ ] **Step 4: Update `manage.test.ts` DOM stub**

In `frontend/src/manage.test.ts`, remove the `<table><tbody id="segment-body"></tbody></table>` line from the `beforeEach` `document.body.innerHTML` (line 25). No test asserts on `#segment-body`; the remaining button tests are unaffected. Also confirm the Fast Replay / Replay tests still pass — `fetchManage` now makes only the `/api/references` fetch, so their `mockImplementation` that also returns `{ segments: [] }` for the segments URL is simply never hit for segments; that is fine.

- [ ] **Step 5: Run the frontend suite**

Run: `cd frontend && npm run build && npm test`
Expected: build succeeds; all tests PASS (manage + segments-view).

- [ ] **Step 6: Typecheck the frontend**

Run: `cd frontend && npm run typecheck`
Expected: no new errors (the removed `ReferenceSegment` import and `segments` param must be fully gone).

- [ ] **Step 7: Manual smoke (main checkout only)**

Launch the dashboard, open Setup. Verify: one Segments table grouped by Level; editing a name persists on blur; toggling Primary persists; clicking a row chevron reveals Conditions/Session/Delete; the Fill button appears for missing-cold segments; no second segment table remains below "Clear All Data".

(If running in a worktree, skip — port-binding/dashboard needs the main checkout per CLAUDE.md.)

- [ ] **Step 8: Commit**

```bash
git add frontend/index.html frontend/src/manage.ts frontend/src/manage.test.ts
git commit -m "feat(setup): use the single merged segments table; drop the manage.ts editor

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Delete the now-dead editor endpoint

**Files:**
- Modify: `python/spinlab/routes/reference.py` (delete `get_reference_segments`)
- Modify: `python/spinlab/db/capture_runs.py` (delete `get_segments_by_reference`)
- Modify: `python/spinlab/api_schemas.py` (delete `ReferenceSegment`, `ReferenceSegmentsResponse`)
- Modify: `frontend/src/types.ts` (drop the `ReferenceSegment` re-export if present)
- Modify/Delete tests: `tests/unit/db/test_db_references.py`, `tests/unit/routes/test_dashboard_references.py` (remove cases targeting the deleted endpoint/method)

**Interfaces:**
- Produces: nothing. This is dead-code removal.

- [ ] **Step 1: Confirm no remaining consumer**

Run: `grep -rn "get_segments_by_reference\|references/.*/segments\|ReferenceSegmentsResponse\|\bReferenceSegment\b" python/ frontend/src/ tests/`
Expected: matches only in the files listed above (the route, the DB method, the schemas, their re-export, and tests that exercise them). If any *production* consumer outside this set appears, STOP and report it — do not delete; the merge does not require this cleanup to function.

- [ ] **Step 2: Write/adjust the failing test (removal assertion)**

Identify the test(s) in `tests/unit/routes/test_dashboard_references.py` that GET `/api/references/{id}/segments`. The endpoint will 404 after removal. Rather than asserting a 404 (brittle), DELETE those specific test cases. Likewise delete the `get_segments_by_reference` cases in `tests/unit/db/test_db_references.py`. Run the file first to know exactly which tests reference it:

Run: `grep -n "get_segments_by_reference\|/segments" tests/unit/db/test_db_references.py tests/unit/routes/test_dashboard_references.py`
Expected: a short list of test functions to remove.

- [ ] **Step 3: Delete the route, DB method, and schemas**

- In `python/spinlab/routes/reference.py`, delete the `get_reference_segments` function (~lines 184-186) and its now-unused `ReferenceSegmentsResponse` import.
- In `python/spinlab/db/capture_runs.py`, delete `get_segments_by_reference` (~lines 311-330).
- In `python/spinlab/api_schemas.py`, delete `class ReferenceSegment` (~lines 392-406) and `class ReferenceSegmentsResponse` (~lines 409-410).
- In `frontend/src/types.ts`, delete the `ReferenceSegment` re-export line if it exists.

- [ ] **Step 4: Delete the identified dead tests**

Remove the test functions identified in Step 2 from both test files. If removing them leaves an unused import in those files, remove the import too.

- [ ] **Step 5: Regenerate frontend types**

Run: `cd frontend && npm run gen-types`
Expected: `ReferenceSegment` / `ReferenceSegmentsResponse` disappear from `frontend/src/api-types.ts`.

- [ ] **Step 6: Run lint + typecheck to catch dangling references**

Run: `ruff check python/spinlab/routes/reference.py python/spinlab/db/capture_runs.py python/spinlab/api_schemas.py && cd frontend && npm run typecheck`
Expected: no errors (no dangling imports of the deleted symbols).

- [ ] **Step 7: Commit**

```bash
git add python/ frontend/src/types.ts frontend/src/api-types.ts tests/
git commit -m "refactor(reference): delete dead get_segments_by_reference editor endpoint

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Full-suite verification

- [ ] **Step 1: Run the complete test suite**

Run: `python -m pytest`
Expected: all pass, ZERO skips that are not pre-accepted `skipif`. Emulator tests must actually run (RA self-launches). If the emulator tests skip with a launch failure, that is a failure to surface — do not treat green-with-skips as done. Frontend smoke tests require `cd frontend && npm run build` to have run (Task 3/4 already built).

- [ ] **Step 2: Run pyright + ruff on touched Python**

Run: `npx pyright python/spinlab/db/segments.py python/spinlab/routes/segments.py python/spinlab/api_schemas.py && ruff check python/`
Expected: no NEW errors vs the baseline (pre-existing tracked errors are acceptable).

- [ ] **Step 3: Final commit (if any lint fixes were needed)**

```bash
git add -A
git commit -m "chore(setup): lint/type cleanup after segments-table merge

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review Notes

- **Spec coverage:** Run-scoped single table (Tasks 2-4); group-by-Level (existing `groupByLevel`, kept); base columns Name/Segment/Primary/Cold + chevron (Task 2-3); detail row Conditions/Session #/state path/Delete (Task 2-3); one access-only API change = `session_ordinal` on `ApiSegment` (Task 1); preserved behaviors — name PATCH, primary PATCH, fill-gap, delete-with-confirm, cold-capture toolbar/banner stay above table (Tasks 3-4); removed manage editor + duplicate container (Task 4); dead endpoint deletion (Task 5); Known Limitation (Name/Primary on shared geography row) is inherent — no task needed, documented in spec.
- **Placeholder scan:** none — every code step shows full code; the one "locate the exact test file" step (Task 1 Step 1, Task 5 Step 2) is a grep with a concrete expected result, not a TODO.
- **Type consistency:** `session_ordinal` (snake_case backend / generated TS) used consistently; helper names `patchDescription` / `deleteSegment` / `startFillGap` match between their definitions (Tasks 2-3) and uses. `renderSegmentsView(container, segs)` signature unchanged.

# Dashboard Segments View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Segments view to the dashboard that lists every segment grouped by game + level, displaying start/end waypoint conditions, and lets the user toggle `is_primary` per segment.

**Architecture:** A new PATCH endpoint for segments. A new TypeScript module renders the list grouped by level. Uses the `/api/segments` response already extended in Plan 1 Task 13 (all segments returned with `primary_only=False`, including `start_conditions`/`end_conditions`). The UI is a simple HTML table per level with a checkbox toggle that hits PATCH.

**Tech Stack:** FastAPI, TypeScript + Vite, vanilla DOM (no framework).

**Prerequisite:** Plan 1 merged (`/api/segments` exposes waypoints + conditions + is_primary).

---

## File Structure

**New files:**
- `frontend/src/segments-view.ts` — rendering + event handlers
- `tests/test_segments_route_patch.py` — route test for PATCH

**Modified files:**
- `python/spinlab/routes/segments.py` — add PATCH endpoint
- `python/spinlab/db/segments.py` — add `set_segment_is_primary`
- `frontend/src/types.ts` — (already extended in Plan 1)
- `frontend/src/main.ts` (or wherever pages are registered) — mount the segments view
- `frontend/index.html` or relevant template — add a container for the view

---

## Task 1: DB helper — set_segment_is_primary

**Files:**
- Modify: `python/spinlab/db/segments.py`
- Append: `tests/test_waypoints_db.py` (or new file)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_segments_is_primary.py
from spinlab.db import Database
from spinlab.models import Segment, Waypoint

def _seed_segment(db, seg_id="s1", primary=True):
    db.upsert_game("g", "Game", "any%")
    wp_a = Waypoint.make("g", 1, "entrance", 0, {})
    wp_b = Waypoint.make("g", 1, "goal", 0, {})
    db.upsert_waypoint(wp_a)
    db.upsert_waypoint(wp_b)
    seg = Segment(
        id=seg_id, game_id="g", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        start_waypoint_id=wp_a.id, end_waypoint_id=wp_b.id,
        is_primary=primary,
    )
    db.upsert_segment(seg)
    return seg

def test_set_segment_is_primary_toggles():
    db = Database(":memory:")
    seg = _seed_segment(db, primary=True)
    db.set_segment_is_primary(seg.id, False)
    row = db.conn.execute(
        "SELECT is_primary FROM segments WHERE id = ?", (seg.id,)
    ).fetchone()
    assert row[0] == 0
    db.set_segment_is_primary(seg.id, True)
    row = db.conn.execute(
        "SELECT is_primary FROM segments WHERE id = ?", (seg.id,)
    ).fetchone()
    assert row[0] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_segments_is_primary.py -v`
Expected: FAIL — `set_segment_is_primary` undefined.

- [ ] **Step 3: Add method to SegmentsMixin**

In `python/spinlab/db/segments.py`:

```python
def set_segment_is_primary(self, segment_id: str, is_primary: bool) -> None:
    now = datetime.now(UTC).isoformat()
    self.conn.execute(
        "UPDATE segments SET is_primary = ?, updated_at = ? WHERE id = ?",
        (int(is_primary), now, segment_id),
    )
    self.conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_segments_is_primary.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db/segments.py tests/test_segments_is_primary.py
git commit -m "feat(db): add set_segment_is_primary"
```

---

## Task 2: PATCH /api/segments/:id endpoint

**Files:**
- Modify: `python/spinlab/routes/segments.py`
- Create: `tests/test_segments_route_patch.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_segments_route_patch.py
# Adapt to the project's FastAPI test fixture style.

def test_patch_segment_toggles_primary(client, seeded_segment_id):
    resp = client.patch(
        f"/api/segments/{seeded_segment_id}",
        json={"is_primary": False},
    )
    assert resp.status_code == 200
    assert resp.json()["is_primary"] is False
    # Second toggle
    resp = client.patch(
        f"/api/segments/{seeded_segment_id}",
        json={"is_primary": True},
    )
    assert resp.status_code == 200
    assert resp.json()["is_primary"] is True

def test_patch_segment_unknown_id_returns_404(client):
    resp = client.patch(
        "/api/segments/nonexistent",
        json={"is_primary": True},
    )
    assert resp.status_code == 404
```

(Create a `seeded_segment_id` fixture in the test file or `conftest.py` that inserts one segment via the Database fixture.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_segments_route_patch.py -v`
Expected: FAIL — endpoint not found.

- [ ] **Step 3: Add endpoint**

In `python/spinlab/routes/segments.py`:

```python
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

class SegmentPatch(BaseModel):
    is_primary: bool

@router.patch("/api/segments/{segment_id}")
def patch_segment(segment_id: str, body: SegmentPatch, db=...):  # use project's DI
    if not db.segment_exists(segment_id):
        raise HTTPException(status_code=404, detail="segment not found")
    db.set_segment_is_primary(segment_id, body.is_primary)
    return {"ok": True, "id": segment_id, "is_primary": body.is_primary}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_segments_route_patch.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/routes/segments.py tests/test_segments_route_patch.py
git commit -m "feat(api): PATCH /api/segments/:id to toggle is_primary"
```

---

## Task 3: Frontend — segments-view module

**Files:**
- Create: `frontend/src/segments-view.ts`
- Create: `frontend/src/segments-view.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
// frontend/src/segments-view.test.ts
import { describe, it, expect } from "vitest";
import { groupByLevel, formatConditions } from "./segments-view";

describe("groupByLevel", () => {
  it("groups segments by level_number preserving ordinal order", () => {
    const segs = [
      { id: "a", level_number: 2, ordinal: 3, start_conditions: {}, end_conditions: {}, is_primary: true },
      { id: "b", level_number: 1, ordinal: 1, start_conditions: {}, end_conditions: {}, is_primary: true },
      { id: "c", level_number: 1, ordinal: 2, start_conditions: {}, end_conditions: {}, is_primary: false },
    ] as any[];
    const grouped = groupByLevel(segs);
    expect(Object.keys(grouped)).toEqual(["1", "2"]);
    expect(grouped["1"].map(s => s.id)).toEqual(["b", "c"]);
    expect(grouped["2"].map(s => s.id)).toEqual(["a"]);
  });
});

describe("formatConditions", () => {
  it("renders empty as dash", () => {
    expect(formatConditions({})).toBe("—");
  });
  it("renders key=value pairs", () => {
    expect(formatConditions({ powerup: "big" })).toBe("powerup=big");
  });
  it("joins multiple with comma", () => {
    expect(formatConditions({ powerup: "big", on_yoshi: true })).toMatch(/powerup=big/);
    expect(formatConditions({ powerup: "big", on_yoshi: true })).toMatch(/on_yoshi=true/);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- segments-view`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement segments-view**

```ts
// frontend/src/segments-view.ts
import type { SegmentApiRow } from "./types";

export function groupByLevel(segs: SegmentApiRow[]): Record<string, SegmentApiRow[]> {
  const out: Record<string, SegmentApiRow[]> = {};
  for (const s of segs) {
    const key = String(s.level_number);
    (out[key] ||= []).push(s);
  }
  for (const key of Object.keys(out)) {
    out[key].sort((a, b) => (a.ordinal ?? 0) - (b.ordinal ?? 0));
  }
  // Return keys in numeric order
  const ordered: Record<string, SegmentApiRow[]> = {};
  for (const key of Object.keys(out).sort((a, b) => Number(a) - Number(b))) {
    ordered[key] = out[key];
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

export function renderSegmentsView(container: HTMLElement, segs: SegmentApiRow[]): void {
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
      "<thead><tr><th>Start</th><th>End</th><th>Attempts</th><th>Primary</th></tr></thead>";
    const tbody = document.createElement("tbody");
    for (const seg of grouped[level]) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${seg.start_type}.${seg.start_ordinal} [${formatConditions(seg.start_conditions)}]</td>` +
        `<td>${seg.end_type}.${seg.end_ordinal} [${formatConditions(seg.end_conditions)}]</td>` +
        `<td></td>` +
        `<td></td>`;
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = seg.is_primary;
      cb.addEventListener("change", async () => {
        cb.disabled = true;
        try { await patchIsPrimary(seg.id, cb.checked); seg.is_primary = cb.checked; }
        catch (err) { cb.checked = seg.is_primary; alert(String(err)); }
        finally { cb.disabled = false; }
      });
      tr.children[3].appendChild(cb);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    section.appendChild(table);
    container.appendChild(section);
  }
}

export async function fetchSegments(gameId: string): Promise<SegmentApiRow[]> {
  const resp = await fetch(`/api/segments?game_id=${encodeURIComponent(gameId)}`);
  if (!resp.ok) throw new Error(`fetch failed: ${resp.status}`);
  return resp.json();
}
```

- [ ] **Step 4: Ensure SegmentApiRow type has needed fields**

Check `frontend/src/types.ts` includes:

```ts
export interface SegmentApiRow {
  id: string;
  game_id: string;
  level_number: number;
  start_type: string;
  start_ordinal: number;
  end_type: string;
  end_ordinal: number;
  ordinal: number | null;
  is_primary: boolean;
  start_waypoint_id: string | null;
  end_waypoint_id: string | null;
  start_conditions: Record<string, string | boolean>;
  end_conditions: Record<string, string | boolean>;
}
```

Plan 1 Task 13 added most of these; confirm all are present. Add missing ones.

- [ ] **Step 5: Run tests**

Run: `cd frontend && npm test -- segments-view`
Expected: PASS.

Run: `cd frontend && npm run typecheck`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/segments-view.ts frontend/src/segments-view.test.ts frontend/src/types.ts
git commit -m "feat(frontend): add segments-view module with is_primary toggle"
```

---

## Task 4: Wire segments-view into the dashboard

**Files:**
- Modify: `frontend/src/main.ts` (or the dashboard entry module)
- Modify: `frontend/index.html` (or whichever template renders the nav)

- [ ] **Step 1: Locate the dashboard nav / router**

Run: `grep -rn "addEventListener\|router\|nav" frontend/src/ --include="*.ts" | head -n 20`
Run: `grep -rn "<nav\|<a href" frontend/index.html`

Identify how existing views (e.g. practice view, capture view) are registered and shown/hidden.

- [ ] **Step 2: Add a Segments nav entry + container**

In `frontend/index.html`, add a nav link and container div:

```html
<a href="#segments" data-view="segments">Segments</a>
...
<div id="view-segments" class="view" hidden></div>
```

- [ ] **Step 3: Register the view**

In `frontend/src/main.ts` (or wherever views are mounted):

```ts
import { fetchSegments, renderSegmentsView } from "./segments-view";

async function showSegmentsView(gameId: string): Promise<void> {
  const container = document.getElementById("view-segments")!;
  container.hidden = false;
  const segs = await fetchSegments(gameId);
  renderSegmentsView(container, segs);
}
```

Hook into the existing nav-click handler to call `showSegmentsView(currentGameId)` when the Segments link is clicked.

- [ ] **Step 4: Type-check + build**

Run: `cd frontend && npm run typecheck && npm run build`
Expected: no errors.

- [ ] **Step 5: Manual smoke test**

1. Start dashboard.
2. Click Segments nav.
3. Verify a level grouping appears with at least one segment (seed via a reference run if DB empty).
4. Toggle the primary checkbox; refresh; confirm value persists.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/main.ts frontend/index.html
git commit -m "feat(frontend): mount segments view in dashboard nav"
```

---

## Task 5: End-to-end verification

- [ ] **Step 1: Run full fast suite**

Run: `pytest -m "not (emulator or slow)"`
Expected: PASS.

- [ ] **Step 2: Run frontend tests**

Run: `cd frontend && npm test`
Expected: PASS.

- [ ] **Step 3: Build frontend**

Run: `cd frontend && npm run build`
Expected: no errors.

- [ ] **Step 4: Manual multi-route smoke test**

1. Do two reference runs through the same level with different powerups.
2. Open Segments view.
3. Expected: the level shows two rows with differing `start_conditions`.
4. Toggle one as primary; verify practice loop only serves that one.

---

## Self-Review Checklist

- [x] Spec requirement: dashboard segments view grouped by level → Task 3/4
- [x] `is_primary` toggle in UI → Task 3
- [x] PATCH endpoint for is_primary → Task 2
- [x] DB helper → Task 1
- [x] Duplicate-level routes legibly displayed → Task 3 (two rows per level with differing conditions)

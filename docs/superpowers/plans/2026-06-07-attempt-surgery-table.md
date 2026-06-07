# Attempt Surgery Table Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a sortable per-segment attempt table to the segment-detail view with reversible episode-level invalidate + auto per-segment recalc, so bad attempts can be cleaned before the golden data session.

**Architecture:** A new read route lists every episode for a segment (carrying `id` + `invalidated`, which `/history` strips); the existing `PATCH /api/attempts/{id}` gains a per-segment model_state rebuild (`update_state_after_episode`) so Best/Floor/Room update on toggle. Frontend adds a pure sort helper + an attempt-table renderer mounted under the existing chart. Best (`gold_ms`) is already computed live via `compute_golds`, so only the model_state cache needs rebuilding.

**Tech Stack:** Python (FastAPI, pydantic dataclasses), TypeScript (Vite, Vitest, happy-dom).

Spec: `docs/superpowers/specs/2026-06-07-attempt-surgery-table-design.md`

---

## File Structure

- `python/spinlab/api_schemas.py` — new `SurgeryAttempt` + `SurgeryAttemptsResponse` schemas.
- `python/spinlab/routes/attempts.py` — new `GET /api/segments/{id}/attempts` route + a pure `surgery_rows()` helper; PATCH gains the recalc call.
- `python/spinlab/db/attempts.py` — `get_attempt_segment_id(attempt_id)` helper.
- `frontend/src/format.ts` — `formatAgo`.
- `frontend/src/attempt-table.ts` (NEW) — pure sort state/comparator + `renderAttemptTable`.
- `frontend/src/segment-detail.ts` — mount the table under the chart; wire invalidate → PATCH → refetch.
- Tests: `tests/unit/test_surgery_rows.py` (new), `tests/unit/test_dashboard_integration.py`, `tests/unit/db/test_event_level_attempts.py`, `frontend/src/format.test.ts`, `frontend/src/attempt-table.test.ts` (new).

---

## Task 1: Surgery list route + pure row builder (BE)

**Files:**
- Modify: `python/spinlab/api_schemas.py`
- Modify: `python/spinlab/routes/attempts.py`
- Test: `tests/unit/test_surgery_rows.py` (new)

- [ ] **Step 1: Write the failing test** for the pure row builder.

Create `tests/unit/test_surgery_rows.py`:

```python
from spinlab.routes.attempts import surgery_rows


def _att(id, created_at, clean_tail_ms, time_ms, deaths, completed, invalidated):
    # Mirrors the dict shape returned by Database.get_segment_attempts.
    return {
        "id": id, "created_at": created_at, "clean_tail_ms": clean_tail_ms,
        "time_ms": time_ms, "deaths": deaths, "completed": completed,
        "invalidated": invalidated,
    }


def test_surgery_rows_assigns_chronological_order_and_floor():
    # Out-of-order input; order must be by created_at ascending (1-based).
    raw = [
        _att(50, "2026-01-01T00:02:00+00:00", 13000, 19000, 1, 1, 0),
        _att(10, "2026-01-01T00:00:00+00:00", 15000, 15000, 0, 1, 0),
        _att(30, "2026-01-01T00:01:00+00:00", 11000, 11000, 0, 1, 0),  # floor
    ]
    rows = surgery_rows(raw)
    by_id = {r["id"]: r for r in rows}
    assert by_id[10]["order"] == 1
    assert by_id[30]["order"] == 2
    assert by_id[50]["order"] == 3
    # Floor = the lowest clean_tail among valid completed rows (11000 -> id 30).
    assert by_id[30]["is_floor"] is True
    assert by_id[10]["is_floor"] is False
    assert by_id[50]["is_floor"] is False
    # total_ms mirrors time_ms; invalidated/completed pass through as bools.
    assert by_id[50]["total_ms"] == 19000
    assert by_id[10]["invalidated"] is False


def test_surgery_rows_floor_ignores_invalidated_and_incomplete():
    raw = [
        _att(1, "2026-01-01T00:00:00+00:00", 8000, 8000, 0, 1, 1),   # faster but INVALID
        _att(2, "2026-01-01T00:01:00+00:00", 9000, 9000, 0, 0, 0),   # faster but INCOMPLETE (clean_tail set defensively)
        _att(3, "2026-01-01T00:02:00+00:00", 12000, 12000, 0, 1, 0),  # the real floor
    ]
    rows = surgery_rows(raw)
    by_id = {r["id"]: r for r in rows}
    assert by_id[3]["is_floor"] is True
    assert by_id[1]["is_floor"] is False
    assert by_id[2]["is_floor"] is False


def test_surgery_rows_incomplete_has_none_clean_tail_no_floor():
    raw = [_att(1, "2026-01-01T00:00:00+00:00", None, 9000, 2, 0, 0)]
    rows = surgery_rows(raw)
    assert rows[0]["clean_tail_ms"] is None
    assert rows[0]["is_floor"] is False
```

- [ ] **Step 2: Run, verify FAIL.**

Run: `python -m pytest tests/unit/test_surgery_rows.py -v`
Expected: FAIL — `cannot import name 'surgery_rows'`.

- [ ] **Step 3: Add the schemas** to `python/spinlab/api_schemas.py` (near the other response models, e.g. after `SegmentHistory`):

```python
class SurgeryAttempt(_BaseResponse):
    id: int
    order: int
    clean_tail_ms: int | None = None
    total_ms: int | None = None
    deaths: int
    created_at: str
    completed: bool
    invalidated: bool
    is_floor: bool


class SurgeryAttemptsResponse(_BaseResponse):
    segment_id: str
    attempts: list[SurgeryAttempt]
```

- [ ] **Step 4: Add the pure helper + route** to `python/spinlab/routes/attempts.py`. The file currently imports `APIRouter, Depends, HTTPException`, `AttemptPatchRequest/Response`, `Database`, `get_db`. Add `get_segment_attempts` usage via the injected `db`, and a new GET route. Insert the helper above the router uses and the route below the existing PATCH:

```python
def surgery_rows(attempts: list[dict]) -> list[dict]:
    """Build SurgeryAttempt dicts from Database.get_segment_attempts output.

    Order is chronological (created_at asc, 1-based). is_floor marks the row
    whose clean_tail equals the lowest clean_tail among valid (completed,
    non-invalidated) episodes — matching how compute_golds picks clean_gold.
    """
    ordered = sorted(attempts, key=lambda a: a["created_at"])
    floor_ms = min(
        (a["clean_tail_ms"] for a in ordered
         if a["completed"] and not a["invalidated"] and a["clean_tail_ms"] is not None),
        default=None,
    )
    rows: list[dict] = []
    for i, a in enumerate(ordered, start=1):
        ct = a["clean_tail_ms"]
        rows.append({
            "id": a["id"],
            "order": i,
            "clean_tail_ms": ct,
            "total_ms": a["time_ms"],
            "deaths": a["deaths"],
            "created_at": a["created_at"],
            "completed": bool(a["completed"]),
            "invalidated": bool(a["invalidated"]),
            "is_floor": floor_ms is not None and ct == floor_ms
                        and bool(a["completed"]) and not bool(a["invalidated"]),
        })
    return rows
```

Then add the route (and import the response model at the top of the file: extend the existing `from spinlab.api_schemas import ...` line to include `SurgeryAttemptsResponse`):

```python
@router.get(
    "/segments/{segment_id}/attempts",
    response_model=SurgeryAttemptsResponse,
)
def list_segment_attempts(
    segment_id: str,
    db: Database = Depends(get_db),
):
    """Every episode for a segment (incl. invalidated), for the surgery table.

    Distinct from /history (which filters invalidated and omits the id, feeding
    the trend chart). Carries id + invalidated so the row can be toggled.
    """
    raw = db.get_segment_attempts(segment_id)
    return {"segment_id": segment_id, "attempts": surgery_rows(raw)}
```

- [ ] **Step 5: Run, verify PASS.**

Run: `python -m pytest tests/unit/test_surgery_rows.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit.**

```bash
git add python/spinlab/api_schemas.py python/spinlab/routes/attempts.py tests/unit/test_surgery_rows.py
git commit -m "feat(surgery): segment attempts list route + pure row builder"
```

---

## Task 2: PATCH recalc + segment lookup (BE)

**Files:**
- Modify: `python/spinlab/db/attempts.py`
- Modify: `python/spinlab/routes/attempts.py`
- Test: `tests/unit/db/test_event_level_attempts.py`

- [ ] **Step 1: Write the failing test** for the db helper + the recalc effect. Add to `tests/unit/db/test_event_level_attempts.py` (it has `db_with_segment` + `log_event_attempt`):

```python
def test_get_attempt_segment_id(db_with_segment):
    db = db_with_segment
    db.log_event_attempt(EventAttempt(
        segment_id="seg1", episode_id="epX", session_id="sess1",
        source=AttemptSource.PRACTICE, outcome=AttemptOutcome.SURVIVED,
        time_ms=12000, created_at=datetime.now(UTC),
    ))
    att = db.get_segment_attempts("seg1")[0]
    assert db.get_attempt_segment_id(att["id"]) == "seg1"
    assert db.get_attempt_segment_id(999_999) is None
```

- [ ] **Step 2: Run, verify FAIL.**

Run: `python -m pytest tests/unit/db/test_event_level_attempts.py::test_get_attempt_segment_id -v`
Expected: FAIL — `AttributeError: ... 'get_attempt_segment_id'`.

- [ ] **Step 3: Add the db helper** to `python/spinlab/db/attempts.py` (near `set_attempt_invalidated`, ~line 405):

```python
    def get_attempt_segment_id(self, attempt_id: int) -> str | None:
        """The segment_id of the event row ``attempt_id``, or None if absent."""
        row = self.conn.execute(
            "SELECT segment_id FROM attempts WHERE id = ?", (attempt_id,),
        ).fetchone()
        return row["segment_id"] if row is not None else None
```

- [ ] **Step 4: Run, verify PASS.**

Run: `python -m pytest tests/unit/db/test_event_level_attempts.py::test_get_attempt_segment_id -v`
Expected: PASS.

- [ ] **Step 5: Wire recalc into the PATCH route.** In `python/spinlab/routes/attempts.py`, add `get_session` + `SessionManager` and trigger a per-segment rebuild after the flag flip. Change the imports (add `from spinlab.session_manager import SessionManager` and extend `from ._deps import get_db` to also import `get_session`) and the handler:

```python
@router.patch("/attempts/{attempt_id}", response_model=AttemptPatchResponse)
def patch_attempt(
    attempt_id: int,
    body: AttemptPatchRequest,
    db: Database = Depends(get_db),
    session: SessionManager = Depends(get_session),
):
    """Toggle invalidation on an attempt (episode-level) and recalc its segment.

    set_attempt_invalidated flips the whole episode. We then rebuild that
    segment's model_state so Best/Floor/Room reflect the change immediately
    (Best/gold is computed live; Expected/Floor live in the model_state cache).
    """
    if not db.attempt_exists(attempt_id):
        raise HTTPException(status_code=404, detail="attempt not found")
    db.set_attempt_invalidated(attempt_id, body.invalidated)
    segment_id = db.get_attempt_segment_id(attempt_id)
    if segment_id is not None and session.game_id is not None:
        # update_state_after_episode uses only segment_id (not game_id) to
        # rebuild + save model_state, so the active scheduler can recalc any
        # of its segments. No active game -> skip; the cache refreshes later.
        session.get_scheduler().update_state_after_episode(segment_id)
    return {"ok": True, "id": attempt_id, "invalidated": body.invalidated}
```

- [ ] **Step 6: Write the recalc-effect test.** Add to `tests/unit/db/test_event_level_attempts.py` — invalidating the gold episode and rebuilding must raise the segment's model floor:

```python
def test_invalidate_then_rebuild_updates_model_floor(db_with_segment):
    import json

    from spinlab.scheduler import Scheduler

    db = db_with_segment
    # Two clean clears: a fast 9s (the floor) and a slower 14s.
    for ep, t in [("epFast", 9000), ("epSlow", 14000)]:
        db.log_event_attempt(EventAttempt(
            segment_id="seg1", episode_id=ep, session_id="sess1",
            source=AttemptSource.PRACTICE, outcome=AttemptOutcome.SURVIVED,
            time_ms=t, created_at=datetime.now(UTC),
        ))
    sched = Scheduler(db, "g1")  # 3rd arg is an estimator-NAME string; default is fine
    sched.update_state_after_episode("seg1")
    out0 = json.loads(db.load_model_state("seg1", "em_suite_sampler")["output_json"])
    assert out0["total"]["floor_ms"] == 9000

    # Invalidate the fast episode, rebuild -> floor rises to 14000.
    fast_id = next(a["id"] for a in db.get_segment_attempts("seg1")
                   if a["clean_tail_ms"] == 9000)
    db.set_attempt_invalidated(fast_id, True)
    sched.update_state_after_episode("seg1")
    out1 = json.loads(db.load_model_state("seg1", "em_suite_sampler")["output_json"])
    assert out1["total"]["floor_ms"] == 14000
```

(`Scheduler.__init__(db, game_id, estimator_name="em_suite_sampler", *, ...)` builds its own estimator internally; `db.load_model_state(segment_id, estimator)` returns a row dict whose `output_json` is the stored `ModelOutput` JSON — both verified against current code.)

- [ ] **Step 7: Run, verify PASS.**

Run: `python -m pytest tests/unit/db/test_event_level_attempts.py -v`
Expected: PASS (both new tests + the rest of the file green).

- [ ] **Step 8: Commit.**

```bash
git add python/spinlab/db/attempts.py python/spinlab/routes/attempts.py tests/unit/db/test_event_level_attempts.py
git commit -m "feat(surgery): PATCH recalcs the segment's model_state on invalidate"
```

---

## Task 3: `formatAgo` (FE)

**Files:**
- Modify: `frontend/src/format.ts`
- Test: `frontend/src/format.test.ts`

- [ ] **Step 1: Regenerate FE types** (the BE added schemas in Task 1):

Run: `cd "c:/Users/thedo/git/spinlab/frontend" && npm run gen-types`
Expected: `api-types.ts` regenerates and includes `SurgeryAttempt`. (gitignored — nothing to commit.)

- [ ] **Step 2: Write the failing test.** Add to `frontend/src/format.test.ts` (add `formatAgo` to the existing `./format` import):

```ts
describe("formatAgo", () => {
  const now = Date.parse("2026-06-07T12:00:00Z");
  const ago = (iso: string) => formatAgo(iso, now);
  it("renders compact units, months skipped", () => {
    expect(ago("2026-06-07T11:59:57Z")).toBe("now");   // < 10s
    expect(ago("2026-06-07T11:59:13Z")).toBe("47s");
    expect(ago("2026-06-07T11:26:00Z")).toBe("34m");
    expect(ago("2026-06-07T05:00:00Z")).toBe("7h");
    expect(ago("2026-06-01T12:00:00Z")).toBe("6d");
    expect(ago("2026-04-12T12:00:00Z")).toBe("8w");    // ~56 days -> 8w (no months)
    expect(ago("2019-06-09T12:00:00Z")).toBe("7y");
  });
  it("returns empty for null/undefined", () => {
    expect(formatAgo(null, now)).toBe("");
    expect(formatAgo(undefined, now)).toBe("");
  });
});
```

- [ ] **Step 3: Run, verify FAIL.**

Run: `cd "c:/Users/thedo/git/spinlab/frontend" && npx vitest run src/format.test.ts`
Expected: FAIL — `formatAgo` not exported.

- [ ] **Step 4: Implement.** Add to `frontend/src/format.ts`:

```ts
/** Compact "time ago" with a unit suffix. Months are skipped so `m` always
 *  means minutes: now / 47s / 34m / 7h / 6d / 8w / 7y. nowMs defaults to Date.now(). */
export function formatAgo(iso: string | null | undefined, nowMs: number = Date.now()): string {
  if (iso == null) return "";
  const secs = Math.max(0, Math.floor((nowMs - Date.parse(iso)) / 1000));
  if (secs < 10) return "now";
  if (secs < 60) return secs + "s";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return mins + "m";
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours + "h";
  const days = Math.floor(hours / 24);
  if (days < 7) return days + "d";
  const weeks = Math.floor(days / 7);
  if (weeks < 52) return weeks + "w";
  return Math.floor(days / 365) + "y";
}
```

- [ ] **Step 5: Run, verify PASS.**

Run: `cd "c:/Users/thedo/git/spinlab/frontend" && npx vitest run src/format.test.ts`
Expected: PASS.

- [ ] **Step 6: Commit.**

```bash
git add frontend/src/format.ts frontend/src/format.test.ts
git commit -m "feat(format): add formatAgo (compact, months-skipped)"
```

---

## Task 4: Attempt table — sort logic + renderer (FE)

**Files:**
- Create: `frontend/src/attempt-table.ts`
- Test: `frontend/src/attempt-table.test.ts` (new)

- [ ] **Step 1: Write the failing test.** Create `frontend/src/attempt-table.test.ts`:

```ts
import { describe, it, expect, vi } from "vitest";
import {
  DEFAULT_SORT,
  nextSortColumn,
  flipSortDir,
  sortAttempts,
  renderAttemptTable,
  type AttemptRow,
} from "./attempt-table";

function row(p: Partial<AttemptRow>): AttemptRow {
  return {
    id: 1, order: 1, clean_tail_ms: 12000, total_ms: 12000, deaths: 0,
    created_at: "2026-01-01T00:00:00Z", completed: true, invalidated: false,
    is_floor: false, ...p,
  };
}

describe("sort state", () => {
  it("default is Clean Tail descending", () => {
    expect(DEFAULT_SORT).toEqual({ column: "clean_tail_ms", dir: "desc" });
  });
  it("clicking a new column uses its default dir; same column is unchanged", () => {
    expect(nextSortColumn(DEFAULT_SORT, "order")).toEqual({ column: "order", dir: "asc" });
    expect(nextSortColumn(DEFAULT_SORT, "clean_tail_ms")).toEqual(DEFAULT_SORT);
  });
  it("flip reverses direction", () => {
    expect(flipSortDir({ column: "order", dir: "asc" })).toEqual({ column: "order", dir: "desc" });
  });
});

describe("sortAttempts", () => {
  it("sorts by clean tail desc, nulls always last", () => {
    const rows = [row({ id: 1, clean_tail_ms: 12000 }), row({ id: 2, clean_tail_ms: null }), row({ id: 3, clean_tail_ms: 41000 })];
    const out = sortAttempts(rows, { column: "clean_tail_ms", dir: "desc" }).map(r => r.id);
    expect(out).toEqual([3, 1, 2]);
  });
  it("nulls stay last even ascending", () => {
    const rows = [row({ id: 1, clean_tail_ms: 12000 }), row({ id: 2, clean_tail_ms: null }), row({ id: 3, clean_tail_ms: 41000 })];
    const out = sortAttempts(rows, { column: "clean_tail_ms", dir: "asc" }).map(r => r.id);
    expect(out).toEqual([1, 3, 2]);
  });
  it("sorts by order ascending", () => {
    const rows = [row({ id: 1, order: 3 }), row({ id: 2, order: 1 }), row({ id: 3, order: 2 })];
    const out = sortAttempts(rows, { column: "order", dir: "asc" }).map(r => r.id);
    expect(out).toEqual([2, 3, 1]);
  });
});

describe("renderAttemptTable", () => {
  it("renders rows, strikes invalidated, stars the floor, calls onToggle", () => {
    document.body.innerHTML = `<div id="host"></div>`;
    const host = document.getElementById("host")!;
    const onToggle = vi.fn();
    const rows = [
      row({ id: 7, clean_tail_ms: 41700, total_ms: 53100, deaths: 1, invalidated: false }),
      row({ id: 18, clean_tail_ms: 11000, total_ms: 11000, is_floor: true }),
      row({ id: 5, clean_tail_ms: 38000, invalidated: true }),
    ];
    renderAttemptTable(host, rows, onToggle, Date.parse("2026-01-02T00:00:00Z"));
    expect(host.querySelectorAll("tbody tr").length).toBe(3);
    // floor row carries the star
    expect(host.textContent).toContain("★");
    // invalidated row is struck
    const struck = host.querySelector("tr.invalidated");
    expect(struck).not.toBeNull();
    // clicking an action button calls onToggle(id, nextInvalidatedValue)
    const btn = host.querySelector<HTMLButtonElement>("button[data-id='7']")!;
    btn.click();
    expect(onToggle).toHaveBeenCalledWith(7, true);
    const restoreBtn = host.querySelector<HTMLButtonElement>("button[data-id='5']")!;
    restoreBtn.click();
    expect(onToggle).toHaveBeenCalledWith(5, false); // already invalid -> restore
  });
});
```

- [ ] **Step 2: Run, verify FAIL.**

Run: `cd "c:/Users/thedo/git/spinlab/frontend" && npx vitest run src/attempt-table.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement** `frontend/src/attempt-table.ts`:

```ts
import { formatTime, formatAgo } from "./format";

export interface AttemptRow {
  id: number;
  order: number;
  clean_tail_ms: number | null;
  total_ms: number | null;
  deaths: number;
  created_at: string;
  completed: boolean;
  invalidated: boolean;
  is_floor: boolean;
}

export type SortColumn = "order" | "clean_tail_ms" | "total_ms" | "deaths" | "created_at";
export interface SortState { column: SortColumn; dir: "asc" | "desc"; }

export const DEFAULT_SORT: SortState = { column: "clean_tail_ms", dir: "desc" };

// Times/deaths default to biggest-first (outliers on top); order/age oldest-first.
function defaultDir(column: SortColumn): "asc" | "desc" {
  return column === "order" || column === "created_at" ? "asc" : "desc";
}

export function nextSortColumn(state: SortState, column: SortColumn): SortState {
  if (column === state.column) return state;
  return { column, dir: defaultDir(column) };
}

export function flipSortDir(state: SortState): SortState {
  return { column: state.column, dir: state.dir === "asc" ? "desc" : "asc" };
}

export function sortAttempts(rows: AttemptRow[], state: SortState): AttemptRow[] {
  const sign = state.dir === "asc" ? 1 : -1;
  const val = (r: AttemptRow): number | string =>
    state.column === "created_at" ? Date.parse(r.created_at) : (r[state.column] as number | null) ?? NaN;
  return [...rows].sort((a, b) => {
    const av = val(a), bv = val(b);
    // Nulls (NaN) always last, regardless of direction.
    const aNull = typeof av === "number" && Number.isNaN(av);
    const bNull = typeof bv === "number" && Number.isNaN(bv);
    if (aNull && bNull) return 0;
    if (aNull) return 1;
    if (bNull) return -1;
    return av < bv ? -1 * sign : av > bv ? 1 * sign : 0;
  });
}

const HEADERS: { col: SortColumn; label: string }[] = [
  { col: "order", label: "Order" },
  { col: "clean_tail_ms", label: "Clean Tail" },
  { col: "total_ms", label: "Total" },
  { col: "deaths", label: "Deaths" },
  { col: "created_at", label: "Ago" },
];

/** Render the surgery table into `host`. onToggle(id, nextInvalidated) fires on
 *  an action click. Sort state is held locally; header click sorts, double-click
 *  flips. nowMs is injectable for tests. */
export function renderAttemptTable(
  host: HTMLElement,
  rows: AttemptRow[],
  onToggle: (id: number, nextInvalidated: boolean) => void,
  nowMs: number = Date.now(),
): void {
  let sort = DEFAULT_SORT;

  const draw = () => {
    const sorted = sortAttempts(rows, sort);
    const ths = HEADERS.map(h => {
      const arrow = h.col === sort.column ? (sort.dir === "asc" ? " ▲" : " ▼") : "";
      return `<th data-col="${h.col}" style="cursor:pointer">${h.label}${arrow}</th>`;
    }).join("") + "<th></th>";
    const trs = sorted.map(r => {
      const star = r.is_floor ? " ★" : "";
      const ct = r.clean_tail_ms == null ? "—" : formatTime(r.clean_tail_ms) + star;
      const action = r.invalidated
        ? `<button data-id="${r.id}" data-next="0">⟲ restore</button>`
        : `<button data-id="${r.id}" data-next="1">⊘ invalidate</button>`;
      return `<tr class="${r.invalidated ? "invalidated" : ""}${r.is_floor ? " floor" : ""}">`
        + `<td>${r.order}</td><td>${ct}</td>`
        + `<td>${r.total_ms == null ? "—" : formatTime(r.total_ms)}</td>`
        + `<td>${r.deaths}</td><td>${formatAgo(r.created_at, nowMs)}</td>`
        + `<td>${action}</td></tr>`;
    }).join("");
    host.innerHTML = `<table class="attempt-surgery"><thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table>`;

    host.querySelectorAll<HTMLElement>("th[data-col]").forEach(th => {
      const col = th.dataset.col as SortColumn;
      th.addEventListener("click", () => { sort = nextSortColumn(sort, col); draw(); });
      th.addEventListener("dblclick", () => { sort = flipSortDir(sort); draw(); });
    });
    host.querySelectorAll<HTMLButtonElement>("button[data-id]").forEach(btn => {
      btn.addEventListener("click", () =>
        onToggle(Number(btn.dataset.id), btn.dataset.next === "1"));
    });
  };

  draw();
}
```

- [ ] **Step 4: Run, verify PASS.**

Run: `cd "c:/Users/thedo/git/spinlab/frontend" && npx vitest run src/attempt-table.test.ts`
Expected: PASS.

- [ ] **Step 5: Typecheck.**

Run: `cd "c:/Users/thedo/git/spinlab/frontend" && npm run typecheck`
Expected: no errors.

- [ ] **Step 6: Commit.**

```bash
git add frontend/src/attempt-table.ts frontend/src/attempt-table.test.ts
git commit -m "feat(surgery): attempt-table sort logic + renderer"
```

---

## Task 5: Mount in segment-detail + invalidate wiring (FE)

**Files:**
- Modify: `frontend/src/segment-detail.ts`
- Modify: `frontend/src/style.css`
- Test: `frontend/src/attempt-table.test.ts` (already covers logic; this task is integration wiring — verified via build + a smoke check)

- [ ] **Step 1: Mount the table** in `frontend/src/segment-detail.ts`. After the chart is built inside `renderSegmentDetail(container, segmentId)` (the function fetches `/history` and builds `_chart`), append a host element, fetch the surgery list, and render. Add imports at the top: `import { renderAttemptTable, type AttemptRow } from "./attempt-table";` and ensure `fetchJSON` is already imported (it is). Add, after the existing chart setup block (after `_chart = new Chart(...)` and its option wiring, before the cold-distribution section):

```ts
  // --- Attempt surgery table ---
  const surgeryHost = document.createElement("div");
  surgeryHost.className = "attempt-surgery-host";
  container.appendChild(surgeryHost);

  const loadAttempts = async () => {
    const resp = await fetchJSON<{ segment_id: string; attempts: AttemptRow[] }>(
      `/api/segments/${encodeURIComponent(segmentId)}/attempts`,
    );
    if (!resp) return;
    renderAttemptTable(surgeryHost, resp.attempts, async (id, nextInvalidated) => {
      await fetch(`/api/attempts/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ invalidated: nextInvalidated }),
      });
      await loadAttempts();  // re-fetch: reflects the recalc'd floor (★) + struck row
    });
  };
  await loadAttempts();
```

(If `renderSegmentDetail` is not `async` or doesn't already `await`, keep the `loadAttempts()` call but drop the outer `await` — call `void loadAttempts();`. Read the function signature first and match it.)

- [ ] **Step 2: Add table styles** to `frontend/src/style.css`:

```css
/* Attempt surgery table */
.attempt-surgery { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 12px; }
.attempt-surgery th { text-align: left; color: #9fb6d6; border-bottom: 1px solid #333; padding: 4px 8px; user-select: none; }
.attempt-surgery td { padding: 4px 8px; }
.attempt-surgery tr.invalidated { text-decoration: line-through; opacity: 0.5; color: #cc7a7a; }
.attempt-surgery tr.floor td { color: var(--gold); }
.attempt-surgery button { background: none; border: none; color: #7a8ba0; cursor: pointer; padding: 0; }
.attempt-surgery button:hover { color: #cdd9e8; }
```

- [ ] **Step 3: Build + typecheck.**

Run: `cd "c:/Users/thedo/git/spinlab/frontend" && npm run typecheck && npm run build`
Expected: no type errors; build succeeds.

- [ ] **Step 4: Run the FE suite** to confirm nothing regressed.

Run: `cd "c:/Users/thedo/git/spinlab/frontend" && npm test`
Expected: all green.

- [ ] **Step 5: Commit.**

```bash
git add frontend/src/segment-detail.ts frontend/src/style.css
git commit -m "feat(surgery): mount attempt table in segment-detail + invalidate wiring"
```

---

## Task 6: Full gate

**Files:** none (verification only)

- [ ] **Step 1: Full Python suite.**

Run: `python -m pytest`
Expected: all pass, 0 skipped beyond the accepted set (CLAUDE.md: skips are failures; the only known warning is `_segments_v07/api.py:165`).

- [ ] **Step 2: Frontend suite + static checks.**

Run: `cd "c:/Users/thedo/git/spinlab/frontend" && npm test && npm run typecheck && npm run build`
Expected: all green.

- [ ] **Step 3: Lint/type the touched Python.**

Run: `ruff check python/spinlab/routes/attempts.py python/spinlab/db/attempts.py python/spinlab/api_schemas.py && npx pyright python/spinlab/routes/attempts.py python/spinlab/db/attempts.py`
Expected: no new errors.

- [ ] **Step 4: Final commit if any gate fixups were needed.**

```bash
git add -A
git commit -m "chore(surgery): gate fixups"
```

---

## Self-Review notes (for the executor)

- **Spec coverage:** table in detail view (T5) · columns Order/Clean Tail/Total/Deaths/Ago (T4) · click-sort + double-click-flip (T4) · default Clean-Tail-desc (T4) · Ago compact skip-months (T3) · ★ floor (T1 is_floor + T4 render) · reversible episode-level invalidate (T2 PATCH + T4 action) · no hard-delete (nothing builds one) · auto per-segment recalc (T2) · surgery list route with id+invalidated distinct from /history (T1) · shows all episodes incl incomplete `—` (T1/T4). All present.
- **Out of scope (do not add):** hard-delete, event-level surgery, recorder guard, hot/cold or start-condition columns, page redesign, making the cold histogram respect invalidation.
- **`renderSegmentDetail` async-ness (Task 5 Step 1):** match the real signature — `await loadAttempts()` only if the function is async; else `void loadAttempts()`.

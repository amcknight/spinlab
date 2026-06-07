# Planning-table Room & Trend columns Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the planning table's redundant `Best` column with `Room` (Exp−Floor, abs + grey %) and `Practice` (slope gain + grey Trend% = gain/Expected), sorted by Room% descending.

**Architecture:** One backend wiring — surface the practice gain that `live_view.py` already computes onto `ModelOutput` so the `/api/model` payload carries it. Everything else (Room, Room%, Trend%, sort) derives in the frontend. Frontend types are codegen'd from the OpenAPI schema, so the new field appears automatically after `npm run gen-types`.

**Tech Stack:** Python (pydantic dataclasses, FastAPI), TypeScript (Vite, Vitest, happy-dom).

Spec: `docs/superpowers/specs/2026-06-07-planning-table-room-trend-columns-design.md`

---

## File Structure

- `python/spinlab/models.py` — add `practice_gain_ms` field to `ModelOutput` (+ `to_dict`/`from_dict`).
- `python/spinlab/estimators/em_suite_sampler.py` — populate the gain in `model_output()`.
- `frontend/src/format.ts` — add `formatPct`.
- `frontend/src/model-logic.ts` — add `selectedGain`, `roomMs`, `roomPct`, `trendPct`, `compareByRoomPctDesc`.
- `frontend/index.html` — update the `#model-table` header row.
- `frontend/src/model-render.ts` — drop Best cell, add Room + Practice cells, sort by Room%.
- `frontend/src/style.css` — `.pct`, `.gain-good`, `.gain-bad`, `.gain-neutral`, table `.gold` classes.
- Tests: `tests/unit/estimators/test_em_suite_sampler.py`, `tests/unit/test_dashboard_integration.py`, `frontend/src/format.test.ts`, `frontend/src/model-logic.test.ts`, `frontend/src/model-render.test.ts`.

The gain definition lives in exactly one place conceptually (the closed-form `scalar - slid`); `live_view.py` and `model_output()` both compute it from the same `expected_episode_time_*` helpers. We are NOT extracting a shared helper in this plan (the two call sites are two lines each); if a third consumer appears, extract then.

---

## Task 1: Add `practice_gain_ms` to ModelOutput and populate it

**Files:**
- Modify: `python/spinlab/models.py:359-383` (`ModelOutput`)
- Modify: `python/spinlab/estimators/em_suite_sampler.py:619-646` (`model_output`)
- Test: `tests/unit/estimators/test_em_suite_sampler.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/estimators/test_em_suite_sampler.py`:

```python
def test_model_output_populates_practice_gain():
    from spinlab.estimators.em_suite_sampler import (
        DEFAULT_FAST_IDX,
        DEFAULT_SLOW_IDX,
        EmSuiteSamplerEstimator,
        SamplerState,
        expected_episode_time_ms,
        expected_episode_time_scalar,
        process_event,
    )
    from tests.factories import make_event_attempt

    # Seed a real gated state (>=2 successes and >=2 deaths) through process_event
    # so both the scalar and the slope compute to floats, not None.
    st = SamplerState()
    for outcome, t in [("survived", 2000), ("died", 500), ("survived", 2100),
                       ("died", 600), ("survived", 1900), ("died", 550)]:
        st = process_event(st, make_event_attempt(outcome=outcome, time_ms=t))

    est = EmSuiteSamplerEstimator()
    out = est.model_output(st, [], events=None)

    scalar = expected_episode_time_scalar(st)
    slid = expected_episode_time_ms(
        st, DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX, apply_slope=True,
    )
    assert scalar is not None and slid is not None
    assert out.practice_gain_ms == scalar - slid


def test_model_output_gain_none_when_slope_ungated():
    from spinlab.estimators.em_suite_sampler import (
        EmSuiteSamplerEstimator,
        SamplerState,
    )
    # Bare gated counters but empty accumulators -> scalar/slope short-circuit to None.
    st = SamplerState()
    st.n_successes = 3
    st.n_deaths = 3
    st.n_attempts_total = 6
    out = EmSuiteSamplerEstimator().model_output(st, [], events=None)
    assert out.practice_gain_ms is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/estimators/test_em_suite_sampler.py::test_model_output_populates_practice_gain -v`
Expected: FAIL with `AttributeError: 'ModelOutput' object has no attribute 'practice_gain_ms'` (or a TypeError on the unexpected attribute).

- [ ] **Step 3: Add the field to `ModelOutput`**

In `python/spinlab/models.py`, change the `ModelOutput` dataclass body and its `to_dict`/`from_dict`:

```python
@pydantic_dataclass(config=ConfigDict(extra="allow"))
class ModelOutput:
    """What every estimator produces — predictions for total time and clean tail.

    Pydantic dataclass: see ``Estimate`` for rationale.
    """
    total: Estimate
    clean: Estimate
    extras: DeathExtras | None = None
    # Closed-form practice gain (ms): expected_now - expected_after_one_slope_step.
    # Positive = practicing is predicted to reduce episode time. None when the
    # slope is ungated. Mirrors live_view.practice_gain_ms; surfaced here so the
    # planning table can show a Practice/Trend% column without a second endpoint.
    practice_gain_ms: float | None = None

    def to_dict(self) -> dict:
        return {
            "total": self.total.to_dict(),
            "clean": self.clean.to_dict(),
            "extras": self.extras.to_dict() if self.extras is not None else None,
            "practice_gain_ms": self.practice_gain_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelOutput":
        extras_d = d.get("extras")
        return cls(
            total=Estimate.from_dict(d["total"]),
            clean=Estimate.from_dict(d["clean"]),
            extras=DeathExtras.from_dict(extras_d) if extras_d is not None else None,
            practice_gain_ms=d.get("practice_gain_ms"),
        )
```

- [ ] **Step 4: Populate it in `model_output`**

In `python/spinlab/estimators/em_suite_sampler.py`, the `model_output` method already computes `scalar`. Add the slope value and gain, and pass `practice_gain_ms` to `ModelOutput`:

```python
    def model_output(  # type: ignore[override]
        self, state: SamplerState, all_attempts: list[AttemptRecord],
        params: dict | None = None,
        events: list[EventAttempt] | None = None,
    ) -> ModelOutput:
        scalar = expected_episode_time_scalar(state)
        # Practice gain: expected now minus expected after one trend-slide step,
        # at the default alpha pair. Mirrors live_view.practice_gain_ms exactly.
        # None when either side is None (gate fails / slope ungated).
        slid = expected_episode_time_ms(
            state, DEFAULT_FAST_IDX, DEFAULT_SLOW_IDX, apply_slope=True,
        )
        practice_gain = (
            scalar - slid if (scalar is not None and slid is not None) else None
        )
        # Floor = running-min CLEAN clear so far (the final-success time with no
        # deaths) — the unreachable-without-a-perfect-run target. min over
        # completed attempts' clean_tail_ms; None until a clean clear exists.
        floor = min(
            (a.clean_tail_ms for a in all_attempts
             if a.completed and a.clean_tail_ms is not None),
            default=None,
        )
        total = Estimate(
            expected_ms=scalar, ms_per_attempt=scalar, floor_ms=floor,
        )
        clean = Estimate(expected_ms=None, ms_per_attempt=None, floor_ms=None)
        return ModelOutput(
            total=total, clean=clean, extras=None,
            practice_gain_ms=practice_gain,
        )
```

(`DEFAULT_FAST_IDX`, `DEFAULT_SLOW_IDX`, and `expected_episode_time_ms` are all module-level in this same file — no new imports.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/estimators/test_em_suite_sampler.py -v`
Expected: PASS (both new tests, plus the existing file green).

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/models.py python/spinlab/estimators/em_suite_sampler.py tests/unit/estimators/test_em_suite_sampler.py
git commit -m "feat(model): surface practice_gain_ms on ModelOutput"
```

---

## Task 2: Confirm the gain reaches the `/api/model` payload

**Files:**
- Test: `tests/unit/test_dashboard_integration.py`

The `/api/model` route serializes each segment's `ModelOutput` via `out.to_dict()`, so the new key flows through automatically. This task is a regression test that locks that contract.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_dashboard_integration.py` (follow the existing patterns in that file for spinning up the app/client and seeding a game with a practiced segment — reuse the same fixtures the other `/api/model` tests there use):

```python
def test_api_model_segment_carries_practice_gain_key(client_with_practiced_segment):
    # client_with_practiced_segment: reuse whatever fixture the existing
    # /api/model tests in this file use to get a TestClient with at least one
    # segment that has model_outputs. (Rename to match the local fixture.)
    client = client_with_practiced_segment
    resp = client.get("/api/model")
    assert resp.status_code == 200
    segments = resp.json()["segments"]
    assert segments, "expected at least one model segment"
    for seg in segments:
        for _name, out in seg["model_outputs"].items():
            # Key must be present (value may be None when the slope is ungated).
            assert "practice_gain_ms" in out
```

If no existing fixture provides a practiced segment, mirror the setup of the nearest existing `/api/model` test in this file rather than inventing a new harness.

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `python -m pytest tests/unit/test_dashboard_integration.py::test_api_model_segment_carries_practice_gain_key -v`
Expected: PASS once Task 1 landed (the key is serialized). If it FAILS with `KeyError`/missing key, Task 1's `to_dict` change was not applied — fix there.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_dashboard_integration.py
git commit -m "test(model): lock practice_gain_ms in /api/model payload"
```

---

## Task 3: Regenerate FE types and add `formatPct`

**Files:**
- Modify: `frontend/src/format.ts`
- Test: `frontend/src/format.test.ts`

- [ ] **Step 1: Regenerate the frontend types from the updated schema**

Run: `cd frontend && npm run gen-types`
Expected: `frontend/src/api-types.ts` regenerates; `ModelOutput` now includes `practice_gain_ms`. (This file and `frontend/openapi.json` are gitignored — nothing to commit from this step.)

- [ ] **Step 2: Write the failing test**

Add to `frontend/src/format.test.ts`:

```ts
import { formatPct } from "./format";

describe("formatPct", () => {
  it("renders a fraction as a whole-number percent", () => {
    expect(formatPct(0.13)).toBe("13%");
    expect(formatPct(0.455)).toBe("46%");
    expect(formatPct(0)).toBe("0%");
  });
  it("returns empty string for null/undefined", () => {
    expect(formatPct(null)).toBe("");
    expect(formatPct(undefined)).toBe("");
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/format.test.ts`
Expected: FAIL — `formatPct` is not exported.

- [ ] **Step 4: Implement `formatPct`**

Add to `frontend/src/format.ts`:

```ts
/** Render a fraction (0.13) as a whole-number percent ("13%"). Empty for null. */
export function formatPct(frac: number | null | undefined): string {
  if (frac == null) return "";
  return Math.round(frac * 100) + "%";
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/format.test.ts`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/format.ts frontend/src/format.test.ts
git commit -m "feat(format): add formatPct helper"
```

---

## Task 4: Room / Trend arithmetic and the Room% sort comparator

**Files:**
- Modify: `frontend/src/model-logic.ts`
- Test: `frontend/src/model-logic.test.ts`

- [ ] **Step 1: Write the failing tests**

Add to `frontend/src/model-logic.test.ts` (it already imports from `./model-logic` and `./types`):

```ts
import {
  selectedGain,
  roomMs,
  roomPct,
  trendPct,
  compareByRoomPctDesc,
} from "./model-logic";

function estOf(expected_ms: number | null, floor_ms: number | null): Estimate {
  return { expected_ms, ms_per_attempt: expected_ms, floor_ms } as Estimate;
}

function segOf(expected_ms: number | null, floor_ms: number | null, gain: number | null): ModelSegment {
  return {
    selected_model: "m",
    model_outputs: { m: { total: estOf(expected_ms, floor_ms), clean: estOf(null, null), extras: null, practice_gain_ms: gain } },
  } as unknown as ModelSegment;
}

describe("room and trend arithmetic", () => {
  it("roomMs = expected - floor", () => {
    expect(roomMs(estOf(12400, 11000))).toBe(1400);
  });
  it("roomMs null when either side missing", () => {
    expect(roomMs(estOf(null, 11000))).toBeNull();
    expect(roomMs(estOf(12400, null))).toBeNull();
    expect(roomMs(null)).toBeNull();
  });
  it("roomPct = (expected - floor) / floor", () => {
    expect(roomPct(estOf(12400, 11000))).toBeCloseTo(0.1273, 4);
  });
  it("roomPct null when floor is 0 or missing", () => {
    expect(roomPct(estOf(12400, 0))).toBeNull();
    expect(roomPct(estOf(12400, null))).toBeNull();
  });
  it("trendPct = gain / expected", () => {
    expect(trendPct(600, estOf(12400, 11000))).toBeCloseTo(0.0484, 4);
  });
  it("trendPct null when gain or expected missing/zero", () => {
    expect(trendPct(null, estOf(12400, 11000))).toBeNull();
    expect(trendPct(600, estOf(0, 0))).toBeNull();
  });
  it("selectedGain pulls practice_gain_ms from the selected output", () => {
    expect(selectedGain(segOf(12400, 11000, 600))).toBe(600);
    expect(selectedGain(segOf(12400, 11000, null))).toBeNull();
  });
});

describe("compareByRoomPctDesc", () => {
  it("sorts higher Room% first, nulls last", () => {
    const a = segOf(24800, 17100, 1200); // 45%
    const b = segOf(13200, 10100, 100);  // 31%
    const c = segOf(12400, 11000, 600);  // 13%
    const n = segOf(null, null, null);   // null -> last
    const sorted = [c, n, a, b].sort(compareByRoomPctDesc);
    expect(sorted).toEqual([a, b, c, n]);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && npx vitest run src/model-logic.test.ts`
Expected: FAIL — the new functions are not exported.

- [ ] **Step 3: Implement the helpers**

Add to `frontend/src/model-logic.ts` (it already imports `ModelSegment`, `Estimate`):

```ts
/** practice_gain_ms from the segment's selected model output, or null. */
export function selectedGain(seg: ModelSegment): number | null {
  const output = seg.model_outputs[seg.selected_model];
  return output?.practice_gain_ms ?? null;
}

/** Room = Expected - Floor (ms), or null when either is missing. */
export function roomMs(est: Estimate | null): number | null {
  if (!est || est.expected_ms == null || est.floor_ms == null) return null;
  return est.expected_ms - est.floor_ms;
}

/** Room% = (Expected - Floor) / Floor, or null when floor is 0/missing. */
export function roomPct(est: Estimate | null): number | null {
  if (!est || est.expected_ms == null || est.floor_ms == null || est.floor_ms === 0) {
    return null;
  }
  return (est.expected_ms - est.floor_ms) / est.floor_ms;
}

/** Trend% = gain / Expected = value per wall-clock second; null when undefined. */
export function trendPct(gain: number | null, est: Estimate | null): number | null {
  if (gain == null || !est || est.expected_ms == null || est.expected_ms === 0) {
    return null;
  }
  return gain / est.expected_ms;
}

/** Sort comparator: highest Room% first, segments with no Room% last. */
export function compareByRoomPctDesc(a: ModelSegment, b: ModelSegment): number {
  const ra = roomPct(selectedEstimate(a));
  const rb = roomPct(selectedEstimate(b));
  if (ra == null && rb == null) return 0;
  if (ra == null) return 1;
  if (rb == null) return -1;
  return rb - ra;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/model-logic.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/model-logic.ts frontend/src/model-logic.test.ts
git commit -m "feat(model-logic): room/room%/trend% helpers + room% sort"
```

---

## Task 5: Rebuild the table — drop Best, add Room + Practice, sort by Room%

**Files:**
- Modify: `frontend/index.html:78-88` (`#model-table` header)
- Modify: `frontend/src/model-render.ts:128-167` (`renderModelTable`)
- Modify: `frontend/src/style.css`
- Test: `frontend/src/model-render.test.ts`

- [ ] **Step 1: Write the failing test**

Add to `frontend/src/model-render.test.ts` (it already sets up `#model-body` and imports `renderModelTable`):

```ts
function modelWith(segments: any[]): any {
  return { estimator: "m", allocator_weights: {}, segments };
}
function seg(name: string, expected: number | null, floor: number | null, gain: number | null, n = 5): any {
  return {
    segment_id: name, description: name, level_number: 1,
    start_type: "entrance", start_ordinal: 0, end_type: "goal", end_ordinal: 0,
    selected_model: "m", n_completed: n, n_attempts: n, gold_ms: floor, clean_gold_ms: floor,
    model_outputs: { m: { total: { expected_ms: expected, ms_per_attempt: expected, floor_ms: floor }, clean: { expected_ms: null, ms_per_attempt: null, floor_ms: null }, extras: null, practice_gain_ms: gain } },
  };
}

describe("renderModelTable room/practice columns", () => {
  beforeEach(() => {
    document.body.innerHTML = `<table><tbody id="model-body"></tbody></table>`;
  });

  it("renders Room (abs + %) and Practice (gain + trend%), no Best", () => {
    renderModelTable(modelWith([seg("L1", 12400, 11000, 600)]), () => {}, true);
    const row = document.querySelector("#model-body tr")!;
    const text = row.textContent ?? "";
    expect(text).toContain("1.4s"); // Room absolute
    expect(text).toContain("13%");  // Room%
    expect(text).toContain("0.6s"); // Practice gain magnitude
    expect(text).toContain("5%");   // Trend% = 600/12400
    // Best (gold_ms) is no longer rendered as its own cell value beyond floor.
    expect(row.querySelector(".gain-good")).not.toBeNull();
  });

  it("sorts rows by Room% descending", () => {
    renderModelTable(
      modelWith([seg("low", 12400, 11000, 600), seg("high", 24800, 17100, 1200)]),
      () => {}, true,
    );
    const links = Array.from(document.querySelectorAll("#model-body a")).map((a) => a.textContent);
    expect(links[0]).toBe("high"); // 45% before 13%
  });

  it("dims Room/Practice when the estimate is missing", () => {
    renderModelTable(modelWith([seg("thin", null, null, null)]), () => {}, true);
    const row = document.querySelector("#model-body tr")!;
    expect(row.querySelectorAll(".dim").length).toBeGreaterThanOrEqual(2);
  });
});
```

(`segmentName` renders the description, so the link text is the segment name; the fixtures set `description` to the short labels used in the sort assertion.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/model-render.test.ts`
Expected: FAIL — current renderer shows Best and has no Room/Practice cells.

- [ ] **Step 3: Update the table header in `index.html`**

Replace the `#model-table` `<thead>` rows (`frontend/index.html:81-86`) with:

```html
                <th title="Level section being practiced">Segment</th>
                <th title="Best clean clear so far (no deaths)">Floor</th>
                <th title="Expected completion time, incl. deaths/reloads">Expected</th>
                <th title="Room to floor: Expected minus Floor (absolute, with % of floor)">Room</th>
                <th title="Practice gain per rep and value per wall-clock second (Trend%)">Practice</th>
                <th title="Completed practice attempts">Runs</th>
```

- [ ] **Step 4: Rewrite `renderModelTable`**

In `frontend/src/model-render.ts`, update the imports and the row-building body. Replace the two existing top-of-file import lines (`./format` and `./model-logic`) with:

```ts
import { segmentName, formatTime, elapsedStr, emptyStateMessage, formatPct } from "./format";
import {
  selectedEstimate,
  selectedGain,
  roomMs,
  roomPct,
  trendPct,
  compareByRoomPctDesc,
} from "./model-logic";
```

(`elapsedStr` is kept because other functions in this file use it; `formatPct` is the only addition to the `./format` line.)

Replace the body of `renderModelTable` from the empty-state `colspan` through the `forEach` with:

```ts
  const body = document.getElementById("model-body")!;
  if (!data.segments || !data.segments.length) {
    const msg = emptyStateMessage(hasActiveRun, "No game loaded");
    body.innerHTML = '<tr><td colspan="6" class="dim">' + msg + "</td></tr>";
    return;
  }
  body.innerHTML = "";
  const segments = [...data.segments].sort(compareByRoomPctDesc);
  segments.forEach((s) => {
    const tr = document.createElement("tr");
    const est = selectedEstimate(s);
    const gain = selectedGain(s);
    const room = roomMs(est);
    const rPct = roomPct(est);
    const tPct = trendPct(gain, est);

    const nameTd = document.createElement("td");
    const nameLink = document.createElement("a");
    nameLink.href = "#";
    nameLink.textContent = segmentName(s);
    nameLink.addEventListener("click", (e) => {
      e.preventDefault();
      onSegmentClick(s.segment_id);
    });
    nameTd.appendChild(nameLink);

    const roomCell = room == null
      ? '<td class="dim"></td>'
      : "<td>" + formatTime(room) + ' <span class="pct">' + formatPct(rPct) + "</span></td>";

    let practiceCell: string;
    if (gain == null) {
      practiceCell = '<td class="dim"></td>';
    } else {
      const cls = gain > 0 ? "gain-good" : gain < 0 ? "gain-bad" : "gain-neutral";
      // ▾ = down triangle (improvement), ▴ = up triangle (worse).
      const arrow = gain > 0 ? "▾" : gain < 0 ? "▴" : "";
      const pct = tPct == null ? "" : formatPct(Math.abs(tPct));
      practiceCell = '<td class="' + cls + '">' + arrow + formatTime(Math.abs(gain))
        + ' <span class="pct">' + pct + "</span></td>";
    }

    const restHtml =
      '<td class="gold">' + formatTime(est?.floor_ms ?? null) + "</td>" +
      "<td>" + formatTime(est?.expected_ms ?? null) + "</td>" +
      roomCell +
      practiceCell +
      "<td>" + s.n_completed + "</td>";

    tr.innerHTML = restHtml;
    tr.prepend(nameTd);
    body.appendChild(tr);
  });
```

- [ ] **Step 5: Add the cell styles**

Append to `frontend/src/style.css`:

```css
/* Planning-table Room/Practice cells */
#model-table .pct { color: #888; font-size: 0.85em; }
#model-table td.gold { color: var(--gold); }
#model-table td.gain-good { color: #5fce7e; }
#model-table td.gain-bad { color: #cc7a7a; }
#model-table td.gain-neutral { color: #888; }
```

(If `--gold` is not defined in `:root`, reuse the same value the Floor styling already uses — grep `--gold` in `style.css`; it was added for the route-bar/segment-summary Floor.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/model-render.test.ts`
Expected: PASS.

- [ ] **Step 7: Typecheck and build**

Run: `cd frontend && npm run typecheck && npm run build`
Expected: no type errors; build succeeds (also refreshes `python/spinlab/static/`).

- [ ] **Step 8: Commit**

```bash
git add frontend/index.html frontend/src/model-render.ts frontend/src/model-render.test.ts frontend/src/style.css
git commit -m "feat(planning-table): Room + Practice columns, drop Best, sort by Room%"
```

---

## Task 6: Full gate

**Files:** none (verification only)

- [ ] **Step 1: Run the full Python suite**

Run: `python -m pytest`
Expected: all pass, 0 skipped beyond the accepted `skipif` set. (If emulator tests cannot launch RA, that is a failure to surface — do not treat skips as green. See CLAUDE.md.)

- [ ] **Step 2: Run the frontend suite + static checks**

Run: `cd frontend && npm test && npm run typecheck && npm run build`
Expected: all green.

- [ ] **Step 3: Lint/type the Python touch**

Run: `ruff check python/spinlab/models.py python/spinlab/estimators/em_suite_sampler.py && npx pyright python/spinlab/models.py python/spinlab/estimators/em_suite_sampler.py`
Expected: no new errors (pre-existing tracked errors are acceptable; do not add new ones).

- [ ] **Step 4: Final commit if anything was fixed up**

```bash
git add -A
git commit -m "chore(planning-table): gate fixes for Room/Trend columns"
```

---

## Self-Review notes (for the executor)

- **Spec coverage:** Drop Best (Task 5) · Floor kept (Task 5) · Room+Room% (Tasks 4-5) · Practice+Trend%=gain/Expected (Tasks 1,4,5) · Room% sort (Tasks 4-5) · gain wired into payload (Tasks 1-2) · None-handling (Tasks 4-5) · Plays/Runs column kept at row end (Task 5). All present.
- **Out of scope (do not touch):** sparkline/detail view, live-card changes, Practice-Next merge, allocator rewiring, learning-curve prior.
- **Trust framing:** Trend% is shown but is NOT the default sort — Room% sorts. Do not change the sort key to Trend%.
- **No near-floor blow-up:** Trend% uses gain/Expected (Expected bounded), not gain/Room — do not "simplify" it to gain/Room.

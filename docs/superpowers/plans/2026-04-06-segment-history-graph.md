# Segment History Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-segment chart showing attempt times and estimator curves over time, accessible via drill-down from the Model tab.

**Architecture:** New `GET /api/segments/{id}/history` endpoint replays attempts through all registered estimators and returns raw data points plus estimator curves. Frontend renders a Chart.js line chart in a drill-down view triggered by clicking segment names in the Model table.

**Tech Stack:** Python/FastAPI (backend), TypeScript/Vite + Chart.js (frontend)

**Spec:** `docs/superpowers/specs/2026-04-06-segment-history-graph-design.md`

---

### Task 1: Add `get_segment_by_id` DB method

**Files:**
- Modify: `python/spinlab/db/segments.py:158-162` (near `segment_exists`)
- Test: `tests/test_db_dashboard.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_db_dashboard.py`, add:

```python
def test_get_segment_by_id(seeded_db):
    seg = seeded_db.get_segment_by_id("s1")
    assert seg is not None
    assert seg.id == "s1"
    assert seg.game_id == GAME_ID

def test_get_segment_by_id_missing(seeded_db):
    seg = seeded_db.get_segment_by_id("nonexistent")
    assert seg is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_db_dashboard.py::test_get_segment_by_id -v`
Expected: FAIL with `AttributeError: 'Database' object has no attribute 'get_segment_by_id'`

- [ ] **Step 3: Write minimal implementation**

In `python/spinlab/db/segments.py`, add after `segment_exists`:

```python
def get_segment_by_id(self, segment_id: str) -> Segment | None:
    row = self.conn.execute(
        "SELECT * FROM segments WHERE id = ?", (segment_id,)
    ).fetchone()
    if row is None:
        return None
    return self._row_to_segment(row)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_db_dashboard.py::test_get_segment_by_id tests/test_db_dashboard.py::test_get_segment_by_id_missing -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db/segments.py tests/test_db_dashboard.py
git commit -m "feat: add get_segment_by_id DB method"
```

---

### Task 2: Add `/api/segments/{segment_id}/history` endpoint

**Files:**
- Modify: `python/spinlab/routes/model.py`
- Test: `tests/test_dashboard_integration.py`

The endpoint replays all attempts for a segment through every registered estimator and returns raw data points plus estimator curves. Uses `_attempts_from_rows` from `scheduler.py` for the conversion.

- [ ] **Step 1: Write the failing test**

In `tests/test_dashboard_integration.py`, add:

```python
def test_segment_history_returns_attempts_and_curves(client):
    resp = client.get("/api/segments/s1/history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["segment_id"] == "s1"
    assert data["description"] == "Yoshi's Island 1"
    # s1 has 3 completed attempts in ATTEMPTS fixture (4500, 3800, 3200)
    assert len(data["attempts"]) == 3
    assert data["attempts"][0]["attempt_number"] == 1
    assert data["attempts"][0]["time_ms"] == 4500
    assert data["attempts"][2]["time_ms"] == 3200
    # Every registered estimator should have curves
    curves = data["estimator_curves"]
    assert "kalman" in curves
    for est_name, est_curves in curves.items():
        assert "total" in est_curves
        assert "clean" in est_curves
        assert len(est_curves["total"]["expected_ms"]) == 3


def test_segment_history_excludes_incomplete(seeded_db, client):
    """s3 has one incomplete (12000, False) and one complete (11500, True)."""
    resp = client.get("/api/segments/s3/history")
    assert resp.status_code == 200
    data = resp.json()
    # Only the completed attempt should appear
    assert len(data["attempts"]) == 1
    assert data["attempts"][0]["time_ms"] == 11500


def test_segment_history_unknown_segment(client):
    resp = client.get("/api/segments/nonexistent/history")
    assert resp.status_code == 404


def test_segment_history_no_completed_attempts(seeded_db, client):
    """s5 has no attempts at all."""
    resp = client.get("/api/segments/s5/history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["attempts"] == []
    for est_curves in data["estimator_curves"].values():
        assert est_curves["total"]["expected_ms"] == []
        assert est_curves["clean"]["expected_ms"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_dashboard_integration.py::test_segment_history_returns_attempts_and_curves -v`
Expected: FAIL with 404 (route doesn't exist yet)

- [ ] **Step 3: Write the endpoint**

In `python/spinlab/routes/model.py`, add these imports at the top:

```python
from spinlab.estimators import get_estimator, list_estimators
from spinlab.scheduler import _attempts_from_rows
from spinlab.models import AttemptRecord
```

(`get_estimator` and `list_estimators` are already imported — just add `_attempts_from_rows` and `AttemptRecord`.)

Then add the route:

```python
@router.get("/segments/{segment_id}/history")
def segment_history(segment_id: str, db: Database = Depends(get_db)):
    seg = db.get_segment_by_id(segment_id)
    if seg is None:
        logger.warning("segment_history: unknown segment %r", segment_id)
        raise HTTPException(status_code=404, detail=f"Segment not found: {segment_id}")

    raw_rows = db.get_segment_attempts(segment_id)
    # _attempts_from_rows filters invalidated; we also need completed only
    all_records = _attempts_from_rows(raw_rows)
    completed = [a for a in all_records if a.completed and a.time_ms is not None]

    # Build attempt data points
    attempts = []
    for i, a in enumerate(completed):
        attempts.append({
            "attempt_number": i + 1,
            "time_ms": a.time_ms,
            "clean_tail_ms": a.clean_tail_ms,
            "deaths": a.deaths,
            "created_at": a.created_at,
        })

    # Load estimator params
    estimator_names = list_estimators()
    estimator_curves: dict[str, dict] = {}

    for est_name in estimator_names:
        est = get_estimator(est_name)
        saved_raw = db.load_allocator_config(f"estimator_params:{est_name}")
        params = json.loads(saved_raw) if saved_raw else None
        priors = est.get_priors(db, seg.game_id)

        total_expected: list[float | None] = []
        total_floor: list[float | None] = []
        clean_expected: list[float | None] = []
        clean_floor: list[float | None] = []

        if completed:
            state = est.init_state(completed[0], priors, params=params)
            out = est.model_output(state, completed[:1])
            total_expected.append(out.total.expected_ms)
            total_floor.append(out.total.floor_ms)
            clean_expected.append(out.clean.expected_ms)
            clean_floor.append(out.clean.floor_ms)

            for j in range(1, len(completed)):
                state = est.process_attempt(
                    state, completed[j], completed[:j + 1], params=params,
                )
                out = est.model_output(state, completed[:j + 1])
                total_expected.append(out.total.expected_ms)
                total_floor.append(out.total.floor_ms)
                clean_expected.append(out.clean.expected_ms)
                clean_floor.append(out.clean.floor_ms)

        estimator_curves[est_name] = {
            "total": {"expected_ms": total_expected, "floor_ms": total_floor},
            "clean": {"expected_ms": clean_expected, "floor_ms": clean_floor},
        }

    return {
        "segment_id": segment_id,
        "description": seg.description,
        "attempts": attempts,
        "estimator_curves": estimator_curves,
    }
```

- [ ] **Step 4: Run all history tests**

Run: `python -m pytest tests/test_dashboard_integration.py -k segment_history -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest -m "not (emulator or slow or frontend)"`
Expected: All pass — no regressions

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/routes/model.py tests/test_dashboard_integration.py
git commit -m "feat: add /api/segments/{id}/history endpoint with estimator replay"
```

---

### Task 3: Add Chart.js dependency

**Files:**
- Modify: `frontend/package.json`

- [ ] **Step 1: Install chart.js**

```bash
cd frontend && npm install chart.js
```

- [ ] **Step 2: Verify it installed**

Check that `chart.js` appears in `package.json` under `dependencies` (not `devDependencies`).

- [ ] **Step 3: Commit**

```bash
git add frontend/package.json frontend/package-lock.json
git commit -m "deps: add chart.js for segment history graphs"
```

---

### Task 4: Add TypeScript types for history API

**Files:**
- Modify: `frontend/src/types.ts`
- Test: `frontend/src/api-contract.test.ts`

- [ ] **Step 1: Add types to `frontend/src/types.ts`**

Add at the end of the file:

```typescript
/** One data point in a segment history chart. */
export interface HistoryAttempt {
  attempt_number: number;
  time_ms: number;
  clean_tail_ms: number | null;
  deaths: number;
  created_at: string;
}

/** Estimator curve data for one time series (total or clean). */
export interface EstimatorCurve {
  expected_ms: (number | null)[];
  floor_ms: (number | null)[];
}

/** Estimator curves keyed by series name. */
export interface EstimatorCurves {
  total: EstimatorCurve;
  clean: EstimatorCurve;
}

/** GET /api/segments/{id}/history */
export interface SegmentHistory {
  segment_id: string;
  description: string;
  attempts: HistoryAttempt[];
  estimator_curves: Record<string, EstimatorCurves>;
}
```

- [ ] **Step 2: Add contract test**

In `frontend/src/api-contract.test.ts`, add:

```typescript
import type { SegmentHistory, HistoryAttempt, EstimatorCurves } from "./types";

describe("SegmentHistory contract", () => {
  it("history response fixture type-checks", () => {
    const history: SegmentHistory = {
      segment_id: "s1",
      description: "Yoshi's Island 1",
      attempts: [
        { attempt_number: 1, time_ms: 4500, clean_tail_ms: 4500, deaths: 0, created_at: "2026-04-01T12:00:00Z" },
        { attempt_number: 2, time_ms: 3800, clean_tail_ms: 3800, deaths: 0, created_at: "2026-04-01T12:05:00Z" },
      ],
      estimator_curves: {
        kalman: {
          total: { expected_ms: [4500, 4150], floor_ms: [null, null] },
          clean: { expected_ms: [4500, 4150], floor_ms: [null, null] },
        },
      },
    };
    expect(history.attempts).toHaveLength(2);
    expect(history.estimator_curves["kalman"]!.total.expected_ms).toHaveLength(2);
  });
});
```

- [ ] **Step 3: Run frontend tests**

Run: `cd frontend && npm test`
Expected: All pass

- [ ] **Step 4: Run typecheck**

Run: `cd frontend && npm run typecheck`
Expected: No errors

- [ ] **Step 5: Commit**

```bash
git add frontend/src/types.ts frontend/src/api-contract.test.ts
git commit -m "feat: add SegmentHistory types and contract test"
```

---

### Task 5: Build segment detail view component

**Files:**
- Create: `frontend/src/segment-detail.ts`
- Test: `frontend/src/segment-detail.test.ts`

This is the self-contained chart component. It fetches data, builds a Chart.js line chart, and wires the total/clean toggle.

- [ ] **Step 1: Write the test**

Create `frontend/src/segment-detail.test.ts`:

```typescript
import { describe, it, expect, vi, beforeEach } from "vitest";
import type { SegmentHistory } from "./types";

const MOCK_HISTORY: SegmentHistory = {
  segment_id: "s1",
  description: "Yoshi's Island 1",
  attempts: [
    { attempt_number: 1, time_ms: 4500, clean_tail_ms: 4500, deaths: 0, created_at: "2026-04-01T12:00:00Z" },
    { attempt_number: 2, time_ms: 3800, clean_tail_ms: 3200, deaths: 0, created_at: "2026-04-01T12:05:00Z" },
    { attempt_number: 3, time_ms: 3200, clean_tail_ms: 3200, deaths: 0, created_at: "2026-04-01T12:10:00Z" },
  ],
  estimator_curves: {
    kalman: {
      total: { expected_ms: [4500, 4150, 3700], floor_ms: [null, null, null] },
      clean: { expected_ms: [4500, 3850, 3500], floor_ms: [null, null, null] },
    },
    rolling_mean: {
      total: { expected_ms: [4500, 4150, 3833], floor_ms: [null, null, null] },
      clean: { expected_ms: [4500, 3850, 3633], floor_ms: [null, null, null] },
    },
  },
};

// Mock fetch globally
const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);

// Mock Chart.js — we can't test canvas rendering in happy-dom,
// but we can verify the component builds datasets correctly.
vi.mock("chart.js", () => ({
  Chart: class {
    data: unknown;
    constructor(_ctx: unknown, config: { data: unknown }) { this.data = config.data; }
    destroy() {}
    update() {}
  },
  LineController: class {},
  LineElement: class {},
  PointElement: class {},
  LinearScale: class {},
  CategoryScale: class {},
  Legend: class {},
  Tooltip: class {},
}));

import { buildChartDatasets } from "./segment-detail";

describe("buildChartDatasets", () => {
  it("builds total datasets from history data", () => {
    const datasets = buildChartDatasets(MOCK_HISTORY, "total");
    // 1 for raw attempts + 1 per estimator
    expect(datasets).toHaveLength(3);
    // First dataset is the raw attempts
    expect(datasets[0]!.label).toBe("Attempts");
    expect(datasets[0]!.data).toEqual([4.5, 3.8, 3.2]);
  });

  it("builds clean datasets from history data", () => {
    const datasets = buildChartDatasets(MOCK_HISTORY, "clean");
    expect(datasets[0]!.label).toBe("Attempts");
    // clean_tail_ms values converted to seconds
    expect(datasets[0]!.data).toEqual([4.5, 3.2, 3.2]);
  });

  it("labels match attempt numbers", () => {
    const datasets = buildChartDatasets(MOCK_HISTORY, "total");
    // All datasets should have same length as attempts
    for (const ds of datasets) {
      expect(ds.data).toHaveLength(3);
    }
  });
});
```

- [ ] **Step 2: Create `frontend/src/segment-detail.ts`**

```typescript
import {
  Chart,
  LineController,
  LineElement,
  PointElement,
  LinearScale,
  CategoryScale,
  Legend,
  Tooltip,
} from "chart.js";
import { fetchJSON } from "./api";
import { formatTime } from "./format";
import type { SegmentHistory } from "./types";

Chart.register(LineController, LineElement, PointElement, LinearScale, CategoryScale, Legend, Tooltip);

/** Colors for estimator curves — visually distinct, accessible on dark bg. */
const ESTIMATOR_COLORS = ["#4fc3f7", "#ff8a65", "#81c784", "#ba68c8", "#fff176"];

type SeriesMode = "total" | "clean";

interface ChartDataset {
  label: string;
  data: (number | null)[];
  borderColor: string;
  backgroundColor: string;
  borderWidth: number;
  pointRadius: number;
  tension: number;
}

export function buildChartDatasets(history: SegmentHistory, mode: SeriesMode): ChartDataset[] {
  const datasets: ChartDataset[] = [];

  // Raw attempt points
  const rawData = history.attempts.map((a) => {
    const ms = mode === "total" ? a.time_ms : a.clean_tail_ms;
    return ms != null ? ms / 1000 : null;
  });
  datasets.push({
    label: "Attempts",
    data: rawData,
    borderColor: "rgba(255, 255, 255, 0.5)",
    backgroundColor: "rgba(255, 255, 255, 0.7)",
    borderWidth: 2,
    pointRadius: 4,
    tension: 0,
  });

  // Estimator curves
  const estimatorNames = Object.keys(history.estimator_curves);
  estimatorNames.forEach((name, i) => {
    const curves = history.estimator_curves[name]!;
    const series = mode === "total" ? curves.total : curves.clean;
    datasets.push({
      label: name,
      data: series.expected_ms.map((v) => (v != null ? v / 1000 : null)),
      borderColor: ESTIMATOR_COLORS[i % ESTIMATOR_COLORS.length]!,
      backgroundColor: "transparent",
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.3,
    });
  });

  return datasets;
}

let _chart: Chart | null = null;
let _history: SegmentHistory | null = null;
let _mode: SeriesMode = "total";

export async function renderSegmentDetail(
  container: HTMLElement,
  segmentId: string,
  onBack: () => void,
): Promise<void> {
  container.innerHTML = "";

  // Header with back button
  const header = document.createElement("div");
  header.className = "detail-header";
  const backBtn = document.createElement("button");
  backBtn.className = "btn-back";
  backBtn.textContent = "\u2190 Back";
  backBtn.addEventListener("click", onBack);
  header.appendChild(backBtn);
  const title = document.createElement("span");
  title.className = "detail-title";
  title.textContent = "Loading...";
  header.appendChild(title);
  container.appendChild(header);

  // Toggle buttons
  const toggleRow = document.createElement("div");
  toggleRow.className = "detail-toggle";
  const totalBtn = document.createElement("button");
  totalBtn.textContent = "Total";
  totalBtn.className = "toggle-btn active";
  const cleanBtn = document.createElement("button");
  cleanBtn.textContent = "Clean Tail";
  cleanBtn.className = "toggle-btn";
  toggleRow.appendChild(totalBtn);
  toggleRow.appendChild(cleanBtn);
  container.appendChild(toggleRow);

  // Canvas
  const canvas = document.createElement("canvas");
  canvas.id = "segment-chart";
  container.appendChild(canvas);

  // Fetch data
  const history = await fetchJSON<SegmentHistory>(
    `/api/segments/${encodeURIComponent(segmentId)}/history`,
  );
  if (!history) {
    title.textContent = "Failed to load";
    return;
  }

  _history = history;
  _mode = "total";
  title.textContent = history.description || segmentId;

  if (history.attempts.length === 0) {
    const msg = document.createElement("p");
    msg.className = "dim";
    msg.textContent = "No completed attempts yet";
    container.appendChild(msg);
    return;
  }

  // Build chart
  const labels = history.attempts.map((a) => String(a.attempt_number));
  _chart = new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: buildChartDatasets(history, "total"),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        y: {
          title: { display: true, text: "Time (s)" },
          ticks: {
            callback: (v) => formatTime(Number(v) * 1000),
          },
        },
        x: {
          title: { display: true, text: "Attempt #" },
        },
      },
      plugins: {
        legend: { position: "top" },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const v = ctx.parsed.y;
              return ctx.dataset.label + ": " + formatTime(v * 1000);
            },
          },
        },
      },
    },
  });

  // Wire toggle
  totalBtn.addEventListener("click", () => {
    if (_mode === "total") return;
    _mode = "total";
    totalBtn.classList.add("active");
    cleanBtn.classList.remove("active");
    updateChart();
  });
  cleanBtn.addEventListener("click", () => {
    if (_mode === "clean") return;
    _mode = "clean";
    cleanBtn.classList.add("active");
    totalBtn.classList.remove("active");
    updateChart();
  });
}

function updateChart(): void {
  if (!_chart || !_history) return;
  _chart.data.datasets = buildChartDatasets(_history, _mode);
  _chart.update();
}

export function destroySegmentDetail(): void {
  if (_chart) {
    _chart.destroy();
    _chart = null;
  }
  _history = null;
  _mode = "total";
}
```

- [ ] **Step 3: Run the test**

Run: `cd frontend && npm test`
Expected: All pass including `segment-detail.test.ts`

- [ ] **Step 4: Run typecheck**

Run: `cd frontend && npm run typecheck`
Expected: No errors

- [ ] **Step 5: Commit**

```bash
git add frontend/src/segment-detail.ts frontend/src/segment-detail.test.ts
git commit -m "feat: add segment detail chart component with Chart.js"
```

---

### Task 6: Wire drill-down into Model tab

**Files:**
- Modify: `frontend/src/model.ts`
- Modify: `frontend/index.html`

This wires the segment detail view into the Model tab: clicking a segment name drills down, back button returns.

- [ ] **Step 1: Add detail container to `frontend/index.html`**

Inside `<section id="tab-model">`, add a detail container right before the closing `</section>` tag (after the model table). Find the line `</section>` that closes `tab-model` (around line 104) and add before it:

```html
      <!-- Segment detail drill-down (hidden by default) -->
      <div id="segment-detail" style="display:none"></div>
```

- [ ] **Step 2: Modify `frontend/src/model.ts` to support drill-down**

Add import at top of `model.ts`:

```typescript
import { renderSegmentDetail, destroySegmentDetail } from "./segment-detail";
```

Add module-level state:

```typescript
let _currentSegmentId: string | null = null;
```

Add a function to show/hide the detail view:

```typescript
function showSegmentDetail(segmentId: string): void {
  _currentSegmentId = segmentId;
  // Hide model content
  (document.getElementById("model-table") as HTMLElement).style.display = "none";
  (document.querySelector(".model-header") as HTMLElement).style.display = "none";
  (document.getElementById("tuning-panel") as HTMLElement).style.display = "none";
  (document.getElementById("practice-controls") as HTMLElement).style.display = "none";
  const practiceCard = document.getElementById("practice-card") as HTMLElement;
  practiceCard.dataset.wasVisible = practiceCard.style.display;
  practiceCard.style.display = "none";

  // Show detail
  const detail = document.getElementById("segment-detail") as HTMLElement;
  detail.style.display = "";
  renderSegmentDetail(detail, segmentId, hideSegmentDetail);
}

function hideSegmentDetail(): void {
  _currentSegmentId = null;
  destroySegmentDetail();

  // Restore model content
  (document.getElementById("model-table") as HTMLElement).style.display = "";
  (document.querySelector(".model-header") as HTMLElement).style.display = "";
  (document.getElementById("tuning-panel") as HTMLElement).style.display = "";
  (document.getElementById("practice-controls") as HTMLElement).style.display = "";
  const practiceCard = document.getElementById("practice-card") as HTMLElement;
  practiceCard.style.display = practiceCard.dataset.wasVisible || "none";

  // Hide detail
  (document.getElementById("segment-detail") as HTMLElement).style.display = "none";

  // Refresh model data
  fetchModel();
}
```

Export `_currentSegmentId` getter so `app.ts` can check if we're in detail mode (optional — for now the SSE update in `fetchModel` is fine since it only runs when the model tab is active).

Modify the `updateModel` function — change the segment name cell from plain text to a clickable link. Replace the line that builds `tr.innerHTML`:

```typescript
    const nameTd = document.createElement("td");
    const nameLink = document.createElement("a");
    nameLink.href = "#";
    nameLink.textContent = segmentName(s);
    nameLink.addEventListener("click", (e) => {
      e.preventDefault();
      showSegmentDetail(s.segment_id);
    });
    nameTd.appendChild(nameLink);
    tr.appendChild(nameTd);

    tr.innerHTML +=
      "<td>" + formatTime(est?.expected_ms ?? null) + "</td>" +
      "<td>" + (formatTrend(est) ?? "\u2014") + "</td>" +
      "<td>" + formatTime(est?.floor_ms ?? null) + "</td>" +
      "<td>" + s.n_completed + "</td>" +
      "<td>" + formatTime(s.gold_ms) + "</td>";
```

Note: the original `tr.innerHTML = ...` set all 6 cells. The new code creates the first cell as a DOM element with a click handler, then appends the remaining 5 cells via innerHTML. The `tr.innerHTML +=` is safe here because the nameTd was already appended.

**Actually, `innerHTML +=` will destroy the previously appended DOM node.** Use this pattern instead:

```typescript
    const nameTd = document.createElement("td");
    const nameLink = document.createElement("a");
    nameLink.href = "#";
    nameLink.textContent = segmentName(s);
    nameLink.addEventListener("click", (e) => {
      e.preventDefault();
      showSegmentDetail(s.segment_id);
    });
    nameTd.appendChild(nameLink);

    const restHtml =
      "<td>" + formatTime(est?.expected_ms ?? null) + "</td>" +
      "<td>" + (formatTrend(est) ?? "\u2014") + "</td>" +
      "<td>" + formatTime(est?.floor_ms ?? null) + "</td>" +
      "<td>" + s.n_completed + "</td>" +
      "<td>" + formatTime(s.gold_ms) + "</td>";

    tr.innerHTML = restHtml;
    tr.prepend(nameTd);
```

This sets the 5 data cells via innerHTML, then prepends the name cell with its click handler intact.

- [ ] **Step 3: Build and verify**

Run: `cd frontend && npm run build`
Expected: Build succeeds

- [ ] **Step 4: Run frontend tests**

Run: `cd frontend && npm test`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add frontend/src/model.ts frontend/index.html
git commit -m "feat: wire segment detail drill-down into Model tab"
```

---

### Task 7: Add CSS for segment detail view

**Files:**
- Modify: `frontend/style.css` (or wherever styles live)

- [ ] **Step 1: Find the stylesheet**

Check `frontend/style.css` or `frontend/public/style.css` for where CSS lives.

- [ ] **Step 2: Add styles**

```css
/* Segment detail drill-down */
.detail-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.5rem;
}
.btn-back {
  background: none;
  border: 1px solid #555;
  color: #ccc;
  cursor: pointer;
  padding: 0.25rem 0.5rem;
  border-radius: 4px;
}
.btn-back:hover { border-color: #aaa; color: #fff; }
.detail-title {
  font-weight: bold;
  font-size: 1.1rem;
}
.detail-toggle {
  display: flex;
  gap: 0.25rem;
  margin-bottom: 0.5rem;
}
.toggle-btn {
  background: #333;
  border: 1px solid #555;
  color: #ccc;
  cursor: pointer;
  padding: 0.25rem 0.75rem;
  border-radius: 4px;
}
.toggle-btn.active {
  background: #4fc3f7;
  color: #111;
  border-color: #4fc3f7;
}
.toggle-btn:hover:not(.active) { border-color: #aaa; }
#segment-chart {
  width: 100%;
  min-height: 300px;
}
/* Make segment names in model table look clickable */
#model-body a {
  color: #4fc3f7;
  text-decoration: none;
}
#model-body a:hover {
  text-decoration: underline;
}
```

- [ ] **Step 3: Build and verify**

Run: `cd frontend && npm run build`
Expected: Build succeeds

- [ ] **Step 4: Commit**

```bash
git add frontend/style.css
git commit -m "feat: add CSS for segment detail chart view"
```

---

### Task 8: Full test suite and final build

- [ ] **Step 1: Build frontend**

Run: `cd frontend && npm run build`
Expected: Success

- [ ] **Step 2: Run frontend tests and typecheck**

Run: `cd frontend && npm test && npm run typecheck`
Expected: All pass

- [ ] **Step 3: Run full Python test suite**

Run: `python -m pytest`
Expected: All pass

- [ ] **Step 4: Commit any remaining changes**

If there are any unstaged fixes, commit them now.

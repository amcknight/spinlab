# Practice Session Time Savings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Display cumulative expected time saved during a practice session, computed as the delta between the sum of segment `expected_ms` at session start and the current sum.

**Architecture:** Snapshot sums at `PracticeSession.start()` (in-memory, session-scoped). `StateBuilder` recomputes the current sums on each state build and emits `saved_total_ms`/`saved_clean_ms` on the `session` dict. Frontend renders a prominent "Time saved this session" panel at the top of the practice card.

**Tech Stack:** Python 3.11+, FastAPI, SQLite, TypeScript, Vite, Vitest.

**Spec:** See [docs/superpowers/specs/2026-04-05-practice-session-savings-design.md](../specs/2026-04-05-practice-session-savings-design.md).

---

## Task 1: Add snapshot helper and fields to PracticeSession

**Files:**
- Modify: `python/spinlab/practice.py`
- Test: `tests/test_practice.py`

- [ ] **Step 1: Write the failing test for snapshot at start**

Add to `tests/test_practice.py`:

```python
import os
from spinlab.practice import PracticeSession
from spinlab.models import AttemptRecord
from spinlab.scheduler import Scheduler


def test_snapshot_expected_times_at_start(db, tmp_path):
    """start() should populate initial_expected_total_ms and _clean_ms
    with the sum of expected_ms across practicable segments."""
    # Seed an attempt so the estimator produces an expected_ms.
    sched = Scheduler(db, "g")
    sched.process_attempt(SEG_ID, time_ms=5000, completed=True, deaths=0)

    from unittest.mock import AsyncMock
    tcp = AsyncMock()
    tcp.is_connected = True
    ps = PracticeSession(tcp=tcp, db=db, game_id="g")
    ps.start()

    assert ps.initial_expected_total_ms is not None
    assert ps.initial_expected_total_ms > 0
    # clean_tail_ms was not supplied but completed+deaths=0 implies it equals time_ms
    assert ps.initial_expected_clean_ms is not None
    assert ps.initial_expected_clean_ms > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_practice.py::test_snapshot_expected_times_at_start -v`
Expected: FAIL with `AttributeError: 'PracticeSession' object has no attribute 'initial_expected_total_ms'`

- [ ] **Step 3: Add fields and helper to PracticeSession**

Modify `python/spinlab/practice.py`. Add imports at the top:

```python
import os

from .allocators import SegmentWithModel
```

Add two fields in `__init__` after `self.segments_completed = 0`:

```python
        self.initial_expected_total_ms: float | None = None
        self.initial_expected_clean_ms: float | None = None
```

Add a helper method on the class:

```python
    def _snapshot_expected_times(
        self, estimator_name: str
    ) -> tuple[float | None, float | None]:
        """Sum expected_ms across practicable segments using the named estimator.

        A segment contributes iff it has a state_path that exists on disk AND
        the estimator produced a non-None expected_ms. Missing clean estimates
        contribute 0 to clean; missing total estimates contribute 0 to total.
        Returns (None, None) if every segment lacked both estimates.
        """
        segments = SegmentWithModel.load_all(self.db, self.game_id, estimator_name)
        total_sum = 0.0
        clean_sum = 0.0
        any_total = False
        any_clean = False
        for seg in segments:
            if not seg.state_path or not os.path.exists(seg.state_path):
                continue
            output = seg.model_outputs.get(estimator_name)
            if output is None:
                continue
            if output.total.expected_ms is not None:
                total_sum += output.total.expected_ms
                any_total = True
            if output.clean.expected_ms is not None:
                clean_sum += output.clean.expected_ms
                any_clean = True
        return (
            total_sum if any_total else None,
            clean_sum if any_clean else None,
        )
```

Modify `start()` to call the helper:

```python
    def start(self) -> None:
        self.db.create_session(self.session_id, self.game_id)
        (
            self.initial_expected_total_ms,
            self.initial_expected_clean_ms,
        ) = self._snapshot_expected_times(self.scheduler.estimator.name)
        self.is_running = True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_practice.py::test_snapshot_expected_times_at_start -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/practice.py tests/test_practice.py
git commit -m "feat(practice): snapshot expected times at session start"
```

---

## Task 2: Snapshot skips segments without a usable state_path

**Files:**
- Test: `tests/test_practice.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_practice.py`:

```python
def test_snapshot_skips_segments_without_state_path(db, tmp_path):
    """Segments whose state_path does not exist on disk are excluded."""
    from spinlab.models import Segment
    # Add a second segment with no variant -> state_path = None
    seg2 = Segment(
        id="g:2:entrance.0:goal.0",
        game_id="g",
        level_number=2,
        start_type="entrance",
        start_ordinal=0,
        end_type="goal",
        end_ordinal=0,
        description="L2",
        ordinal=2,
    )
    db.upsert_segment(seg2)

    # Seed attempts on BOTH segments so they each have estimates.
    sched = Scheduler(db, "g")
    sched.process_attempt(SEG_ID, time_ms=5000, completed=True, deaths=0)
    sched.process_attempt("g:2:entrance.0:goal.0", time_ms=8000, completed=True, deaths=0)

    from unittest.mock import AsyncMock
    tcp = AsyncMock()
    tcp.is_connected = True
    ps = PracticeSession(tcp=tcp, db=db, game_id="g")
    ps.start()

    # Only SEG_ID had a real state_path; seg2 contributes nothing.
    # The sum should reflect only SEG_ID's expected_ms (~5000).
    assert ps.initial_expected_total_ms is not None
    assert ps.initial_expected_total_ms < 6000
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/test_practice.py::test_snapshot_skips_segments_without_state_path -v`
Expected: PASS (already implemented correctly in Task 1)

- [ ] **Step 3: Commit**

```bash
git add tests/test_practice.py
git commit -m "test(practice): snapshot excludes segments without state_path"
```

---

## Task 3: Snapshot returns (None, None) when no segment has estimates

**Files:**
- Test: `tests/test_practice.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_practice.py`:

```python
def test_snapshot_all_missing_returns_none(db):
    """When no segment has estimates at session start, both snapshots are None."""
    from unittest.mock import AsyncMock
    tcp = AsyncMock()
    tcp.is_connected = True
    # No process_attempt call -> no model state -> no expected_ms
    ps = PracticeSession(tcp=tcp, db=db, game_id="g")
    ps.start()

    assert ps.initial_expected_total_ms is None
    assert ps.initial_expected_clean_ms is None
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/test_practice.py::test_snapshot_all_missing_returns_none -v`
Expected: PASS (helper already handles this)

- [ ] **Step 3: Commit**

```bash
git add tests/test_practice.py
git commit -m "test(practice): snapshot returns None when no estimates exist"
```

---

## Task 4: Expose current_expected_times() for live recompute

**Files:**
- Modify: `python/spinlab/practice.py`
- Test: `tests/test_practice.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_practice.py`:

```python
def test_current_expected_times_reflects_model_updates(db, tmp_path):
    """After process_attempt runs, current_expected_times() returns the new sum."""
    sched = Scheduler(db, "g")
    sched.process_attempt(SEG_ID, time_ms=5000, completed=True, deaths=0)

    from unittest.mock import AsyncMock
    tcp = AsyncMock()
    tcp.is_connected = True
    ps = PracticeSession(tcp=tcp, db=db, game_id="g")
    ps.start()
    initial_total, _ = ps.initial_expected_total_ms, ps.initial_expected_clean_ms

    # Simulate a faster attempt pulling the estimate down.
    ps.scheduler.process_attempt(SEG_ID, time_ms=3000, completed=True, deaths=0)

    cur_total, cur_clean = ps.current_expected_times()
    assert cur_total is not None
    assert cur_total < initial_total
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_practice.py::test_current_expected_times_reflects_model_updates -v`
Expected: FAIL with `AttributeError: 'PracticeSession' object has no attribute 'current_expected_times'`

- [ ] **Step 3: Add the public method**

In `python/spinlab/practice.py`, add this method after `_snapshot_expected_times`:

```python
    def current_expected_times(self) -> tuple[float | None, float | None]:
        """Current sum of expected_ms across practicable segments, using the
        scheduler's currently selected estimator."""
        return self._snapshot_expected_times(self.scheduler.estimator.name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_practice.py::test_current_expected_times_reflects_model_updates -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/practice.py tests/test_practice.py
git commit -m "feat(practice): expose current_expected_times for live recompute"
```

---

## Task 5: StateBuilder emits saved_total_ms / saved_clean_ms

**Files:**
- Modify: `python/spinlab/state_builder.py`
- Test: `tests/test_session_manager.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_session_manager.py`. First locate an existing practice-mode test for the pattern, then append:

```python
def test_practice_state_emits_saved_ms(session_manager_with_practice):
    """get_state() emits saved_total_ms and saved_clean_ms on session."""
    sm = session_manager_with_practice
    # Fake a snapshot + a lower current sum to create positive savings.
    sm.practice_session.initial_expected_total_ms = 10000.0
    sm.practice_session.initial_expected_clean_ms = 8000.0
    sm.practice_session.current_expected_times = lambda: (7500.0, 6500.0)

    state = sm.get_state()
    assert state["session"]["saved_total_ms"] == 2500.0
    assert state["session"]["saved_clean_ms"] == 1500.0


def test_practice_state_saved_ms_null_when_no_snapshot(session_manager_with_practice):
    """If initial snapshot is None, savings fields are None."""
    sm = session_manager_with_practice
    sm.practice_session.initial_expected_total_ms = None
    sm.practice_session.initial_expected_clean_ms = None
    sm.practice_session.current_expected_times = lambda: (None, None)

    state = sm.get_state()
    assert state["session"]["saved_total_ms"] is None
    assert state["session"]["saved_clean_ms"] is None
```

If no `session_manager_with_practice` fixture exists, create one at the top of the test file (or in the nearest module fixture block). Use the same pattern as existing SessionManager fixtures — look for `get_state()` usages in this file and reuse that fixture if practice_session is populated. If not, add:

```python
@pytest.fixture
def session_manager_with_practice(tmp_path):
    """A SessionManager in PRACTICE mode with a stubbed PracticeSession."""
    from unittest.mock import MagicMock
    from spinlab.db import Database
    from spinlab.session_manager import SessionManager
    from spinlab.models import Mode

    db = Database(tmp_path / "test.db")
    db.upsert_game("g", "Game", "any%")
    sm = SessionManager(db=db, tcp=MagicMock())
    sm.game_id = "g"
    sm.game_name = "Game"
    sm.mode = Mode.PRACTICE
    ps = MagicMock()
    ps.session_id = "sess"
    ps.started_at = "2026-04-05T00:00:00Z"
    ps.segments_attempted = 0
    ps.segments_completed = 0
    ps.current_segment_id = None
    ps.initial_expected_total_ms = None
    ps.initial_expected_clean_ms = None
    ps.current_expected_times = lambda: (None, None)
    sm.practice_session = ps
    return sm
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_session_manager.py::test_practice_state_emits_saved_ms tests/test_session_manager.py::test_practice_state_saved_ms_null_when_no_snapshot -v`
Expected: FAIL with `KeyError: 'saved_total_ms'` or similar

- [ ] **Step 3: Add emission in StateBuilder**

Modify `python/spinlab/state_builder.py`. In `_build_practice_state`, after setting `base["session"] = {...}`, add:

```python
        cur_total, cur_clean = ps.current_expected_times()
        saved_total = (
            ps.initial_expected_total_ms - cur_total
            if ps.initial_expected_total_ms is not None and cur_total is not None
            else None
        )
        saved_clean = (
            ps.initial_expected_clean_ms - cur_clean
            if ps.initial_expected_clean_ms is not None and cur_clean is not None
            else None
        )
        base["session"]["saved_total_ms"] = saved_total
        base["session"]["saved_clean_ms"] = saved_clean
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_session_manager.py::test_practice_state_emits_saved_ms tests/test_session_manager.py::test_practice_state_saved_ms_null_when_no_snapshot -v`
Expected: PASS

- [ ] **Step 5: Run full fast test suite**

Run: `pytest -m "not (emulator or slow)"`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/state_builder.py tests/test_session_manager.py
git commit -m "feat(state): emit saved_total_ms and saved_clean_ms in practice state"
```

---

## Task 6: Add frontend types for savings fields

**Files:**
- Modify: `frontend/src/types.ts`
- Test: `frontend/src/api-contract.test.ts`

- [ ] **Step 1: Write the failing test**

Append to `frontend/src/api-contract.test.ts`:

```ts
describe("SessionInfo savings fields", () => {
  it("accepts numeric savings values", () => {
    const s: SessionInfo = {
      id: "sess",
      started_at: "2026-04-05T00:00:00Z",
      segments_attempted: 3,
      segments_completed: 2,
      saved_total_ms: 1500,
      saved_clean_ms: 800,
    };
    expect(s.saved_total_ms).toBe(1500);
    expect(s.saved_clean_ms).toBe(800);
  });

  it("accepts null savings values", () => {
    const s: SessionInfo = {
      id: "sess",
      started_at: "2026-04-05T00:00:00Z",
      segments_attempted: 0,
      segments_completed: 0,
      saved_total_ms: null,
      saved_clean_ms: null,
    };
    expect(s.saved_total_ms).toBeNull();
    expect(s.saved_clean_ms).toBeNull();
  });
});
```

Ensure the test file imports `SessionInfo`: check the top of `api-contract.test.ts` and add `SessionInfo` to the import list from `./types` if not already present.

- [ ] **Step 2: Run type check to verify it fails**

Run: `cd frontend && npm run typecheck`
Expected: FAIL — `Property 'saved_total_ms' does not exist on type 'SessionInfo'`

- [ ] **Step 3: Add fields to SessionInfo**

In `frontend/src/types.ts`, update the `SessionInfo` interface:

```ts
export interface SessionInfo {
  id: string;
  started_at: string;
  segments_attempted: number;
  segments_completed: number;
  saved_total_ms: number | null;
  saved_clean_ms: number | null;
}
```

- [ ] **Step 4: Run tests and typecheck**

Run: `cd frontend && npm run typecheck && npm test`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/types.ts frontend/src/api-contract.test.ts
git commit -m "feat(frontend): add saved_total_ms/saved_clean_ms to SessionInfo"
```

---

## Task 7: Add formatSavings helper

**Files:**
- Modify: `frontend/src/format.ts`
- Test: `frontend/src/format.test.ts`

- [ ] **Step 1: Write the failing test**

Append to `frontend/src/format.test.ts`:

```ts
import { formatSavings } from "./format";

describe("formatSavings", () => {
  it("returns null for null", () => {
    expect(formatSavings(null)).toBeNull();
  });

  it("returns null for undefined", () => {
    expect(formatSavings(undefined)).toBeNull();
  });

  it("formats positive savings with + sign", () => {
    expect(formatSavings(3200)).toBe("+3.2s");
  });

  it("formats negative savings with - sign", () => {
    expect(formatSavings(-1100)).toBe("-1.1s");
  });

  it("formats zero as +0.0s", () => {
    expect(formatSavings(0)).toBe("+0.0s");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- format.test.ts`
Expected: FAIL — `formatSavings is not exported`

- [ ] **Step 3: Add helper**

Append to `frontend/src/format.ts`:

```ts
export function formatSavings(ms: number | null | undefined): string | null {
  if (ms == null) return null;
  const sign = ms >= 0 ? "+" : "-";
  const s = Math.abs(ms) / 1000;
  return sign + s.toFixed(1) + "s";
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npm test -- format.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/format.ts frontend/src/format.test.ts
git commit -m "feat(frontend): add formatSavings helper"
```

---

## Task 8: Add savings panel to practice card HTML

**Files:**
- Modify: `frontend/index.html`

- [ ] **Step 1: Add the panel DOM**

In `frontend/index.html`, modify the `#practice-card` block. Before the existing `<div class="card">` (which currently holds `#current-goal`), add:

```html
        <div id="savings-panel" class="savings-panel" style="display:none">
          <div class="savings-label">Time saved this session</div>
          <div class="savings-values">
            <span id="savings-total" class="savings-value"></span>
            <span class="savings-sep">·</span>
            <span id="savings-clean" class="savings-value"></span>
          </div>
        </div>
```

The final `#practice-card` block should look like:

```html
      <div id="practice-card" style="display:none">
        <div id="savings-panel" class="savings-panel" style="display:none">
          <div class="savings-label">Time saved this session</div>
          <div class="savings-values">
            <span id="savings-total" class="savings-value"></span>
            <span class="savings-sep">·</span>
            <span id="savings-clean" class="savings-value"></span>
          </div>
        </div>
        <div class="card">
          <div class="segment-header">
            <span id="current-goal" class="goal-label"></span>
            <span id="current-attempts" class="dim"></span>
          </div>
          <div id="insight" class="insight-card"></div>
        </div>
        <h3>Recent</h3>
        ...
```

- [ ] **Step 2: Verify HTML renders**

Run: `cd frontend && npm run build`
Expected: Build succeeds.

- [ ] **Step 3: Commit**

```bash
git add frontend/index.html
git commit -m "feat(frontend): add savings-panel DOM to practice card"
```

---

## Task 9: Render savings panel from session state

**Files:**
- Modify: `frontend/src/model.ts`
- Create: `frontend/src/savings-panel.test.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/savings-panel.test.ts`:

```ts
import { describe, it, expect, beforeEach } from "vitest";
import { updateSavingsPanel } from "./model";
import type { SessionInfo } from "./types";

function setupDOM() {
  document.body.innerHTML = `
    <div id="savings-panel" style="display:none">
      <span id="savings-total"></span>
      <span id="savings-clean"></span>
    </div>
  `;
}

describe("updateSavingsPanel", () => {
  beforeEach(setupDOM);

  it("hides panel when session is null", () => {
    updateSavingsPanel(null);
    const panel = document.getElementById("savings-panel") as HTMLElement;
    expect(panel.style.display).toBe("none");
  });

  it("hides panel when both savings are null", () => {
    const session: SessionInfo = {
      id: "s", started_at: "x", segments_attempted: 0, segments_completed: 0,
      saved_total_ms: null, saved_clean_ms: null,
    };
    updateSavingsPanel(session);
    const panel = document.getElementById("savings-panel") as HTMLElement;
    expect(panel.style.display).toBe("none");
  });

  it("shows panel with formatted values when savings present", () => {
    const session: SessionInfo = {
      id: "s", started_at: "x", segments_attempted: 0, segments_completed: 0,
      saved_total_ms: 3200, saved_clean_ms: 1800,
    };
    updateSavingsPanel(session);
    const panel = document.getElementById("savings-panel") as HTMLElement;
    expect(panel.style.display).toBe("");
    expect(document.getElementById("savings-total")!.textContent).toBe("+3.2s total");
    expect(document.getElementById("savings-clean")!.textContent).toBe("+1.8s clean");
  });

  it("applies positive class for positive savings", () => {
    const session: SessionInfo = {
      id: "s", started_at: "x", segments_attempted: 0, segments_completed: 0,
      saved_total_ms: 500, saved_clean_ms: 500,
    };
    updateSavingsPanel(session);
    const total = document.getElementById("savings-total")!;
    expect(total.className).toContain("positive");
  });

  it("applies negative class for regressions", () => {
    const session: SessionInfo = {
      id: "s", started_at: "x", segments_attempted: 0, segments_completed: 0,
      saved_total_ms: -500, saved_clean_ms: -200,
    };
    updateSavingsPanel(session);
    const total = document.getElementById("savings-total")!;
    expect(total.className).toContain("negative");
  });

  it("hides one value and shows the other when mixed", () => {
    const session: SessionInfo = {
      id: "s", started_at: "x", segments_attempted: 0, segments_completed: 0,
      saved_total_ms: 1000, saved_clean_ms: null,
    };
    updateSavingsPanel(session);
    const panel = document.getElementById("savings-panel") as HTMLElement;
    expect(panel.style.display).toBe("");
    expect(document.getElementById("savings-total")!.textContent).toBe("+1.0s total");
    expect(document.getElementById("savings-clean")!.textContent).toBe("");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- savings-panel.test.ts`
Expected: FAIL — `updateSavingsPanel is not exported from ./model`

- [ ] **Step 3: Add updateSavingsPanel and wire it into updatePracticeCard**

In `frontend/src/model.ts`, update the import from `./format`:

```ts
import { segmentName, formatTime, elapsedStr, formatSavings } from "./format";
```

Add the type import:

```ts
import type { AppState, ModelData, TuningData, SessionInfo } from "./types";
```

Add the exported function (above `updatePracticeCard`):

```ts
export function updateSavingsPanel(session: SessionInfo | null): void {
  const panel = document.getElementById("savings-panel") as HTMLElement | null;
  if (!panel) return;

  const totalStr = session ? formatSavings(session.saved_total_ms) : null;
  const cleanStr = session ? formatSavings(session.saved_clean_ms) : null;

  if (totalStr === null && cleanStr === null) {
    panel.style.display = "none";
    return;
  }
  panel.style.display = "";

  const totalEl = document.getElementById("savings-total")!;
  const cleanEl = document.getElementById("savings-clean")!;

  if (totalStr !== null) {
    totalEl.textContent = totalStr + " total";
    totalEl.className =
      "savings-value " + ((session!.saved_total_ms ?? 0) >= 0 ? "positive" : "negative");
  } else {
    totalEl.textContent = "";
    totalEl.className = "savings-value";
  }

  if (cleanStr !== null) {
    cleanEl.textContent = cleanStr + " clean";
    cleanEl.className =
      "savings-value " + ((session!.saved_clean_ms ?? 0) >= 0 ? "positive" : "negative");
  } else {
    cleanEl.textContent = "";
    cleanEl.className = "savings-value";
  }
}
```

Then call it from `updatePracticeCard`. Add this line right after `card.style.display = "";`:

```ts
  updateSavingsPanel(data.session);
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npm test -- savings-panel.test.ts`
Expected: PASS

- [ ] **Step 5: Run full frontend tests and typecheck**

Run: `cd frontend && npm run typecheck && npm test`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/model.ts frontend/src/savings-panel.test.ts
git commit -m "feat(frontend): render time savings panel in practice card"
```

---

## Task 10: Style the savings panel

**Files:**
- Modify: `python/spinlab/static/style.css` (or wherever the frontend CSS lives — check `frontend/src/` first for a `.css` import)

- [ ] **Step 1: Locate the CSS file**

Run: `cd frontend && grep -r "practice-card\|savings" src/ --include="*.css" -l` or `ls frontend/*.css frontend/src/*.css 2>/dev/null`

Note: the build may also copy from `python/spinlab/static/style.css`. Check `frontend/index.html` line 7 (`<link rel="stylesheet" href="/style.css">`) to determine the source.

- [ ] **Step 2: Add styles**

Append to the correct CSS file:

```css
.savings-panel {
  margin-bottom: 0.75rem;
  padding: 0.75rem 1rem;
  border: 1px solid var(--border, #333);
  border-radius: 4px;
  background: var(--panel-bg, rgba(255,255,255,0.03));
}
.savings-label {
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--dim, #888);
  margin-bottom: 0.25rem;
}
.savings-values {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  font-size: 1.5rem;
  font-weight: 600;
}
.savings-value.positive { color: #4caf50; }
.savings-value.negative { color: #e57373; }
.savings-sep { color: var(--dim, #888); font-weight: 400; }
```

Reuse whatever color variables the existing stylesheet uses. If variables like `--border`, `--dim` don't exist, replace with the literal values from surrounding styles (e.g. `#333`, `#888`).

- [ ] **Step 3: Verify in dev server (manual smoke test)**

Run: `cd frontend && npm run build`
Expected: Build succeeds. No manual browser verification required in this plan — user will visually verify at the end.

- [ ] **Step 4: Commit**

```bash
git add <the CSS file you modified>
git commit -m "style(frontend): add savings-panel styles"
```

---

## Task 11: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full backend test suite**

Run: `pytest -m "not (emulator or slow)"`
Expected: All pass, no regressions.

- [ ] **Step 2: Run frontend tests and typecheck**

Run: `cd frontend && npm run typecheck && npm test`
Expected: All pass.

- [ ] **Step 3: Build the frontend**

Run: `cd frontend && npm run build`
Expected: Build succeeds, outputs to `python/spinlab/static/`.

- [ ] **Step 4: No commit needed — verification only.**

---

## Self-Review Notes

**Spec coverage:**
- Snapshot at start → Tasks 1-3
- Live recompute method → Task 4
- StateBuilder emission → Task 5
- Frontend types → Task 6
- formatSavings helper → Task 7
- DOM panel → Task 8
- Rendering + hide behavior → Task 9
- Styling → Task 10

**Edge cases covered:**
- Missing state_path → Task 2
- All-missing snapshot returns None → Task 3
- Null savings in state → Task 5 (second test)
- UI hides when both null → Task 9
- UI handles mixed null/present → Task 9
- Positive/negative coloring → Task 9

**Not in scope (per spec):** snapshot persistence, per-estimator comparison, uncertainty bands, cross-session history.

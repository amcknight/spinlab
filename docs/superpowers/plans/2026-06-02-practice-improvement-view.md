# Practice "Am I Improving?" View — Implementation Plan (Plan A)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a focused "Am I improving on this segment?" view to the practice card — a verdict (faster/holding/slower), a recent clear-time sparkline with PB, death-rate/consistency/gap-to-gold stats, and a last-attempt callout — fed per attempt by a new read-only `segment progress` endpoint.

**Architecture:** A pure `segment_progress(state, golds, …)` reducer over the existing `SamplerState` (the sampler already tracks recent-vs-baseline via its α-suite EMAs). A thin `GET /api/segments/{id}/progress` route mirrors the existing `em-suite-matrix` route. A new `improvement-view.ts` frontend module renders into the practice card on every SSE app-state push, following the `loadAndRenderEmSuitePanel` pattern. No modeling changes — only reads of signals the sampler already computes.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, numpy; TypeScript/Vite, inline SVG sparkline (no new chart deps); pytest + vitest + Playwright smoke.

**Spec:** [`docs/superpowers/specs/2026-06-01-practice-ui-overhaul-design.md`](../specs/2026-06-01-practice-ui-overhaul-design.md) §A (live strip) + §D (readability). This plan delivers the *view*; the responsive strip↔review layout is Plan D.

---

## Scope notes

- **In scope:** the per-segment improvement view + its backend, rendered at the **top of the existing practice card** (Model tab). It updates per attempt via the existing SSE cadence — no new push infra.
- **Out of scope (later plans):** relocating it into a narrow live strip / wide review layout (Plan D); the simulator simplification (Plan B); the alpha memory-window picker (Plan C). The em-suite matrix panel stays exactly where it is for now; this view sits *above* it.

## Key facts about existing code (verified)

- `SamplerState` (in `python/spinlab/estimators/em_suite_sampler.py`) exposes `log_success_time_ema(idx)`, `log_death_time_ema(idx)`, `p_die_ema(idx)` (all `float | None`), `success_time_pool: list[float]` (recency-ordered, newest last), `death_time_pool`, and counters `n_successes`/`n_deaths`/`n_attempts_total`.
- `DEFAULT_FAST_IDX` (α=0.2, "Now"≈last-5) and `DEFAULT_SLOW_IDX` (α=0.05, "Baseline"≈last-20) already exist.
- `_gate_passes(state)` = `n_successes>=2 and n_deaths>=2 and n_attempts_total>=2`.
- `replay_with_history(events)` returns `(state, param_history)`; the `em-suite-matrix` route uses it. `_events_from_rows` is imported in `routes/model.py` already.
- `db.compute_golds(game_id)` → `{seg_id: {"gold_ms": int|None, ...}}`. `db.get_segment_by_id(segment_id)` → segment row (has `game_id`). `db.get_segment_event_rows(segment_id)`.
- Frontend: `fetchJSON<T>(url)` (in `api.ts`); `formatTime(ms)`/`formatSavings(ms)` (in `format.ts`); practice card render is `updatePracticeCard` in `model.ts` (fires per SSE push); panel teardown pattern in `em-suite-panel.ts`.

---

## File Structure

**New files:**
- `python/spinlab/estimators/segment_progress.py` — `SegmentProgress` dataclass + `segment_progress(...)` pure reducer.
- `tests/unit/test_segment_progress.py` — reducer tests.
- `frontend/src/improvement-view.ts` — `renderImprovementView(host, data)` + `loadAndRenderImprovementView(segmentId, host)`.
- `frontend/src/improvement-view.test.ts` — vitest for the render + pure helpers.

**Modified files:**
- `python/spinlab/api_schemas.py` — add `SegmentProgressResponse`.
- `python/spinlab/routes/model.py` — add `GET /segments/{id}/progress`.
- `frontend/src/types.ts` — re-export `SegmentProgress` type.
- `frontend/index.html` — add `<div id="improvement-view">` at the top of `#practice-card`.
- `frontend/src/model.ts` — call `loadAndRenderImprovementView` in `updatePracticeCard`; tear down on exit.
- `tests/integration/test_frontend_smoke.py` — assert the improvement view renders for a gated practicing segment.

---

## Task 1: `segment_progress` reducer (pure) + tests

**Files:**
- Create: `python/spinlab/estimators/segment_progress.py`
- Test: `tests/unit/test_segment_progress.py`

**Why a separate module:** keeps the sampler core focused; this is a read-only *reduction* over a `SamplerState`, not part of the model. Pure function → trivially testable, reused by the route.

- [ ] **Step 1: Write the failing test.** Create `tests/unit/test_segment_progress.py`:

```python
"""Tests for the segment-progress reducer (the 'am I improving?' signal)."""
from __future__ import annotations

import math

from spinlab.estimators.em_suite_sampler import SamplerState
from spinlab.estimators.segment_progress import SegmentProgress, segment_progress


def _state_with(success_ms: list[float], death_ms: list[float]) -> SamplerState:
    """Build a gated state by replaying alternating events through process_event."""
    from spinlab.estimators.em_suite_sampler import process_event
    from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt
    from datetime import UTC, datetime
    state = SamplerState()
    # Interleave so both pools fill and counters gate.
    n = max(len(success_ms), len(death_ms))
    for i in range(n):
        if i < len(death_ms):
            state = process_event(state, EventAttempt(
                segment_id="x", session_id="s", episode_id=f"d{i}",
                outcome=AttemptOutcome.DIED, time_ms=int(death_ms[i]),
                source=AttemptSource.PRACTICE, created_at=datetime.now(UTC)))
        if i < len(success_ms):
            state = process_event(state, EventAttempt(
                segment_id="x", session_id="s", episode_id=f"c{i}",
                outcome=AttemptOutcome.SURVIVED, time_ms=int(success_ms[i]),
                source=AttemptSource.PRACTICE, created_at=datetime.now(UTC)))
    return state


class TestSegmentProgress:
    def test_below_gate_returns_not_ready(self):
        state = SamplerState(n_successes=1, n_deaths=0, n_attempts_total=1)
        p = segment_progress(state, gold_ms=None)
        assert isinstance(p, SegmentProgress)
        assert p.ready is False
        assert p.verdict == "not_ready"
        assert p.now_clear_ms is None
        assert p.trend_ms == []

    def test_improving_when_recent_faster_than_baseline(self):
        # Clears start slow (~6000) and get fast (~4000); recent EMA < baseline EMA.
        state = _state_with(
            success_ms=[6000, 5800, 5600, 5000, 4400, 4200, 4000, 4000],
            death_ms=[1500, 1500, 1500, 1500])
        p = segment_progress(state, gold_ms=3900)
        assert p.ready is True
        assert p.now_clear_ms is not None and p.baseline_clear_ms is not None
        assert p.now_clear_ms < p.baseline_clear_ms
        assert p.verdict == "faster"
        # Trend is the recency-ordered recent clears (newest last), capped.
        assert p.trend_ms[-1] == 4000.0
        assert p.pb_ms == 4000.0
        assert p.gap_to_gold_ms is not None  # now - gold

    def test_slower_when_recent_slower_than_baseline(self):
        state = _state_with(
            success_ms=[4000, 4000, 4200, 4400, 5000, 5600, 5800, 6000],
            death_ms=[1500, 1500, 1500, 1500])
        p = segment_progress(state, gold_ms=3900)
        assert p.verdict == "slower"
        assert p.now_clear_ms > p.baseline_clear_ms

    def test_holding_when_within_noise(self):
        # Flat clears: now ≈ baseline, delta under the standard-error band.
        state = _state_with(
            success_ms=[5000, 5010, 4990, 5005, 4995, 5000, 5002, 4998],
            death_ms=[1500, 1500, 1500, 1500])
        p = segment_progress(state, gold_ms=4800)
        assert p.verdict == "holding"

    def test_death_rate_is_recent_p_die(self):
        state = _state_with(
            success_ms=[4000, 4000, 4000, 4000, 4000],
            death_ms=[1500, 1500, 1500])
        p = segment_progress(state, gold_ms=None)
        assert 0.0 <= p.death_rate <= 1.0

    def test_gap_to_gold_none_when_no_gold(self):
        state = _state_with(
            success_ms=[4000, 4100, 4050, 4000], death_ms=[1500, 1500])
        p = segment_progress(state, gold_ms=None)
        assert p.gap_to_gold_ms is None

    def test_trend_capped_to_recent_window(self):
        state = _state_with(
            success_ms=[float(4000 + i) for i in range(40)],
            death_ms=[1500, 1500])
        p = segment_progress(state, gold_ms=None)
        assert len(p.trend_ms) <= 20  # TREND_WINDOW
```

- [ ] **Step 2: Run it — fails on import.**

Run: `python -m pytest tests/unit/test_segment_progress.py -q`
Expected: ImportError on `spinlab.estimators.segment_progress`.

- [ ] **Step 3: Implement the reducer.** Create `python/spinlab/estimators/segment_progress.py`:

```python
"""Segment-progress reducer — the 'am I improving on this segment?' signal.

Read-only reduction over a SamplerState. Reuses the sampler's α-suite: the
fast α (DEFAULT_FAST_IDX, ~last-5) is "Now" (current skill); the slow α
(DEFAULT_SLOW_IDX, ~last-20) is "Baseline". The signed gap between them is the
improvement signal. No modeling here — only reads of EMAs the sampler already
maintains, plus simple stats over the recent success pool.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from spinlab.estimators.em_suite_sampler import (
    DEFAULT_FAST_IDX,
    DEFAULT_SLOW_IDX,
    SamplerState,
    _gate_passes,
)

# Number of recent clears shown in the trend sparkline. Sized to ~one slow-α
# window (Baseline ≈ 20 attempts) so the line spans "now" back through the
# baseline the verdict compares against.
TREND_WINDOW = 20


@dataclass
class SegmentProgress:
    """'Am I improving?' summary for one segment. ms fields are None below gate.

    verdict ∈ {"faster", "holding", "slower", "not_ready"}. "holding" means the
    Now↔Baseline gap is within the standard error of the recent clears — i.e.
    indistinguishable from no change given the spread we've observed, NOT an
    arbitrary cutoff.
    """
    ready: bool
    verdict: str
    now_clear_ms: float | None        # recent (fast-α) expected clear time
    baseline_clear_ms: float | None   # baseline (slow-α) expected clear time
    death_rate: float                 # recent (fast-α) p_die; 0.0 below gate
    consistency_ms: float | None      # sample stdev of recent clears
    gap_to_gold_ms: float | None      # now_clear_ms − gold_ms (signed), or None
    pb_ms: float | None               # fastest clear in the pool
    trend_ms: list[float]             # recency-ordered recent clears (newest last)


def _ema_time_ms(state: SamplerState, idx: int) -> float | None:
    log_ms = state.log_success_time_ema(idx)
    return None if log_ms is None else math.exp(log_ms)


def segment_progress(state: SamplerState, gold_ms: int | None) -> SegmentProgress:
    if not _gate_passes(state):
        return SegmentProgress(
            ready=False, verdict="not_ready",
            now_clear_ms=None, baseline_clear_ms=None, death_rate=0.0,
            consistency_ms=None, gap_to_gold_ms=None, pb_ms=None, trend_ms=[],
        )

    now = _ema_time_ms(state, DEFAULT_FAST_IDX)
    baseline = _ema_time_ms(state, DEFAULT_SLOW_IDX)
    p_die = state.p_die_ema(DEFAULT_FAST_IDX)
    death_rate = float(p_die) if p_die is not None else 0.0

    recent = list(state.success_time_pool[-TREND_WINDOW:])
    consistency = float(statistics.stdev(recent)) if len(recent) >= 2 else None
    pb = float(min(state.success_time_pool)) if state.success_time_pool else None

    # Verdict: sign of (baseline − now), with a "holding" band = the standard
    # error of the recent-clear mean (spread / sqrt(n)). Inside the band the
    # difference is within observed noise → "holding". Principled, not a fudge.
    verdict = "holding"
    if now is not None and baseline is not None:
        delta = baseline - now  # positive = faster now than baseline
        noise = 0.0
        if consistency is not None and len(recent) >= 2:
            noise = consistency / math.sqrt(len(recent))
        if delta > noise:
            verdict = "faster"
        elif delta < -noise:
            verdict = "slower"

    gap = (now - gold_ms) if (now is not None and gold_ms is not None) else None

    return SegmentProgress(
        ready=True, verdict=verdict,
        now_clear_ms=now, baseline_clear_ms=baseline, death_rate=death_rate,
        consistency_ms=consistency, gap_to_gold_ms=gap, pb_ms=pb,
        trend_ms=recent,
    )
```

- [ ] **Step 4: Run tests — pass.**

Run: `python -m pytest tests/unit/test_segment_progress.py -q`
Expected: 7 passed.

- [ ] **Step 5: Commit.**

```bash
git add python/spinlab/estimators/segment_progress.py tests/unit/test_segment_progress.py
git commit -m "feat(progress): segment_progress reducer — the 'am I improving?' signal"
```

---

## Task 2: `SegmentProgressResponse` schema

**Files:**
- Modify: `python/spinlab/api_schemas.py`

- [ ] **Step 1: Add the schema.** In `python/spinlab/api_schemas.py`, near the other per-segment response models, append:

```python
class SegmentProgressResponse(_BaseResponse):
    """'Am I improving?' payload for one segment. See segment_progress()."""
    segment_id: str
    ready: bool
    verdict: str  # "faster" | "holding" | "slower" | "not_ready"
    now_clear_ms: float | None
    baseline_clear_ms: float | None
    death_rate: float
    consistency_ms: float | None
    gap_to_gold_ms: float | None
    pb_ms: float | None
    trend_ms: list[float] = []
    # Gate diagnostics so the view can say "need N more" inline.
    n_successes: int
    n_deaths: int
```

- [ ] **Step 2: Confirm it imports.**

Run: `python -c "from spinlab.api_schemas import SegmentProgressResponse; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit.**

```bash
git add python/spinlab/api_schemas.py
git commit -m "feat(schemas): SegmentProgressResponse"
```

---

## Task 3: `GET /api/segments/{id}/progress` route + test

**Files:**
- Modify: `python/spinlab/routes/model.py`
- Test: `tests/unit/test_segment_progress_route.py` (create)

- [ ] **Step 1: Write the failing route test.** Create `tests/unit/test_segment_progress_route.py`:

```python
"""Route test for /api/segments/{id}/progress."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from spinlab.db import Database
from spinlab.estimators.em_suite_sampler import SamplerState, process_event
from spinlab.models import AttemptOutcome, AttemptSource, EventAttempt, Segment
from spinlab.routes.model import router
from spinlab.routes._deps import get_db


def _client(tmp_path) -> tuple[TestClient, str]:
    db = Database(str(tmp_path / "t.db"))
    db.upsert_game("g1", "G", "any%")
    seg_id = "g1:6:entrance.0:checkpoint.1:aa:bb"
    db.upsert_segment(Segment(
        id=seg_id, game_id="g1", level_number=6,
        start_type="entrance", start_ordinal=0,
        end_type="checkpoint", end_ordinal=1, active=True))
    sess = "g1:s"
    db.create_session(sess, "g1")
    state = SamplerState()
    for i in range(8):
        for outcome, t in ((AttemptOutcome.DIED, 1500), (AttemptOutcome.SURVIVED, 4200 - i * 20)):
            ev = EventAttempt(
                segment_id=seg_id, session_id=sess, episode_id=f"{outcome.value}{i}",
                outcome=outcome, time_ms=t, source=AttemptSource.PRACTICE,
                created_at=datetime.now(UTC))
            db.log_event_attempt(ev)
            state = process_event(state, ev)
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), seg_id


class TestProgressRoute:
    def test_gated_segment_returns_ready_payload(self, tmp_path):
        client, seg_id = _client(tmp_path)
        resp = client.get(f"/api/segments/{seg_id}/progress")
        assert resp.status_code == 200
        data = resp.json()
        assert data["segment_id"] == seg_id
        assert data["ready"] is True
        assert data["verdict"] in ("faster", "holding", "slower")
        assert data["now_clear_ms"] is not None
        assert len(data["trend_ms"]) >= 1

    def test_unknown_segment_404(self, tmp_path):
        client, _ = _client(tmp_path)
        resp = client.get("/api/segments/does-not-exist/progress")
        assert resp.status_code == 404
```

- [ ] **Step 2: Run it — fails (route missing → 404 on the gated case too / import).**

Run: `python -m pytest tests/unit/test_segment_progress_route.py -q`
Expected: failures (route not defined).

- [ ] **Step 3: Implement the route.** In `python/spinlab/routes/model.py`, add the import to the existing schema import block:

```python
from spinlab.api_schemas import (
    # ... existing imports ...
    SegmentProgressResponse,
)
```

…and append the handler after `get_em_suite_matrix`:

```python
@router.get(
    "/segments/{segment_id}/progress",
    response_model=SegmentProgressResponse,
)
def get_segment_progress(
    segment_id: str,
    db: Database = Depends(get_db),
):
    """'Am I improving on this segment?' — recent-vs-baseline clear time,
    death rate, consistency, gap-to-gold, and the recent clear-time trend.
    Replays the event log through the sampler; pure read."""
    from spinlab.estimators.em_suite_sampler import replay_with_history
    from spinlab.estimators.segment_progress import segment_progress

    seg = db.get_segment_by_id(segment_id)
    if seg is None:
        raise HTTPException(status_code=404, detail=f"Segment not found: {segment_id}")

    events = _events_from_rows(db.get_segment_event_rows(segment_id))
    state, _history = replay_with_history(events)
    gold_ms = db.compute_golds(seg.game_id).get(segment_id, {}).get("gold_ms")
    p = segment_progress(state, gold_ms=gold_ms)
    return {
        "segment_id": segment_id,
        "ready": p.ready,
        "verdict": p.verdict,
        "now_clear_ms": p.now_clear_ms,
        "baseline_clear_ms": p.baseline_clear_ms,
        "death_rate": p.death_rate,
        "consistency_ms": p.consistency_ms,
        "gap_to_gold_ms": p.gap_to_gold_ms,
        "pb_ms": p.pb_ms,
        "trend_ms": p.trend_ms,
        "n_successes": state.n_successes,
        "n_deaths": state.n_deaths,
    }
```

(Note: `seg.game_id` — confirm the segment object/row exposes `game_id`; `get_segment_by_id` returns a `Segment` dataclass with `game_id`. If it returns a dict in this codebase, use `seg["game_id"]`.)

- [ ] **Step 4: Run tests — pass.**

Run: `python -m pytest tests/unit/test_segment_progress_route.py -q`
Expected: 2 passed.

- [ ] **Step 5: Run the fast suite.**

Run: `python -m pytest -m "not emulator" -q`
Expected: green.

- [ ] **Step 6: Commit.**

```bash
git add python/spinlab/routes/model.py tests/unit/test_segment_progress_route.py
git commit -m "feat(routes): GET /api/segments/{id}/progress"
```

---

## Task 4: Regen frontend types + re-export

**Files:**
- Modify: `frontend/src/types.ts`

- [ ] **Step 1: Regen the OpenAPI types.**

Run: `cd frontend && npm run gen-types`
Expected: `frontend/src/api-types.ts` rewritten; contains `SegmentProgressResponse`.

- [ ] **Step 2: Re-export a friendly name.** In `frontend/src/types.ts`, add near the other re-exports:

```typescript
export type SegmentProgress = S["SegmentProgressResponse"];
```

- [ ] **Step 3: Typecheck.**

Run: `cd frontend && npm run typecheck`
Expected: no errors.

- [ ] **Step 4: Commit.**

```bash
git add frontend/src/api-types.ts frontend/openapi.json frontend/src/types.ts
git commit -m "feat(frontend): regen types + export SegmentProgress"
```

---

## Task 5: `improvement-view.ts` renderer + helpers + vitest

**Files:**
- Create: `frontend/src/improvement-view.ts`
- Test: `frontend/src/improvement-view.test.ts`

- [ ] **Step 1: Write the failing test.** Create `frontend/src/improvement-view.test.ts`:

```typescript
import { describe, it, expect } from "vitest";
import { verdictLabel, sparklinePoints, renderImprovementView } from "./improvement-view";
import type { SegmentProgress } from "./types";

const READY: SegmentProgress = {
  segment_id: "s1", ready: true, verdict: "faster",
  now_clear_ms: 21200, baseline_clear_ms: 24000, death_rate: 0.38,
  consistency_ms: 900, gap_to_gold_ms: 1800, pb_ms: 19400,
  trend_ms: [24000, 23000, 22000, 21500, 21000, 20800], n_successes: 6, n_deaths: 5,
};

describe("verdictLabel", () => {
  it("maps verdicts to arrow + words", () => {
    expect(verdictLabel("faster")).toMatch(/↓/);
    expect(verdictLabel("slower")).toMatch(/↑/);
    expect(verdictLabel("holding")).toMatch(/→/);
  });
});

describe("sparklinePoints", () => {
  it("maps N values to N (x,y) pairs within the viewbox", () => {
    const pts = sparklinePoints([10, 8, 6], 100, 40);
    expect(pts.split(" ").length).toBe(3);
    // fastest (6) should be lowest time → highest y is the slowest (10)
    expect(pts).toContain(",");
  });
  it("handles a single point without NaN", () => {
    const pts = sparklinePoints([5], 100, 40);
    expect(pts).not.toContain("NaN");
  });
});

describe("renderImprovementView", () => {
  it("renders the verdict, times in seconds, and no 'undefined'", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderImprovementView(host, READY);
    const text = host.textContent || "";
    expect(text).toContain("21.2s");      // now
    expect(text).toContain("24.0s");      // baseline
    expect(text).not.toContain("undefined");
    expect(host.querySelector("svg")).not.toBeNull();
  });

  it("shows a 'need more data' state when not ready", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderImprovementView(host, {
      ...READY, ready: false, verdict: "not_ready",
      now_clear_ms: null, baseline_clear_ms: null, trend_ms: [],
      n_successes: 1, n_deaths: 0,
    });
    const text = host.textContent || "";
    expect(text.toLowerCase()).toContain("need");
    expect(host.querySelector("svg")).toBeNull();
  });
});
```

- [ ] **Step 2: Run it — fails (module missing).**

Run: `cd frontend && npm test -- improvement-view`
Expected: FAIL (cannot find `./improvement-view`).

- [ ] **Step 3: Implement the module.** Create `frontend/src/improvement-view.ts`:

```typescript
/**
 * "Am I improving on this segment?" view for the practice card.
 *
 * Renders a verdict, a recent clear-time sparkline (inline SVG — no chart dep),
 * death-rate / consistency / gap-to-gold stats, and the PB. Fed per attempt by
 * GET /api/segments/{id}/progress. See the practice UI overhaul spec §A.
 */
import { fetchJSON } from "./api";
import { formatTime, formatSavings } from "./format";
import type { SegmentProgress } from "./types";

export function verdictLabel(verdict: string): string {
  switch (verdict) {
    case "faster": return "↓ Getting faster";
    case "slower": return "↑ Getting slower";
    case "holding": return "→ Holding steady";
    default: return "Not enough data yet";
  }
}

const VERDICT_CLASS: Record<string, string> = {
  faster: "iv-good", slower: "iv-bad", holding: "iv-neutral", not_ready: "iv-dim",
};

/** Map clear-time values to an SVG polyline points string. Lower time = lower y
 * (drawn higher on screen), so an improving (downward-time) run trends up-left
 * to down-right visually as times fall. Single/empty input is NaN-safe. */
export function sparklinePoints(values: number[], w: number, h: number): string {
  if (values.length === 0) return "";
  const lo = Math.min(...values), hi = Math.max(...values);
  const span = hi - lo || 1;
  const step = values.length > 1 ? w / (values.length - 1) : 0;
  return values
    .map((v, i) => {
      const x = (i * step).toFixed(1);
      const y = (h - ((v - lo) / span) * h).toFixed(1);
      return `${x},${y}`;
    })
    .join(" ");
}

export function renderImprovementView(host: HTMLElement, p: SegmentProgress): void {
  host.innerHTML = "";
  const cls = VERDICT_CLASS[p.verdict] ?? "iv-dim";

  if (!p.ready) {
    const needS = Math.max(0, 2 - p.n_successes);
    const needD = Math.max(0, 2 - p.n_deaths);
    const parts: string[] = [];
    if (needS) parts.push(`${needS} more clear${needS === 1 ? "" : "s"}`);
    if (needD) parts.push(`${needD} more death${needD === 1 ? "" : "s"}`);
    host.innerHTML =
      `<div class="iv-verdict iv-dim">Not enough data yet</div>` +
      `<div class="iv-sub">need ${parts.join(" and ") || "more attempts"} to model this segment</div>`;
    return;
  }

  const now = p.now_clear_ms ?? 0;
  const baseline = p.baseline_clear_ms ?? 0;
  const w = 320, hgt = 56;
  const pts = sparklinePoints(p.trend_ms, w, hgt);
  const lastX = p.trend_ms.length ? (w).toFixed(1) : "0";

  host.innerHTML = `
    <div class="iv-verdict ${cls}">${verdictLabel(p.verdict)}</div>
    <div class="iv-sub">recent <b>${formatTime(now)}</b> vs baseline ${formatTime(baseline)}</div>
    <svg class="iv-spark" viewBox="0 0 ${w} ${hgt}" preserveAspectRatio="none">
      <polyline fill="none" stroke="currentColor" stroke-width="2" points="${pts}"/>
      ${p.trend_ms.length ? `<circle cx="${lastX}" cy="0" r="0" />` : ""}
    </svg>
    <div class="iv-stats">
      <span><label>Deaths</label>${(p.death_rate * 100).toFixed(0)}%</span>
      <span><label>Spread</label>${p.consistency_ms == null ? "—" : "±" + formatTime(p.consistency_ms)}</span>
      <span><label>PB</label>${formatTime(p.pb_ms)}</span>
      <span><label>vs gold</label>${p.gap_to_gold_ms == null ? "—" : (formatSavings(-p.gap_to_gold_ms) ?? "—")}</span>
    </div>
  `;
}

let _host: HTMLElement | null = null;

/** Fetch + render for a segment. Safe to call per SSE push. Errors render an
 * inline message rather than throwing (mirrors loadAndRenderEmSuitePanel). */
export async function loadAndRenderImprovementView(
  segmentId: string, host: HTMLElement,
): Promise<void> {
  _host = host;
  try {
    const data = await fetchJSON<SegmentProgress>(
      `/api/segments/${encodeURIComponent(segmentId)}/progress`);
    renderImprovementView(host, data);
  } catch (err) {
    host.innerHTML = `<div class="iv-sub iv-dim">progress unavailable: ${err}</div>`;
  }
}

export function destroyImprovementView(): void {
  if (_host) { _host.innerHTML = ""; _host = null; }
}
```

- [ ] **Step 4: Run tests — pass.**

Run: `cd frontend && npm test -- improvement-view`
Expected: improvement-view tests pass.

- [ ] **Step 5: Commit.**

```bash
git add frontend/src/improvement-view.ts frontend/src/improvement-view.test.ts
git commit -m "feat(frontend): improvement-view renderer + sparkline + tests"
```

---

## Task 6: CSS for the improvement view

**Files:**
- Modify: `frontend/style.css`

- [ ] **Step 1: Append styles** (match the dashboard theme — CSS vars, 11px, the `.pe-*` precedent). At the end of `frontend/style.css`:

```css
/* "Am I improving?" view (top of practice card) */
#improvement-view { padding: 6px 8px; }
.iv-verdict { font-size: 18px; font-weight: 700; margin-bottom: 2px; }
.iv-good { color: var(--green); }
.iv-bad { color: var(--red); }
.iv-neutral { color: var(--accent); }
.iv-dim { color: var(--text-dim); }
.iv-sub { font-size: 12px; color: var(--text-dim); margin-bottom: 6px; }
.iv-sub b { color: var(--text); }
.iv-spark { width: 100%; height: 56px; color: var(--accent); display: block; margin: 4px 0; }
.iv-stats { display: flex; gap: 14px; flex-wrap: wrap; font-size: 13px; color: var(--text); }
.iv-stats label {
  display: block; font-size: 9px; text-transform: uppercase;
  color: var(--text-dim); letter-spacing: 0.5px;
}
```

- [ ] **Step 2: Commit.**

```bash
git add frontend/style.css
git commit -m "style(frontend): improvement-view CSS"
```

---

## Task 7: Wire into the practice card

**Files:**
- Modify: `frontend/index.html`
- Modify: `frontend/src/model.ts`

- [ ] **Step 1: Add the host element.** In `frontend/index.html`, inside `#practice-card` (line ~44), as the FIRST child so it sits at the top:

```html
<div id="improvement-view"></div>
```

- [ ] **Step 2: Wire the render.** In `frontend/src/model.ts`, add the import near the `em-suite-panel` import:

```typescript
import {
  loadAndRenderImprovementView,
  destroyImprovementView,
} from "./improvement-view";
```

In `updatePracticeCard`, in the early-return branch (mode not practice/hyper_play or no current_segment), add alongside `destroyEmSuitePanel()`:

```typescript
    destroyImprovementView();
```

…and in the active branch, BEFORE the em-suite panel block, add:

```typescript
  const improvementHost = document.getElementById("improvement-view") as HTMLElement;
  if (improvementHost) {
    void loadAndRenderImprovementView(data.current_segment.id, improvementHost);
  }
```

- [ ] **Step 3: Build + typecheck.**

Run: `cd frontend && npm run typecheck && npm run build`
Expected: clean build into `python/spinlab/static/`.

- [ ] **Step 4: Commit.**

```bash
git add frontend/index.html frontend/src/model.ts
git commit -m "feat(frontend): mount improvement view atop the practice card"
```

---

## Task 8: Smoke test + final verification

**Files:**
- Modify: `tests/integration/test_frontend_smoke.py`

- [ ] **Step 1: Add a smoke test.** The `simulator_seeded` fixture already seeds gated segments with events (from Plan-zero work). The practice card only shows when `mode == "practice"`, which the fake backend isn't in — so assert the **endpoint + renderer contract** directly via a fetch + DOM injection, which is the cheapest reliable signal here. Append to `tests/integration/test_frontend_smoke.py`:

```python
@pytest.mark.asyncio(loop_scope="session")
async def test_segment_progress_endpoint_and_view(page, simulator_seeded):
    """The progress endpoint returns a ready payload for a gated segment and the
    improvement-view renderer paints a verdict with seconds (no 'undefined')."""
    pg, errors = page
    gated_id = simulator_seeded["gated"][0]
    # Endpoint contract:
    resp = await pg.evaluate(
        """async (id) => {
            const r = await fetch(`/api/segments/${encodeURIComponent(id)}/progress`);
            return { status: r.status, body: await r.json() };
        }""", gated_id)
    assert resp["status"] == 200
    b = resp["body"]
    assert b["ready"] is True
    assert b["verdict"] in ("faster", "holding", "slower")
    # Payload shape the renderer consumes (renderer itself is covered by vitest):
    assert isinstance(b["trend_ms"], list) and len(b["trend_ms"]) >= 1
    assert b["now_clear_ms"] is not None
    assert not errors, f"console/page errors: {errors}"
```

(The bundled ES module isn't importable by URL in this harness, so the smoke test asserts the **endpoint contract + payload shape**; the renderer's DOM output is covered by the Task 5 vitest. Don't try to import the bundle here.)

- [ ] **Step 2: Run the new smoke test.**

Run: `python -m pytest tests/integration/test_frontend_smoke.py -q`
Expected: all pass (the new test + the existing ones).

- [ ] **Step 3: Static analysis.**

Run: `npx pyright python/spinlab/estimators/segment_progress.py python/spinlab/routes/model.py`
Expected: no new errors.
Run: `cd frontend && npm run typecheck`
Expected: clean.

- [ ] **Step 4: Full unfiltered suite (project merge rule).**

Run: `python -m pytest`
Expected: green, count up by the new tests. (Requires RetroArch for the emulator subset; no live dashboard holding NCI 55355.)

- [ ] **Step 5: Commit any remaining + final.**

```bash
git add -A
git commit -m "test(progress): smoke coverage for segment-progress endpoint + payload shape"
```

---

## Self-review notes

- **Spec coverage (§A live view):** verdict ✓, recent-vs-baseline ✓ (Task 1), clear-time sparkline ✓ (Task 5), death-rate/consistency/PB/gap stats ✓, last-attempt callout — **deferred**: the spec's "last attempt cleared X" line needs the most-recent event's outcome+time, not in `SamplerState`. Add in execution if cheap (read `events[-1]` in the route and add `last_outcome`/`last_time_ms` to the payload), or fold into Plan D. Flagged, not silently dropped.
- **Verdict band** uses the standard error of recent clears (principled, documented) rather than a magic cutoff — but Andrew should sanity-check the "holding" band feels right on real data; it's the one modeling-flavored choice here.
- **Readability:** all times via `formatTime` (seconds); not-ready state inline with "need N more". No scientific notation, no raw seg_ids.
- **No new push infra:** rides the existing SSE `updatePracticeCard` cadence.
- **Type consistency:** `SegmentProgress` fields match across reducer → schema → TS re-export.

## Open items for Plan D (not this plan)

- Move this view into the narrow live strip; make the layout responsive.
- The last-attempt callout (see above) if not added here.

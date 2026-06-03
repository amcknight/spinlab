# D-Live-FE1: Episode-Time Graph Component — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the default occupant of the live view's swappable graph slot — an inline-SVG episode-time trend (blue line over completions) with a diagonal clean-clear "floor" line, a seconds Y-axis, and per-completion death counts — as a pure, tested render function consuming the shipped `/segments/{id}/live` payload.

**Architecture:** A standalone `frontend/src/episode-graph.ts` module: pure helpers (y-mapping, line-point builders, axis ticks) + a `renderEpisodeGraph(host, data)` that paints one SVG. No fetch, no SSE wiring, no chart dep (matches the existing `improvement-view.ts` inline-SVG pattern). It consumes the `LiveSegmentViewResponse` payload (per-completion `series` of `{episode_ms, deaths, clean_ms, running_floor_ms}`). Wiring into the practice card, the segment summary, the route bar, and liveliness are later sub-plans (FE-2/3/4).

**Tech Stack:** TypeScript/Vite, inline SVG, vitest (happy-dom). No new deps.

**Spec:** [`docs/superpowers/specs/2026-06-02-live-practice-view-design.md`](../specs/2026-06-02-live-practice-view-design.md) — §"Graph #1 — episode-time trend" + the Computation Sources table. BE shipped (`08a3550`): `GET /api/segments/{id}/live` returns the payload this graph renders.

---

## Key facts about existing code (verified)

- BE payload `LiveSegmentViewResponse` (in `api_schemas.py`): `segment_id, ready:bool, expected_episode_ms, practice_gain_ms, death_rate:float, floor_ms, last_episode_ms, last_clean_ms, last_deaths, last_rank, series:list[dict], n_successes, n_deaths`. `series` items are `{episode_ms:float, deaths:int, clean_ms:float|null, running_floor_ms:float|null}` (chronological, one per completed episode). `floor_ms` = the final running-min clean clear (≤ every episode time).
- `frontend/src/format.ts`: `formatTime(ms|null) -> "12.3s" | "—"`.
- Inline-SVG precedent: `frontend/src/improvement-view.ts` `sparklinePoints(values, w, h)` (lower value = lower y). The episode graph generalizes this with an explicit shared y-scale across two series + an axis.
- `frontend/src/types.ts` re-exports API types as `S["..."]` and holds frontend-only convenience interfaces (e.g. `SegmentLike`). The `series` schema is `list[dict]`, which codegen types loosely — so we add a frontend-only `EpisodePoint` interface for the item shape.
- Tests: vitest files live beside source as `*.test.ts` (e.g. `improvement-view.test.ts`). Run `cd frontend && npm test -- <name>`.

## File Structure

**New files:**
- `frontend/src/episode-graph.ts` — helpers + `renderEpisodeGraph`.
- `frontend/src/episode-graph.test.ts` — vitest.

**Modified files:**
- `frontend/src/types.ts` — regen + `LiveSegmentView` re-export + `EpisodePoint` convenience type.
- `frontend/style.css` — graph styles.

---

## Task 1: Types — regen + re-exports

**Files:**
- Modify: `frontend/src/types.ts`

- [ ] **Step 1: Regen OpenAPI types.**

Run: `cd frontend && npm run gen-types`
Expected: `frontend/src/api-types.ts` rewritten; contains `LiveSegmentViewResponse` and `RouteSummaryResponse` (the BE shipped these).

- [ ] **Step 2: Add re-exports + the frontend-only point type.** In `frontend/src/types.ts`, add near the other API re-exports (after the `SegmentProgress` line):

```typescript
export type LiveSegmentView = S["LiveSegmentViewResponse"];
export type RouteSummary = S["RouteSummaryResponse"];
```

…and in the frontend-only conveniences section (after `SegmentLike`), add:

```typescript
/** One completed-episode point in a LiveSegmentView.series. The API types
 *  `series` loosely (schema `list[dict]`), so this names the item shape the
 *  episode graph relies on. Keep in sync with live_view.py's series dicts. */
export interface EpisodePoint {
  episode_ms: number;
  deaths: number;
  clean_ms: number | null;
  running_floor_ms: number | null;
}
```

- [ ] **Step 3: Typecheck.**

Run: `cd frontend && npm run typecheck`
Expected: no errors.

- [ ] **Step 4: Commit.**

```bash
git add frontend/src/api-types.ts frontend/openapi.json frontend/src/types.ts
git commit -m "feat(frontend): regen types + LiveSegmentView/RouteSummary/EpisodePoint"
```

---

## Task 2: `episode-graph.ts` helpers + tests (Red)

**Files:**
- Create: `frontend/src/episode-graph.test.ts`

- [ ] **Step 1: Write the failing test.** Create `frontend/src/episode-graph.test.ts`:

```typescript
import { describe, it, expect } from "vitest";
import {
  yForTime, linePoints, axisTicks, deathLabels, renderEpisodeGraph,
} from "./episode-graph";
import type { EpisodePoint, LiveSegmentView } from "./types";

const PTS: EpisodePoint[] = [
  { episode_ms: 31000, deaths: 4, clean_ms: 14200, running_floor_ms: 14200 },
  { episode_ms: 24000, deaths: 2, clean_ms: 13800, running_floor_ms: 13800 },
  { episode_ms: 16800, deaths: 1, clean_ms: 12800, running_floor_ms: 12800 },
];

// Layout constants the helpers are tested against (mirror the module's).
const GEO = { left: 30, right: 392, top: 10, bottom: 104 };

describe("yForTime", () => {
  it("maps lo time to the bottom and hi time to the top", () => {
    // lower time = lower on chart (larger y); higher time = top (smaller y)
    expect(yForTime(12800, 12800, 31000, GEO.top, GEO.bottom)).toBeCloseTo(GEO.bottom, 1);
    expect(yForTime(31000, 12800, 31000, GEO.top, GEO.bottom)).toBeCloseTo(GEO.top, 1);
  });
  it("is NaN-safe when lo == hi (single distinct value)", () => {
    const y = yForTime(5000, 5000, 5000, GEO.top, GEO.bottom);
    expect(Number.isNaN(y)).toBe(false);
  });
});

describe("linePoints", () => {
  it("builds one x,y pair per point spanning the plot width", () => {
    const pts = linePoints(PTS.map(p => p.episode_ms), 12800, 31000, GEO);
    const pairs = pts.trim().split(" ");
    expect(pairs.length).toBe(3);
    expect(pts).not.toContain("NaN");
    // first x at left, last x at right
    expect(pairs[0].startsWith(String(GEO.left))).toBe(true);
    expect(pairs[2].startsWith(String(GEO.right))).toBe(true);
  });
  it("skips null values (floor line may have gaps) without NaN", () => {
    const pts = linePoints([14200, null, 12800], 12800, 31000, GEO);
    expect(pts).not.toContain("NaN");
    expect(pts.trim().split(" ").length).toBe(2); // null dropped
  });
});

describe("axisTicks", () => {
  it("returns labeled tick values within [lo, hi]", () => {
    const ticks = axisTicks(12800, 31000, 3);
    expect(ticks.length).toBe(3);
    for (const t of ticks) {
      expect(t.ms).toBeGreaterThanOrEqual(12800);
      expect(t.ms).toBeLessThanOrEqual(31000);
      expect(t.label).toMatch(/s$/); // formatted seconds
    }
  });
});

describe("deathLabels", () => {
  it("emits one label per point with its death count and x position", () => {
    const labels = deathLabels(PTS, GEO);
    expect(labels.length).toBe(3);
    expect(labels[0].deaths).toBe(4);
    expect(labels[2].x).toBeCloseTo(GEO.right, 1);
  });
});

describe("renderEpisodeGraph", () => {
  const READY: LiveSegmentView = {
    segment_id: "s1", ready: true, expected_episode_ms: 21800,
    practice_gain_ms: 500, death_rate: 0.62, floor_ms: 12800,
    last_episode_ms: 16800, last_clean_ms: 13600, last_deaths: 1, last_rank: 2,
    series: PTS as unknown as Record<string, never>[],
    n_successes: 6, n_deaths: 5,
  };

  it("renders an svg with episode + floor polylines and no NaN/undefined", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderEpisodeGraph(host, READY);
    const svg = host.querySelector("svg");
    expect(svg).not.toBeNull();
    expect(host.querySelectorAll("polyline").length).toBe(2); // episode + floor
    const html = host.innerHTML;
    expect(html).not.toContain("NaN");
    expect(html).not.toContain("undefined");
    expect(html).toContain("floor"); // floor label present
  });

  it("renders a placeholder (no svg) when not ready", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderEpisodeGraph(host, { ...READY, ready: false, series: [], floor_ms: null });
    expect(host.querySelector("svg")).toBeNull();
    expect((host.textContent || "").toLowerCase()).toContain("not enough");
  });

  it("renders a placeholder when ready but no completed episodes", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderEpisodeGraph(host, { ...READY, series: [], floor_ms: null });
    expect(host.querySelector("svg")).toBeNull();
  });
});
```

- [ ] **Step 2: Run — Red.**

Run: `cd frontend && npm test -- episode-graph`
Expected: FAIL (cannot find `./episode-graph`).

---

## Task 3: `episode-graph.ts` implementation (Green)

**Files:**
- Create: `frontend/src/episode-graph.ts`

- [ ] **Step 1: Implement.** Create `frontend/src/episode-graph.ts`:

```typescript
/**
 * Episode-time trend graph — the default occupant of the live view's graph slot.
 *
 * Plots one point per completed episode (episode time, incl. deaths+reload) as a
 * blue line that sinks toward a diagonal "floor" (the running-best clean clear).
 * Per-completion death counts sit under each point. Seconds Y-axis, lower = faster.
 * Pure render over the /segments/{id}/live payload — no fetch, no chart dep.
 * See the D-Live spec, "Graph #1".
 */
import { formatTime } from "./format";
import type { EpisodePoint, LiveSegmentView } from "./types";

// SVG geometry (viewBox units). Left gutter holds y-axis labels; bottom band
// holds the per-completion death counts.
const GEO = { left: 30, right: 392, top: 10, bottom: 104 } as const;
const VIEW_W = 400;
const VIEW_H = 124;
const DEATH_Y = 120; // baseline for the death-count row
const AXIS_TICKS = 3;

/** Map a time (ms) to a y pixel: lower time = lower on the chart (larger y),
 *  higher time = top. NaN-safe when lo == hi. */
export function yForTime(
  v: number, lo: number, hi: number, top: number, bottom: number,
): number {
  const span = hi - lo || 1;
  const frac = (hi - v) / span; // v=hi -> 0 (top), v=lo -> 1 (bottom)
  return top + frac * (bottom - top);
}

/** Build an SVG polyline points string for a series of times against a shared
 *  [lo, hi] scale. `null` entries are skipped (the floor line can have gaps
 *  before the first completed clean). NaN-safe. */
export function linePoints(
  values: (number | null)[],
  lo: number, hi: number,
  geo: { left: number; right: number; top: number; bottom: number },
): string {
  const n = values.length;
  const step = n > 1 ? (geo.right - geo.left) / (n - 1) : 0;
  const out: string[] = [];
  values.forEach((v, i) => {
    if (v == null) return;
    const x = (geo.left + i * step).toFixed(1);
    const y = yForTime(v, lo, hi, geo.top, geo.bottom).toFixed(1);
    out.push(`${x},${y}`);
  });
  return out.join(" ");
}

/** Evenly spaced y-axis ticks across [lo, hi], formatted in seconds. */
export function axisTicks(
  lo: number, hi: number, count: number,
): { ms: number; label: string }[] {
  if (count < 1) return [];
  if (count === 1) return [{ ms: hi, label: formatTime(hi) }];
  const ticks: { ms: number; label: string }[] = [];
  for (let i = 0; i < count; i++) {
    const ms = hi - (i * (hi - lo)) / (count - 1);
    ticks.push({ ms, label: formatTime(ms) });
  }
  return ticks;
}

/** x position + death count for each completed episode. */
export function deathLabels(
  points: EpisodePoint[],
  geo: { left: number; right: number },
): { x: number; deaths: number }[] {
  const n = points.length;
  const step = n > 1 ? (geo.right - geo.left) / (n - 1) : 0;
  return points.map((p, i) => ({ x: geo.left + i * step, deaths: p.deaths }));
}

function placeholder(host: HTMLElement, msg: string): void {
  host.innerHTML = `<div class="eg-empty">${msg}</div>`;
}

export function renderEpisodeGraph(host: HTMLElement, data: LiveSegmentView): void {
  host.innerHTML = "";
  if (!data.ready) {
    placeholder(host, "Not enough data yet");
    return;
  }
  const points = (data.series ?? []) as unknown as EpisodePoint[];
  if (points.length === 0 || data.floor_ms == null) {
    placeholder(host, "No completed runs yet");
    return;
  }

  const episodes = points.map(p => p.episode_ms);
  const lo = data.floor_ms;                       // floor <= every episode time
  const hi = Math.max(...episodes);
  const episodePts = linePoints(episodes, lo, hi, GEO);
  const floorPts = linePoints(points.map(p => p.running_floor_ms), lo, hi, GEO);
  const floorY = yForTime(lo, lo, hi, GEO.top, GEO.bottom);

  const ticks = axisTicks(lo, hi, AXIS_TICKS)
    .map(t => `<text x="2" y="${(yForTime(t.ms, lo, hi, GEO.top, GEO.bottom) + 3).toFixed(1)}" class="eg-axis">${t.label}</text>`)
    .join("");
  const deaths = deathLabels(points, GEO)
    .map(d => `<text x="${d.x.toFixed(1)}" y="${DEATH_Y}" class="eg-death" text-anchor="middle">${d.deaths}</text>`)
    .join("");
  const lastX = (GEO.left + (points.length > 1 ? GEO.right - GEO.left : 0)).toFixed(1);
  const lastY = yForTime(episodes[episodes.length - 1], lo, hi, GEO.top, GEO.bottom).toFixed(1);

  host.innerHTML = `
    <svg class="eg-svg" viewBox="0 0 ${VIEW_W} ${VIEW_H}" preserveAspectRatio="none">
      ${ticks}
      <polyline class="eg-floor" fill="none" points="${floorPts}"/>
      <text x="${GEO.right - 48}" y="${(floorY - 3).toFixed(1)}" class="eg-floor-label">floor ${formatTime(lo)}</text>
      <polyline class="eg-line" fill="none" points="${episodePts}"/>
      <circle class="eg-last" cx="${lastX}" cy="${lastY}" r="3.5"/>
      ${deaths}
    </svg>
  `;
}
```

- [ ] **Step 2: Run — Green.**

Run: `cd frontend && npm test -- episode-graph`
Expected: all pass.

- [ ] **Step 3: Full vitest + typecheck.**

Run: `cd frontend && npm test && npm run typecheck`
Expected: green + clean.

- [ ] **Step 4: Commit.**

```bash
git add frontend/src/episode-graph.ts frontend/src/episode-graph.test.ts
git commit -m "feat(live-view): episode-time graph component (line + diagonal floor + death counts)"
```

---

## Task 4: CSS

**Files:**
- Modify: `frontend/style.css`

- [ ] **Step 1: Append styles** at the end of `frontend/style.css` (match the dark theme + CSS vars used elsewhere):

```css
/* Episode-time graph (live view, default graph slot) */
.eg-svg { width: 100%; height: 140px; display: block; }
.eg-line { stroke: var(--accent); stroke-width: 2.5; }
.eg-floor { stroke: var(--green); stroke-width: 1; stroke-dasharray: 4 3; opacity: 0.75; }
.eg-last { fill: var(--accent); }
.eg-axis { fill: var(--text-dim); font-size: 9px; }
.eg-floor-label { fill: var(--green); font-size: 9px; }
.eg-death { fill: var(--red); font-size: 8px; }
.eg-empty { color: var(--text-dim); font-size: 12px; padding: 16px 8px; text-align: center; }
```

- [ ] **Step 2: Build.**

Run: `cd frontend && npm run build`
Expected: clean build into `python/spinlab/static/`.

- [ ] **Step 3: Commit.**

```bash
git add frontend/style.css
git commit -m "style(live-view): episode-time graph styles"
```

---

## Task 5: Verification

**Files:** none.

- [ ] **Step 1: Frontend checks.** `cd frontend && npm run typecheck && npm test` → clean + green (incl. episode-graph).
- [ ] **Step 2: Fast suite** (frontend smoke builds the bundle): `python -m pytest -m "not emulator" -q` → green. (Build first: `cd frontend && npm run build`.)
- [ ] **Step 3: No live dashboard needed** (pure component; no new backend). Full emulator gate is unchanged from `08a3550` — run `python -m pytest` before the eventual D-Live merge, not necessarily per FE sub-plan.

---

## Self-review notes

- **Spec coverage (Graph #1):** Y axis in seconds ✓ (`axisTicks`), episode-time line ✓, diagonal floor from `running_floor_ms` ✓ (`linePoints` over the floor series, with the floor label), per-completion death counts ✓ (`deathLabels`), lower=faster ✓ (`yForTime`), last-completion dot ✓. **Deferred (later FE sub-plans):** the live climbing dot (FE-4 liveliness — needs the current-attempt timer); the graph slot *picker* (FE-4/D-Viz — this is just the default occupant); session-start vertical line (FE-4, needs BE-2's session data); mounting into the practice card (FE-4).
- **No fabricated values:** not-ready and no-completed-episodes render a placeholder, never a fake axis; `null` floor entries are skipped, not zero-filled; `formatTime(null)` → "—".
- **No magic numbers beyond named layout constants:** `GEO`, `VIEW_W/H`, `DEATH_Y`, `AXIS_TICKS` are named with rationale; they're pixel-geometry, the legitimate place for layout literals (mirrors `improvement-view.ts`'s `w/hgt`).
- **Reuse:** `formatTime` from format.ts; inline-SVG pattern from improvement-view.ts; no new deps.
- **Type consistency:** `EpisodePoint` (types.ts) is the series-item contract; `renderEpisodeGraph(host, LiveSegmentView)`; helpers (`yForTime`, `linePoints`, `axisTicks`, `deathLabels`) names match tests.
- **The `series as unknown as EpisodePoint[]` cast** is the one type seam — unavoidable because the BE schema types `series` as `list[dict]`; documented on `EpisodePoint`.

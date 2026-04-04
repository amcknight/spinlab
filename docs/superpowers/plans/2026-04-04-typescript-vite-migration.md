# TypeScript + Vite Migration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the 6 vanilla JS dashboard modules to TypeScript with Vite build tooling and Vitest tests, fixing bugs found along the way.

**Architecture:** New `frontend/` directory with TypeScript source that builds to `python/spinlab/static/`. Vite dev server proxies `/api` to FastAPI. Pure logic separated from DOM bindings for testability.

**Tech Stack:** TypeScript 5.x, Vite 6.x, Vitest 3.x, happy-dom (for lightweight DOM in tests)

**Spec:** `docs/superpowers/specs/2026-04-04-typescript-vite-migration-design.md`

---

### Task 1: Scaffold frontend project

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/tsconfig.json`
- Create: `frontend/vite.config.ts`
- Modify: `.gitignore`

- [ ] **Step 1: Create `frontend/package.json`**

```json
{
  "name": "spinlab-dashboard",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc && vite build",
    "preview": "vite preview",
    "test": "vitest run",
    "test:watch": "vitest",
    "typecheck": "tsc --noEmit"
  },
  "devDependencies": {
    "typescript": "^5.8",
    "vite": "^6.3",
    "vitest": "^3.1",
    "happy-dom": "^17.4"
  }
}
```

- [ ] **Step 2: Create `frontend/tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "outDir": "dist",
    "rootDir": "src",
    "sourceMap": true,
    "declaration": false,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true,
    "lib": ["ES2022", "DOM", "DOM.Iterable"]
  },
  "include": ["src"]
}
```

- [ ] **Step 3: Create `frontend/vite.config.ts`**

```ts
import { defineConfig } from "vite";

export default defineConfig({
  root: ".",
  build: {
    outDir: "../python/spinlab/static",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "happy-dom",
  },
});
```

- [ ] **Step 4: Add frontend build output and node_modules to `.gitignore`**

Append to `.gitignore`:
```
# Frontend build tooling
frontend/node_modules/
```

- [ ] **Step 5: Install dependencies**

Run from `frontend/`:
```bash
cd frontend && npm install
```
Expected: `node_modules/` created, `package-lock.json` generated.

- [ ] **Step 6: Verify Vite runs**

```bash
cd frontend && npx vite --version
```
Expected: prints Vite version number.

- [ ] **Step 7: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/tsconfig.json frontend/vite.config.ts .gitignore
git commit -m "chore: scaffold frontend project with Vite + TypeScript + Vitest"
```

---

### Task 2: Define shared types (`types.ts`)

**Files:**
- Create: `frontend/src/types.ts`

These interfaces are derived from the Python dataclasses and route handlers. They are the central contract between backend and frontend.

- [ ] **Step 1: Create `frontend/src/types.ts`**

```ts
// -- Enums as string unions --

export type Mode =
  | "idle"
  | "reference"
  | "practice"
  | "replay"
  | "fill_gap"
  | "cold_fill";

export type EndpointType = "entrance" | "checkpoint" | "goal";

// -- API response shapes --

export interface Estimate {
  expected_ms: number | null;
  ms_per_attempt: number | null;
  floor_ms: number | null;
}

export interface ModelOutput {
  total: Estimate;
  clean: Estimate;
}

/** Shape of a segment in the /api/model response. */
export interface ModelSegment {
  segment_id: string;
  description: string;
  level_number: number;
  start_type: string;
  start_ordinal: number;
  end_type: string;
  end_ordinal: number;
  selected_model: string;
  model_outputs: Record<string, ModelOutput>;
  n_completed: number;
  n_attempts: number;
  gold_ms: number | null;
  clean_gold_ms: number | null;
}

export interface EstimatorInfo {
  name: string;
  display_name: string;
}

/** GET /api/model */
export interface ModelData {
  estimator: string | null;
  estimators: EstimatorInfo[];
  allocator_weights: Record<string, number> | null;
  segments: ModelSegment[];
}

export interface ParamDef {
  name: string;
  display_name: string;
  default: number;
  min: number;
  max: number;
  step: number;
  description: string;
  value: number;
}

/** GET /api/estimator-params */
export interface TuningData {
  estimator: string;
  params: ParamDef[];
}

export interface DraftState {
  run_id: string;
  segments_captured: number;
}

export interface ColdFillState {
  current: number;
  total: number;
  segment_label: string;
}

export interface SessionInfo {
  id: string;
  started_at: string;
  segments_attempted: number;
  segments_completed: number;
}

/** Segment as it appears in current_segment (practice state). */
export interface CurrentSegment {
  id: string;
  game_id: string;
  level_number: number;
  start_type: string;
  start_ordinal: number;
  end_type: string;
  end_ordinal: number;
  description: string;
  attempt_count: number;
  model_outputs: Record<string, ModelOutput>;
  selected_model: string;
  state_path: string | null;
}

export interface RecentAttempt {
  id: number;
  segment_id: string;
  completed: number;
  time_ms: number | null;
  description: string;
  level_number: number;
  start_type: string;
  start_ordinal: number;
  end_type: string;
  end_ordinal: number;
}

/** GET /api/state and SSE event payload. */
export interface AppState {
  mode: Mode;
  tcp_connected: boolean;
  game_id: string | null;
  game_name: string | null;
  current_segment: CurrentSegment | null;
  recent: RecentAttempt[];
  session: SessionInfo | null;
  sections_captured: number;
  allocator_weights: Record<string, number> | null;
  estimator: string | null;
  capture_run_id: string | null;
  draft: DraftState | null;
  cold_fill: ColdFillState | null;
}

/** Segment as returned by /api/references/{id}/segments. */
export interface ReferenceSegment {
  id: string;
  game_id: string;
  level_number: number;
  start_type: string;
  start_ordinal: number;
  end_type: string;
  end_ordinal: number;
  description: string;
  active: number;
  ordinal: number | null;
  reference_id: string | null;
  state_path: string | null;
}

export interface Reference {
  id: string;
  game_id: string;
  name: string;
  created_at: string;
  active: number;
  draft: number;
  has_spinrec: boolean;
}

/** Any object with segment-naming fields (used by segmentName/shortSegName). */
export interface SegmentLike {
  description?: string;
  level_number: number;
  start_type: string;
  start_ordinal: number;
  end_type: string;
  end_ordinal: number;
}
```

- [ ] **Step 2: Verify types compile**

```bash
cd frontend && npx tsc --noEmit
```
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/types.ts
git commit -m "feat(frontend): add TypeScript interfaces for all API response shapes"
```

---

### Task 3: Migrate `format.ts` with tests

**Files:**
- Create: `frontend/src/format.ts`
- Create: `frontend/src/format.test.ts`
- Delete: `python/spinlab/static/format.js` (deferred to Task 9)

This is the easiest module — pure functions, zero DOM.

- [ ] **Step 1: Write tests in `frontend/src/format.test.ts`**

```ts
import { describe, it, expect } from "vitest";
import { segmentName, formatTime, elapsedStr } from "./format";
import type { SegmentLike } from "./types";

describe("segmentName", () => {
  it("returns description when present", () => {
    const seg: SegmentLike = {
      description: "Iggy's Castle",
      level_number: 3,
      start_type: "entrance",
      start_ordinal: 0,
      end_type: "goal",
      end_ordinal: 0,
    };
    expect(segmentName(seg)).toBe("Iggy's Castle");
  });

  it("builds name from entrance to goal", () => {
    const seg: SegmentLike = {
      level_number: 5,
      start_type: "entrance",
      start_ordinal: 0,
      end_type: "goal",
      end_ordinal: 0,
    };
    expect(segmentName(seg)).toBe("L5 start \u2192 goal");
  });

  it("builds name from checkpoint to checkpoint", () => {
    const seg: SegmentLike = {
      level_number: 2,
      start_type: "checkpoint",
      start_ordinal: 1,
      end_type: "checkpoint",
      end_ordinal: 2,
    };
    expect(segmentName(seg)).toBe("L2 cp1 \u2192 cp2");
  });

  it("handles empty string description as falsy", () => {
    const seg: SegmentLike = {
      description: "",
      level_number: 1,
      start_type: "entrance",
      start_ordinal: 0,
      end_type: "goal",
      end_ordinal: 0,
    };
    expect(segmentName(seg)).toBe("L1 start \u2192 goal");
  });
});

describe("formatTime", () => {
  it("returns em dash for null", () => {
    expect(formatTime(null)).toBe("\u2014");
  });

  it("returns em dash for undefined", () => {
    expect(formatTime(undefined)).toBe("\u2014");
  });

  it("formats milliseconds to seconds with one decimal", () => {
    expect(formatTime(12345)).toBe("12.3s");
  });

  it("formats zero", () => {
    expect(formatTime(0)).toBe("0.0s");
  });
});

describe("elapsedStr", () => {
  it("returns empty string for null", () => {
    expect(elapsedStr(null)).toBe("");
  });

  it("returns empty string for empty string", () => {
    expect(elapsedStr("")).toBe("");
  });

  it("returns 0:00 for invalid date", () => {
    expect(elapsedStr("not-a-date")).toBe("0:00");
  });

  it("formats elapsed time as m:ss", () => {
    const twoMinutesAgo = new Date(Date.now() - 123_000).toISOString();
    const result = elapsedStr(twoMinutesAgo);
    // Allow 1s tolerance for test execution time
    expect(result).toMatch(/^2:0[2-4]$/);
  });

  it("pads seconds with leading zero", () => {
    const fiveSecondsAgo = new Date(Date.now() - 5_000).toISOString();
    const result = elapsedStr(fiveSecondsAgo);
    expect(result).toMatch(/^0:0[4-6]$/);
  });
});
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd frontend && npx vitest run src/format.test.ts
```
Expected: FAIL — `./format` module not found.

- [ ] **Step 3: Create `frontend/src/format.ts`**

```ts
import type { SegmentLike } from "./types";

export function segmentName(s: SegmentLike): string {
  if (s.description) return s.description;
  const start = s.start_type === "entrance" ? "start" : "cp" + s.start_ordinal;
  const end = s.end_type === "goal" ? "goal" : "cp" + s.end_ordinal;
  return "L" + s.level_number + " " + start + " \u2192 " + end;
}

export function formatTime(ms: number | null | undefined): string {
  if (ms == null) return "\u2014";
  const s = ms / 1000;
  return s.toFixed(1) + "s";
}

export function elapsedStr(startedAt: string | null | undefined): string {
  if (!startedAt) return "";
  const start = new Date(startedAt);
  if (!Number.isFinite(start.getTime())) return "0:00";
  const diff = Math.floor((Date.now() - start.getTime()) / 1000);
  const m = Math.floor(diff / 60);
  const s = diff % 60;
  return m + ":" + String(s).padStart(2, "0");
}
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
cd frontend && npx vitest run src/format.test.ts
```
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/format.ts frontend/src/format.test.ts
git commit -m "feat(frontend): migrate format.ts with unit tests"
```

---

### Task 4: Migrate `api.ts` with tests

**Files:**
- Create: `frontend/src/api.ts`
- Create: `frontend/src/api.test.ts`

The API module gets typed function signatures and testable error handling. Toast creation stays as a DOM side-effect but the fetch/error logic becomes testable.

- [ ] **Step 1: Write tests in `frontend/src/api.test.ts`**

```ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fetchJSON, postJSON } from "./api";

// Mock global fetch
const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

beforeEach(() => {
  mockFetch.mockReset();
  document.body.innerHTML = "";
});

describe("fetchJSON", () => {
  it("returns parsed JSON on success", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ mode: "idle" }),
    });
    const result = await fetchJSON("/api/state");
    expect(result).toEqual({ mode: "idle" });
    expect(mockFetch).toHaveBeenCalledWith("/api/state", {});
  });

  it("returns null and shows toast on HTTP error", async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      statusText: "Not Found",
      json: () => Promise.reject(new Error("no json")),
    });
    const result = await fetchJSON("/api/missing");
    expect(result).toBeNull();
    const toast = document.getElementById("toast");
    expect(toast?.textContent).toContain("Not Found");
  });

  it("prefers detail from JSON error body", async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      statusText: "Conflict",
      json: () => Promise.resolve({ detail: "draft_pending" }),
    });
    const result = await fetchJSON("/api/practice/start");
    expect(result).toBeNull();
    const toast = document.getElementById("toast");
    expect(toast?.textContent).toContain("draft_pending");
  });

  it("returns null and shows toast on network error", async () => {
    mockFetch.mockRejectedValue(new Error("Failed to fetch"));
    const result = await fetchJSON("/api/state");
    expect(result).toBeNull();
    const toast = document.getElementById("toast");
    expect(toast?.textContent).toContain("Failed to fetch");
  });
});

describe("postJSON", () => {
  it("sends POST with JSON body", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ status: "ok" }),
    });
    const result = await postJSON("/api/practice/start", { foo: "bar" });
    expect(result).toEqual({ status: "ok" });
    expect(mockFetch).toHaveBeenCalledWith("/api/practice/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: '{"foo":"bar"}',
    });
  });

  it("sends POST without body when body is null", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ status: "ok" }),
    });
    await postJSON("/api/reference/stop");
    expect(mockFetch).toHaveBeenCalledWith("/api/reference/stop", {
      method: "POST",
    });
  });
});
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd frontend && npx vitest run src/api.test.ts
```
Expected: FAIL — `./api` module not found.

- [ ] **Step 3: Create `frontend/src/api.ts`**

```ts
import type { AppState } from "./types";

const TOAST_TIMEOUT_MS = 8000;
const FALLBACK_POLL_MS = 5000;

let toastTimer: ReturnType<typeof setTimeout> | null = null;

function showToast(msg: string): void {
  let el = document.getElementById("toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add("visible");
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el!.classList.remove("visible"), TOAST_TIMEOUT_MS);
}

export async function fetchJSON<T = unknown>(
  url: string,
  opts: RequestInit = {},
): Promise<T | null> {
  try {
    const res = await fetch(url, opts);
    if (!res.ok) {
      let detail = res.statusText;
      try {
        detail = (await res.json()).detail || detail;
      } catch (_) {
        // no JSON body
      }
      showToast(url + ": " + detail);
      return null;
    }
    return (await res.json()) as T;
  } catch (e) {
    showToast("Request failed: " + ((e as Error).message || url));
    return null;
  }
}

export async function postJSON<T = unknown>(
  url: string,
  body: unknown = null,
): Promise<T | null> {
  const opts: RequestInit = { method: "POST" };
  if (body !== null) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  return fetchJSON<T>(url, opts);
}

export function connectSSE(onMessage: (data: AppState) => void): EventSource {
  const es = new EventSource("/api/events");
  es.onmessage = (e) => {
    try {
      const data: AppState = JSON.parse(e.data);
      onMessage(data);
    } catch (_) {
      // malformed SSE payload
    }
  };
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) {
      startFallbackPoll(onMessage);
    }
  };
  return es;
}

let fallbackInterval: ReturnType<typeof setInterval> | null = null;

function startFallbackPoll(onMessage: (data: AppState) => void): void {
  if (fallbackInterval) return;
  fallbackInterval = setInterval(async () => {
    const data = await fetchJSON<AppState>("/api/state");
    if (data) onMessage(data);
  }, FALLBACK_POLL_MS);
}
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
cd frontend && npx vitest run src/api.test.ts
```
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api.ts frontend/src/api.test.ts
git commit -m "feat(frontend): migrate api.ts with fetch/toast tests"
```

---

### Task 5: Migrate `header.ts`

**Files:**
- Create: `frontend/src/header.ts`

No tests for this module — it's almost entirely DOM binding. The one piece of pure logic (`shortSegName`) is identical to `segmentName` in format.ts, so we refactor it to reuse that.

- [ ] **Step 1: Create `frontend/src/header.ts`**

```ts
import { fetchJSON, postJSON } from "./api";
import { segmentName } from "./format";
import type { AppState } from "./types";

let allRoms: string[] = [];
let popoverOpen = false;

export async function loadRomList(): Promise<void> {
  const data = await fetchJSON<{ roms: string[] }>("/api/roms");
  if (data?.roms) allRoms = data.roms;
}

export function updateHeader(data: AppState): void {
  const gameEl = document.getElementById("game-name")!;
  gameEl.textContent =
    data.tcp_connected && data.game_name ? data.game_name : "No game";

  // Persist for next page load
  if (data.game_name) localStorage.setItem("spinlab_game_name", data.game_name);

  const chip = document.getElementById("mode-chip")!;
  const label = document.getElementById("mode-label")!;
  const stopBtn = document.getElementById("mode-stop") as HTMLElement;

  chip.className = "mode-chip";
  stopBtn.style.display = "none";

  if (!data.tcp_connected) {
    chip.classList.add("disconnected");
    label.textContent = "Disconnected";
  } else if (data.draft) {
    chip.classList.add("draft");
    label.textContent = "Draft \u2014 " + data.draft.segments_captured + " segments";
  } else if (data.mode === "reference") {
    chip.classList.add("recording");
    label.textContent = "Recording \u2014 " + (data.sections_captured || 0) + " segments";
    stopBtn.style.display = "";
  } else if (data.mode === "practice") {
    chip.classList.add("practicing");
    const seg = data.current_segment;
    label.textContent = "Practicing" + (seg ? " \u2014 " + segmentName(seg) : "");
    stopBtn.style.display = "";
  } else if (data.mode === "replay") {
    chip.classList.add("replaying");
    label.textContent = "Replaying\u2026";
    stopBtn.style.display = "";
  } else {
    chip.classList.add("idle");
    label.textContent = "Idle";
  }
}

export function initHeader(): void {
  const selectorBtn = document.getElementById("game-selector")!;
  const popover = document.getElementById("game-popover") as HTMLElement;
  const filter = document.getElementById("rom-filter") as HTMLInputElement;

  const lastGame = localStorage.getItem("spinlab_game_name");
  if (lastGame) document.getElementById("game-name")!.textContent = lastGame;

  selectorBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    popoverOpen = !popoverOpen;
    popover.style.display = popoverOpen ? "" : "none";
    if (popoverOpen) {
      filter.value = "";
      renderRoms("");
      filter.focus();
      if (!allRoms.length) loadRomList().then(() => renderRoms(""));
    }
  });

  filter.addEventListener("input", (e) =>
    renderRoms((e.target as HTMLInputElement).value),
  );

  document.addEventListener("click", (e) => {
    if (
      popoverOpen &&
      !popover.contains(e.target as Node) &&
      e.target !== selectorBtn
    ) {
      popoverOpen = false;
      popover.style.display = "none";
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && popoverOpen) {
      popoverOpen = false;
      popover.style.display = "none";
    }
  });

  document.getElementById("mode-stop")!.addEventListener("click", async () => {
    const chip = document.getElementById("mode-chip")!;
    if (chip.classList.contains("recording"))
      await postJSON("/api/reference/stop");
    else if (chip.classList.contains("practicing"))
      await postJSON("/api/practice/stop");
    else if (chip.classList.contains("replaying"))
      await postJSON("/api/replay/stop");
  });
}

function renderRoms(filterText: string): void {
  const ul = document.getElementById("rom-list")!;
  ul.innerHTML = "";
  const lf = filterText.toLowerCase();
  const matches = allRoms.filter((r) => r.toLowerCase().includes(lf));
  matches.forEach((rom) => {
    const li = document.createElement("li");
    li.textContent = rom.replace(/\.(sfc|smc|fig|swc)$/i, "");
    li.addEventListener("click", async () => {
      const res = await postJSON<{ status?: string; message?: string }>(
        "/api/emulator/launch",
        { rom },
      );
      if (res?.status === "error") {
        alert(res.message);
        return;
      }
      popoverOpen = false;
      (document.getElementById("game-popover") as HTMLElement).style.display =
        "none";
    });
    ul.appendChild(li);
  });
}
```

**Refactor note:** The original `header.js` had a `shortSegName()` function that was a near-duplicate of `format.js`'s `segmentName()`. This version reuses `segmentName` from `format.ts`, eliminating the duplication.

- [ ] **Step 2: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/header.ts
git commit -m "feat(frontend): migrate header.ts, deduplicate shortSegName"
```

---

### Task 6: Migrate `model.ts` — extract logic, fix bugs

**Files:**
- Create: `frontend/src/model-logic.ts`
- Create: `frontend/src/model-logic.test.ts`
- Create: `frontend/src/model.ts`

This is the most valuable migration. The current JS has a bug: it accesses `model_outputs[selected_model].expected_time_ms` but the API returns `model_outputs[selected_model].total.expected_ms`. TypeScript catches this at compile time. We extract the data-to-display-value logic into a testable pure module.

- [ ] **Step 1: Create `frontend/src/model-logic.ts`** — pure data extraction

```ts
import type { ModelSegment, Estimate, CurrentSegment, AppState } from "./types";

/** Extract the selected estimate for a model segment (total time series). */
export function selectedEstimate(seg: ModelSegment): Estimate | null {
  const output = seg.model_outputs[seg.selected_model];
  return output?.total ?? null;
}

/** Extract the selected estimate from the current practice segment. */
export function currentEstimate(seg: CurrentSegment): Estimate | null {
  const output = seg.model_outputs[seg.selected_model];
  return output?.total ?? null;
}

/** Format ms_per_attempt for display, or return null if unavailable. */
export function formatTrend(est: Estimate | null): string | null {
  if (!est || est.ms_per_attempt == null) return null;
  return est.ms_per_attempt.toFixed(1) + " ms/att";
}

/** Determine whether practice controls should allow starting. */
export function canStartPractice(state: AppState): boolean {
  return state.tcp_connected && state.game_id !== null && state.mode === "idle";
}
```

- [ ] **Step 2: Write tests in `frontend/src/model-logic.test.ts`**

```ts
import { describe, it, expect } from "vitest";
import { selectedEstimate, currentEstimate, formatTrend, canStartPractice } from "./model-logic";
import type { ModelSegment, CurrentSegment, Estimate, AppState } from "./types";

const ESTIMATE: Estimate = {
  expected_ms: 5000,
  ms_per_attempt: -12.3,
  floor_ms: 3000,
};

const MODEL_OUTPUT = { total: ESTIMATE, clean: { expected_ms: null, ms_per_attempt: null, floor_ms: null } };

describe("selectedEstimate", () => {
  it("returns total estimate for selected model", () => {
    const seg: ModelSegment = {
      segment_id: "s1",
      description: "test",
      level_number: 1,
      start_type: "entrance",
      start_ordinal: 0,
      end_type: "goal",
      end_ordinal: 0,
      selected_model: "kalman",
      model_outputs: { kalman: MODEL_OUTPUT },
      n_completed: 5,
      n_attempts: 10,
      gold_ms: 2000,
      clean_gold_ms: null,
    };
    expect(selectedEstimate(seg)).toEqual(ESTIMATE);
  });

  it("returns null when selected model has no output", () => {
    const seg: ModelSegment = {
      segment_id: "s1",
      description: "test",
      level_number: 1,
      start_type: "entrance",
      start_ordinal: 0,
      end_type: "goal",
      end_ordinal: 0,
      selected_model: "kalman",
      model_outputs: {},
      n_completed: 0,
      n_attempts: 0,
      gold_ms: null,
      clean_gold_ms: null,
    };
    expect(selectedEstimate(seg)).toBeNull();
  });
});

describe("formatTrend", () => {
  it("formats negative trend", () => {
    expect(formatTrend(ESTIMATE)).toBe("-12.3 ms/att");
  });

  it("returns null for null estimate", () => {
    expect(formatTrend(null)).toBeNull();
  });

  it("returns null when ms_per_attempt is null", () => {
    expect(formatTrend({ expected_ms: 1000, ms_per_attempt: null, floor_ms: null })).toBeNull();
  });
});

describe("canStartPractice", () => {
  const BASE_STATE: AppState = {
    mode: "idle",
    tcp_connected: true,
    game_id: "game1",
    game_name: "Test Game",
    current_segment: null,
    recent: [],
    session: null,
    sections_captured: 0,
    allocator_weights: null,
    estimator: null,
    capture_run_id: null,
    draft: null,
    cold_fill: null,
  };

  it("returns true when idle, connected, and game loaded", () => {
    expect(canStartPractice(BASE_STATE)).toBe(true);
  });

  it("returns false when not connected", () => {
    expect(canStartPractice({ ...BASE_STATE, tcp_connected: false })).toBe(false);
  });

  it("returns false when no game loaded", () => {
    expect(canStartPractice({ ...BASE_STATE, game_id: null })).toBe(false);
  });

  it("returns false when already practicing", () => {
    expect(canStartPractice({ ...BASE_STATE, mode: "practice" })).toBe(false);
  });
});
```

- [ ] **Step 3: Run tests — verify they fail**

```bash
cd frontend && npx vitest run src/model-logic.test.ts
```
Expected: FAIL — module not found.

- [ ] **Step 4: Run tests — verify they pass**

```bash
cd frontend && npx vitest run src/model-logic.test.ts
```
Expected: all tests PASS.

- [ ] **Step 5: Create `frontend/src/model.ts`** — DOM bindings using extracted logic

```ts
import { segmentName, formatTime, elapsedStr } from "./format";
import { fetchJSON, postJSON } from "./api";
import { selectedEstimate, currentEstimate, formatTrend, canStartPractice } from "./model-logic";
import type { AppState, ModelData, TuningData } from "./types";

const ALLOCATOR_COLORS: Record<string, string> = {
  greedy: "#4caf50",
  random: "#2196f3",
  round_robin: "#ff9800",
};
const ALLOCATOR_LABELS: Record<string, string> = {
  greedy: "Greedy",
  random: "Random",
  round_robin: "Round Robin",
};
const ALLOCATOR_ORDER = ["greedy", "random", "round_robin"];

let _currentWeights: Record<string, number> | null = null;
let _tuningParams: TuningData | null = null;

function renderWeightSlider(weights: Record<string, number>): void {
  _currentWeights = { ...weights };
  const slider = document.getElementById("weight-slider");
  const legend = document.getElementById("weight-legend");
  if (!slider || !legend) return;

  slider.innerHTML = "";
  legend.innerHTML = "";

  const entries = ALLOCATOR_ORDER.filter((k) => k in weights);

  entries.forEach((name) => {
    const seg = document.createElement("div");
    seg.className = "weight-segment";
    seg.style.flex = String(weights[name]);
    seg.style.background = ALLOCATOR_COLORS[name] ?? "#666";
    seg.dataset.allocator = name;
    slider.appendChild(seg);
  });

  const totalWidth = () => slider.getBoundingClientRect().width;
  for (let i = 0; i < entries.length - 1; i++) {
    const handle = document.createElement("div");
    handle.className = "weight-handle";
    handle.dataset.index = String(i);
    positionHandle(handle, entries, weights);
    slider.appendChild(handle);

    handle.addEventListener("mousedown", (e: MouseEvent) => {
      e.preventDefault();
      handle.classList.add("dragging");
      const left = entries[i]!;
      const right = entries[i + 1]!;
      const startX = e.clientX;
      const startLeftW = weights[left]!;
      const startRightW = weights[right]!;
      const pxPerPercent = totalWidth() / 100;

      const onMove = (ev: MouseEvent) => {
        const dx = ev.clientX - startX;
        const dp = Math.round(dx / pxPerPercent);
        const newLeft = Math.max(0, Math.min(startLeftW + startRightW, startLeftW + dp));
        const newRight = startLeftW + startRightW - newLeft;
        weights[left] = newLeft;
        weights[right] = newRight;
        updateSliderVisuals(entries, weights, slider, legend);
      };
      const onUp = () => {
        handle.classList.remove("dragging");
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        _currentWeights = { ...weights };
        postJSON("/api/allocator-weights", weights);
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  }

  renderLegend(entries, weights, legend);
}

function positionHandle(
  handle: HTMLElement,
  entries: string[],
  weights: Record<string, number>,
): void {
  let cumulative = 0;
  const idx = parseInt(handle.dataset.index!);
  for (let i = 0; i <= idx; i++) cumulative += weights[entries[i]!]!;
  handle.style.left = cumulative + "%";
}

function updateSliderVisuals(
  entries: string[],
  weights: Record<string, number>,
  slider: HTMLElement,
  legend: HTMLElement,
): void {
  const segments = slider.querySelectorAll(".weight-segment") as NodeListOf<HTMLElement>;
  entries.forEach((name, i) => {
    if (segments[i]) segments[i].style.flex = String(weights[name]);
  });
  const handles = slider.querySelectorAll(".weight-handle") as NodeListOf<HTMLElement>;
  handles.forEach((h) => positionHandle(h, entries, weights));
  renderLegend(entries, weights, legend);
}

function renderLegend(
  entries: string[],
  weights: Record<string, number>,
  legend: HTMLElement,
): void {
  legend.innerHTML = "";
  entries.forEach((name) => {
    const item = document.createElement("span");
    item.className = "weight-legend-item";
    const dot = document.createElement("span");
    dot.className = "weight-dot";
    dot.style.background = ALLOCATOR_COLORS[name] ?? "#666";
    item.appendChild(dot);
    item.appendChild(
      document.createTextNode((ALLOCATOR_LABELS[name] ?? name) + " " + weights[name] + "%"),
    );
    legend.appendChild(item);
  });
}

export async function fetchModel(): Promise<void> {
  const data = await fetchJSON<ModelData>("/api/model");
  if (data) updateModel(data);
}

function updateModel(data: ModelData): void {
  const body = document.getElementById("model-body")!;
  if (!data.segments || !data.segments.length) {
    body.innerHTML = '<tr><td colspan="6" class="dim">No game loaded</td></tr>';
    return;
  }
  body.innerHTML = "";
  data.segments.forEach((s) => {
    const tr = document.createElement("tr");
    const est = selectedEstimate(s);

    tr.innerHTML =
      "<td>" + segmentName(s) + "</td>" +
      "<td>" + formatTime(est?.expected_ms ?? null) + "</td>" +
      "<td>" + (formatTrend(est) ?? "\u2014") + "</td>" +
      "<td>" + formatTime(est?.floor_ms ?? null) + "</td>" +
      "<td>" + s.n_completed + "</td>" +
      "<td>" + formatTime(s.gold_ms) + "</td>";
    body.appendChild(tr);
  });

  const estSelect = document.getElementById("estimator-select") as HTMLSelectElement | null;
  if (estSelect && data.estimators) {
    const current = data.estimator || estSelect.value;
    estSelect.innerHTML = "";
    data.estimators.forEach((e) => {
      const opt = document.createElement("option");
      opt.value = e.name;
      opt.textContent = e.display_name;
      estSelect.appendChild(opt);
    });
    estSelect.value = current;
  }
}

export function updatePracticeCard(data: AppState): void {
  const card = document.getElementById("practice-card") as HTMLElement;
  if (data.mode !== "practice" || !data.current_segment) {
    card.style.display = "none";
    return;
  }
  card.style.display = "";

  const cs = data.current_segment;
  document.getElementById("current-goal")!.textContent = segmentName(cs);
  document.getElementById("current-attempts")!.textContent =
    "Attempt " + (cs.attempt_count || 0);

  const insight = document.getElementById("insight")!;
  const est = currentEstimate(cs);
  const trend = formatTrend(est);
  if (trend) {
    insight.innerHTML = "<span>" + trend + "</span>";
  } else {
    insight.textContent = "No data yet";
  }

  const recent = document.getElementById("recent")!;
  recent.innerHTML = "";
  (data.recent || []).forEach((r) => {
    const li = document.createElement("li");
    const time = formatTime(r.time_ms);
    const cls = r.completed ? "ahead" : "behind";
    li.innerHTML =
      '<span class="' + cls + '">' + time + "</span>" +
      ' <span class="dim">' + segmentName(r) + "</span>";
    recent.appendChild(li);
  });

  const stats = document.getElementById("session-stats");
  if (stats && data.session) {
    stats.textContent =
      (data.session.segments_completed || 0) +
      "/" +
      (data.session.segments_attempted || 0) +
      " cleared | " +
      elapsedStr(data.session.started_at);
  }

  if (data.allocator_weights) {
    renderWeightSlider(data.allocator_weights);
  }
}

export function updatePracticeControls(data: AppState): void {
  const startBtn = document.getElementById("btn-practice-start") as HTMLButtonElement;
  const stopBtn = document.getElementById("btn-practice-stop") as HTMLElement;
  const isPracticing = data.mode === "practice";
  startBtn.style.display = isPracticing ? "none" : "";
  startBtn.disabled = !canStartPractice(data);
  stopBtn.style.display = isPracticing ? "" : "none";
}

async function fetchTuningParams(): Promise<void> {
  const data = await fetchJSON<TuningData>("/api/estimator-params");
  if (!data) return;
  _tuningParams = data;
  renderTuningParams(data);
}

function renderTuningParams(data: TuningData): void {
  const container = document.getElementById("tuning-params");
  if (!container) return;
  container.innerHTML = "";
  if (!data.params || data.params.length === 0) {
    container.innerHTML = '<p class="tuning-empty">No tunable parameters</p>';
    return;
  }
  data.params.forEach((p) => {
    const row = document.createElement("div");
    row.className = "tuning-row";
    row.innerHTML =
      '<span class="tuning-label">' + p.display_name + "</span>" +
      '<input type="range" class="tuning-slider" ' +
      'data-param="' + p.name + '" ' +
      'min="' + p.min + '" max="' + p.max + '" step="' + p.step + '" ' +
      'value="' + p.value + '">' +
      '<input type="number" class="tuning-value" ' +
      'data-param="' + p.name + '" ' +
      'min="' + p.min + '" max="' + p.max + '" step="' + p.step + '" ' +
      'value="' + p.value + '">';
    container.appendChild(row);

    const slider = row.querySelector(".tuning-slider") as HTMLInputElement;
    const input = row.querySelector(".tuning-value") as HTMLInputElement;
    slider.addEventListener("input", () => {
      input.value = slider.value;
    });
    input.addEventListener("input", () => {
      slider.value = input.value;
    });
  });
}

function collectTuningParams(): Record<string, number> {
  const params: Record<string, number> = {};
  document.querySelectorAll<HTMLInputElement>("#tuning-params .tuning-slider").forEach((slider) => {
    params[slider.dataset.param!] = parseFloat(slider.value);
  });
  return params;
}

async function applyTuningParams(): Promise<void> {
  const params = collectTuningParams();
  await postJSON("/api/estimator-params", { params });
  fetchModel();
}

async function resetTuningDefaults(): Promise<void> {
  if (!_tuningParams) return;
  _tuningParams.params.forEach((p) => {
    const slider = document.querySelector<HTMLInputElement>(
      '.tuning-slider[data-param="' + p.name + '"]',
    );
    const input = document.querySelector<HTMLInputElement>(
      '.tuning-value[data-param="' + p.name + '"]',
    );
    if (slider) slider.value = String(p.default);
    if (input) input.value = String(p.default);
  });
  await applyTuningParams();
}

export function initModelTab(): void {
  document.getElementById("estimator-select")!.addEventListener("change", async (e) => {
    await postJSON("/api/estimator", { name: (e.target as HTMLSelectElement).value });
    fetchModel();
    fetchTuningParams();
  });
  document.getElementById("btn-practice-start")!.addEventListener("click", () =>
    postJSON("/api/practice/start"),
  );
  document.getElementById("btn-practice-stop")!.addEventListener("click", () =>
    postJSON("/api/practice/stop"),
  );

  const toggle = document.getElementById("tuning-toggle");
  const panel = document.getElementById("tuning-panel");
  const body = document.getElementById("tuning-body") as HTMLElement | null;
  if (toggle && panel && body) {
    toggle.addEventListener("click", () => {
      panel.classList.toggle("collapsed");
      body.style.display = panel.classList.contains("collapsed") ? "none" : "";
    });
  }
  document.getElementById("btn-tuning-apply")?.addEventListener("click", applyTuningParams);
  document.getElementById("btn-tuning-reset")?.addEventListener("click", resetTuningDefaults);

  fetchTuningParams();
}
```

- [ ] **Step 6: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/model-logic.ts frontend/src/model-logic.test.ts frontend/src/model.ts
git commit -m "feat(frontend): migrate model.ts, fix model_output field access bug

The JS was accessing model_outputs[selected_model].expected_time_ms
but the API returns model_outputs[selected_model].total.expected_ms.
TypeScript now enforces the correct nested structure via the Estimate type."
```

---

### Task 7: Migrate `manage.ts`

**Files:**
- Create: `frontend/src/manage.ts`

Mostly DOM bindings. The reference/segment rendering logic is straightforward string interpolation.

- [ ] **Step 1: Create `frontend/src/manage.ts`**

```ts
import { segmentName, formatTime } from "./format";
import { fetchJSON, postJSON } from "./api";
import type { AppState, Reference, ReferenceSegment } from "./types";

let lastState: AppState | null = null;

export async function fetchManage(): Promise<void> {
  const refsData = await fetchJSON<{ references: Reference[] }>("/api/references");
  if (!refsData) return;
  const refs = refsData.references || [];

  let segments: ReferenceSegment[] = [];
  const captureId = lastState?.capture_run_id;
  if (captureId) {
    const segData = await fetchJSON<{ segments: ReferenceSegment[] }>(
      "/api/references/" + captureId + "/segments",
    );
    segments = segData?.segments || [];
  } else {
    const active = refs.find((r) => r.active);
    if (active) {
      const segData = await fetchJSON<{ segments: ReferenceSegment[] }>(
        "/api/references/" + active.id + "/segments",
      );
      segments = segData?.segments || [];
    }
  }
  updateManage(refs, segments);
}

function updateManage(refs: Reference[], segments: ReferenceSegment[]): void {
  const sel = document.getElementById("ref-select") as HTMLSelectElement;
  const btnStart = document.getElementById("btn-ref-start") as HTMLButtonElement;
  const btnReplay = document.getElementById("btn-replay") as HTMLButtonElement;
  const draftPrompt = document.getElementById("draft-prompt") as HTMLElement;

  const busy =
    lastState != null &&
    (lastState.mode === "reference" || lastState.mode === "replay");
  const hasDraft = lastState?.draft != null;

  sel.disabled = busy || hasDraft;
  btnStart.disabled = busy || hasDraft || !lastState?.tcp_connected;
  (document.getElementById("btn-ref-rename") as HTMLButtonElement).disabled =
    busy || hasDraft;
  (document.getElementById("btn-ref-delete") as HTMLButtonElement).disabled =
    busy || hasDraft;

  if (hasDraft && lastState?.draft) {
    draftPrompt.style.display = "";
    document.getElementById("draft-summary")!.textContent =
      "\u2713 Captured " + lastState.draft.segments_captured + " segments";
  } else {
    draftPrompt.style.display = "none";
  }

  sel.innerHTML = "";
  if (!refs.length) {
    const opt = document.createElement("option");
    opt.textContent = "No references";
    opt.disabled = true;
    sel.appendChild(opt);
    document.getElementById("segment-body")!.innerHTML = "";
    btnReplay.disabled = true;
    return;
  }
  refs.forEach((r) => {
    const opt = document.createElement("option");
    opt.value = r.id;
    opt.textContent = r.name + (r.active ? " \u25cf" : "");
    if (r.active) opt.selected = true;
    sel.appendChild(opt);
  });

  const selectedRef = refs.find((r) => r.id === sel.value);
  btnReplay.disabled =
    busy || hasDraft || !selectedRef?.has_spinrec || !lastState?.tcp_connected;

  const cfBanner = document.getElementById("cold-fill-banner") as HTMLElement | null;
  if (cfBanner) {
    if (lastState?.mode === "cold_fill" && lastState?.cold_fill) {
      const cf = lastState.cold_fill;
      cfBanner.innerHTML =
        '<div class="cold-fill-status">' +
        "<strong>Capturing cold starts</strong> \u2014 " +
        "Die to continue (" + cf.current + "/" + cf.total + ")" +
        (cf.segment_label ? " \u2014 " + cf.segment_label : "") +
        "</div>";
      cfBanner.style.display = "block";
    } else {
      cfBanner.style.display = "none";
    }
  }

  const body = document.getElementById("segment-body")!;
  body.innerHTML = "";
  segments.forEach((s) => {
    const tr = document.createElement("tr");
    const hasState = s.state_path != null;
    const stateCell = hasState
      ? '<span class="state-ok">\u2705</span>'
      : '<button class="btn-fill-gap" data-id="' + s.id + '">\u274c</button>';
    tr.innerHTML =
      '<td><input class="segment-name-input" value="' +
      (s.description || "") +
      '" ' +
      'placeholder="' + segmentName(s) + '" ' +
      'data-id="' + s.id + '" data-field="description"></td>' +
      "<td>" + s.level_number + "</td>" +
      "<td>" +
      (s.start_type === "entrance" ? "start" : "cp" + s.start_ordinal) +
      " \u2192 " +
      (s.end_type === "goal" ? "goal" : "cp" + s.end_ordinal) +
      "</td>" +
      "<td>" + stateCell + "</td>" +
      '<td><button class="btn-x" data-id="' + s.id + '">\u2715</button></td>';
    body.appendChild(tr);
  });
}

export function updateManageState(data: AppState): void {
  lastState = data;
}

export function initManageTab(): void {
  document.getElementById("segment-body")!.addEventListener("focusout", async (e) => {
    const target = e.target as HTMLElement;
    if (!target.classList.contains("segment-name-input")) return;
    const input = target as HTMLInputElement;
    const id = input.dataset.id;
    const field = input.dataset.field;
    const value = input.value;
    await fetchJSON("/api/segments/" + id, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [field!]: value }),
    });
  });

  document.getElementById("segment-body")!.addEventListener("click", async (e) => {
    const target = e.target as HTMLElement;
    if (target.classList.contains("btn-fill-gap")) {
      const id = target.dataset.id;
      const data = await postJSON<{ status?: string }>("/api/segments/" + id + "/fill-gap");
      if (data?.status === "started") {
        target.textContent = "\u23f3";
        (target as HTMLButtonElement).disabled = true;
      }
      return;
    }
    if (!target.classList.contains("btn-x")) return;
    if (!confirm("Remove this segment?")) return;
    await fetchJSON("/api/segments/" + target.dataset.id, { method: "DELETE" });
    fetchManage();
  });

  document
    .getElementById("ref-select")!
    .addEventListener("change", async (e) => {
      await postJSON(
        "/api/references/" + (e.target as HTMLSelectElement).value + "/activate",
      );
      fetchManage();
    });

  document.getElementById("btn-ref-rename")!.addEventListener("click", async () => {
    const sel = document.getElementById("ref-select") as HTMLSelectElement;
    const name = prompt(
      "New name:",
      sel.options[sel.selectedIndex]?.text.replace(" \u25cf", ""),
    );
    if (!name) return;
    await fetchJSON("/api/references/" + sel.value, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    fetchManage();
  });

  document.getElementById("btn-ref-delete")!.addEventListener("click", async () => {
    if (!confirm("Delete this reference and all its segments?")) return;
    const sel = document.getElementById("ref-select") as HTMLSelectElement;
    await fetchJSON("/api/references/" + sel.value, { method: "DELETE" });
    fetchManage();
  });

  document.getElementById("btn-ref-start")!.addEventListener("click", () =>
    postJSON("/api/reference/start"),
  );

  document.getElementById("btn-replay")!.addEventListener("click", async () => {
    const sel = document.getElementById("ref-select") as HTMLSelectElement;
    await postJSON("/api/replay/start", { ref_id: sel.value });
  });

  document.getElementById("btn-draft-save")!.addEventListener("click", async () => {
    const input = document.getElementById("draft-name") as HTMLInputElement;
    const name = input.value.trim();
    if (!name) {
      input.focus();
      return;
    }
    await postJSON("/api/references/draft/save", { name });
    input.value = "";
    fetchManage();
  });

  document.getElementById("btn-draft-discard")!.addEventListener("click", async () => {
    if (!confirm("Discard this capture? This cannot be undone.")) return;
    await postJSON("/api/references/draft/discard");
    fetchManage();
  });

  document.getElementById("btn-reset")!.addEventListener("click", async () => {
    if (!confirm("Clear all session data? This cannot be undone.")) return;
    const data = await postJSON<{ status?: string }>("/api/reset");
    document.getElementById("reset-status")!.textContent =
      data?.status === "ok" ? "Data cleared." : "Error clearing data.";
  });
}
```

- [ ] **Step 2: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/manage.ts
git commit -m "feat(frontend): migrate manage.ts to TypeScript"
```

---

### Task 8: Migrate `app.ts` and `index.html`, wire up build

**Files:**
- Create: `frontend/src/app.ts`
- Create: `frontend/index.html`
- Modify: `python/spinlab/dashboard.py` (static dir handling)

- [ ] **Step 1: Create `frontend/src/app.ts`**

```ts
import { connectSSE, fetchJSON } from "./api";
import { initHeader, updateHeader } from "./header";
import {
  updatePracticeCard,
  updatePracticeControls,
  fetchModel,
  initModelTab,
} from "./model";
import { fetchManage, initManageTab, updateManageState } from "./manage";
import type { AppState } from "./types";

function updateFromState(data: AppState): void {
  updateHeader(data);
  updatePracticeCard(data);
  updatePracticeControls(data);
  updateManageState(data);

  const activeTab = document.querySelector(".tab.active") as HTMLElement | null;
  if (activeTab?.dataset.tab === "model") fetchModel();
  if (
    activeTab?.dataset.tab === "manage" ||
    data.mode === "reference" ||
    data.mode === "replay"
  ) {
    fetchManage();
  }
}

// Tab switching
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document
      .querySelectorAll(".tab-content")
      .forEach((c) => c.classList.remove("active"));
    (btn as HTMLElement).classList.add("active");
    document
      .getElementById("tab-" + (btn as HTMLElement).dataset.tab)
      ?.classList.add("active");
    if ((btn as HTMLElement).dataset.tab === "model") fetchModel();
    if ((btn as HTMLElement).dataset.tab === "manage") fetchManage();
  });
});

// Init
initHeader();
initModelTab();
initManageTab();

// Connect SSE with initial poll
connectSSE(updateFromState);
fetchJSON<AppState>("/api/state").then((data) => {
  if (data) updateFromState(data);
});
```

- [ ] **Step 2: Create `frontend/index.html`**

Copy from current `python/spinlab/static/index.html` but replace the script tag:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SpinLab</title>
  <link rel="stylesheet" href="/style.css">
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#127922;</text></svg>">
</head>
<body>
<div id="app">
  <!-- Same HTML body as current index.html, lines 12-151 -->
  <!-- Copy verbatim from python/spinlab/static/index.html -->
</div>
<script type="module" src="/src/app.ts"></script>
</body>
</html>
```

The full HTML body content should be copied exactly from the existing `index.html` (the `<header>` through `</main>` block). Only two lines change: the stylesheet `href` drops the `/static` prefix and `?v=21` (Vite handles cache-busting), and the `<script>` tag points to the TS source.

- [ ] **Step 3: Copy `style.css` to `frontend/`**

```bash
cp python/spinlab/static/style.css frontend/style.css
```

Vite will include this in the build output.

- [ ] **Step 4: Run the build**

```bash
cd frontend && npm run build
```

Expected: `python/spinlab/static/` now contains built `index.html`, JS bundle, and CSS. The output should be a single JS file and the CSS.

- [ ] **Step 5: Verify FastAPI serves the built output**

Start the server and check `localhost:8000` in a browser. The dashboard should look and behave identically to before.

- [ ] **Step 6: Verify Vite dev server with proxy**

```bash
cd frontend && npm run dev
```

Open `localhost:5173`. With FastAPI running on 8000, the dashboard should work with hot-reload.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/app.ts frontend/index.html frontend/style.css
git commit -m "feat(frontend): migrate app.ts and index.html, complete Vite build pipeline"
```

---

### Task 9: Clean up old JS files and update FastAPI

**Files:**
- Delete: `python/spinlab/static/app.js`
- Delete: `python/spinlab/static/api.js`
- Delete: `python/spinlab/static/format.js`
- Delete: `python/spinlab/static/header.js`
- Delete: `python/spinlab/static/model.js`
- Delete: `python/spinlab/static/manage.js`
- Delete: `python/spinlab/static/index.html`
- Delete: `python/spinlab/static/style.css`
- Modify: `.gitignore`

- [ ] **Step 1: Delete old JS source files**

```bash
git rm python/spinlab/static/app.js python/spinlab/static/api.js python/spinlab/static/format.js python/spinlab/static/header.js python/spinlab/static/model.js python/spinlab/static/manage.js python/spinlab/static/index.html python/spinlab/static/style.css
```

- [ ] **Step 2: Add build output to `.gitignore`**

Append to `.gitignore`:
```
# Vite build output (regenerated by npm run build)
python/spinlab/static/
```

- [ ] **Step 3: Run `npm run build` and verify server works**

```bash
cd frontend && npm run build
```

Start FastAPI, load `localhost:8000`, verify dashboard works.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore: remove old JS files, gitignore Vite build output

The frontend source of truth is now frontend/src/*.ts.
Build with: cd frontend && npm run build"
```

---

### Task 10: Add CLAUDE.md frontend section

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add frontend dev instructions to CLAUDE.md**

Add after the existing Testing section:

```markdown
## Frontend (TypeScript + Vite)

Source lives in `frontend/src/`. Built output goes to `python/spinlab/static/` (git-ignored).

- **Dev server:** `cd frontend && npm run dev` (port 5173, proxies /api to FastAPI on 8000)
- **Build:** `cd frontend && npm run build`
- **Tests:** `cd frontend && npm test`
- **Type check:** `cd frontend && npm run typecheck`

Run `npm run build` after frontend changes before testing with FastAPI directly.
Types in `frontend/src/types.ts` must stay in sync with Python response models.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add frontend dev workflow to CLAUDE.md"
```

---

### Task 11: Write high-value tests that couldn't exist before

**Files:**
- Create: `frontend/src/model-logic.test.ts` (extend existing)
- Create: `frontend/src/api.test.ts` (extend existing)
- Create: `frontend/src/integration.test.ts`

Now that we have types and separated logic, write tests that catch the classes of bugs that have plagued the dashboard.

- [ ] **Step 1: Add API contract tests to `frontend/src/api-contract.test.ts`**

These tests validate that our TypeScript types match what the Python API actually returns, using snapshot fixtures from real API responses.

```ts
import { describe, it, expect } from "vitest";
import type {
  AppState,
  ModelData,
  TuningData,
  Reference,
  ReferenceSegment,
} from "./types";

/**
 * Fixture snapshots captured from real API responses.
 * If these stop compiling, the API contract has drifted.
 */

const IDLE_STATE: AppState = {
  mode: "idle",
  tcp_connected: true,
  game_id: "smw-kaizo",
  game_name: "Kaizo Mario World",
  current_segment: null,
  recent: [],
  session: null,
  sections_captured: 0,
  allocator_weights: { greedy: 60, random: 20, round_robin: 20 },
  estimator: "kalman",
  capture_run_id: null,
  draft: null,
  cold_fill: null,
};

const PRACTICE_STATE: AppState = {
  mode: "practice",
  tcp_connected: true,
  game_id: "smw-kaizo",
  game_name: "Kaizo Mario World",
  current_segment: {
    id: "seg-001",
    game_id: "smw-kaizo",
    level_number: 3,
    start_type: "entrance",
    start_ordinal: 0,
    end_type: "checkpoint",
    end_ordinal: 1,
    description: "Iggy approach",
    attempt_count: 14,
    model_outputs: {
      kalman: {
        total: { expected_ms: 8500, ms_per_attempt: -45.2, floor_ms: 6200 },
        clean: { expected_ms: 7100, ms_per_attempt: -30.1, floor_ms: 5800 },
      },
    },
    selected_model: "kalman",
    state_path: "/data/states/seg-001.state",
  },
  recent: [
    {
      id: 1,
      segment_id: "seg-001",
      completed: 1,
      time_ms: 8200,
      description: "Iggy approach",
      level_number: 3,
      start_type: "entrance",
      start_ordinal: 0,
      end_type: "checkpoint",
      end_ordinal: 1,
    },
  ],
  session: {
    id: "sess-abc",
    started_at: "2026-04-04T10:00:00Z",
    segments_attempted: 14,
    segments_completed: 3,
  },
  sections_captured: 0,
  allocator_weights: { greedy: 60, random: 20, round_robin: 20 },
  estimator: "kalman",
  capture_run_id: null,
  draft: null,
  cold_fill: null,
};

const MODEL_RESPONSE: ModelData = {
  estimator: "kalman",
  estimators: [
    { name: "kalman", display_name: "Kalman Filter" },
    { name: "rolling_mean", display_name: "Rolling Mean" },
  ],
  allocator_weights: { greedy: 60, random: 20, round_robin: 20 },
  segments: [
    {
      segment_id: "seg-001",
      description: "Iggy approach",
      level_number: 3,
      start_type: "entrance",
      start_ordinal: 0,
      end_type: "checkpoint",
      end_ordinal: 1,
      selected_model: "kalman",
      model_outputs: {
        kalman: {
          total: { expected_ms: 8500, ms_per_attempt: -45.2, floor_ms: 6200 },
          clean: { expected_ms: 7100, ms_per_attempt: -30.1, floor_ms: 5800 },
        },
      },
      n_completed: 3,
      n_attempts: 14,
      gold_ms: 7800,
      clean_gold_ms: 6500,
    },
  ],
};

describe("API contract fixtures", () => {
  it("idle state fixture type-checks", () => {
    // If this compiles, the types match. Runtime check for safety:
    expect(IDLE_STATE.mode).toBe("idle");
    expect(IDLE_STATE.current_segment).toBeNull();
  });

  it("practice state has correct nested model_output structure", () => {
    const seg = PRACTICE_STATE.current_segment!;
    const output = seg.model_outputs[seg.selected_model]!;
    // This is the bug the migration fixed — old JS accessed output.expected_time_ms
    // which doesn't exist. The correct path is output.total.expected_ms.
    expect(output.total.expected_ms).toBe(8500);
    expect(output.total.ms_per_attempt).toBe(-45.2);
    expect(output.total.floor_ms).toBe(6200);
  });

  it("model response segments have nested Estimate structure", () => {
    const seg = MODEL_RESPONSE.segments[0]!;
    const output = seg.model_outputs[seg.selected_model]!;
    expect(output.total.expected_ms).toBe(8500);
    expect(output.clean.expected_ms).toBe(7100);
  });
});
```

- [ ] **Step 2: Add model logic edge-case tests to `frontend/src/model-logic.test.ts`**

Append to the existing test file:

```ts
describe("selectedEstimate edge cases", () => {
  it("handles segment with multiple estimators", () => {
    const seg: ModelSegment = {
      segment_id: "s1",
      description: "",
      level_number: 1,
      start_type: "entrance",
      start_ordinal: 0,
      end_type: "goal",
      end_ordinal: 0,
      selected_model: "rolling_mean",
      model_outputs: {
        kalman: {
          total: { expected_ms: 5000, ms_per_attempt: -10, floor_ms: 3000 },
          clean: { expected_ms: null, ms_per_attempt: null, floor_ms: null },
        },
        rolling_mean: {
          total: { expected_ms: 6000, ms_per_attempt: -5, floor_ms: 4000 },
          clean: { expected_ms: null, ms_per_attempt: null, floor_ms: null },
        },
      },
      n_completed: 10,
      n_attempts: 20,
      gold_ms: 2500,
      clean_gold_ms: null,
    };
    const est = selectedEstimate(seg);
    // Should return rolling_mean's total, not kalman's
    expect(est?.expected_ms).toBe(6000);
  });

  it("handles segment with all-null estimates", () => {
    const seg: ModelSegment = {
      segment_id: "s1",
      description: "",
      level_number: 1,
      start_type: "entrance",
      start_ordinal: 0,
      end_type: "goal",
      end_ordinal: 0,
      selected_model: "kalman",
      model_outputs: {
        kalman: {
          total: { expected_ms: null, ms_per_attempt: null, floor_ms: null },
          clean: { expected_ms: null, ms_per_attempt: null, floor_ms: null },
        },
      },
      n_completed: 0,
      n_attempts: 0,
      gold_ms: null,
      clean_gold_ms: null,
    };
    const est = selectedEstimate(seg);
    expect(est).not.toBeNull();
    expect(est!.expected_ms).toBeNull();
  });
});
```

- [ ] **Step 3: Run all frontend tests**

```bash
cd frontend && npm test
```
Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api-contract.test.ts frontend/src/model-logic.test.ts
git commit -m "test(frontend): add API contract fixtures and model logic edge cases

Contract tests catch type drift between Python API and TypeScript interfaces.
Edge case tests cover multi-estimator selection and null estimate handling."
```

---

### Task 12: Update Python API tests for consistency

**Files:**
- Modify: `tests/test_dashboard_integration.py`

Now that we have explicit TypeScript types, add a Python-side test that verifies the `/api/model` response structure matches what the frontend expects. This closes the loop — if someone changes the Python response shape, both the Python test and the TypeScript compiler will catch it.

- [ ] **Step 1: Add model response structure assertion to `tests/test_dashboard_integration.py`**

Add a test that verifies the exact field names and nesting of the model endpoint response, matching the TypeScript `ModelData` and `ModelSegment` interfaces:

```python
def test_model_response_matches_frontend_types(client_with_game):
    """Verify /api/model response structure matches frontend TypeScript types.

    The frontend expects: segments[].model_outputs[name].total.expected_ms
    NOT: segments[].model_outputs[name].expected_time_ms (old flat structure)
    """
    resp = client_with_game.get("/api/model")
    assert resp.status_code == 200
    data = resp.json()

    # Top-level keys match ModelData interface
    assert set(data.keys()) == {"estimator", "estimators", "allocator_weights", "segments"}

    if data["segments"]:
        seg = data["segments"][0]
        # Keys match ModelSegment interface
        expected_keys = {
            "segment_id", "description", "level_number",
            "start_type", "start_ordinal", "end_type", "end_ordinal",
            "selected_model", "model_outputs",
            "n_completed", "n_attempts", "gold_ms", "clean_gold_ms",
        }
        assert set(seg.keys()) == expected_keys

        # model_outputs has nested total/clean structure
        if seg["model_outputs"]:
            output = next(iter(seg["model_outputs"].values()))
            assert set(output.keys()) == {"total", "clean"}
            assert set(output["total"].keys()) == {"expected_ms", "ms_per_attempt", "floor_ms"}
```

- [ ] **Step 2: Run the test**

```bash
pytest tests/test_dashboard_integration.py::test_model_response_matches_frontend_types -v
```
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_dashboard_integration.py
git commit -m "test: add Python-side model response structure assertion

Verifies /api/model response shape matches the frontend TypeScript
ModelData/ModelSegment interfaces. Catches backend changes that would
break the frontend contract."
```

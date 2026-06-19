import { beforeEach, describe, expect, it, vi } from "vitest";
import { updateHeader } from "./header";
import type { AppState } from "./types";

// Stub fetch so header module-level postJSON calls don't throw
vi.stubGlobal("fetch", vi.fn());

function baseState(over: Partial<AppState>): AppState {
  return {
    mode: "idle", emu_connected: true, game_id: "g", game_name: "G",
    current_segment: null, recent: [], session: null, sections_captured: null,
    allocator_weights: null, estimator: null, capture_run_id: null,
    replay: null, paused_run: null, cold_fill: null, has_active_run: false,
    segments_missing_cold: 0,
    ...over,
  } as AppState;
}

beforeEach(() => {
  document.body.innerHTML = `
    <span id="game-name"></span>
    <div id="mode-chip"><span id="mode-label"></span>
      <button id="mode-stop" style="display:none"></button>
      <button id="cold-fill-skip" style="display:none"></button>
      <button id="cold-fill-exit" style="display:none"></button>
    </div>`;
});

describe("cold-fill header controls", () => {
  it("shows skip+exit only in cold_fill", () => {
    updateHeader(baseState({ mode: "cold_fill", cold_fill: { current: 1, total: 2, segment_label: "L1" } }));
    expect((document.getElementById("cold-fill-skip") as HTMLElement).style.display).toBe("");
    expect((document.getElementById("cold-fill-exit") as HTMLElement).style.display).toBe("");
    updateHeader(baseState({ mode: "idle" }));
    expect((document.getElementById("cold-fill-skip") as HTMLElement).style.display).toBe("none");
    expect((document.getElementById("cold-fill-exit") as HTMLElement).style.display).toBe("none");
  });
});

describe("practice mode chip sub-state", () => {
  const label = () => document.getElementById("mode-label")!.textContent ?? "";

  it("appends Science / Paused / Grinding to the Practicing chip", () => {
    updateHeader(baseState({ mode: "practice", practice_experimental: true }));
    expect(label()).toContain("Science");

    updateHeader(baseState({ mode: "practice", practice_paused: true }));
    expect(label()).toContain("Paused");

    updateHeader(baseState({ mode: "practice", practice_grind: true, practice_experimental: true }));
    expect(label()).toContain("Grinding");
    expect(label()).toContain("Science");
  });

  it("plain practice chip is just 'Practice' (no sub-state, no segment)", () => {
    updateHeader(baseState({ mode: "practice", current_segment: { description: "L6" } as never }));
    const txt = label();
    expect(txt).toBe("Practice");  // tight: no "Practicing", no segment name
  });
});

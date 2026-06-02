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
    expect(text).toContain("21.2s");
    expect(text).toContain("24.0s");
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

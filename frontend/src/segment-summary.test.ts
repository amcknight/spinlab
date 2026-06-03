import { describe, it, expect } from "vitest";
import { renderSegmentSummary, ordinal } from "./segment-summary";
import type { LiveSegmentView } from "./types";

const READY: LiveSegmentView = {
  segment_id: "s1", ready: true,
  expected_episode_ms: 21_800, practice_gain_ms: 500, death_rate: 0.62,
  floor_ms: 12_800, last_episode_ms: 16_800, last_clean_ms: 13_600,
  last_deaths: 1, last_rank: 2,
  series: [] as unknown as Record<string, never>[],
  n_successes: 6, n_deaths: 5,
  expected_episode_diff_ms: -2_100,
  practice_gain_diff_ms: -100,
  floor_diff_ms: -800,
  death_rate_diff: -0.08,
};

describe("ordinal", () => {
  it("formats English ordinals", () => {
    expect(ordinal(1)).toBe("1st");
    expect(ordinal(2)).toBe("2nd");
    expect(ordinal(3)).toBe("3rd");
    expect(ordinal(4)).toBe("4th");
    expect(ordinal(11)).toBe("11th");
    expect(ordinal(22)).toBe("22nd");
  });
});

describe("renderSegmentSummary", () => {
  it("renders segment name + 4-column stat cluster with colored diffs", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderSegmentSummary(host, { name: "L1 entrance → goal", live: READY });
    expect(host.querySelector(".sg-name")!.textContent).toContain("L1 entrance");
    const stacks = host.querySelectorAll(".ss-stack");
    expect(stacks.length).toBe(4);  // Practice + Floor + Expected + Deaths
    expect(host.querySelectorAll(".ss-diff.good").length).toBeGreaterThanOrEqual(1);
  });
  it("renders the 'last completion' headline with rank + decomposition", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderSegmentSummary(host, { name: "L1", live: READY });
    const headline = host.querySelector(".sg-headline")!;
    expect(headline.textContent).toContain("last completion");
    expect(headline.textContent).toContain("16.8s");
    expect(headline.textContent).toContain("2nd best");
    expect(headline.textContent).toContain("1 death");
    expect(headline.textContent).toContain("13.6s clean");
  });
  it("renders 'no completed runs yet' when last_episode_ms is null", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderSegmentSummary(host, { name: "L1", live: { ...READY, last_episode_ms: null, last_rank: null, last_deaths: null, last_clean_ms: null } });
    expect(host.querySelector(".sg-headline")!.textContent!.toLowerCase()).toContain("no completed");
  });
  it("hides Floor column when floor_diff_ms is null or zero", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderSegmentSummary(host, { name: "L1", live: { ...READY, floor_diff_ms: 0 } });
    expect(host.querySelector(".sg-floor")).toBeNull();
  });
  it("renders 'not enough data' when ready=false", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderSegmentSummary(host, { name: "L1", live: { ...READY, ready: false } });
    expect(host.textContent!.toLowerCase()).toContain("not enough");
  });
});

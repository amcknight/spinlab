import { describe, it, expect, beforeEach } from "vitest";
import { renderRunGraph } from "./run-graph";
import type { RouteSummary } from "./types";

function base(overrides: Partial<RouteSummary>): RouteSummary {
  return {
    game_id: "g", exp_run_ms: 252000, exp_deaths: 21,
    n_estimable: 3, n_skipped: 0,
    session_started_at: null, exp_run_diff_ms: null, exp_deaths_diff: null,
    practice_saved_ms: null, floor_improvement_ms: null,
    run_series: [], baseline_exp_run_ms: null, floor_total_ms: null,
    ...overrides,
  } as RouteSummary;
}

describe("renderRunGraph", () => {
  let host: HTMLElement;
  beforeEach(() => { host = document.createElement("div"); });

  it("shows a placeholder when the series is empty", () => {
    renderRunGraph(host, base({ run_series: [], baseline_exp_run_ms: 250000 }));
    expect(host.querySelector("svg")).toBeNull();
    expect(host.textContent).toMatch(/not enough data/i);
  });

  it("shows a placeholder when the baseline is missing", () => {
    renderRunGraph(host, base({ run_series: [250000, 248000], baseline_exp_run_ms: null }));
    expect(host.querySelector("svg")).toBeNull();
  });

  it("draws curve, baseline, floor and a last-point marker", () => {
    renderRunGraph(host, base({
      run_series: [258000, 255000, 252000],
      baseline_exp_run_ms: 258000,
      floor_total_ms: 231000,
    }));
    expect(host.querySelector("svg")).not.toBeNull();
    expect(host.querySelector(".rg-line")).not.toBeNull();
    expect(host.querySelector(".rg-baseline")).not.toBeNull();
    expect(host.querySelector(".rg-floor")).not.toBeNull();
    expect(host.querySelector(".rg-last")).not.toBeNull();
  });
});

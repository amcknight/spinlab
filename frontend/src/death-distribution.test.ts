import { describe, it, expect, vi } from "vitest";
import { binSamples, BIN_COUNT } from "./death-distribution";

// Mock Chart.js — happy-dom doesn't run canvas. We just verify the
// component constructs the chart with the expected data shape and
// writes the header text we expect.
vi.mock("chart.js", () => ({
  Chart: class {
    data: unknown;
    options: unknown;
    static register() {}
    constructor(_ctx: unknown, config: { data: unknown; options: unknown }) {
      this.data = config.data;
      this.options = config.options;
    }
    destroy() {}
    update() {}
  },
  BarController: class {},
  BarElement: class {},
  LinearScale: class {},
  CategoryScale: class {},
  Legend: class {},
  Tooltip: class {},
}));

describe("binSamples", () => {
  it("returns BIN_COUNT zero-counts on empty input", () => {
    const { bins, lo, hi } = binSamples([], []);
    expect(bins).toHaveLength(BIN_COUNT);
    for (const b of bins) {
      expect(b.deaths).toBe(0);
      expect(b.completions).toBe(0);
    }
    expect(lo).toBe(0);
    expect(hi).toBe(0);
  });

  it("places a single sample at time 0 into bin 0", () => {
    const { bins } = binSamples([[0, 1.0]], []);
    expect(bins[0]!.deaths).toBe(1);
    expect(bins[0]!.completions).toBe(0);
    for (let i = 1; i < BIN_COUNT; i++) {
      expect(bins[i]!.deaths).toBe(0);
    }
  });

  it("clamps a sample at hi into the topmost bin", () => {
    // Single sample at exactly the max — must not overflow.
    const { bins } = binSamples([[10_000, 1.0]], []);
    expect(bins[BIN_COUNT - 1]!.deaths).toBe(1);
    // Total across all bins == sample count.
    const total = bins.reduce((acc, b) => acc + b.deaths, 0);
    expect(total).toBe(1);
  });

  it("counts deaths and completions independently per bin", () => {
    // hi will be ceil(max / 1000) * 1000 = 10000 → bin width 500ms.
    const deaths: [number, number][] = [
      [100, 1.0],   // bin 0
      [200, 1.0],   // bin 0
      [5100, 1.0],  // bin 10
    ];
    const completions: [number, number][] = [
      [150, 1.0],   // bin 0
      [5100, 1.0],  // bin 10
      [9900, 1.0],  // bin 19
    ];
    const { bins } = binSamples(deaths, completions);
    expect(bins[0]!.deaths).toBe(2);
    expect(bins[0]!.completions).toBe(1);
    expect(bins[10]!.deaths).toBe(1);
    expect(bins[10]!.completions).toBe(1);
    expect(bins[19]!.deaths).toBe(0);
    expect(bins[19]!.completions).toBe(1);
  });

  it("rounds hi up to the nearest second", () => {
    const { hi } = binSamples([[3200, 1.0]], []);
    expect(hi).toBe(4000);
  });

  it("ignores sample weights for bar heights (raw counts only)", () => {
    // With max=40 → hi=1000 → width=50, t=40 lands in bin 0
    // (floor(40/50)=0). Both samples share the same bin regardless of
    // weight; the assertion is on raw count, not weighted sum.
    const { bins } = binSamples([[40, 0.001], [40, 0.001]], []);
    expect(bins[0]!.deaths).toBe(2);
  });
});

import { renderDeathDistribution } from "./death-distribution";
import type { components } from "./api-types";

type DeathExtras = components["schemas"]["DeathExtras"];

const SAMPLE_EXTRAS: DeathExtras = {
  halflife_attempts: 20,
  n_attempts_effective: 5.0,
  n_episodes_with_death_eff: 2.0,
  n_episodes_completed_eff: 4.0,
  p_die_per_attempt: 0.4,
  n_lives_died_effective: 2.0,
  n_lives_survived_effective: 4.0,
  p_die_per_life: 0.333,
  death_samples: [[2000, 1.0], [2200, 0.9]],
  completion_samples: [[4500, 1.0], [4800, 0.9], [3800, 0.7], [5100, 0.5]],
  expected_death_time_ms: 2100,
  expected_completion_time_ms: 4500,
};

describe("renderDeathDistribution", () => {
  it("renders the header stats and a chart canvas when extras are present", () => {
    const container = document.createElement("div");
    renderDeathDistribution(container, SAMPLE_EXTRAS);
    expect(container.querySelector(".death-distribution")).not.toBeNull();
    const header = container.querySelector(".death-distribution-header");
    expect(header?.textContent).toContain("0.33");  // p_die_per_life rounded
    expect(header?.textContent).toContain("0.40");  // p_die_per_attempt
    expect(header?.textContent).toContain("20");    // halflife
    expect(container.querySelector("canvas")).not.toBeNull();
  });

  it("renders an empty-state message when extras are null", () => {
    const container = document.createElement("div");
    renderDeathDistribution(container, null);
    expect(container.querySelector(".death-distribution")).not.toBeNull();
    expect(container.querySelector("canvas")).toBeNull();
    expect(container.textContent).toContain("No death data");
  });

  it("renders an empty-state message when sample arrays are both empty", () => {
    const container = document.createElement("div");
    const empty: DeathExtras = {
      ...SAMPLE_EXTRAS,
      death_samples: [],
      completion_samples: [],
      expected_death_time_ms: null,
      expected_completion_time_ms: null,
      p_die_per_attempt: null,
      p_die_per_life: null,
    };
    renderDeathDistribution(container, empty);
    expect(container.querySelector(".death-distribution")).not.toBeNull();
    expect(container.querySelector("canvas")).toBeNull();
    expect(container.textContent).toContain("No death data");
  });
});

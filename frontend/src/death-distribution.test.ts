import { describe, it, expect, vi } from "vitest";
import { renderColdHistogram } from "./death-distribution";
import type { ColdDistribution } from "./types";

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
  BarController: class {}, BarElement: class {},
  LinearScale: class {}, CategoryScale: class {},
  Legend: class {}, Tooltip: class {},
}));

describe("renderColdHistogram", () => {
  it("builds two datasets (deaths, completions) with one bar per ColdBin", () => {
    const dist: ColdDistribution = {
      bins: [
        { lo_ms: 0, hi_ms: 500, n_deaths: 2, n_completions: 0 },
        { lo_ms: 500, hi_ms: 1000, n_deaths: 1, n_completions: 1 },
        { lo_ms: 1000, hi_ms: 1500, n_deaths: 0, n_completions: 2 },
        { lo_ms: 1500, hi_ms: 2000, n_deaths: 0, n_completions: 1 },
        { lo_ms: 2000, hi_ms: 2500, n_deaths: 0, n_completions: 0 },
      ],
      n_cold_attempts: 7,
      mu_d_ms: 333,
      mu_c_ms: 1500,
      p_die_per_attempt: 0.5,
      p_die_per_life: 0.43,
    };
    const canvas = document.createElement("canvas");
    const chart = renderColdHistogram(canvas, dist);
    const data = (chart as any).data;
    expect(data.datasets).toHaveLength(2);
    expect(data.datasets[0].label).toMatch(/deaths/i);
    expect(data.datasets[1].label).toMatch(/completions/i);
    expect(data.datasets[0].data).toEqual([2, 1, 0, 0, 0]);
    expect(data.datasets[1].data).toEqual([0, 1, 2, 1, 0]);
  });
});

import { describe, it, expect, vi } from "vitest";
import { renderHazard } from "./hazard-render";
import type { ColdDistribution } from "./types";

vi.mock("chart.js", () => ({
  Chart: class {
    data: any; options: any;
    static register() {}
    constructor(_ctx: unknown, config: { data: any; options: any }) {
      this.data = config.data; this.options = config.options;
    }
    destroy() {} update() {}
  },
  BarController: class {}, BarElement: class {},
  LinearScale: class {}, CategoryScale: class {},
  Legend: class {}, Tooltip: class {},
}));

const SAMPLE: ColdDistribution = {
  bins: [
    { lo_ms: 0,    hi_ms: 500, n_deaths: 0, n_completions: 0, hazard: 0.1, at_risk_w: 10.0 },
    { lo_ms: 500,  hi_ms: 1000, n_deaths: 0, n_completions: 0, hazard: 0.3, at_risk_w: 5.0 },
    { lo_ms: 1000, hi_ms: 1500, n_deaths: 0, n_completions: 0, hazard: null, at_risk_w: 0.0 },
  ],
  n_cold_attempts: 10, mu_d_ms: null, mu_c_ms: null,
  p_die_per_attempt: null, p_die_per_life: null, halflife: 20,
};

describe("renderHazard", () => {
  it("creates one bar per bin with hazard values", () => {
    const chart = renderHazard(document.createElement("canvas"), SAMPLE);
    const data = (chart as any).data;
    expect(data.datasets).toHaveLength(1);
    // null bin renders as 0-height; chart.js drops nulls cleanly
    expect(data.datasets[0].data).toEqual([0.1, 0.3, null]);
  });

  it("computes per-bar opacity from at_risk_w / bins[0].at_risk_w", () => {
    const chart = renderHazard(document.createElement("canvas"), SAMPLE);
    const bg = (chart as any).data.datasets[0].backgroundColor as string[];
    // bin 0: at_risk 10/10 = 1.0  → full opacity
    // bin 1: at_risk 5/10 = 0.5   → half
    // bin 2: at_risk 0/10 = 0.0   → zero
    expect(bg[0]).toMatch(/rgba\(255,\s*241,\s*118,\s*1(\.0+)?\)/);
    expect(bg[1]).toMatch(/rgba\(255,\s*241,\s*118,\s*0\.5\d*\)/);
    expect(bg[2]).toMatch(/rgba\(255,\s*241,\s*118,\s*0(\.0+)?\)/);
  });
});

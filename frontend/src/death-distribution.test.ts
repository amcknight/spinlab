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
  LineController: class {}, LineElement: class {}, PointElement: class {},
  LinearScale: class {}, CategoryScale: class {},
  Legend: class {}, Tooltip: class {},
}));

function makeDist(overrides: Partial<ColdDistribution> = {}): ColdDistribution {
  return {
    bins: [
      { lo_ms: 0, hi_ms: 500, n_deaths: 2, n_completions: 0, at_risk_w: 0 },
      { lo_ms: 500, hi_ms: 1000, n_deaths: 1, n_completions: 1, at_risk_w: 0 },
      { lo_ms: 1000, hi_ms: 1500, n_deaths: 0, n_completions: 2, at_risk_w: 0 },
      { lo_ms: 1500, hi_ms: 2000, n_deaths: 0, n_completions: 1, at_risk_w: 0 },
      { lo_ms: 2000, hi_ms: 2500, n_deaths: 0, n_completions: 0, at_risk_w: 0 },
    ],
    n_cold_attempts: 7,
    mu_d_ms: 333,
    mu_c_ms: 1500,
    p_die_per_attempt: 0.5,
    p_die_per_life: 0.43,
    halflife: 0,
    ...overrides,
  };
}

describe("renderColdHistogram", () => {
  it("builds death/completion bars + 2 PDF overlay lines on the completion side", () => {
    const dist = makeDist();
    const canvas = document.createElement("canvas");
    const chart = renderColdHistogram(canvas, dist);
    const data = (chart as any).data;
    expect(data.datasets).toHaveLength(4);
    expect(data.datasets[0].label).toMatch(/deaths/i);
    expect(data.datasets[1].label).toMatch(/completions/i);
    expect(data.datasets[0].data).toEqual([2, 1, 0, 0, 0]);
    expect(data.datasets[1].data).toEqual([0, 1, 2, 1, 0]);
    const overlayLabels = data.datasets.slice(2).map((d: any) => d.label);
    expect(overlayLabels).toEqual([
      "Normal (survival)", "Lognormal (survival)",
    ]);
  });

  it("overlays are all-null when the matching moments are missing", () => {
    // No σ_c / log-moments provided => overlays should be null arrays.
    const dist = makeDist();
    const canvas = document.createElement("canvas");
    const chart = renderColdHistogram(canvas, dist);
    const overlays = (chart as any).data.datasets.slice(2);
    for (const ds of overlays) {
      expect(ds.data.every((v: number | null) => v === null)).toBe(true);
    }
  });

  it("Normal-survival overlay peaks at μ_c when σ_c is provided", () => {
    // Bin centers (250, 750, 1250, 1750, 2250). With μ_c=1250, σ_c=400 the
    // middle bin (idx 2) should be the maximum of the Normal-survival curve.
    const dist = makeDist({ mu_c_ms: 1250, sigma_c_ms: 400 });
    const canvas = document.createElement("canvas");
    const chart = renderColdHistogram(canvas, dist);
    const normalSurv = (chart as any).data.datasets[2].data as number[];
    const maxIdx = normalSurv.indexOf(Math.max(...normalSurv));
    expect(maxIdx).toBe(2);
  });
});

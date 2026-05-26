import { describe, it, expect, vi } from "vitest";
import { binSamples, BIN_COUNT } from "./death-distribution";

// vitest's happy-dom env is configured globally; vi.mock for Chart.js is
// only needed when we exercise the render function (Step 3.x).

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

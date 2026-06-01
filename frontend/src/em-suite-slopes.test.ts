import { describe, expect, it } from "vitest";

import { __test__ } from "./em-suite-slopes";

const { cellColor, formatSlopeCell, maxAbsOver } = __test__;

describe("formatSlopeCell", () => {
  it("formats a finite slope to two decimals", () => {
    expect(formatSlopeCell(-0.234)).toBe("-0.23");
    expect(formatSlopeCell(1.5)).toBe("1.50");
  });

  it("returns empty string for null", () => {
    expect(formatSlopeCell(null)).toBe("");
  });

  it("returns empty string for non-finite values", () => {
    expect(formatSlopeCell(Number.POSITIVE_INFINITY)).toBe("");
    expect(formatSlopeCell(Number.NaN)).toBe("");
  });
});

describe("maxAbsOver", () => {
  it("returns the maximum absolute value across cells", () => {
    const grid = [
      [null, 0.1, -0.3],
      [0.2, null, null],
      [null, null, null],
    ];
    expect(maxAbsOver(grid)).toBeCloseTo(0.3);
  });

  it("ignores null and non-finite cells", () => {
    const grid = [
      [null, Number.NaN],
      [Number.POSITIVE_INFINITY, 0.4],
    ];
    expect(maxAbsOver(grid)).toBeCloseTo(0.4);
  });

  it("floors at the small-magnitude threshold to avoid screaming on near-zero data", () => {
    // Empty / all-null grid still has a non-zero floor so divides don't blow up.
    expect(maxAbsOver([[null, null]])).toBeGreaterThan(0);
  });
});

describe("cellColor", () => {
  it("returns dark grey for null cells", () => {
    expect(cellColor(null, 1.0)).toBe("#222");
  });

  it("returns dark grey when maxAbs is 0 (no signal)", () => {
    expect(cellColor(0.5, 0)).toBe("#222");
  });

  it("uses green-family hue for negative slopes (improving)", () => {
    // hsl(130, 70%, ...) — hue 130 is the green-family used for "improving"
    expect(cellColor(-0.5, 1.0)).toMatch(/hsl\(130,/);
  });

  it("uses red-family hue for positive slopes (regressing)", () => {
    expect(cellColor(0.5, 1.0)).toMatch(/hsl\(0,/);
  });
});

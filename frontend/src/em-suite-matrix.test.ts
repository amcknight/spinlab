import { describe, expect, it } from "vitest";

import { formatMatrixCell, isAlphaPairValid } from "./em-suite-matrix";

describe("formatMatrixCell", () => {
  it("formats ms to one-decimal seconds with 's' suffix", () => {
    expect(formatMatrixCell(25_600)).toBe("25.6s");
    expect(formatMatrixCell(1_234)).toBe("1.2s");
  });

  it("returns em-dash for null", () => {
    expect(formatMatrixCell(null)).toBe("—");
  });

  it("returns em-dash for non-finite values", () => {
    expect(formatMatrixCell(Number.POSITIVE_INFINITY)).toBe("—");
    expect(formatMatrixCell(Number.NaN)).toBe("—");
  });
});

describe("isAlphaPairValid", () => {
  it("returns true when fast > slow", () => {
    expect(isAlphaPairValid(5, 2)).toBe(true);
  });

  it("returns false when fast == slow", () => {
    expect(isAlphaPairValid(3, 3)).toBe(false);
  });

  it("returns false when fast < slow", () => {
    expect(isAlphaPairValid(1, 4)).toBe(false);
  });
});

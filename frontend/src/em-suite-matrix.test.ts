import { describe, expect, it } from "vitest";

import {
  formatMatrixCell,
  isAlphaPairValid,
  windowLabel,
  windowAttempts,
  isWindowSufficient,
  renderEmSuiteMatrix,
} from "./em-suite-matrix";
import type { EmSuiteMatrixResponse } from "./types";

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

describe("windowLabel / windowAttempts / isWindowSufficient", () => {
  it("maps alpha to a memory-window label", () => {
    expect(windowLabel(0)).toBe("all-time");
    expect(windowLabel(1)).toBe("last 1");
    expect(windowLabel(0.2)).toBe("last ~5");
    expect(windowLabel(0.05)).toBe("last ~20");
  });
  it("maps alpha to an attempt count (Infinity for all-time)", () => {
    expect(windowAttempts(0)).toBe(Infinity);
    expect(windowAttempts(0.2)).toBe(5);
    expect(windowAttempts(0.01)).toBe(100);
  });
  it("a window is sufficient only when no longer than the attempts seen", () => {
    expect(isWindowSufficient(0, 8)).toBe(true);       // all-time always
    expect(isWindowSufficient(0.2, 8)).toBe(true);     // 5 <= 8
    expect(isWindowSufficient(0.05, 8)).toBe(false);   // 20 > 8
  });
});

const MATRIX: EmSuiteMatrixResponse = {
  segment_id: "s1",
  alpha_grid: [0.0, 0.01, 0.02, 0.03, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0],
  // index-aligned per-alpha sample(0); fill plausible values, nulls allowed.
  baseline: [24000, 23800, 23600, 23400, 24000, 22500, 22000, 21500, 21000, 20800],
  matrix: [],
  n_attempts_total: 8, n_successes: 5, n_deaths: 3,
  param_history: {} as never,
  slope_matrices: {} as never,
};

describe("renderEmSuiteMatrix (window picker)", () => {
  it("renders Now and Baseline pickers defaulting to last~5 / last~20", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderEmSuiteMatrix(host, MATRIX);
    const now = host.querySelector<HTMLSelectElement>("#ems-now")!;
    const base = host.querySelector<HTMLSelectElement>("#ems-baseline")!;
    expect(now).not.toBeNull();
    expect(base).not.toBeNull();
    // Default Now = alpha 0.2 (index 6), Baseline = alpha 0.05 (index 4).
    expect(now.value).toBe("6");
    expect(base.value).toBe("4");
  });

  it("shows each chosen window's expected time in seconds and no 'undefined'", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderEmSuiteMatrix(host, MATRIX);
    const readout = host.querySelector("#ems-readout")!.textContent || "";
    expect(readout).toContain("22.0s");  // Now (alpha 0.2) baseline[6]
    expect(readout).toContain("24.0s");  // Baseline (alpha 0.05) baseline[4]
    expect(readout).not.toContain("undefined");
  });

  it("annotates an insufficient window (longer than attempts seen)", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderEmSuiteMatrix(host, MATRIX);  // n=8; baseline window (20) is insufficient
    const readout = host.querySelector("#ems-readout")!.textContent || "";
    expect(readout.toLowerCase()).toContain("all-time"); // "≈ all-time" note for the 20-window
  });
});

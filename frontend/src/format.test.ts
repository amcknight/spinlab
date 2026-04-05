import { describe, it, expect } from "vitest";
import { segmentName, formatTime, elapsedStr, formatSavings } from "./format";
import type { SegmentLike } from "./types";

describe("segmentName", () => {
  it("returns description when present", () => {
    const seg: SegmentLike = {
      description: "Iggy's Castle",
      level_number: 3,
      start_type: "entrance",
      start_ordinal: 0,
      end_type: "goal",
      end_ordinal: 0,
    };
    expect(segmentName(seg)).toBe("Iggy's Castle");
  });

  it("builds name from entrance to goal", () => {
    const seg: SegmentLike = {
      level_number: 5,
      start_type: "entrance",
      start_ordinal: 0,
      end_type: "goal",
      end_ordinal: 0,
    };
    expect(segmentName(seg)).toBe("L5 start → goal");
  });

  it("builds name from checkpoint to checkpoint", () => {
    const seg: SegmentLike = {
      level_number: 2,
      start_type: "checkpoint",
      start_ordinal: 1,
      end_type: "checkpoint",
      end_ordinal: 2,
    };
    expect(segmentName(seg)).toBe("L2 cp1 → cp2");
  });

  it("handles empty string description as falsy", () => {
    const seg: SegmentLike = {
      description: "",
      level_number: 1,
      start_type: "entrance",
      start_ordinal: 0,
      end_type: "goal",
      end_ordinal: 0,
    };
    expect(segmentName(seg)).toBe("L1 start → goal");
  });
});

describe("formatTime", () => {
  it("returns em dash for null", () => {
    expect(formatTime(null)).toBe("—");
  });

  it("returns em dash for undefined", () => {
    expect(formatTime(undefined)).toBe("—");
  });

  it("formats milliseconds to seconds with one decimal", () => {
    expect(formatTime(12345)).toBe("12.3s");
  });

  it("formats zero", () => {
    expect(formatTime(0)).toBe("0.0s");
  });
});

describe("elapsedStr", () => {
  it("returns empty string for null", () => {
    expect(elapsedStr(null)).toBe("");
  });

  it("returns empty string for empty string", () => {
    expect(elapsedStr("")).toBe("");
  });

  it("returns 0:00 for invalid date", () => {
    expect(elapsedStr("not-a-date")).toBe("0:00");
  });

  it("formats elapsed time as m:ss", () => {
    const twoMinutesAgo = new Date(Date.now() - 123_000).toISOString();
    const result = elapsedStr(twoMinutesAgo);
    expect(result).toMatch(/^2:0[2-4]$/);
  });

  it("pads seconds with leading zero", () => {
    const fiveSecondsAgo = new Date(Date.now() - 5_000).toISOString();
    const result = elapsedStr(fiveSecondsAgo);
    expect(result).toMatch(/^0:0[4-6]$/);
  });
});

describe("formatSavings", () => {
  it("returns null for null", () => {
    expect(formatSavings(null)).toBeNull();
  });

  it("returns null for undefined", () => {
    expect(formatSavings(undefined)).toBeNull();
  });

  it("formats positive savings with + sign", () => {
    expect(formatSavings(3200)).toBe("+3.2s");
  });

  it("formats negative savings with - sign", () => {
    expect(formatSavings(-1100)).toBe("-1.1s");
  });

  it("formats zero as +0.0s", () => {
    expect(formatSavings(0)).toBe("+0.0s");
  });
});

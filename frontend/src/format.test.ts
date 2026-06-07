import { describe, it, expect } from "vitest";
import { segmentName, formatTime, elapsedStr, formatSavings, emptyStateMessage, NO_ACTIVE_RUN_MESSAGE, formatPct, formatAgo } from "./format";
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

  it("breaks into minutes at and above 60s", () => {
    expect(formatTime(112_600)).toBe("1m52.6s");
  });

  it("zero-pads the seconds in minute form", () => {
    expect(formatTime(62_600)).toBe("1m02.6s");
  });

  it("keeps seconds form just under a minute", () => {
    expect(formatTime(59_900)).toBe("59.9s");
  });

  it("shows a clean minute as Xm00.0s", () => {
    expect(formatTime(60_000)).toBe("1m00.0s");
  });

  it("breaks into hours above an hour", () => {
    // 1h 1m 1.5s
    expect(formatTime(3_661_500)).toBe("1h01m01.5s");
  });

  it("breaks into days above a day", () => {
    // 1d 1h 1m 1.5s
    expect(formatTime(90_061_500)).toBe("1d01h01m01.5s");
  });

  it("prefixes a minus sign for negative durations", () => {
    expect(formatTime(-112_600)).toBe("-1m52.6s");
  });

  it("rounds tenths without rolling seconds to 60", () => {
    // 119.96s rounds to 120.0s → 2m00.0s, not 1m60.0s
    expect(formatTime(119_960)).toBe("2m00.0s");
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

describe("emptyStateMessage", () => {
  it("returns the no-active-run wording when there is no active run", () => {
    expect(emptyStateMessage(false, "No segments")).toBe(NO_ACTIVE_RUN_MESSAGE);
  });

  it("returns the caller's no-segments copy when a run is active", () => {
    expect(emptyStateMessage(true, "No segments")).toBe("No segments");
    expect(emptyStateMessage(true, "No game loaded")).toBe("No game loaded");
  });
});

describe("formatPct", () => {
  it("renders a fraction as a whole-number percent", () => {
    expect(formatPct(0.13)).toBe("13%");
    expect(formatPct(0.455)).toBe("46%");
    expect(formatPct(0)).toBe("0%");
  });
  it("returns empty string for null/undefined", () => {
    expect(formatPct(null)).toBe("");
    expect(formatPct(undefined)).toBe("");
  });
});

describe("formatAgo", () => {
  const now = Date.parse("2026-06-07T12:00:00Z");
  const ago = (iso: string) => formatAgo(iso, now);
  it("renders compact units, months skipped", () => {
    expect(ago("2026-06-07T11:59:57Z")).toBe("now");   // < 10s
    expect(ago("2026-06-07T11:59:13Z")).toBe("47s");
    expect(ago("2026-06-07T11:26:00Z")).toBe("34m");
    expect(ago("2026-06-07T05:00:00Z")).toBe("7h");
    expect(ago("2026-06-01T12:00:00Z")).toBe("6d");
    expect(ago("2026-04-12T12:00:00Z")).toBe("8w");    // ~56 days -> 8w (no months)
    expect(ago("2019-06-09T12:00:00Z")).toBe("7y");
  });
  it("returns empty for null/undefined", () => {
    expect(formatAgo(null, now)).toBe("");
    expect(formatAgo(undefined, now)).toBe("");
  });
});

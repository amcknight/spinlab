import { describe, it, expect } from "vitest";
import { groupByLevel, formatConditions } from "./segments-view";

describe("groupByLevel", () => {
  it("groups segments by level_number preserving ordinal order", () => {
    const segs = [
      { id: "a", level_number: 2, ordinal: 3, start_conditions: {}, end_conditions: {}, is_primary: true },
      { id: "b", level_number: 1, ordinal: 1, start_conditions: {}, end_conditions: {}, is_primary: true },
      { id: "c", level_number: 1, ordinal: 2, start_conditions: {}, end_conditions: {}, is_primary: false },
    ] as any[];
    const grouped = groupByLevel(segs);
    expect(Object.keys(grouped)).toEqual(["1", "2"]);
    expect(grouped["1"]?.map((s: any) => s.id)).toEqual(["b", "c"]);
    expect(grouped["2"]?.map((s: any) => s.id)).toEqual(["a"]);
  });
});

describe("formatConditions", () => {
  it("renders empty as dash", () => {
    expect(formatConditions({})).toBe("—");
  });
  it("renders key=value pairs", () => {
    expect(formatConditions({ powerup: "big" })).toBe("powerup=big");
  });
  it("includes multiple keys", () => {
    const out = formatConditions({ powerup: "big", on_yoshi: true });
    expect(out).toMatch(/powerup=big/);
    expect(out).toMatch(/on_yoshi=true/);
  });
});

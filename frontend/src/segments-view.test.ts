import { describe, it, expect } from "vitest";
import { groupByLevel, formatConditions, renderSegmentsView, coldCaptureButtonEnabled, coldCaptureButtonVisible } from "./segments-view";

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

describe("renderSegmentsView", () => {
  it("shows a cold-present marker when has_cold_state is true", () => {
    const container = document.createElement("div");
    const segs = [
      { id: "a", level_number: 1, ordinal: 1, start_type: "entrance", start_ordinal: 0,
        end_type: "goal", end_ordinal: 0, start_conditions: {}, end_conditions: {},
        is_primary: true, has_cold_state: true, description: "", session_ordinal: 1 },
    ] as any[];
    renderSegmentsView(container, segs);
    expect(container.querySelector(".seg-cold")?.textContent).toBe("✅");
  });
});

describe("renderSegmentsView merged table", () => {
  it("renders an editable name input with description as value and segment label as placeholder", () => {
    const container = document.createElement("div");
    const segs = [
      { id: "a", level_number: 1, ordinal: 1, start_type: "entrance", start_ordinal: 0,
        end_type: "goal", end_ordinal: 0, start_conditions: {}, end_conditions: {},
        is_primary: true, has_cold_state: true, description: "Yoshi spot", session_ordinal: 2 },
    ] as any[];
    renderSegmentsView(container, segs);
    const input = container.querySelector("input.segment-name-input") as HTMLInputElement;
    expect(input).not.toBeNull();
    expect(input.value).toBe("Yoshi spot");
    expect(input.placeholder.length).toBeGreaterThan(0);
  });

  it("hides the detail row until the expander is clicked, then shows Conditions and Session #", () => {
    const container = document.createElement("div");
    const segs = [
      { id: "a", level_number: 1, ordinal: 1, start_type: "entrance", start_ordinal: 0,
        end_type: "goal", end_ordinal: 0, start_conditions: { powerup: "cape" },
        end_conditions: {}, is_primary: true, has_cold_state: true,
        description: "", session_ordinal: 2 },
    ] as any[];
    renderSegmentsView(container, segs);
    const detail = container.querySelector("tr.seg-detail") as HTMLElement;
    expect(detail.style.display).toBe("none");
    (container.querySelector(".seg-expander") as HTMLElement).click();
    expect(detail.style.display).not.toBe("none");
    expect(detail.textContent).toContain("powerup=cape");
    expect(detail.textContent).toContain("2"); // session ordinal
  });
});

describe("coldCaptureButtonEnabled", () => {
  it("enabled only when idle, an active run exists, and emulator is connected", () => {
    expect(coldCaptureButtonEnabled("idle", true, true)).toBe(true);
    expect(coldCaptureButtonEnabled("idle", true, false)).toBe(false);
    expect(coldCaptureButtonEnabled("idle", false, true)).toBe(false);
    expect(coldCaptureButtonEnabled("cold_fill", true, true)).toBe(false);
    expect(coldCaptureButtonEnabled("reference", true, true)).toBe(false);
  });
});

describe("coldCaptureButtonVisible", () => {
  it("shows only when the active run has at least one missing-cold segment", () => {
    expect(coldCaptureButtonVisible(0)).toBe(false);
    expect(coldCaptureButtonVisible(1)).toBe(true);
    expect(coldCaptureButtonVisible(5)).toBe(true);
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

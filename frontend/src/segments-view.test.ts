import { describe, it, expect, vi } from "vitest";
import { groupByLevel, formatConditions, renderSegmentsView, coldCaptureButtonEnabled, coldCaptureButtonVisible, deleteSegment, startFillGap } from "./segments-view";

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

  it("renders a single table with one header row and a compact divider per level", () => {
    const container = document.createElement("div");
    const segs = [
      { id: "a", level_number: 1, ordinal: 1, start_type: "entrance", start_ordinal: 0,
        end_type: "goal", end_ordinal: 0, start_conditions: {}, end_conditions: {},
        is_primary: true, has_cold_state: true, description: "", session_ordinal: 1 },
      { id: "b", level_number: 2, ordinal: 1, start_type: "entrance", start_ordinal: 0,
        end_type: "goal", end_ordinal: 0, start_conditions: {}, end_conditions: {},
        is_primary: true, has_cold_state: true, description: "", session_ordinal: 1 },
    ] as any[];
    renderSegmentsView(container, segs);
    // One table, one set of column headers (not one per level).
    expect(container.querySelectorAll("table").length).toBe(1);
    expect(container.querySelectorAll("thead").length).toBe(1);
    // One compact divider row per level, labelled "Level N".
    const dividers = container.querySelectorAll("tr.seg-level-divider");
    expect(dividers.length).toBe(2);
    expect(dividers[0]?.textContent).toBe("Level 1");
    expect(dividers[1]?.textContent).toBe("Level 2");
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

describe("renderSegmentsView cold cell", () => {
  it("renders a Fill button (not a checkmark) when has_cold_state is false", () => {
    const container = document.createElement("div");
    const segs = [
      { id: "a", level_number: 1, ordinal: 1, start_type: "entrance", start_ordinal: 0,
        end_type: "goal", end_ordinal: 0, start_conditions: {}, end_conditions: {},
        is_primary: false, has_cold_state: false, description: "", session_ordinal: 1 },
    ] as any[];
    renderSegmentsView(container, segs);
    const btn = container.querySelector(".seg-cold .btn-fill-gap") as HTMLButtonElement;
    expect(btn).not.toBeNull();
    expect(btn.textContent).toContain("Fill");
  });
});

describe("segment action helpers", () => {
  it("deleteSegment issues DELETE to /api/segments/{id}", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) });
    vi.stubGlobal("fetch", fetchMock);
    await deleteSegment("seg1");
    expect(fetchMock).toHaveBeenCalledWith("/api/segments/seg1", { method: "DELETE" });
  });

  it("startFillGap POSTs to /api/segments/{id}/fill-gap", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ status: "started" }) });
    vi.stubGlobal("fetch", fetchMock);
    const out = await startFillGap("seg1");
    expect(fetchMock).toHaveBeenCalledWith("/api/segments/seg1/fill-gap", { method: "POST" });
    expect(out.status).toBe("started");
  });

  it("deleteSegment throws when fetch resolves with ok: false", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 404 });
    vi.stubGlobal("fetch", fetchMock);
    await expect(deleteSegment("x")).rejects.toThrow();
  });

  it("startFillGap throws when fetch resolves with ok: false", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 500 });
    vi.stubGlobal("fetch", fetchMock);
    await expect(startFillGap("x")).rejects.toThrow();
  });
});

describe("renderSegmentsView cold cell checkmark", () => {
  it("renders checkmark text when has_cold_state is true", () => {
    const container = document.createElement("div");
    const segs = [
      { id: "b", level_number: 1, ordinal: 1, start_type: "entrance", start_ordinal: 0,
        end_type: "goal", end_ordinal: 0, start_conditions: {}, end_conditions: {},
        is_primary: true, has_cold_state: true, description: "", session_ordinal: 1 },
    ] as any[];
    renderSegmentsView(container, segs);
    expect(container.querySelector(".seg-cold")?.textContent).toBe("✅");
  });
});

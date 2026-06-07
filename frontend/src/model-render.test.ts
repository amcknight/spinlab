import { describe, it, expect, beforeEach } from "vitest";
import { renderModelTable } from "./model-render";
import type { ModelData } from "./types";

function modelWith(segments: any[]): any {
  return { estimator: "m", allocator_weights: {}, segments };
}
function seg(name: string, expected: number | null, floor: number | null, gain: number | null, n = 5): any {
  return {
    segment_id: name, description: name, level_number: 1,
    start_type: "entrance", start_ordinal: 0, end_type: "goal", end_ordinal: 0,
    selected_model: "m", n_completed: n, n_attempts: n, gold_ms: floor, clean_gold_ms: floor,
    model_outputs: { m: { total: { expected_ms: expected, ms_per_attempt: expected, floor_ms: floor }, clean: { expected_ms: null, ms_per_attempt: null, floor_ms: null }, extras: null, practice_gain_ms: gain } },
  };
}

describe("renderModelTable room/practice columns", () => {
  beforeEach(() => {
    document.body.innerHTML = `<table><tbody id="model-body"></tbody></table>`;
  });

  it("renders Room (abs + %) and Practice (gain + trend%), no Best", () => {
    renderModelTable(modelWith([seg("L1", 12400, 11000, 600)]), () => {}, true);
    const row = document.querySelector("#model-body tr")!;
    const text = row.textContent ?? "";
    expect(text).toContain("1.4s"); // Room absolute
    expect(text).toContain("13%");  // Room%
    expect(text).toContain("0.6s"); // Practice gain magnitude
    expect(text).toContain("5%");   // Trend% = 600/12400
    expect(row.querySelector(".gain-good")).not.toBeNull();
  });

  it("sorts rows by Room% descending", () => {
    renderModelTable(
      modelWith([seg("low", 12400, 11000, 600), seg("high", 24800, 17100, 1200)]),
      () => {}, true,
    );
    const links = Array.from(document.querySelectorAll("#model-body a")).map((a) => a.textContent);
    expect(links[0]).toBe("high"); // 45% before 13%
  });

  it("dims Room/Practice when the estimate is missing", () => {
    renderModelTable(modelWith([seg("thin", null, null, null)]), () => {}, true);
    const row = document.querySelector("#model-body tr")!;
    expect(row.querySelectorAll(".dim").length).toBeGreaterThanOrEqual(2);
  });
});

const EMPTY_MODEL = { segments: [] } as unknown as ModelData;

describe("renderModelTable empty states", () => {
  beforeEach(() => {
    document.body.innerHTML = `<table><tbody id="model-body"></tbody></table>`;
  });

  it("shows the no-active-run empty state when hasActiveRun is false", () => {
    renderModelTable(EMPTY_MODEL, () => {}, false);
    const body = document.getElementById("model-body")!;
    expect(body.textContent).toContain("No reference run selected");
  });

  it("with an active run but no segments keeps the generic empty copy", () => {
    renderModelTable(EMPTY_MODEL, () => {}, true);
    const body = document.getElementById("model-body")!;
    expect(body.textContent).toContain("No game loaded");
    expect(body.textContent).not.toContain("No reference run selected");
  });
});

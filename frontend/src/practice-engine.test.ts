import { describe, it, expect, beforeEach } from "vitest";
import { renderPracticeEnginePanel, buildEvaluateRequest } from "./practice-engine";
import type { PracticeEngineState, PracticeEngineEvaluateResponse } from "./types";

const MOCK_STATE: PracticeEngineState = {
  gated_segments: [
    { seg_id: "s1", description: "Level 1", level_number: 1,
      start_type: "entrance", start_ordinal: 0, end_type: "goal", end_ordinal: 0,
      e_sample_0_ms: 4500, e_sample_1_ms: 4300,
      pool_success: 40, pool_death: 20, gold_ms: 4200 },
    { seg_id: "s2", description: "Level 2", level_number: 2,
      start_type: "entrance", start_ordinal: 0, end_type: "goal", end_ordinal: 0,
      e_sample_0_ms: 6000, e_sample_1_ms: 5800,
      pool_success: 35, pool_death: 25, gold_ms: 5800 },
  ],
  ungated_segments: [],
  matrix_built_at: null,
  N: 20000,
};

const MOCK_EVAL: PracticeEngineEvaluateResponse = {
  objective_value: 10500,
  per_segment_values: [
    { seg_id: "s1", value: 200, value_per_second: 200/4500,
      e_sample_0_ms: 4500, e_sample_1_ms: 4300 },
    { seg_id: "s2", value: 150, value_per_second: 150/6000,
      e_sample_0_ms: 6000, e_sample_1_ms: 5850 },
  ],
  total_time_summary: {
    bins: [10000, 10500, 11000, 11500],
    counts: [200, 500, 300],
    mean: 10500, median: 10500, p10: 10200, p90: 10800,
    finished_pct: 100,
    aborted_by_segment: {},
  },
};

describe("renderPracticeEnginePanel", () => {
  beforeEach(() => {
    document.body.innerHTML = `<div id="practice-engine-panel"></div>`;
  });

  it("renders gated segments in the controls table", () => {
    const container = document.getElementById("practice-engine-panel")!;
    renderPracticeEnginePanel(container, MOCK_STATE);
    expect(container.querySelector(".pe-segments-input")).not.toBeNull();
    const rows = container.querySelectorAll(".pe-segments-input tbody tr");
    expect(rows.length).toBe(2);
  });

  it("shows ungated segments separately if any", () => {
    const stateWithUngated: PracticeEngineState = {
      ...MOCK_STATE,
      ungated_segments: [{ seg_id: "s3", reason: "needs more data",
        description: "", level_number: 3,
        start_type: "entrance", start_ordinal: 0, end_type: "goal", end_ordinal: 0 }],
    };
    const container = document.getElementById("practice-engine-panel")!;
    renderPracticeEnginePanel(container, stateWithUngated);
    expect(container.textContent).toContain("needs more data");
  });

  it("fill-from-gold button populates inputs with cumulative golds", () => {
    const container = document.getElementById("practice-engine-panel")!;
    renderPracticeEnginePanel(container, MOCK_STATE);
    const fillBtn = container.querySelector<HTMLButtonElement>("#pe-fill-gold")!;
    fillBtn.click();
    const s1Input = container.querySelector<HTMLInputElement>('input.pe-seg-split[data-seg-id="s1"]')!;
    const s2Input = container.querySelector<HTMLInputElement>('input.pe-seg-split[data-seg-id="s2"]')!;
    expect(s1Input.value).toBe("4200");          // cum gold through s1
    expect(s2Input.value).toBe(String(4200 + 5800));  // cum gold through s2
  });
});

describe("buildEvaluateRequest", () => {
  it("packages no_reset request without policy_kwargs", () => {
    const req = buildEvaluateRequest({
      policy: "no_reset",
      cumSplits: {},
      slack: 0,
      objective: "expected_wall_clock_per_attempt",
      objectiveCtx: {},
    });
    expect(req.policy).toBe("no_reset");
    expect(req.objective).toBe("expected_wall_clock_per_attempt");
    expect(req.objective_ctx).toEqual({});
  });

  it("packages target_paced request with cum_splits + slack", () => {
    const req = buildEvaluateRequest({
      policy: "target_paced",
      cumSplits: { s1: 6000, s2: 12000 },
      slack: 0.15,
      objective: "q",
      objectiveCtx: { target_ms: 11000 },
    });
    expect(req.policy).toBe("target_paced");
    expect(req.policy_kwargs).toEqual({
      cum_splits_ms: { s1: 6000, s2: 12000 },
      slack: 0.15,
    });
    expect(req.objective_ctx).toEqual({ target_ms: 11000 });
  });
});

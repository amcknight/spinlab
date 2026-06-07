import { describe, it, expect, beforeEach } from "vitest";
import { renderPracticeEnginePanel, updatePanelResults, buildEvaluateRequest } from "./practice-engine";
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

  it("renders the practice-next list container and the status line", () => {
    const container = document.getElementById("practice-engine-panel")!;
    renderPracticeEnginePanel(container, MOCK_STATE);
    expect(container.querySelector("#pe-rank-body")).not.toBeNull();
    expect(container.querySelector(".pe-status")?.textContent ?? "").toContain("ready");
  });

  it("collapses policy/objective controls behind an Advanced details (closed)", () => {
    const container = document.getElementById("practice-engine-panel")!;
    renderPracticeEnginePanel(container, MOCK_STATE);
    const adv = container.querySelector<HTMLDetailsElement>("details.pe-advanced");
    expect(adv).not.toBeNull();
    expect(adv!.open).toBe(false);
    // Controls live inside Advanced, not at top level.
    expect(adv!.querySelector("#pe-policy")).not.toBeNull();
    expect(adv!.querySelector("#pe-objective")).not.toBeNull();
  });

  it("shows not-ready segments inline in the list (not a separate block)", () => {
    const stateWithUngated: PracticeEngineState = {
      ...MOCK_STATE,
      ungated_segments: [{
        seg_id: "s3", reason: "needs more data", description: "", level_number: 3,
        start_type: "entrance", start_ordinal: 0, end_type: "goal", end_ordinal: 0,
      }],
    };
    const container = document.getElementById("practice-engine-panel")!;
    renderPracticeEnginePanel(container, stateWithUngated);
    const notReady = container.querySelector("#pe-notready");
    expect(notReady).not.toBeNull();
    expect(notReady!.textContent).toContain("needs more data");
    expect(container.querySelector(".pe-ungated")).toBeNull(); // old separate block gone
  });

  it("fill-from-gold (inside Advanced) populates cumulative splits", () => {
    const container = document.getElementById("practice-engine-panel")!;
    renderPracticeEnginePanel(container, MOCK_STATE);
    container.querySelector<HTMLButtonElement>("#pe-fill-gold")!.click();
    const s1 = container.querySelector<HTMLInputElement>('input.pe-seg-split[data-seg-id="s1"]')!;
    const s2 = container.querySelector<HTMLInputElement>('input.pe-seg-split[data-seg-id="s2"]')!;
    expect(s1.value).toBe("4200");
    expect(s2.value).toBe(String(4200 + 5800));
  });

  it("shows the no-active-run empty state when hasActiveRun is false", () => {
    const container = document.getElementById("practice-engine-panel")!;
    // Even with segments present, no active run wins the empty state.
    renderPracticeEnginePanel(container, MOCK_STATE, false);
    const status = container.querySelector(".pe-status")?.textContent ?? "";
    expect(status).toContain("No reference run selected");
    expect(status).not.toContain("ready");
  });

  it("with an active run but zero tracked segments shows the no-segments copy", () => {
    const empty: PracticeEngineState = { ...MOCK_STATE, gated_segments: [], ungated_segments: [] };
    const container = document.getElementById("practice-engine-panel")!;
    renderPracticeEnginePanel(container, empty, true);
    const status = container.querySelector(".pe-status")?.textContent ?? "";
    expect(status).toContain("No segments tracked yet");
    expect(status).not.toContain("No reference run selected");
  });
});

describe("updatePanelResults", () => {
  beforeEach(() => {
    document.body.innerHTML = `<div id="practice-engine-panel"></div>`;
  });

  it("renders a ranked practice-next list with plain payoff, no scientific notation", () => {
    const container = document.getElementById("practice-engine-panel")!;
    renderPracticeEnginePanel(container, MOCK_STATE);
    updatePanelResults(container, MOCK_EVAL);
    const rows = container.querySelectorAll("#pe-rank-body li");
    expect(rows.length).toBe(2);
    const text = container.querySelector("#pe-rank-body")!.textContent || "";
    expect(text).not.toMatch(/e[+-]\d/i);       // no 5.71e-2
    expect(text).not.toContain("undefined");
    // s1 (value_per_second 200/4500 ≈ 0.0444) ranks above s2 (150/6000 = 0.025).
    expect(rows[0]!.textContent).toContain("Level 1");
    expect(rows[1]!.textContent).toContain("Level 2");
    // now → after is rendered (guards the times field / arrow).
    expect(rows[0]!.querySelector(".pe-rank-times")?.textContent).toMatch(/→/);
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

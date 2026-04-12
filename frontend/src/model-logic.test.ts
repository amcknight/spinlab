import { describe, it, expect, test } from "vitest";
import { selectedEstimate, currentEstimate, formatTrend, canStartPractice, canStartSpeedRun } from "./model-logic";
import type { ModelSegment, CurrentSegment, Estimate, AppState } from "./types";

const ESTIMATE: Estimate = {
  expected_ms: 5000,
  ms_per_attempt: -12.3,
  floor_ms: 3000,
};

const MODEL_OUTPUT = { total: ESTIMATE, clean: { expected_ms: null, ms_per_attempt: null, floor_ms: null } };

describe("selectedEstimate", () => {
  it("returns total estimate for selected model", () => {
    const seg: ModelSegment = {
      segment_id: "s1",
      description: "test",
      level_number: 1,
      start_type: "entrance",
      start_ordinal: 0,
      end_type: "goal",
      end_ordinal: 0,
      selected_model: "kalman",
      model_outputs: { kalman: MODEL_OUTPUT },
      n_completed: 5,
      n_attempts: 10,
      gold_ms: 2000,
      clean_gold_ms: null,
    };
    expect(selectedEstimate(seg)).toEqual(ESTIMATE);
  });

  it("returns null when selected model has no output", () => {
    const seg: ModelSegment = {
      segment_id: "s1",
      description: "test",
      level_number: 1,
      start_type: "entrance",
      start_ordinal: 0,
      end_type: "goal",
      end_ordinal: 0,
      selected_model: "kalman",
      model_outputs: {},
      n_completed: 0,
      n_attempts: 0,
      gold_ms: null,
      clean_gold_ms: null,
    };
    expect(selectedEstimate(seg)).toBeNull();
  });
});

describe("formatTrend", () => {
  it("formats negative trend", () => {
    expect(formatTrend(ESTIMATE)).toBe("-12.3 ms/att");
  });

  it("returns null for null estimate", () => {
    expect(formatTrend(null)).toBeNull();
  });

  it("returns null when ms_per_attempt is null", () => {
    expect(formatTrend({ expected_ms: 1000, ms_per_attempt: null, floor_ms: null })).toBeNull();
  });
});

describe("canStartPractice", () => {
  const BASE_STATE: AppState = {
    mode: "idle",
    tcp_connected: true,
    game_id: "game1",
    game_name: "Test Game",
    current_segment: null,
    recent: [],
    session: null,
    sections_captured: 0,
    allocator_weights: null,
    estimator: null,
    capture_run_id: null,
    draft: null,
    replay: null,
    cold_fill: null,
  };

  it("returns true when idle, connected, and game loaded", () => {
    expect(canStartPractice(BASE_STATE)).toBe(true);
  });

  it("returns false when not connected", () => {
    expect(canStartPractice({ ...BASE_STATE, tcp_connected: false })).toBe(false);
  });

  it("returns false when no game loaded", () => {
    expect(canStartPractice({ ...BASE_STATE, game_id: null })).toBe(false);
  });

  it("returns false when already practicing", () => {
    expect(canStartPractice({ ...BASE_STATE, mode: "practice" })).toBe(false);
  });
});

describe("selectedEstimate edge cases", () => {
  it("handles segment with multiple estimators", () => {
    const seg: ModelSegment = {
      segment_id: "s1",
      description: "",
      level_number: 1,
      start_type: "entrance",
      start_ordinal: 0,
      end_type: "goal",
      end_ordinal: 0,
      selected_model: "rolling_mean",
      model_outputs: {
        kalman: {
          total: { expected_ms: 5000, ms_per_attempt: -10, floor_ms: 3000 },
          clean: { expected_ms: null, ms_per_attempt: null, floor_ms: null },
        },
        rolling_mean: {
          total: { expected_ms: 6000, ms_per_attempt: -5, floor_ms: 4000 },
          clean: { expected_ms: null, ms_per_attempt: null, floor_ms: null },
        },
      },
      n_completed: 10,
      n_attempts: 20,
      gold_ms: 2500,
      clean_gold_ms: null,
    };
    const est = selectedEstimate(seg);
    // Should return rolling_mean's total, not kalman's
    expect(est?.expected_ms).toBe(6000);
  });

  it("handles segment with all-null estimates", () => {
    const seg: ModelSegment = {
      segment_id: "s1",
      description: "",
      level_number: 1,
      start_type: "entrance",
      start_ordinal: 0,
      end_type: "goal",
      end_ordinal: 0,
      selected_model: "kalman",
      model_outputs: {
        kalman: {
          total: { expected_ms: null, ms_per_attempt: null, floor_ms: null },
          clean: { expected_ms: null, ms_per_attempt: null, floor_ms: null },
        },
      },
      n_completed: 0,
      n_attempts: 0,
      gold_ms: null,
      clean_gold_ms: null,
    };
    const est = selectedEstimate(seg);
    expect(est).not.toBeNull();
    expect(est!.expected_ms).toBeNull();
  });
});

test("canStartSpeedRun returns true when idle and connected", () => {
  const state = {
    mode: "idle" as const,
    tcp_connected: true,
    game_id: "g",
    game_name: "Game",
    current_segment: null,
    recent: [],
    session: null,
    sections_captured: 0,
    allocator_weights: null,
    estimator: null,
    capture_run_id: null,
    draft: null,
    replay: null,
    cold_fill: null,
  };
  expect(canStartSpeedRun(state)).toBe(true);
});

test("canStartSpeedRun returns false during practice", () => {
  const state = {
    mode: "practice" as const,
    tcp_connected: true,
    game_id: "g",
    game_name: "Game",
    current_segment: null,
    recent: [],
    session: null,
    sections_captured: 0,
    allocator_weights: null,
    estimator: null,
    capture_run_id: null,
    draft: null,
    replay: null,
    cold_fill: null,
  };
  expect(canStartSpeedRun(state)).toBe(false);
});

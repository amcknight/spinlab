import { describe, it, expect } from "vitest";
import { selectedEstimate, currentEstimate, formatTrend, canStartPractice } from "./model-logic";
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

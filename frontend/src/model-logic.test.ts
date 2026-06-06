import { describe, it, expect, test } from "vitest";
import { selectedEstimate, currentEstimate, formatTrend, canStartPractice, canStartHyperPlay } from "./model-logic";
import { practiceCardState } from "./model-logic";
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
      selected_model: "em_suite_sampler",
      model_outputs: { em_suite_sampler: MODEL_OUTPUT },
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
      selected_model: "em_suite_sampler",
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
    emu_connected: true,
    game_id: "game1",
    game_name: "Test Game",
    current_segment: null,
    recent: [],
    session: null,
    sections_captured: 0,
    allocator_weights: null,
    estimator: null,
    capture_run_id: null,
    paused_run: null,
    replay: null,
    cold_fill: null,
    has_active_run: false,
    segments_missing_cold: 0,
    has_frozen_session: false,
  };

  it("returns true when idle, connected, and game loaded", () => {
    expect(canStartPractice(BASE_STATE)).toBe(true);
  });

  it("returns false when not connected", () => {
    expect(canStartPractice({ ...BASE_STATE, emu_connected: false })).toBe(false);
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
      selected_model: "model_b",
      model_outputs: {
        model_a: {
          total: { expected_ms: 5000, ms_per_attempt: -10, floor_ms: 3000 },
          clean: { expected_ms: null, ms_per_attempt: null, floor_ms: null },
        },
        model_b: {
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
    // Should return model_b's total (the selected model), not model_a's.
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
      selected_model: "em_suite_sampler",
      model_outputs: {
        em_suite_sampler: {
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

test("canStartHyperPlay returns true when idle and connected", () => {
  const state = {
    mode: "idle" as const,
    emu_connected: true,
    game_id: "g",
    game_name: "Game",
    current_segment: null,
    recent: [],
    session: null,
    sections_captured: 0,
    allocator_weights: null,
    estimator: null,
    capture_run_id: null,
    paused_run: null,
    replay: null,
    cold_fill: null,
    has_active_run: false,
    segments_missing_cold: 0,
    has_frozen_session: false,
  };
  expect(canStartHyperPlay(state)).toBe(true);
});

test("canStartHyperPlay returns false during practice", () => {
  const state = {
    mode: "practice" as const,
    emu_connected: true,
    game_id: "g",
    game_name: "Game",
    current_segment: null,
    recent: [],
    session: null,
    sections_captured: 0,
    allocator_weights: null,
    estimator: null,
    capture_run_id: null,
    paused_run: null,
    replay: null,
    cold_fill: null,
    has_active_run: false,
    segments_missing_cold: 0,
    has_frozen_session: false,
  };
  expect(canStartHyperPlay(state)).toBe(false);
});

describe("practiceCardState", () => {
  const args = (over: Partial<Parameters<typeof practiceCardState>[0]>) => ({
    mode: "idle", hasCurrentSegment: false, hasFrozenSession: false,
    hasLastPracticed: false, hasGameId: true, ...over,
  });

  it("is live while practicing with a current segment", () => {
    expect(practiceCardState(args({ mode: "practice", hasCurrentSegment: true }))).toBe("live");
  });
  it("is live during hyper_play with a current segment", () => {
    expect(practiceCardState(args({ mode: "hyper_play", hasCurrentSegment: true }))).toBe("live");
  });
  it("is hidden while practicing with no current segment yet", () => {
    expect(practiceCardState(args({ mode: "practice", hasCurrentSegment: false }))).toBe("hidden");
  });
  it("is frozen when idle with a frozen session and a remembered segment", () => {
    expect(practiceCardState(args({
      mode: "idle", hasFrozenSession: true, hasLastPracticed: true, hasGameId: true,
    }))).toBe("frozen");
  });
  it("is hidden when frozen-session exists but no remembered segment (e.g. fresh reload)", () => {
    expect(practiceCardState(args({
      mode: "idle", hasFrozenSession: true, hasLastPracticed: false,
    }))).toBe("hidden");
  });
  it("is hidden when idle with no frozen session", () => {
    expect(practiceCardState(args({ mode: "idle", hasFrozenSession: false }))).toBe("hidden");
  });
  it("is hidden when the frozen session was cleared (e.g. game switch) despite a remembered segment", () => {
    expect(practiceCardState(args({
      mode: "idle", hasFrozenSession: false, hasLastPracticed: true,
    }))).toBe("hidden");
  });
  it("is hidden when frozen + remembered but no game is loaded", () => {
    expect(practiceCardState(args({
      mode: "idle", hasFrozenSession: true, hasLastPracticed: true, hasGameId: false,
    }))).toBe("hidden");
  });
});

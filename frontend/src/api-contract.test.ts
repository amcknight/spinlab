import { describe, it, expect } from "vitest";
import type {
  AppState,
  ModelData,
  TuningData,
  Reference,
  ReferenceSegment,
} from "./types";

/**
 * Fixture snapshots captured from real API responses.
 * If these stop compiling, the API contract has drifted.
 */

const IDLE_STATE: AppState = {
  mode: "idle",
  tcp_connected: true,
  game_id: "smw-kaizo",
  game_name: "Kaizo Mario World",
  current_segment: null,
  recent: [],
  session: null,
  sections_captured: 0,
  allocator_weights: { greedy: 60, random: 20, round_robin: 20 },
  estimator: "kalman",
  capture_run_id: null,
  draft: null,
  cold_fill: null,
};

const PRACTICE_STATE: AppState = {
  mode: "practice",
  tcp_connected: true,
  game_id: "smw-kaizo",
  game_name: "Kaizo Mario World",
  current_segment: {
    id: "seg-001",
    game_id: "smw-kaizo",
    level_number: 3,
    start_type: "entrance",
    start_ordinal: 0,
    end_type: "checkpoint",
    end_ordinal: 1,
    description: "Iggy approach",
    attempt_count: 14,
    model_outputs: {
      kalman: {
        total: { expected_ms: 8500, ms_per_attempt: -45.2, floor_ms: 6200 },
        clean: { expected_ms: 7100, ms_per_attempt: -30.1, floor_ms: 5800 },
      },
    },
    selected_model: "kalman",
    state_path: "/data/states/seg-001.state",
  },
  recent: [
    {
      id: 1,
      segment_id: "seg-001",
      completed: 1,
      time_ms: 8200,
      description: "Iggy approach",
      level_number: 3,
      start_type: "entrance",
      start_ordinal: 0,
      end_type: "checkpoint",
      end_ordinal: 1,
    },
  ],
  session: {
    id: "sess-abc",
    started_at: "2026-04-04T10:00:00Z",
    segments_attempted: 14,
    segments_completed: 3,
  },
  sections_captured: 0,
  allocator_weights: { greedy: 60, random: 20, round_robin: 20 },
  estimator: "kalman",
  capture_run_id: null,
  draft: null,
  cold_fill: null,
};

const MODEL_RESPONSE: ModelData = {
  estimator: "kalman",
  estimators: [
    { name: "kalman", display_name: "Kalman Filter" },
    { name: "rolling_mean", display_name: "Rolling Mean" },
  ],
  allocator_weights: { greedy: 60, random: 20, round_robin: 20 },
  segments: [
    {
      segment_id: "seg-001",
      description: "Iggy approach",
      level_number: 3,
      start_type: "entrance",
      start_ordinal: 0,
      end_type: "checkpoint",
      end_ordinal: 1,
      selected_model: "kalman",
      model_outputs: {
        kalman: {
          total: { expected_ms: 8500, ms_per_attempt: -45.2, floor_ms: 6200 },
          clean: { expected_ms: 7100, ms_per_attempt: -30.1, floor_ms: 5800 },
        },
      },
      n_completed: 3,
      n_attempts: 14,
      gold_ms: 7800,
      clean_gold_ms: 6500,
    },
  ],
};

describe("API contract fixtures", () => {
  it("idle state fixture type-checks", () => {
    expect(IDLE_STATE.mode).toBe("idle");
    expect(IDLE_STATE.current_segment).toBeNull();
  });

  it("practice state has correct nested model_output structure", () => {
    const seg = PRACTICE_STATE.current_segment!;
    const output = seg.model_outputs[seg.selected_model]!;
    // This is the bug the migration fixed — old JS accessed output.expected_time_ms
    // which doesn't exist. The correct path is output.total.expected_ms.
    expect(output.total.expected_ms).toBe(8500);
    expect(output.total.ms_per_attempt).toBe(-45.2);
    expect(output.total.floor_ms).toBe(6200);
  });

  it("model response segments have nested Estimate structure", () => {
    const seg = MODEL_RESPONSE.segments[0]!;
    const output = seg.model_outputs[seg.selected_model]!;
    expect(output.total.expected_ms).toBe(8500);
    expect(output.clean.expected_ms).toBe(7100);
  });
});

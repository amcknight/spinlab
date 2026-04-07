import { describe, it, expect, vi, beforeEach } from "vitest";
import type { SegmentHistory } from "./types";

const MOCK_HISTORY: SegmentHistory = {
  segment_id: "s1",
  description: "Yoshi's Island 1",
  attempts: [
    { attempt_number: 1, time_ms: 4500, clean_tail_ms: 4500, deaths: 0, created_at: "2026-04-01T12:00:00Z" },
    { attempt_number: 2, time_ms: 3800, clean_tail_ms: 3200, deaths: 0, created_at: "2026-04-01T12:05:00Z" },
    { attempt_number: 3, time_ms: 3200, clean_tail_ms: 3200, deaths: 0, created_at: "2026-04-01T12:10:00Z" },
  ],
  estimator_curves: {
    kalman: {
      total: { expected_ms: [4500, 4150, 3700], floor_ms: [null, null, null] },
      clean: { expected_ms: [4500, 3850, 3500], floor_ms: [null, null, null] },
    },
    rolling_mean: {
      total: { expected_ms: [4500, 4150, 3833], floor_ms: [null, null, null] },
      clean: { expected_ms: [4500, 3850, 3633], floor_ms: [null, null, null] },
    },
  },
};

// Mock Chart.js — we can't test canvas rendering in happy-dom,
// but we can verify the component builds datasets correctly.
vi.mock("chart.js", () => ({
  Chart: class {
    data: unknown;
    static register() {}
    constructor(_ctx: unknown, config: { data: unknown }) { this.data = config.data; }
    destroy() {}
    update() {}
  },
  LineController: class {},
  LineElement: class {},
  PointElement: class {},
  LinearScale: class {},
  CategoryScale: class {},
  Legend: class {},
  Tooltip: class {},
}));

import { buildChartDatasets } from "./segment-detail";

describe("buildChartDatasets", () => {
  it("builds total datasets from history data", () => {
    const datasets = buildChartDatasets(MOCK_HISTORY, "total");
    // 1 for raw attempts + 1 per estimator
    expect(datasets).toHaveLength(3);
    // First dataset is the raw attempts
    expect(datasets[0]!.label).toBe("Attempts");
    expect(datasets[0]!.data).toEqual([4.5, 3.8, 3.2]);
  });

  it("builds clean datasets from history data", () => {
    const datasets = buildChartDatasets(MOCK_HISTORY, "clean");
    expect(datasets[0]!.label).toBe("Attempts");
    // clean_tail_ms values converted to seconds
    expect(datasets[0]!.data).toEqual([4.5, 3.2, 3.2]);
  });

  it("labels match attempt numbers", () => {
    const datasets = buildChartDatasets(MOCK_HISTORY, "total");
    // All datasets should have same length as attempts
    for (const ds of datasets) {
      expect(ds.data).toHaveLength(3);
    }
  });
});

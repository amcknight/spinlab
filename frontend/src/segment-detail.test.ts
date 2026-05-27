import { describe, it, expect, vi, beforeEach } from "vitest";
import type { SegmentHistory } from "./types";

const MOCK_HISTORY: SegmentHistory = {
  segment_id: "s1",
  description: "Yoshi's Island 1",
  level_number: 1,
  start_type: "entrance",
  start_ordinal: 0,
  end_type: "goal",
  end_ordinal: 0,
  selected_model: "kalman",
  attempts: [
    { attempt_number: 1, time_ms: 4500, clean_tail_ms: 4500, deaths: 0, created_at: "2026-04-01T12:00:00Z" },
    { attempt_number: 2, time_ms: 3800, clean_tail_ms: 3200, deaths: 0, created_at: "2026-04-01T12:05:00Z" },
    { attempt_number: 3, time_ms: 3200, clean_tail_ms: 3200, deaths: 0, created_at: "2026-04-01T12:10:00Z" },
  ],
  estimator_curves: {
    kalman: {
      total: { expected_ms: [4500, 4150, 3700], floor_ms: [null, null, null] },
      clean: { expected_ms: [4500, 3850, 3500], floor_ms: [null, null, null] },
      final_extras: null,
    },
    rolling_mean: {
      total: { expected_ms: [4500, 4150, 3833], floor_ms: [null, null, null] },
      clean: { expected_ms: [4500, 3850, 3633], floor_ms: [null, null, null] },
      final_extras: null,
    },
  },
};

// Mock Chart.js — we can't test canvas rendering in happy-dom,
// but we can verify the component builds datasets correctly.
// The Bar* exports are needed because segment-detail imports
// death-distribution, which registers BarController/BarElement.
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
  BarController: class {},
  BarElement: class {},
  LinearScale: class {},
  CategoryScale: class {},
  Legend: class {},
  Tooltip: class {},
}));

import { buildChartDatasets, renderSegmentDetail, destroySegmentDetail } from "./segment-detail";
import type { ColdDistribution } from "./types";

const MOCK_DIST: ColdDistribution = {
  bins: [
    { lo_ms: 0, hi_ms: 1000, n_deaths: 3, n_completions: 1, hazard: 0.75, at_risk_w: 4 },
    { lo_ms: 1000, hi_ms: 2000, n_deaths: 1, n_completions: 2, hazard: 0.33, at_risk_w: 3 },
  ],
  n_cold_attempts: 7,
  mu_d_ms: 500,
  mu_c_ms: 1500,
  p_die_per_attempt: 0.57,
  p_die_per_life: 0.57,
  halflife: 0,
};

const MOCK_HISTORY_WITH_DIST: SegmentHistory = {
  ...MOCK_HISTORY,
  cold_distribution: MOCK_DIST,
};

const MOCK_HISTORY_NO_DIST: SegmentHistory = {
  ...MOCK_HISTORY,
  cold_distribution: null,
};

function mockFetch(history: SegmentHistory) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => history,
    }),
  );
}

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

describe("Cold distribution toggle", () => {
  beforeEach(() => {
    destroySegmentDetail();
  });

  it("renders Histogram by default and switches to Hazard on click", async () => {
    mockFetch(MOCK_HISTORY_WITH_DIST);
    const container = document.createElement("div");
    await renderSegmentDetail(container, "s1", () => {});

    // Histogram tab is active by default
    const histBtn = container.querySelector<HTMLButtonElement>(".cold-tab[data-chart='histogram']");
    const hazBtn = container.querySelector<HTMLButtonElement>(".cold-tab[data-chart='hazard']");
    expect(histBtn).not.toBeNull();
    expect(hazBtn).not.toBeNull();
    expect(histBtn!.classList.contains("active")).toBe(true);
    expect(hazBtn!.classList.contains("active")).toBe(false);

    // Hazard button is enabled when cold_distribution is present
    expect(hazBtn!.disabled).toBe(false);

    // Click Hazard
    hazBtn!.click();
    expect(hazBtn!.classList.contains("active")).toBe(true);
    expect(histBtn!.classList.contains("active")).toBe(false);

    // Click Histogram again to revert
    histBtn!.click();
    expect(histBtn!.classList.contains("active")).toBe(true);
    expect(hazBtn!.classList.contains("active")).toBe(false);
  });

  it("disables Hazard tab when cold_distribution is null", async () => {
    mockFetch(MOCK_HISTORY_NO_DIST);
    const container = document.createElement("div");
    await renderSegmentDetail(container, "s1", () => {});

    const hazBtn = container.querySelector<HTMLButtonElement>(".cold-tab[data-chart='hazard']");
    expect(hazBtn).not.toBeNull();
    expect(hazBtn!.disabled).toBe(true);

    // Empty state message should still appear
    const emptyMsg = container.querySelector(".death-distribution .dim");
    expect(emptyMsg).not.toBeNull();
    expect(emptyMsg!.textContent).toMatch(/No cold data/);
  });
});

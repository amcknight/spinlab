import { describe, it, expect } from "vitest";
import {
  yForTime, linePoints, axisTicks, niceTimeTicks, deathLabels, renderEpisodeGraph,
} from "./episode-graph";
import type { EpisodePoint, LiveSegmentView } from "./types";

const PTS: EpisodePoint[] = [
  { episode_ms: 31000, deaths: 4, clean_ms: 14200, running_floor_ms: 14200 },
  { episode_ms: 24000, deaths: 2, clean_ms: 13800, running_floor_ms: 13800 },
  { episode_ms: 16800, deaths: 1, clean_ms: 12800, running_floor_ms: 12800 },
];

const GEO = { left: 30, right: 392, top: 10, bottom: 104 };

describe("yForTime", () => {
  it("maps lo time to the bottom and hi time to the top", () => {
    expect(yForTime(12800, 12800, 31000, GEO.top, GEO.bottom)).toBeCloseTo(GEO.bottom, 1);
    expect(yForTime(31000, 12800, 31000, GEO.top, GEO.bottom)).toBeCloseTo(GEO.top, 1);
  });
  it("is NaN-safe when lo == hi (single distinct value)", () => {
    const y = yForTime(5000, 5000, 5000, GEO.top, GEO.bottom);
    expect(Number.isNaN(y)).toBe(false);
  });
});

describe("linePoints", () => {
  it("builds one x,y pair per point spanning the plot width", () => {
    const pts = linePoints(PTS.map(p => p.episode_ms), 12800, 31000, GEO);
    const pairs = pts.trim().split(" ");
    expect(pairs.length).toBe(3);
    expect(pts).not.toContain("NaN");
    expect(pairs[0]!.startsWith(String(GEO.left))).toBe(true);
    expect(pairs[2]!.startsWith(String(GEO.right))).toBe(true);
  });
  it("skips null values (floor line may have gaps) without NaN", () => {
    const pts = linePoints([14200, null, 12800], 12800, 31000, GEO);
    expect(pts).not.toContain("NaN");
    expect(pts.trim().split(" ").length).toBe(2);
  });
});

describe("axisTicks", () => {
  it("returns round-number labeled tick values within [lo, hi]", () => {
    const ticks = axisTicks(12800, 31000, 4);
    expect(ticks.length).toBeGreaterThanOrEqual(1);
    for (const t of ticks) {
      expect(t.ms).toBeGreaterThanOrEqual(12800);
      expect(t.ms).toBeLessThanOrEqual(31000);
      expect(t.label).toMatch(/s$/);
    }
  });
});

describe("niceTimeTicks", () => {
  it("returns round-number tick values within range, count >= 3", () => {
    const ticks = niceTimeTicks(39100, 400100, 4);
    expect(ticks.length).toBeGreaterThanOrEqual(3);
    // All ticks share a single nice step → every tick is a multiple of it.
    const step = ticks[1]! - ticks[0]!;
    for (const t of ticks) {
      expect(t % step).toBeCloseTo(0, 6);
      expect(t).toBeGreaterThanOrEqual(39100);
      expect(t).toBeLessThanOrEqual(400100);
    }
  });
  it("returns a single tick when range is non-positive", () => {
    expect(niceTimeTicks(5000, 5000, 4)).toEqual([5000]);
  });
});

describe("deathLabels", () => {
  it("emits one label per point with its death count and x position", () => {
    const labels = deathLabels(PTS, GEO);
    expect(labels.length).toBe(3);
    expect(labels[0]!.deaths).toBe(4);
    expect(labels[2]!.x).toBeCloseTo(GEO.right, 1);
  });
});

describe("renderEpisodeGraph", () => {
  const READY: LiveSegmentView = {
    segment_id: "s1", ready: true, expected_episode_ms: 21800,
    practice_gain_ms: 500, death_rate: 0.62, floor_ms: 12800,
    last_episode_ms: 16800, last_clean_ms: 13600, last_deaths: 1, last_rank: 2,
    series: PTS as unknown as Record<string, never>[],
    n_successes: 6, n_deaths: 5,
  };

  it("renders an svg with episode + floor polylines and no NaN/undefined", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderEpisodeGraph(host, READY);
    const svg = host.querySelector("svg");
    expect(svg).not.toBeNull();
    expect(host.querySelectorAll("polyline").length).toBe(2);
    const html = host.innerHTML;
    expect(html).not.toContain("NaN");
    expect(html).not.toContain("undefined");
    expect(html).toContain("floor");
  });

  it("omits the death label for a 0-death completion", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    const pts: EpisodePoint[] = [
      { episode_ms: 31000, deaths: 4, clean_ms: 14200, running_floor_ms: 14200 },
      { episode_ms: 14000, deaths: 0, clean_ms: 14000, running_floor_ms: 14000 },
      { episode_ms: 16800, deaths: 1, clean_ms: 12800, running_floor_ms: 12800 },
    ];
    renderEpisodeGraph(host, { ...READY, series: pts as unknown as Record<string, never>[] });
    const deathEls = host.querySelectorAll(".eg-death");
    // 3 completions but one has 0 deaths → only 2 labels.
    expect(deathEls.length).toBe(2);
    for (const el of deathEls) expect(el.textContent).not.toBe("0");
  });

  it("renders a placeholder (no svg) when not ready", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderEpisodeGraph(host, { ...READY, ready: false, series: [], floor_ms: null });
    expect(host.querySelector("svg")).toBeNull();
    expect((host.textContent || "").toLowerCase()).toContain("not enough");
  });

  it("renders a placeholder when ready but no completed episodes", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderEpisodeGraph(host, { ...READY, series: [], floor_ms: null });
    expect(host.querySelector("svg")).toBeNull();
  });
});

import { describe, it, expect } from "vitest";
import { renderRouteBar, formatRate, type RouteBarData } from "./route-bar";

const NOW_S = 1717_000_000.0;

const SESSION: RouteBarData = {
  title: "Beto · any%",
  gameId: "g0",
  routeSummary: {
    game_id: "g0",
    exp_run_ms: 115_000.0, exp_deaths: 3.5,
    n_estimable: 8, n_skipped: 0,
    session_started_at: NOW_S - 3600,  // 1h ago
    exp_run_diff_ms: -5_000.0,
    exp_deaths_diff: -0.5,
    practice_saved_ms: 5_000.0,
    floor_improvement_ms: 1_500.0,
    run_series: [], baseline_exp_run_ms: null, floor_total_ms: null,
    session_ended_at: null,
  },
  nowSeconds: NOW_S,
};

describe("formatRate", () => {
  it("ms-per-hour computed from saved + elapsed seconds", () => {
    // 5000 ms saved / 1.0 hr = 5000 ms/hr → '5.0s/hr'
    expect(formatRate(5000, 3600)).toBe("5.0s/hr");
  });
  it("returns '—' for null or zero elapsed", () => {
    expect(formatRate(null, 3600)).toBe("—");
    expect(formatRate(5000, 0)).toBe("—");
  });
});

describe("renderRouteBar", () => {
  it("renders title + practice-saved with rate + duration", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderRouteBar(host, SESSION);
    expect(host.querySelector(".rb-title")!.textContent).toContain("Beto");
    const saved = host.querySelector(".rb-saved")!;
    expect(saved.textContent).toContain("Saved 5.0s");
    expect(saved.textContent).toMatch(/1:00:00|01:00:00/);  // session elapsed
  });
  it("tints Saved + rate green for a positive (ahead) session", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderRouteBar(host, SESSION);  // practice_saved_ms = +5000
    expect(host.querySelectorAll(".rb-saved-good").length).toBeGreaterThanOrEqual(1);
    expect(host.querySelector(".rb-saved-bad")).toBeNull();
  });
  it("tints Saved + rate red for a negative (behind) session", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderRouteBar(host, { ...SESSION, routeSummary: { ...SESSION.routeSummary, practice_saved_ms: -3000 } });
    expect(host.querySelectorAll(".rb-saved-bad").length).toBeGreaterThanOrEqual(1);
    expect(host.querySelector(".rb-saved-good")).toBeNull();
  });
  it("renders Exp. Run + Exp. Deaths stat columns with colored diffs", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderRouteBar(host, SESSION);
    const stacks = host.querySelectorAll(".ss-stack");
    expect(stacks.length).toBeGreaterThanOrEqual(2);  // Exp. Run + Exp. Deaths (Floors only when non-zero)
    // -5s on exp_run → improvement → 'good'
    const goods = host.querySelectorAll(".ss-diff.good");
    expect(goods.length).toBeGreaterThanOrEqual(1);
  });
  it("renders Floors column only when floor_improvement_ms > 0", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderRouteBar(host, { ...SESSION, routeSummary: { ...SESSION.routeSummary, floor_improvement_ms: 0 } });
    expect(host.querySelector(".rb-floors")).toBeNull();
  });
  it("labels the floor-improvement stat 'Floor saved' when floor_improvement_ms > 0", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderRouteBar(host, SESSION);  // floor_improvement_ms = 1500
    const floors = host.querySelector(".rb-floors")!;
    expect(floors).not.toBeNull();
    expect(floors.querySelector(".ss-label")!.textContent).toBe("Floor saved");
  });
  it("renders 'n of m segments estimable' when n_skipped > 0", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderRouteBar(host, { ...SESSION, routeSummary: { ...SESSION.routeSummary, n_skipped: 4 } });
    expect((host.textContent ?? "").toLowerCase()).toContain("estimable");
  });
  it("shows a (frozen) badge when the session is frozen", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderRouteBar(host, {
      title: "Beto · any%", gameId: "g", nowSeconds: 1060,
      routeSummary: { ...SESSION.routeSummary, session_started_at: 1000, session_ended_at: 1060 },
    });
    expect(host.querySelector(".rb-frozen")).not.toBeNull();
    expect(host.textContent).toContain("(frozen)");
    // Elapsed is pinned by caller-supplied nowSeconds (= ended_at): 60s.
    expect(host.textContent).toContain("0:01:00");
  });
  it("shows no (frozen) badge for a live session", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderRouteBar(host, {
      title: "Beto · any%", gameId: "g", nowSeconds: 1030,
      routeSummary: { ...SESSION.routeSummary, session_started_at: 1000, session_ended_at: null },
    });
    expect(host.querySelector(".rb-frozen")).toBeNull();
  });
  it("hides Practice saved when no active session", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderRouteBar(host, { ...SESSION, routeSummary: {
      ...SESSION.routeSummary,
      session_started_at: null, practice_saved_ms: null,
      exp_run_diff_ms: null, exp_deaths_diff: null, floor_improvement_ms: null,
    } });
    expect(host.querySelector(".rb-saved")).toBeNull();
  });
});

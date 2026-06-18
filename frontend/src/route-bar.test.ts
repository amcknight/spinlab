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
    floor_series: [],
    session_ended_at: null,
    menu_armed: false,
    session_paused_at: null,
    session_pause_offset_sec: 0,
    experimental: false,
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
  it("pins elapsed at ended_at when frozen; status badges now live in the mode chip", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderRouteBar(host, {
      title: "Beto · any%", gameId: "g", nowSeconds: 1060,
      routeSummary: { ...SESSION.routeSummary, session_started_at: 1000,
        session_ended_at: 1060, experimental: true },
    });
    // The frozen clock-pin is unchanged; "(frozen)" + the status badges
    // (PAUSED / Science / Grinding) were retired into the top-right mode chip.
    expect(host.querySelector(".rb-frozen")).toBeNull();
    expect(host.querySelector(".rb-science")).toBeNull();
    expect(host.textContent).toContain("0:01:00");  // elapsed pinned to ended_at
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

describe("route bar pause + menu", () => {
  function dataWith(overrides: Partial<RouteBarData["routeSummary"]>, nowSeconds = NOW_S): RouteBarData {
    return { ...SESSION, nowSeconds, routeSummary: { ...SESSION.routeSummary, ...overrides } };
  }

  it("freezes elapsed at the pause-start while paused", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    // started_at = NOW_S - 3600 (1h ago); paused_at = NOW_S - 100 => frozen elapsed 100s,
    // regardless of how far nowSeconds advances.
    renderRouteBar(host, dataWith(
      { session_paused_at: NOW_S - 100 },
      NOW_S + 9999,  // clock advanced; must NOT affect the frozen elapsed
    ));
    // (PAUSED badge moved to the mode chip; the clock-freeze behavior stays.)
    // elapsed = (NOW_S - 100) - (NOW_S - 3600) - 0 = 3500s = 0:58:20
    // Wait: started_at = NOW_S - 3600, paused_at = NOW_S - 100
    // effectiveNow = paused_at = NOW_S - 100
    // elapsedSec = (NOW_S - 100) - (NOW_S - 3600) - 0 = 3500s = 58:20 => "0:58:20"
    expect(host.querySelector(".rb-saved")!.textContent).toMatch(/0:58:20/);
  });

  it("subtracts the pause offset when running", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    // elapsed = nowSeconds - started_at - offset = (NOW_S) - (NOW_S - 3600) - 600 = 3000s = 50:00
    renderRouteBar(host, dataWith({ session_pause_offset_sec: 600 }));
    expect(host.querySelector(".rb-paused")).toBeNull();
    expect(host.querySelector(".rb-saved")!.textContent).toMatch(/0:50:00/);
  });

  it("shows the R-menu hint when armed", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderRouteBar(host, dataWith({ menu_armed: true }));
    expect(host.querySelector(".rb-menu-hint")).not.toBeNull();
  });

  it("no hint and no badge in the normal active case", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderRouteBar(host, dataWith({}));
    expect(host.querySelector(".rb-menu-hint")).toBeNull();
    expect(host.querySelector(".rb-paused")).toBeNull();
  });
});

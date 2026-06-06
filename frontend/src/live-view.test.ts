import { describe, it, expect, vi } from "vitest";
import { loadAndRenderLiveView, destroyLiveView } from "./live-view";

// Mock fetchJSON so tests don't hit the network.
vi.mock("./api", () => ({
  fetchJSON: vi.fn(async (url: string) => {
    if (url.includes("/live-summary")) {
      return {
        game_id: "g0", exp_run_ms: 115_000, exp_deaths: 3.5,
        n_estimable: 8, n_skipped: 0,
        session_started_at: null,
        exp_run_diff_ms: null, exp_deaths_diff: null,
        practice_saved_ms: null, floor_improvement_ms: null,
        run_series: [115000, 114000],
        baseline_exp_run_ms: 116000,
        floor_total_ms: 110000,
        session_ended_at: null,
      };
    }
    return {
      segment_id: "s0", ready: true,
      expected_episode_ms: 21_800, practice_gain_ms: 500, death_rate: 0.62,
      floor_ms: 12_800, last_episode_ms: 16_800, last_clean_ms: 13_600,
      last_deaths: 1, last_rank: 2,
      series: [{ episode_ms: 16800, deaths: 1, clean_ms: 13600, running_floor_ms: 12800 }],
      n_successes: 6, n_deaths: 5,
      expected_episode_diff_ms: null, practice_gain_diff_ms: null,
      floor_diff_ms: null, death_rate_diff: null,
    };
  }),
}));

function setupHosts() {
  document.body.innerHTML = `
    <div id="rb"></div><div id="rg2"></div><div id="ss"></div><div id="gs"></div>
  `;
  return {
    routeBar: document.getElementById("rb")!,
    runGraph: document.getElementById("rg2")!,
    segmentSummary: document.getElementById("ss")!,
    graph: document.getElementById("gs")!,
  };
}

describe("loadAndRenderLiveView", () => {
  it("populates all three hosts on success", async () => {
    const hosts = setupHosts();
    await loadAndRenderLiveView({
      segmentId: "s0", gameId: "g0", segmentName: "L1",
      title: "Beto · any%", hosts,
    });
    expect(hosts.routeBar.innerHTML).toContain("Beto");
    expect(hosts.segmentSummary.innerHTML).toContain("L1");
    expect(hosts.graph.querySelector("svg")).not.toBeNull();
  });
  it("keeps prior content visible while a reload's fetch is in flight (no blank flash)", async () => {
    const hosts = setupHosts();
    await loadAndRenderLiveView({
      segmentId: "s0", gameId: "g0", segmentName: "L1",
      title: "Beto · any%", hosts,
    });
    expect(hosts.routeBar.innerHTML).toContain("Beto");
    expect(hosts.segmentSummary.innerHTML).toContain("L1");

    // Reload with both fetches held in-flight so we can inspect the DOM
    // mid-load. A re-render on every SSE push must not blank the panels
    // first (that empty -> fetch-latency -> refill gap is the flicker).
    const api = await import("./api");
    let release!: () => void;
    const pending = new Promise<null>((res) => { release = () => res(null); });
    (api.fetchJSON as ReturnType<typeof vi.fn>)
      .mockReturnValueOnce(pending)
      .mockReturnValueOnce(pending);

    const reload = loadAndRenderLiveView({
      segmentId: "s1", gameId: "g0", segmentName: "L2",
      title: "Beto · any%", hosts,
    });
    // Before the in-flight fetch resolves, the prior content must remain.
    expect(hosts.routeBar.innerHTML).toContain("Beto");
    expect(hosts.segmentSummary.innerHTML).toContain("L1");

    release();
    await reload;
    destroyLiveView();
  });
  it("renders the run graph from the summary payload", async () => {
    const hosts = setupHosts();
    await loadAndRenderLiveView({
      segmentId: "s0", gameId: "g0", segmentName: "L1",
      title: "Beto · any%", hosts,
    });
    expect(hosts.runGraph.querySelector("svg")).not.toBeNull();
    destroyLiveView();
    expect(hosts.runGraph.innerHTML).toBe("");
  });
  it("frozen summary skips the 1s tick and pins elapsed to ended_at", async () => {
    const api = await import("./api");
    const frozenImpl = async (url: string) => {
      if (url.includes("/live-summary")) {
        return {
          game_id: "g0", exp_run_ms: 115_000, exp_deaths: 3.5,
          n_estimable: 8, n_skipped: 0,
          session_started_at: 1000, exp_run_diff_ms: null, exp_deaths_diff: null,
          practice_saved_ms: 6200, floor_improvement_ms: null,
          run_series: [120000, 115000], baseline_exp_run_ms: 121200, floor_total_ms: 110000,
          session_ended_at: 1060,
        };
      }
      return {
        segment_id: "s0", ready: true, expected_episode_ms: 21_800, practice_gain_ms: 500,
        death_rate: 0.62, floor_ms: 12_800, last_episode_ms: 16_800, last_clean_ms: 13_600,
        last_deaths: 1, last_rank: 2,
        series: [{ episode_ms: 16800, deaths: 1, clean_ms: 13600, running_floor_ms: 12800 }],
        n_successes: 6, n_deaths: 5,
        expected_episode_diff_ms: null, practice_gain_diff_ms: null,
        floor_diff_ms: null, death_rate_diff: null,
      };
    };
    // loadAndRenderLiveView makes exactly two fetchJSON calls (/live + /live-summary).
    // Use mockImplementationOnce twice so the override reverts to the default module
    // mock afterward and does not leak into later tests.
    (api.fetchJSON as ReturnType<typeof vi.fn>)
      .mockImplementationOnce(frozenImpl)
      .mockImplementationOnce(frozenImpl);
    const spy = vi.spyOn(globalThis, "setInterval");
    const hosts = setupHosts();
    await loadAndRenderLiveView({
      segmentId: "s0", gameId: "g0", segmentName: "L1", title: "Beto · any%", hosts,
    });
    expect(spy).not.toHaveBeenCalled();          // frozen: no elapsed-tick interval
    expect(hosts.routeBar.textContent).toContain("(frozen)");
    expect(hosts.routeBar.textContent).toContain("0:01:00");  // 60s frozen elapsed
    spy.mockRestore();
    destroyLiveView();
  });
  it("renders inline error per host on fetch failure", async () => {
    const api = await import("./api");
    (api.fetchJSON as ReturnType<typeof vi.fn>).mockImplementationOnce(() =>
      Promise.reject(new Error("boom")),
    );
    const hosts = setupHosts();
    await loadAndRenderLiveView({
      segmentId: "s0", gameId: "g0", segmentName: "L1",
      title: "Beto · any%", hosts,
    });
    // Whichever host owned the failed call shows the inline error; others still render.
    const combined = hosts.routeBar.innerHTML + hosts.segmentSummary.innerHTML;
    expect(combined.toLowerCase()).toContain("unavailable");
  });
});

describe("destroyLiveView", () => {
  it("clears the elapsed-tick timer", async () => {
    const hosts = setupHosts();
    await loadAndRenderLiveView({
      segmentId: "s0", gameId: "g0", segmentName: "L1",
      title: "Beto · any%", hosts,
    });
    destroyLiveView();
    // No assertion beyond 'no throw'; the next test must start clean.
  });
});

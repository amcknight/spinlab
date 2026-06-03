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
    <div id="rb"></div><div id="ss"></div><div id="gs"></div>
  `;
  return {
    routeBar: document.getElementById("rb")!,
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

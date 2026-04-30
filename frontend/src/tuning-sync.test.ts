import { describe, it, expect, vi, beforeEach } from "vitest";
import { syncTuningWithGame, _resetTuningGameCache } from "./model";

// Mock fetch so syncTuningWithGame doesn't try to hit the network when it
// triggers a refetch.  We assert against the URL the call would have hit.
const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

function paramsResponse() {
  return {
    ok: true,
    json: () =>
      Promise.resolve({
        estimator: "kalman",
        params: [
          { name: "R", display_name: "Obs. Noise", default: 25, value: 25,
            min: 0.01, max: 1000, step: 0.1, description: "" },
        ],
      }),
  };
}

function setupDOM() {
  // renderTuningParams looks up these elements; without them the renderer
  // bails before doing anything testable.
  document.body.innerHTML = `
    <div id="tuning-params"></div>
    <div class="tuning-actions"></div>
  `;
}

describe("syncTuningWithGame", () => {
  beforeEach(() => {
    mockFetch.mockReset();
    setupDOM();
    _resetTuningGameCache();
  });

  it("fetches tuning params when game_id transitions from null to a value", async () => {
    mockFetch.mockResolvedValue(paramsResponse());

    syncTuningWithGame(null);
    expect(mockFetch).not.toHaveBeenCalled();

    syncTuningWithGame("smw-any");
    // Allow the awaited fetch to resolve.
    await new Promise((r) => setTimeout(r, 0));

    expect(mockFetch).toHaveBeenCalledWith("/api/estimator-params", {});
  });

  it("does not refetch when the same game_id is re-emitted", async () => {
    mockFetch.mockResolvedValue(paramsResponse());

    syncTuningWithGame("smw-any");
    await new Promise((r) => setTimeout(r, 0));
    syncTuningWithGame("smw-any");
    syncTuningWithGame("smw-any");
    await new Promise((r) => setTimeout(r, 0));

    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("refetches when switching to a different game", async () => {
    mockFetch.mockResolvedValue(paramsResponse());

    syncTuningWithGame("smw-any");
    await new Promise((r) => setTimeout(r, 0));
    syncTuningWithGame("kaizo-1");
    await new Promise((r) => setTimeout(r, 0));

    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it("does not fetch when game_id transitions to null", async () => {
    syncTuningWithGame(null);
    syncTuningWithGame(null);

    expect(mockFetch).not.toHaveBeenCalled();
  });
});

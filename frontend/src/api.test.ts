import { describe, it, expect, vi, beforeEach } from "vitest";
import { fetchJSON, postJSON, fetchStateWithRetry } from "./api";

const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

beforeEach(() => {
  mockFetch.mockReset();
  document.body.innerHTML = "";
});

describe("fetchJSON", () => {
  it("returns parsed JSON on success", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ mode: "idle" }),
    });
    const result = await fetchJSON("/api/state");
    expect(result).toEqual({ mode: "idle" });
    expect(mockFetch).toHaveBeenCalledWith("/api/state", {});
  });

  it("returns null and shows toast on HTTP error", async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      statusText: "Not Found",
      json: () => Promise.reject(new Error("no json")),
    });
    const result = await fetchJSON("/api/missing");
    expect(result).toBeNull();
    const toast = document.getElementById("toast");
    expect(toast?.textContent).toContain("Not Found");
  });

  it("prefers detail from JSON error body", async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      statusText: "Conflict",
      json: () => Promise.resolve({ detail: "draft_pending" }),
    });
    const result = await fetchJSON("/api/practice/start");
    expect(result).toBeNull();
    const toast = document.getElementById("toast");
    expect(toast?.textContent).toContain("draft_pending");
  });

  it("returns null and shows toast on network error", async () => {
    mockFetch.mockRejectedValue(new Error("Failed to fetch"));
    const result = await fetchJSON("/api/state");
    expect(result).toBeNull();
    const toast = document.getElementById("toast");
    expect(toast?.textContent).toContain("Failed to fetch");
  });
});

describe("fetchStateWithRetry", () => {
  const err500 = () => ({
    ok: false,
    statusText: "Internal Server Error",
    json: () => Promise.reject(new Error("no json")),
  });

  it("retries on failure and returns data once the backend is reachable", async () => {
    mockFetch
      .mockResolvedValueOnce(err500())
      .mockResolvedValueOnce(err500())
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ mode: "idle" }) });
    const result = await fetchStateWithRetry(5, 0);
    expect(result).toEqual({ mode: "idle" });
    expect(mockFetch).toHaveBeenCalledTimes(3);
  });

  it("does not show a toast while retrying (silent startup)", async () => {
    mockFetch
      .mockResolvedValueOnce(err500())
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ mode: "idle" }) });
    await fetchStateWithRetry(5, 0);
    expect(document.getElementById("toast")).toBeNull();
  });

  it("returns null after exhausting attempts", async () => {
    mockFetch.mockResolvedValue(err500());
    const result = await fetchStateWithRetry(3, 0);
    expect(result).toBeNull();
    expect(mockFetch).toHaveBeenCalledTimes(3);
  });
});

describe("postJSON", () => {
  it("sends POST with JSON body", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ status: "ok" }),
    });
    const result = await postJSON("/api/practice/start", { foo: "bar" });
    expect(result).toEqual({ status: "ok" });
    expect(mockFetch).toHaveBeenCalledWith("/api/practice/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: '{"foo":"bar"}',
    });
  });

  it("sends POST without body when body is null", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ status: "ok" }),
    });
    await postJSON("/api/reference/stop");
    expect(mockFetch).toHaveBeenCalledWith("/api/reference/stop", {
      method: "POST",
    });
  });
});

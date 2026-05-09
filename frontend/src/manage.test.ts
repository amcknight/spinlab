import { describe, it, expect, vi, beforeEach } from "vitest";
import { initManageTab } from "./manage";

const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

beforeEach(() => {
  mockFetch.mockReset();
  document.body.innerHTML = `
    <select id="ref-select"></select>
    <button id="btn-ref-start"></button>
    <button id="btn-replay"></button>
    <button id="btn-ref-rename"></button>
    <button id="btn-ref-delete"></button>
    <button id="btn-resume"></button>
    <button id="btn-save-and-finish"></button>
    <button id="btn-discard-run"></button>
    <button id="btn-reset"></button>
    <input id="finalize-name" />
    <div id="paused-run-card"></div>
    <div id="paused-run-summary"></div>
    <div id="recording-indicator"></div>
    <div id="recording-seg-count"></div>
    <div id="cold-fill-banner"></div>
    <div id="reset-status"></div>
    <table><tbody id="segment-body"></tbody></table>
    <table><tbody id="sessions-body"></tbody></table>
  `;
  // Stub confirm so discard prompt doesn't block tests
  vi.stubGlobal("confirm", () => true);
  initManageTab();
  mockFetch.mockResolvedValue({
    ok: true,
    json: () => Promise.resolve({ status: "ok" }),
  });
});

describe("Resume button", () => {
  it("POSTs to /api/reference/resume with empty body", async () => {
    document.getElementById("btn-resume")!.click();
    await Promise.resolve();
    expect(mockFetch).toHaveBeenCalledWith("/api/reference/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
  });
});

describe("Save & Finish button", () => {
  it("POSTs to /api/reference/save_and_finish with name from input", async () => {
    (document.getElementById("finalize-name") as HTMLInputElement).value = "My Run";
    document.getElementById("btn-save-and-finish")!.click();
    await Promise.resolve();
    expect(mockFetch).toHaveBeenCalledWith("/api/reference/save_and_finish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "My Run" }),
    });
  });

  it("falls back to 'Untitled' when name input is empty", async () => {
    (document.getElementById("finalize-name") as HTMLInputElement).value = "";
    document.getElementById("btn-save-and-finish")!.click();
    await Promise.resolve();
    const call = mockFetch.mock.calls[0];
    expect(call).toBeDefined();
    expect(JSON.parse(call![1].body)).toEqual({ name: "Untitled" });
  });
});

describe("Discard Run button", () => {
  it("POSTs to /api/reference/discard_run after confirm", async () => {
    document.getElementById("btn-discard-run")!.click();
    await Promise.resolve();
    expect(mockFetch).toHaveBeenCalledWith("/api/reference/discard_run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
  });

  it("does NOT POST when confirm is denied", async () => {
    vi.stubGlobal("confirm", () => false);
    document.getElementById("btn-discard-run")!.click();
    await Promise.resolve();
    expect(mockFetch).not.toHaveBeenCalled();
  });
});

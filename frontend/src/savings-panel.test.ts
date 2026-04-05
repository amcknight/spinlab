import { describe, it, expect, beforeEach } from "vitest";
import { updateSavingsPanel } from "./model";
import type { SessionInfo } from "./types";

function setupDOM() {
  document.body.innerHTML = `
    <div id="savings-panel" style="display:none">
      <span id="savings-total"></span>
      <span id="savings-clean"></span>
    </div>
  `;
}

describe("updateSavingsPanel", () => {
  beforeEach(setupDOM);

  it("hides panel when session is null", () => {
    updateSavingsPanel(null);
    const panel = document.getElementById("savings-panel") as HTMLElement;
    expect(panel.style.display).toBe("none");
  });

  it("hides panel when both savings are null", () => {
    const session: SessionInfo = {
      id: "s", started_at: "x", segments_attempted: 0, segments_completed: 0,
      saved_total_ms: null, saved_clean_ms: null,
    };
    updateSavingsPanel(session);
    const panel = document.getElementById("savings-panel") as HTMLElement;
    expect(panel.style.display).toBe("none");
  });

  it("shows panel with formatted values when savings present", () => {
    const session: SessionInfo = {
      id: "s", started_at: "x", segments_attempted: 0, segments_completed: 0,
      saved_total_ms: 3200, saved_clean_ms: 1800,
    };
    updateSavingsPanel(session);
    const panel = document.getElementById("savings-panel") as HTMLElement;
    expect(panel.style.display).toBe("");
    expect(document.getElementById("savings-total")!.textContent).toBe("+3.2s total");
    expect(document.getElementById("savings-clean")!.textContent).toBe("+1.8s clean");
  });

  it("applies positive class for positive savings", () => {
    const session: SessionInfo = {
      id: "s", started_at: "x", segments_attempted: 0, segments_completed: 0,
      saved_total_ms: 500, saved_clean_ms: 500,
    };
    updateSavingsPanel(session);
    const total = document.getElementById("savings-total")!;
    expect(total.className).toContain("positive");
  });

  it("applies negative class for regressions", () => {
    const session: SessionInfo = {
      id: "s", started_at: "x", segments_attempted: 0, segments_completed: 0,
      saved_total_ms: -500, saved_clean_ms: -200,
    };
    updateSavingsPanel(session);
    const total = document.getElementById("savings-total")!;
    expect(total.className).toContain("negative");
  });

  it("hides one value and shows the other when mixed", () => {
    const session: SessionInfo = {
      id: "s", started_at: "x", segments_attempted: 0, segments_completed: 0,
      saved_total_ms: 1000, saved_clean_ms: null,
    };
    updateSavingsPanel(session);
    const panel = document.getElementById("savings-panel") as HTMLElement;
    expect(panel.style.display).toBe("");
    expect(document.getElementById("savings-total")!.textContent).toBe("+1.0s total");
    expect(document.getElementById("savings-clean")!.textContent).toBe("");
  });
});

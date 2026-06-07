import { describe, it, expect, beforeEach } from "vitest";
import { renderModelTable } from "./model-render";
import type { ModelData } from "./types";

const EMPTY_MODEL = { segments: [] } as unknown as ModelData;

describe("renderModelTable empty states", () => {
  beforeEach(() => {
    document.body.innerHTML = `<table><tbody id="model-body"></tbody></table>`;
  });

  it("shows the no-active-run empty state when hasActiveRun is false", () => {
    renderModelTable(EMPTY_MODEL, () => {}, false);
    const body = document.getElementById("model-body")!;
    expect(body.textContent).toContain("No reference run selected");
  });

  it("with an active run but no segments keeps the generic empty copy", () => {
    renderModelTable(EMPTY_MODEL, () => {}, true);
    const body = document.getElementById("model-body")!;
    expect(body.textContent).toContain("No game loaded");
    expect(body.textContent).not.toContain("No reference run selected");
  });
});

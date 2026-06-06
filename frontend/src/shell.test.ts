import { describe, it, expect } from "vitest";
import { nextPage, tabLabel, type Page } from "./shell";

describe("shell page toggle", () => {
  it("toggles play <-> setup", () => {
    expect(nextPage("play")).toBe<Page>("setup");
    expect(nextPage("setup")).toBe<Page>("play");
  });

  it("edge-tab label names the DESTINATION page", () => {
    // On Play, the tab takes you to Setup, so it reads 'Setup'.
    expect(tabLabel("play")).toBe("Setup");
    expect(tabLabel("setup")).toBe("Play");
  });
});

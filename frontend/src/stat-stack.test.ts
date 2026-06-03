import { describe, it, expect } from "vitest";
import { renderStatStack, type StatStack } from "./stat-stack";

describe("renderStatStack", () => {
  it("renders label / value / diff with correct color for improvement", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    const stack: StatStack = {
      label: "Expected",
      value: "18.7s",
      diff: { text: "-2.1s", sign: "good" },
    };
    renderStatStack(host, stack);
    expect(host.querySelector(".ss-label")!.textContent).toBe("Expected");
    expect(host.querySelector(".ss-value")!.textContent).toBe("18.7s");
    const diff = host.querySelector(".ss-diff")!;
    expect(diff.textContent).toBe("-2.1s");
    expect(diff.classList.contains("good")).toBe(true);
  });
  it("hides the diff row entirely when diff is null", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderStatStack(host, { label: "Floor", value: "12.8s", diff: null });
    expect(host.querySelector(".ss-diff")).toBeNull();
  });
  it("renders an em-dash for null value", () => {
    document.body.innerHTML = `<div id="h"></div>`;
    const host = document.getElementById("h")!;
    renderStatStack(host, { label: "Expected", value: null, diff: null });
    expect(host.querySelector(".ss-value")!.textContent).toBe("—");
  });
});

import { describe, it, expect, vi } from "vitest";
import {
  DEFAULT_SORT,
  nextSortColumn,
  flipSortDir,
  sortAttempts,
  renderAttemptTable,
  type AttemptRow,
} from "./attempt-table";

function row(p: Partial<AttemptRow>): AttemptRow {
  return {
    id: 1, order: 1, clean_tail_ms: 12000, total_ms: 12000, deaths: 0,
    created_at: "2026-01-01T00:00:00Z", completed: true, invalidated: false,
    is_floor: false, ...p,
  };
}

describe("sort state", () => {
  it("default is Clean Tail descending", () => {
    expect(DEFAULT_SORT).toEqual({ column: "clean_tail_ms", dir: "desc" });
  });
  it("clicking a new column uses its default dir; same column is unchanged", () => {
    expect(nextSortColumn(DEFAULT_SORT, "order")).toEqual({ column: "order", dir: "asc" });
    expect(nextSortColumn(DEFAULT_SORT, "clean_tail_ms")).toEqual(DEFAULT_SORT);
  });
  it("flip reverses direction", () => {
    expect(flipSortDir({ column: "order", dir: "asc" })).toEqual({ column: "order", dir: "desc" });
  });
});

describe("sortAttempts", () => {
  it("sorts by clean tail desc, nulls always last", () => {
    const rows = [row({ id: 1, clean_tail_ms: 12000 }), row({ id: 2, clean_tail_ms: null }), row({ id: 3, clean_tail_ms: 41000 })];
    const out = sortAttempts(rows, { column: "clean_tail_ms", dir: "desc" }).map(r => r.id);
    expect(out).toEqual([3, 1, 2]);
  });
  it("nulls stay last even ascending", () => {
    const rows = [row({ id: 1, clean_tail_ms: 12000 }), row({ id: 2, clean_tail_ms: null }), row({ id: 3, clean_tail_ms: 41000 })];
    const out = sortAttempts(rows, { column: "clean_tail_ms", dir: "asc" }).map(r => r.id);
    expect(out).toEqual([1, 3, 2]);
  });
  it("sorts by order ascending", () => {
    const rows = [row({ id: 1, order: 3 }), row({ id: 2, order: 1 }), row({ id: 3, order: 2 })];
    const out = sortAttempts(rows, { column: "order", dir: "asc" }).map(r => r.id);
    expect(out).toEqual([2, 3, 1]);
  });
});

describe("renderAttemptTable", () => {
  it("renders rows, strikes invalidated, stars the floor, calls onToggle", () => {
    document.body.innerHTML = `<div id="host"></div>`;
    const host = document.getElementById("host")!;
    const onToggle = vi.fn();
    const rows = [
      row({ id: 7, clean_tail_ms: 41700, total_ms: 53100, deaths: 1, invalidated: false }),
      row({ id: 18, clean_tail_ms: 11000, total_ms: 11000, is_floor: true }),
      row({ id: 5, clean_tail_ms: 38000, invalidated: true }),
    ];
    renderAttemptTable(host, rows, onToggle, Date.parse("2026-01-02T00:00:00Z"));
    expect(host.querySelectorAll("tbody tr").length).toBe(3);
    expect(host.textContent).toContain("★");
    const struck = host.querySelector("tr.invalidated");
    expect(struck).not.toBeNull();
    const btn = host.querySelector<HTMLButtonElement>("button[data-id='7']")!;
    btn.click();
    expect(onToggle).toHaveBeenCalledWith(7, true);
    const restoreBtn = host.querySelector<HTMLButtonElement>("button[data-id='5']")!;
    restoreBtn.click();
    expect(onToggle).toHaveBeenCalledWith(5, false);
  });
});

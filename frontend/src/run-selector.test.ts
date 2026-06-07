import { describe, it, expect, vi, beforeEach } from "vitest";
import { runSelectorOptions } from "./run-selector";
import type { Reference } from "./types";

function ref(partial: { id: string; name: string; created_at: string; active?: boolean }): Reference {
  return {
    game_id: "g1",
    status: "saved",
    // Reference.active is codegen'd as 0/1 (SQLite boolean).
    active: partial.active ? 1 : 0,
    kind: "live",
    has_replay: false,
    id: partial.id,
    name: partial.name,
    created_at: partial.created_at,
  } as Reference;
}

describe("runSelectorOptions", () => {
  it("disables when there is no game", () => {
    const view = runSelectorOptions([], false);
    expect(view.disabled).toBe(true);
    expect(view.options).toEqual([]);
  });

  it("is enabled with a game even when there are no runs", () => {
    const view = runSelectorOptions([], true);
    expect(view.disabled).toBe(false);
    expect(view.options).toEqual([]);
  });

  it("with fewer than 4 runs lists all alphabetically by name", () => {
    const runs = [
      ref({ id: "b", name: "Charlie", created_at: "2026-01-03T00:00:00Z" }),
      ref({ id: "a", name: "Alpha", created_at: "2026-01-01T00:00:00Z" }),
      ref({ id: "c", name: "Bravo", created_at: "2026-01-02T00:00:00Z" }),
    ];
    const view = runSelectorOptions(runs, true);
    expect(view.options.map((o) => o.name)).toEqual(["Alpha", "Bravo", "Charlie"]);
  });

  it("with >=4 runs puts the most-recent (by created_at desc) first, then the rest alphabetical", () => {
    // 5 runs. Recent group = 3 newest by created_at desc.
    const runs = [
      ref({ id: "old1", name: "Zeta", created_at: "2026-01-01T00:00:00Z" }),
      ref({ id: "old2", name: "Mike", created_at: "2026-01-02T00:00:00Z" }),
      ref({ id: "new3", name: "Yankee", created_at: "2026-01-05T00:00:00Z" }),
      ref({ id: "new1", name: "Alpha", created_at: "2026-01-07T00:00:00Z" }),
      ref({ id: "new2", name: "Bravo", created_at: "2026-01-06T00:00:00Z" }),
    ];
    const view = runSelectorOptions(runs, true);
    // Recent group: newest 3 by created_at desc -> Alpha(01-07), Bravo(01-06), Yankee(01-05)
    // Remainder alphabetical: Mike, Zeta
    expect(view.options.map((o) => o.id)).toEqual(["new1", "new2", "new3", "old2", "old1"]);
    // Recent group flagged
    expect(view.options.slice(0, 3).every((o) => o.recent)).toBe(true);
    expect(view.options.slice(3).every((o) => o.recent)).toBe(false);
  });

  it("carries the active flag through", () => {
    const runs = [
      ref({ id: "a", name: "Alpha", created_at: "2026-01-01T00:00:00Z", active: true }),
      ref({ id: "b", name: "Bravo", created_at: "2026-01-02T00:00:00Z" }),
    ];
    const view = runSelectorOptions(runs, true);
    expect(view.options.find((o) => o.id === "a")!.active).toBe(true);
    expect(view.options.find((o) => o.id === "b")!.active).toBe(false);
  });

  it("does not duplicate a run that is both in the recent group and the remainder", () => {
    const runs = [
      ref({ id: "1", name: "A", created_at: "2026-01-01T00:00:00Z" }),
      ref({ id: "2", name: "B", created_at: "2026-01-02T00:00:00Z" }),
      ref({ id: "3", name: "C", created_at: "2026-01-03T00:00:00Z" }),
      ref({ id: "4", name: "D", created_at: "2026-01-04T00:00:00Z" }),
    ];
    const view = runSelectorOptions(runs, true);
    expect(view.options).toHaveLength(4);
    expect(new Set(view.options.map((o) => o.id)).size).toBe(4);
  });
});

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { openDeleteDialog } from "./run-selector";
import type { Reference } from "./types";

function ref(partial: { id: string; name: string }): Reference {
  return {
    game_id: "g1",
    status: "saved",
    active: 0,
    kind: "live",
    has_replay: false,
    id: partial.id,
    name: partial.name,
    created_at: "2026-01-01T00:00:00Z",
  } as Reference;
}

const DIALOG_ID = "run-delete-dialog";

describe("openDeleteDialog", () => {
  let mockFetch: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    document.body.innerHTML = "";
    // Default: any /api/references re-fetch (from refreshRunSelector) returns
    // an empty list so the success path's refresh doesn't blow up.
    mockFetch = vi.fn(async () =>
      ({ ok: true, json: async () => ({ references: [] }) }) as unknown as Response,
    );
    vi.stubGlobal("fetch", mockFetch);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function deleteCalls(): string[] {
    return mockFetch.mock.calls
      .filter((c) => (c[1] as RequestInit | undefined)?.method === "DELETE")
      .map((c) => c[0] as string);
  }

  it("renders the title, the run name, and the three buttons", () => {
    openDeleteDialog(ref({ id: "r1", name: "My Run" }));
    const dialog = document.getElementById(DIALOG_ID)!;
    expect(dialog).toBeTruthy();
    expect(dialog.textContent).toContain("Delete this Reference Run?");
    expect(dialog.textContent).toContain("My Run");
    expect(document.getElementById("run-delete-run-only")).toBeTruthy();
    expect(document.getElementById("run-delete-run-and-data")).toBeTruthy();
    expect(document.getElementById("run-delete-cancel")).toBeTruthy();
  });

  it("[Delete Run] calls DELETE ...?mode=run_only", () => {
    openDeleteDialog(ref({ id: "r1", name: "My Run" }));
    (document.getElementById("run-delete-run-only") as HTMLButtonElement).click();
    expect(deleteCalls()).toContain("/api/references/r1?mode=run_only");
  });

  it("[Delete Run + Data] calls DELETE ...?mode=run_and_data and carries the danger class", () => {
    openDeleteDialog(ref({ id: "r1", name: "My Run" }));
    const btn = document.getElementById("run-delete-run-and-data") as HTMLButtonElement;
    expect(btn.className).toMatch(/btn-danger/);
    btn.click();
    expect(deleteCalls()).toContain("/api/references/r1?mode=run_and_data");
  });

  it("[Cancel] makes no network call and closes the dialog", () => {
    openDeleteDialog(ref({ id: "r1", name: "My Run" }));
    (document.getElementById("run-delete-cancel") as HTMLButtonElement).click();
    expect(mockFetch).not.toHaveBeenCalled();
    expect(document.getElementById(DIALOG_ID)).toBeNull();
  });

  it("does not stack duplicate dialogs when opened twice", () => {
    openDeleteDialog(ref({ id: "r1", name: "My Run" }));
    openDeleteDialog(ref({ id: "r1", name: "My Run" }));
    expect(document.querySelectorAll("#" + DIALOG_ID)).toHaveLength(1);
  });
});

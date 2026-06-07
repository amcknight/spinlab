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

  it("Escape closes the dialog without making a DELETE request", () => {
    openDeleteDialog(ref({ id: "r1", name: "My Run" }));
    expect(document.getElementById(DIALOG_ID)).toBeTruthy();

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));

    expect(document.getElementById(DIALOG_ID)).toBeNull();
    expect(deleteCalls()).toHaveLength(0);
  });

  it("clicking the backdrop closes the dialog without making a DELETE request", () => {
    openDeleteDialog(ref({ id: "r1", name: "My Run" }));
    const overlay = document.getElementById(DIALOG_ID) as HTMLElement;
    expect(overlay).toBeTruthy();

    // Clicking the inner box must NOT close the dialog.
    // The handler guards: if (e.target === overlay); box click bubbles up but
    // e.target remains the inner node, so the guard rejects it.
    const box = overlay.querySelector(".run-delete-box") as HTMLElement;
    box.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(document.getElementById(DIALOG_ID)).toBeTruthy();

    // Dispatching directly on the overlay node sets e.target === overlay,
    // which satisfies the handler's guard and closes the dialog.
    overlay.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(document.getElementById(DIALOG_ID)).toBeNull();
    expect(deleteCalls()).toHaveLength(0);
  });

  it("a failed DELETE leaves the dialog open and does not trigger a refresh", async () => {
    // Override fetch: DELETE returns a failure; GET returns the default success.
    mockFetch = vi.fn(async (url: string, init?: RequestInit) => {
      if ((init as RequestInit | undefined)?.method === "DELETE") {
        return {
          ok: false,
          statusText: "Internal Server Error",
          json: async () => ({ detail: "backend error" }),
        } as unknown as Response;
      }
      // Default success for any subsequent GET (refreshRunSelector).
      return { ok: true, json: async () => ({ references: [] }) } as unknown as Response;
    });
    vi.stubGlobal("fetch", mockFetch);

    openDeleteDialog(ref({ id: "r1", name: "My Run" }));

    (document.getElementById("run-delete-run-only") as HTMLButtonElement).click();

    // Let the async performDelete settle.
    await new Promise((resolve) => setTimeout(resolve, 0));

    // Dialog must still be present — bail path did not close it.
    expect(document.getElementById(DIALOG_ID)).toBeTruthy();

    // No follow-up GET to /api/references should have been made.
    const getCalls = mockFetch.mock.calls.filter(
      (c) => (c[1] as RequestInit | undefined)?.method !== "DELETE",
    );
    expect(getCalls).toHaveLength(0);
  });
});

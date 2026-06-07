import { fetchJSON, postJSON } from "./api";
import type { AppState, Reference } from "./types";

// --- Pure ordering/grouping helper -----------------------------------------

export interface RunOption {
  id: string;
  name: string;
  active: boolean;
  // True for the members of the "recent" group rendered under a section header.
  recent: boolean;
}

export interface RunSelectorView {
  options: RunOption[];
  disabled: boolean;
}

// Above this many saved runs the flat alphabetical list gets unwieldy, so we
// surface a "recent" group on top. At/under it, a single alphabetical list is
// short enough to scan, so grouping just adds noise.
const RECENT_GROUP_THRESHOLD = 4;
// How many of the most-recently-created runs go in the "recent" group. Three is
// a glanceable shortlist — the runs you most likely want right after finishing
// a session — without crowding out the alphabetical remainder.
const RECENT_GROUP_SIZE = 3;

function byNameAsc(a: Reference, b: Reference): number {
  return a.name.localeCompare(b.name);
}

function byCreatedDesc(a: Reference, b: Reference): number {
  // ISO timestamps sort lexicographically; newest first.
  return b.created_at.localeCompare(a.created_at);
}

function toOption(r: Reference, recent: boolean): RunOption {
  // Reference.active is codegen'd as 0/1 (SQLite boolean); coerce to a real bool.
  return { id: r.id, name: r.name, active: !!r.active, recent };
}

/**
 * Order + group a game's saved reference runs for the selector. Pure: no DOM,
 * no fetch. With no game it reports `disabled:true` (the gate). With >=4 runs it
 * emits a recent group (newest RECENT_GROUP_SIZE by created_at desc) followed by
 * the remaining runs alphabetically; with fewer, a single alphabetical list.
 */
export function runSelectorOptions(
  runs: Reference[],
  hasGame: boolean,
): RunSelectorView {
  if (!hasGame) return { options: [], disabled: true };
  if (runs.length < RECENT_GROUP_THRESHOLD) {
    return {
      options: [...runs].sort(byNameAsc).map((r) => toOption(r, false)),
      disabled: false,
    };
  }
  const recent = [...runs].sort(byCreatedDesc).slice(0, RECENT_GROUP_SIZE);
  const recentIds = new Set(recent.map((r) => r.id));
  const remainder = runs.filter((r) => !recentIds.has(r.id)).sort(byNameAsc);
  return {
    options: [
      ...recent.map((r) => toOption(r, true)),
      ...remainder.map((r) => toOption(r, false)),
    ],
    disabled: false,
  };
}

// --- DOM wiring ------------------------------------------------------------

let refs: Reference[] = [];
let currentGameId: string | null = null;
let popoverOpen = false;
let busy = false;
let onActiveChangedCb: (() => void | Promise<void>) | null = null;
// Distinguishes "first state ever" (game may legitimately be null) from a
// later null game, so we refresh exactly once on bootstrap.
let gameInitialized = false;

function el<T extends HTMLElement>(id: string): T {
  return document.getElementById(id) as T;
}

function activeRef(): Reference | undefined {
  return refs.find((r) => r.active);
}

function closePopover(): void {
  popoverOpen = false;
  const pop = el<HTMLElement>("run-popover");
  if (pop) pop.style.display = "none";
}

function makeSectionHeader(text: string): HTMLLIElement {
  const li = document.createElement("li");
  li.textContent = text;
  li.className = "rom-section-header";
  return li;
}

async function activateRun(id: string): Promise<void> {
  // postJSON/fetchJSON return null + toast on failure; bail so we don't refresh
  // or fire the callback against a mutation that didn't land.
  if ((await postJSON("/api/references/" + id + "/activate")) === null) return;
  await refreshRunSelector();
  if (onActiveChangedCb) await onActiveChangedCb();
}

async function renameRun(id: string, currentName: string): Promise<void> {
  const name = prompt("New name:", currentName);
  if (!name || !name.trim()) return;
  const res = await fetchJSON("/api/references/" + id, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name.trim() }),
  });
  if (res === null) return;
  await refreshRunSelector();
  if (onActiveChangedCb) await onActiveChangedCb();
}

async function deleteRun(id: string): Promise<void> {
  // TODO(Task 5): replace with 3-way delete dialog (?mode=run_only|run_and_data)
  if (!confirm("Delete this reference run?")) return;
  if ((await fetchJSON("/api/references/" + id, { method: "DELETE" })) === null)
    return;
  await refreshRunSelector();
  if (onActiveChangedCb) await onActiveChangedCb();
}

function makeRunRow(opt: RunOption): HTMLLIElement {
  const li = document.createElement("li");
  li.className = "run-row" + (opt.active ? " active" : "");

  const label = document.createElement("span");
  label.className = "run-name";
  label.textContent = (opt.active ? "● " : "") + opt.name; // ● prefix when active
  li.appendChild(label);

  const actions = document.createElement("span");
  actions.className = "run-actions";

  const renameBtn = document.createElement("button");
  renameBtn.className = "btn-sm run-rename";
  renameBtn.title = "Rename";
  renameBtn.innerHTML = "&#9998;"; // ✎
  renameBtn.disabled = busy;
  renameBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    renameRun(opt.id, opt.name);
  });
  actions.appendChild(renameBtn);

  const delBtn = document.createElement("button");
  delBtn.className = "btn-sm btn-danger-sm run-delete";
  delBtn.title = "Delete";
  delBtn.innerHTML = "&#10005;"; // ✕
  delBtn.disabled = busy;
  delBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    deleteRun(opt.id);
  });
  actions.appendChild(delBtn);

  li.appendChild(actions);

  // Clicking the row (not its buttons) switches the active run.
  li.addEventListener("click", () => {
    if (busy || opt.active) return;
    closePopover();
    activateRun(opt.id);
  });

  return li;
}

function renderButtonLabel(): void {
  const nameEl = el<HTMLElement>("run-name-label");
  if (!nameEl) return;
  const active = activeRef();
  nameEl.textContent = active ? active.name : "No run";
}

function renderList(): void {
  const ul = el<HTMLUListElement>("run-list");
  if (!ul) return;
  ul.innerHTML = "";
  const view = runSelectorOptions(refs, currentGameId != null);
  if (!view.options.length) {
    const li = document.createElement("li");
    li.className = "rom-section-header";
    li.textContent = "No saved runs";
    ul.appendChild(li);
    return;
  }
  const recent = view.options.filter((o) => o.recent);
  const rest = view.options.filter((o) => !o.recent);
  if (recent.length) {
    ul.appendChild(makeSectionHeader("Recent"));
    recent.forEach((o) => ul.appendChild(makeRunRow(o)));
    const sep = document.createElement("li");
    sep.className = "rom-separator";
    ul.appendChild(sep);
  }
  rest.forEach((o) => ul.appendChild(makeRunRow(o)));
}

function applyDisabled(): void {
  const btn = el<HTMLButtonElement>("run-selector");
  if (!btn) return;
  // No game -> disabled (nothing to select). Busy capture modes -> disabled
  // (switching/deleting runs mid-capture is unsafe).
  btn.disabled = currentGameId == null || busy;
}

/** Re-fetch the saved references and re-render the button + popover. */
export async function refreshRunSelector(): Promise<void> {
  const data = await fetchJSON<{ references: Reference[] }>("/api/references");
  refs = data?.references || [];

  // Auto-select: if the game has saved runs but none is active, establish one
  // by activating the most-recently-created. (Backend sets active on finalize,
  // so this mainly covers older data / post-deletion gaps.)
  if (refs.length && !refs.some((r) => r.active)) {
    const newest = [...refs].sort(byCreatedDesc)[0]!;
    await postJSON("/api/references/" + newest.id + "/activate");
    const re = await fetchJSON<{ references: Reference[] }>("/api/references");
    refs = re?.references || refs;
    if (onActiveChangedCb) await onActiveChangedCb();
  }

  renderButtonLabel();
  renderList();
  applyDisabled();
}

/**
 * Per-state update. Tracks the game id and busy state; refreshes the run list
 * when the game changes (or on the first state).
 */
export function updateRunSelector(state: AppState): void {
  const prevGame = currentGameId;
  const firstState = !gameInitialized;
  currentGameId = state.game_id ?? null;
  gameInitialized = true;

  busy =
    state.mode === "reference" ||
    state.mode === "replay" ||
    state.mode === "cold_fill" ||
    state.mode === "fill_gap";

  if (firstState || currentGameId !== prevGame) {
    refreshRunSelector();
  } else {
    // Game unchanged — still reflect busy/gating changes on the existing list.
    applyDisabled();
    renderList();
  }
}

/** Wire the selector button toggle + outside-click/Escape close. */
export function initRunSelector(
  onActiveChanged: () => void | Promise<void>,
): void {
  onActiveChangedCb = onActiveChanged;
  const btn = el<HTMLButtonElement>("run-selector");
  const pop = el<HTMLElement>("run-popover");
  if (!btn || !pop) return;

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (btn.disabled) return;
    popoverOpen = !popoverOpen;
    pop.style.display = popoverOpen ? "" : "none";
    if (popoverOpen) renderList();
  });

  document.addEventListener("click", (e) => {
    if (popoverOpen && !pop.contains(e.target as Node) && e.target !== btn) {
      closePopover();
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && popoverOpen) closePopover();
  });
}

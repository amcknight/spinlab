import { fetchJSON, postJSON } from "./api";
import { segmentName } from "./format";
import type { AppState } from "./types";

let allRoms: string[] = [];
let recentlyPlayed: string[] = [];
let recentlyAdded: string[] = [];
let popoverOpen = false;
let currentMode: string = "idle";

export async function loadRomList(): Promise<void> {
  const data = await fetchJSON<{
    roms: string[];
    recently_played?: string[];
    recently_added?: string[];
  }>("/api/roms");
  if (data?.roms) allRoms = data.roms;
  if (data?.recently_played) recentlyPlayed = data.recently_played;
  if (data?.recently_added) recentlyAdded = data.recently_added;
}

export function updateHeader(data: AppState): void {
  currentMode = data.mode ?? "idle";

  const gameEl = document.getElementById("game-name")!;
  gameEl.textContent =
    data.emu_connected && data.game_name ? data.game_name : "No game";

  const chip = document.getElementById("mode-chip")!;
  const label = document.getElementById("mode-label")!;
  const stopBtn = document.getElementById("mode-stop") as HTMLElement;

  chip.className = "mode-chip";
  stopBtn.style.display = "none";

  if (!data.emu_connected) {
    chip.classList.add("disconnected");
    label.textContent = "Disconnected";
  } else if (data.paused_run) {
    chip.classList.add("draft");
    label.textContent = "Draft — " + data.paused_run.segments_captured + " segments";
  } else if (data.mode === "reference") {
    chip.classList.add("recording");
    label.textContent = "Recording — " + (data.sections_captured || 0) + " segments";
    stopBtn.style.display = "";
  } else if (data.mode === "practice") {
    chip.classList.add("practicing");
    const seg = data.current_segment;
    label.textContent = "Practicing" + (seg ? " — " + segmentName(seg) : "");
    stopBtn.style.display = "";
  } else if (data.mode === "hyper_play") {
    chip.classList.add("practicing");
    const seg = data.current_segment;
    label.textContent = "Hyper Play" + (seg ? " — " + segmentName(seg) : "");
    stopBtn.style.display = "";
  } else if (data.mode === "replay") {
    chip.classList.add("replaying");
    const r = data.replay;
    if (r && r.total > 0) {
      label.textContent = "Replaying " + r.total + " frames…";
    } else {
      label.textContent = "Replaying…";
    }
    stopBtn.style.display = "";
  } else if (data.mode === "cold_fill" && data.cold_fill) {
    chip.classList.add("recording");
    label.textContent =
      "Cold starts — " + data.cold_fill.current + "/" + data.cold_fill.total;
  } else if (data.mode === "fill_gap") {
    chip.classList.add("recording");
    label.textContent = "Filling gap…";
  } else {
    chip.classList.add("idle");
    label.textContent = "Idle";
  }
}

export function initHeader(): void {
  const selectorBtn = document.getElementById("game-selector")!;
  const popover = document.getElementById("game-popover") as HTMLElement;
  const filter = document.getElementById("rom-filter") as HTMLInputElement;

  selectorBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    popoverOpen = !popoverOpen;
    popover.style.display = popoverOpen ? "" : "none";
    if (popoverOpen) {
      filter.value = "";
      renderRoms("");
      filter.focus();
      if (!allRoms.length) loadRomList().then(() => renderRoms(""));
    }
  });

  filter.addEventListener("input", (e) =>
    renderRoms((e.target as HTMLInputElement).value),
  );

  document.addEventListener("click", (e) => {
    if (
      popoverOpen &&
      !popover.contains(e.target as Node) &&
      e.target !== selectorBtn
    ) {
      popoverOpen = false;
      popover.style.display = "none";
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && popoverOpen) {
      popoverOpen = false;
      popover.style.display = "none";
    }
  });

  const stopBtn = document.getElementById("mode-stop") as HTMLButtonElement;
  stopBtn.addEventListener("click", async () => {
    stopBtn.disabled = true;
    try {
      if (currentMode === "reference") await postJSON("/api/reference/stop");
      else if (currentMode === "practice") await postJSON("/api/practice/stop");
      else if (currentMode === "hyper_play") await postJSON("/api/hyperplay/stop");
      else if (currentMode === "replay") await postJSON("/api/replay/stop");
    } finally {
      stopBtn.disabled = false;
    }
  });
}

function makeLaunchItem(rom: string): HTMLLIElement {
  const li = document.createElement("li");
  li.textContent = rom.replace(/\.(sfc|smc|fig|swc)$/i, "");
  li.addEventListener("click", async () => {
    const res = await postJSON<{ status?: string; message?: string }>(
      "/api/emulator/launch",
      { rom },
    );
    if (res?.status === "error") {
      alert(res.message);
      return;
    }
    popoverOpen = false;
    (document.getElementById("game-popover") as HTMLElement).style.display =
      "none";
  });
  return li;
}

function makeSectionHeader(text: string): HTMLLIElement {
  const li = document.createElement("li");
  li.textContent = text;
  li.className = "rom-section-header";
  return li;
}

function renderRoms(filterText: string): void {
  const ul = document.getElementById("rom-list")!;
  ul.innerHTML = "";
  const lf = filterText.toLowerCase();

  if (lf) {
    // Filtering active: just show matching ROMs alphabetically.
    allRoms
      .filter((r) => r.toLowerCase().includes(lf))
      .forEach((rom) => ul.appendChild(makeLaunchItem(rom)));
    return;
  }

  // No filter: show recent sections then full alphabetical list.
  if (recentlyPlayed.length > 0) {
    ul.appendChild(makeSectionHeader("Recently Played"));
    recentlyPlayed.forEach((rom) => ul.appendChild(makeLaunchItem(rom)));
  }
  if (recentlyAdded.length > 0) {
    ul.appendChild(makeSectionHeader("Recently Added"));
    recentlyAdded.forEach((rom) => ul.appendChild(makeLaunchItem(rom)));
  }
  if (recentlyPlayed.length > 0 || recentlyAdded.length > 0) {
    const sep = document.createElement("li");
    sep.className = "rom-separator";
    ul.appendChild(sep);
  }
  allRoms.forEach((rom) => ul.appendChild(makeLaunchItem(rom)));
}

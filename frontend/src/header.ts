import { fetchJSON, postJSON } from "./api";
import { segmentName } from "./format";
import type { AppState } from "./types";

let allRoms: string[] = [];
let popoverOpen = false;

export async function loadRomList(): Promise<void> {
  const data = await fetchJSON<{ roms: string[] }>("/api/roms");
  if (data?.roms) allRoms = data.roms;
}

export function updateHeader(data: AppState): void {
  const gameEl = document.getElementById("game-name")!;
  gameEl.textContent =
    data.tcp_connected && data.game_name ? data.game_name : "No game";

  if (data.game_name) localStorage.setItem("spinlab_game_name", data.game_name);

  const chip = document.getElementById("mode-chip")!;
  const label = document.getElementById("mode-label")!;
  const stopBtn = document.getElementById("mode-stop") as HTMLElement;

  chip.className = "mode-chip";
  stopBtn.style.display = "none";

  if (!data.tcp_connected) {
    chip.classList.add("disconnected");
    label.textContent = "Disconnected";
  } else if (data.draft) {
    chip.classList.add("draft");
    label.textContent = "Draft — " + data.draft.segments_captured + " segments";
  } else if (data.mode === "reference") {
    chip.classList.add("recording");
    label.textContent = "Recording — " + (data.sections_captured || 0) + " segments";
    stopBtn.style.display = "";
  } else if (data.mode === "practice") {
    chip.classList.add("practicing");
    const seg = data.current_segment;
    label.textContent = "Practicing" + (seg ? " — " + segmentName(seg) : "");
    stopBtn.style.display = "";
  } else if (data.mode === "replay") {
    chip.classList.add("replaying");
    label.textContent = "Replaying…";
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

  const lastGame = localStorage.getItem("spinlab_game_name");
  if (lastGame) document.getElementById("game-name")!.textContent = lastGame;

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

  document.getElementById("mode-stop")!.addEventListener("click", async () => {
    const chip = document.getElementById("mode-chip")!;
    if (chip.classList.contains("recording"))
      await postJSON("/api/reference/stop");
    else if (chip.classList.contains("practicing"))
      await postJSON("/api/practice/stop");
    else if (chip.classList.contains("replaying"))
      await postJSON("/api/replay/stop");
  });
}

function renderRoms(filterText: string): void {
  const ul = document.getElementById("rom-list")!;
  ul.innerHTML = "";
  const lf = filterText.toLowerCase();
  const matches = allRoms.filter((r) => r.toLowerCase().includes(lf));
  matches.forEach((rom) => {
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
    ul.appendChild(li);
  });
}

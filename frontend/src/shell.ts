export type Page = "play" | "setup";

/** Toggle to the other page. */
export function nextPage(current: Page): Page {
  return current === "play" ? "setup" : "play";
}

/** The edge tab is labelled with the page it takes you TO. */
export function tabLabel(current: Page): string {
  return current === "play" ? "Setup" : "Play";
}

// The root element carries data-page="play|setup"; CSS keys the slide + tint
// off it. #sweep-tab is the edge tab; #sweep-tab-label holds its text.
const ROOT_ID = "sweep-shell";

export function currentPage(): Page {
  const root = document.getElementById(ROOT_ID);
  return (root?.dataset.page as Page) ?? "play";
}

function setPage(page: Page, onShow: (p: Page) => void): void {
  const root = document.getElementById(ROOT_ID);
  const label = document.getElementById("sweep-tab-label");
  if (!root) return;
  root.dataset.page = page;
  if (label) label.textContent = tabLabel(page);
  onShow(page);
}

/** Wire the edge tab. `onShow` fires once for the initial page and again on
 *  every toggle, so the caller can lazily fetch that page's data. */
export function initShell(onShow: (p: Page) => void): void {
  const tab = document.getElementById("sweep-tab");
  tab?.addEventListener("click", () => setPage(nextPage(currentPage()), onShow));
  // Fire once for the page the markup starts on (play).
  setPage(currentPage(), onShow);
}

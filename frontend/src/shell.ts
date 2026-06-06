export type Page = "play" | "setup";

/** Toggle to the other page. */
export function nextPage(current: Page): Page {
  return current === "play" ? "setup" : "play";
}

/** The edge tab is labelled with the page it takes you TO. */
export function tabLabel(current: Page): string {
  return current === "play" ? "Setup" : "Play";
}

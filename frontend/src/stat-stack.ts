/**
 * Right-aligned label / value / colored-diff vertical stack used by the route
 * bar and segment summary. Pure render — caller formats strings and decides
 * diff sign ("good" = improvement = green; "bad" = regression = red).
 */

export interface StatDiff {
  text: string;
  sign: "good" | "bad" | "neutral";
}

export interface StatStack {
  label: string;
  value: string | null;
  diff: StatDiff | null;
}

export function renderStatStack(host: HTMLElement, s: StatStack): void {
  const label = `<div class="ss-label">${s.label}</div>`;
  const valueText = s.value ?? "—";
  const value = `<div class="ss-value">${valueText}</div>`;
  const diff = s.diff
    ? `<div class="ss-diff ${s.diff.sign}">${s.diff.text}</div>`
    : "";
  host.innerHTML = `<div class="ss-stack">${label}${value}${diff}</div>`;
}

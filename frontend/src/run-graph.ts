/**
 * Run-level session-improvement graph — sits above the segment graph in the
 * live view. Plots the projected full-run time (Exp.Run) after each in-session
 * attempt as a curve declining from the session-start baseline toward a dashed
 * floor = sum of every segment's best clean clear (the theoretical best run).
 * Pure render over the /games/{id}/live-summary payload — no fetch, no chart dep.
 * See the iter-2 spec, Part 1.
 */
import { formatTime } from "./format";
import { yForTime, linePoints } from "./episode-graph";
import type { RouteSummary } from "./types";

// Same viewBox geometry as the segment graph so the two stack visually aligned.
const GEO = { left: 30, right: 392, top: 10, bottom: 104 } as const;
const VIEW_W = 400;
const VIEW_H = 124;

function placeholder(host: HTMLElement, msg: string): void {
  host.innerHTML = `<div class="rg-empty">${msg}</div>`;
}

export function renderRunGraph(host: HTMLElement, data: RouteSummary): void {
  host.innerHTML = "";
  const series = (data.run_series ?? []) as number[];
  const baseline = data.baseline_exp_run_ms ?? null;
  if (series.length === 0 || baseline == null) {
    placeholder(host, "Run trend: not enough data yet");
    return;
  }
  const floor = data.floor_total_ms ?? null;
  // Y-scale: bottom = floor when known, else the fastest in-session run; top = the
  // slowest of baseline / series. Keeps the whole curve and both lines in frame.
  const lo = floor ?? Math.min(...series);
  const hi = Math.max(baseline, ...series);
  const curve = linePoints(series, lo, hi, GEO);
  const baseY = yForTime(baseline, lo, hi, GEO.top, GEO.bottom);

  const parts: string[] = [];
  parts.push(`<line class="rg-baseline" x1="${GEO.left}" y1="${baseY.toFixed(1)}" x2="${GEO.right}" y2="${baseY.toFixed(1)}"/>`);
  parts.push(`<text x="${GEO.left}" y="${(baseY - 3).toFixed(1)}" class="rg-baseline-label">start ${formatTime(baseline)}</text>`);
  if (floor != null) {
    const floorY = yForTime(floor, lo, hi, GEO.top, GEO.bottom);
    parts.push(`<line class="rg-floor" x1="${GEO.left}" y1="${floorY.toFixed(1)}" x2="${GEO.right}" y2="${floorY.toFixed(1)}"/>`);
    parts.push(`<text x="${(GEO.right - 64).toFixed(1)}" y="${(floorY - 3).toFixed(1)}" class="rg-floor-label">floor ${formatTime(floor)}</text>`);
  }
  parts.push(`<polyline class="rg-line" fill="none" points="${curve}"/>`);

  const n = series.length;
  const lastX = GEO.left + (n > 1 ? GEO.right - GEO.left : 0);
  const lastY = yForTime(series[n - 1]!, lo, hi, GEO.top, GEO.bottom);
  parts.push(`<circle class="rg-last" cx="${lastX.toFixed(1)}" cy="${lastY.toFixed(1)}" r="3.5"/>`);

  host.innerHTML = `<svg class="rg-svg" viewBox="0 0 ${VIEW_W} ${VIEW_H}" preserveAspectRatio="none">${parts.join("")}</svg>`;
}

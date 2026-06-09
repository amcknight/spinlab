/**
 * Route bar — top section of the live practice view.
 *
 * Stable across segment switches. Renders: title (game · category), Practice
 * saved + rate + session-elapsed (live ticking via caller-supplied nowSeconds),
 * stat columns (Floors* · Exp. Run · Exp. Deaths) each as a stat-stack with
 * colored diffs vs session start. * = render only when floor improvement non-zero.
 * Pure render — no fetch. The coordinator (live-view.ts) supplies nowSeconds
 * and re-calls renderRouteBar on a setInterval tick.
 */
import { formatTime } from "./format";
import { renderStatStack, type StatStack, type StatDiff } from "./stat-stack";
import type { RouteSummary } from "./types";

// Seconds per hour — the rate is "per hour", so we divide elapsed seconds by
// this to convert to hours before computing ms/hr.
const SECONDS_PER_HOUR = 3600;
// Minute/second wall-clock divisors for the H:MM:SS session-elapsed clock.
const SECONDS_PER_MINUTE = 60;

export interface RouteBarData {
  title: string;
  gameId: string;
  routeSummary: RouteSummary;
  nowSeconds: number;  // wall clock, supplied by caller (live ticking)
}

/** ms saved / hours elapsed → 'X.Xs/hr' (lower bound on resolution; '—' on null/zero). */
export function formatRate(savedMs: number | null, elapsedSec: number): string {
  if (savedMs == null || elapsedSec <= 0) return "—";
  const msPerHour = savedMs / (elapsedSec / SECONDS_PER_HOUR);
  // formatTime expects ms and renders 'X.Ys'; tack on '/hr' to make the rate.
  return formatTime(msPerHour) + "/hr";
}

/** Whole-second elapsed → 'H:MM:SS'. Used for the session clock. */
function formatElapsedHMS(elapsedSec: number): string {
  const total = Math.max(0, Math.floor(elapsedSec));
  const h = Math.floor(total / SECONDS_PER_HOUR);
  const m = Math.floor((total % SECONDS_PER_HOUR) / SECONDS_PER_MINUTE);
  const s = total % SECONDS_PER_MINUTE;
  return h + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
}

function diffMs(deltaMs: number | null | undefined): StatDiff | null {
  // Lower expected/run → improvement → 'good'. So delta<0 means improvement.
  if (deltaMs == null) return null;
  if (deltaMs === 0) return { text: "0", sign: "neutral" };
  const sign: StatDiff["sign"] = deltaMs < 0 ? "good" : "bad";
  const text = (deltaMs < 0 ? "-" : "+") + formatTime(Math.abs(deltaMs));
  return { text, sign };
}

function diffDeaths(delta: number | null | undefined): StatDiff | null {
  if (delta == null) return null;
  if (delta === 0) return { text: "0", sign: "neutral" };
  const sign: StatDiff["sign"] = delta < 0 ? "good" : "bad";
  return { text: (delta < 0 ? "" : "+") + delta.toFixed(1), sign };
}

export function renderRouteBar(host: HTMLElement, data: RouteBarData): void {
  const rs = data.routeSummary;
  const frozenBadge = rs.session_ended_at != null
    ? `<span class="rb-frozen">(frozen)</span>`
    : "";
  const sessionStartedAt = rs.session_started_at ?? null;
  const sessionActive = sessionStartedAt != null;
  // Freeze the clock while paused: pin "now" to the pause-start and subtract
  // the accumulated paused span so elapsed (and savings/hr, which divides by
  // it) stop advancing. Resume clears session_paused_at and grows the offset.
  const pauseOffset = rs.session_pause_offset_sec ?? 0;
  const pausedAt = rs.session_paused_at ?? null;
  const effectiveNow = pausedAt != null ? pausedAt : data.nowSeconds;
  const elapsedSec = sessionActive
    ? Math.max(0, effectiveNow - sessionStartedAt - pauseOffset)
    : 0;
  const pausedBadge = pausedAt != null
    ? `<span class="rb-paused">PAUSED</span>`
    : "";
  const menuHint = rs.menu_armed
    ? `<div class="rb-menu-hint">R menu — X: Pause</div>`
    : "";

  // Tint Saved + rate green when the session is ahead (saved_ms > 0), red when
  // behind (< 0), neutral otherwise. Lower run time = saved time = improvement.
  const savedMs = rs.practice_saved_ms ?? null;
  const savedTint = savedMs == null || savedMs === 0
    ? "" : savedMs > 0 ? " rb-saved-good" : " rb-saved-bad";
  const savedBlock = sessionActive
    ? `<div class="rb-saved">Saved <span class="rb-saved-val${savedTint}">${formatTime(savedMs)}</span> · `
      + `<span class="rb-saved-val${savedTint}">${formatRate(savedMs, elapsedSec)}</span> · `
      + `${formatElapsedHMS(elapsedSec)}</div>`
    : "";

  const skippedBlock = rs.n_skipped > 0
    ? `<div class="rb-skipped dim">${rs.n_estimable} of ${rs.n_estimable + rs.n_skipped} segments estimable</div>`
    : "";

  const stacks: string[] = [];

  const floorImprovement = rs.floor_improvement_ms ?? 0;
  if (floorImprovement > 0) {
    stacks.push(stackHtml("rb-floors", {
      label: "Floor saved",
      value: formatTime(floorImprovement),
      diff: null,  // always positive when shown
    }));
  }
  stacks.push(stackHtml("rb-exp-run", {
    label: "Exp. Run",
    value: formatTime(rs.exp_run_ms),
    diff: diffMs(rs.exp_run_diff_ms),
  }));
  stacks.push(stackHtml("rb-exp-deaths", {
    label: "Exp. Deaths",
    value: rs.exp_deaths != null ? rs.exp_deaths.toFixed(1) : null,
    diff: diffDeaths(rs.exp_deaths_diff),
  }));

  host.innerHTML = `
    <div class="rb-root">
      <div class="rb-left">
        <div class="rb-title">${escapeHtml(data.title)}${frozenBadge}${pausedBadge}</div>
        ${savedBlock}
        ${menuHint}
        ${skippedBlock}
      </div>
      <div class="rb-stats">${stacks.join("")}</div>
    </div>
  `;
}

function stackHtml(slotClass: string, s: StatStack): string {
  // Render the stack into a detached div so we can grab its outerHTML — keeps
  // renderStatStack as the single source of truth for the stat markup. The
  // outer wrapper preserves slot classes (e.g. rb-floors) for CSS + tests.
  const tmp = document.createElement("div");
  tmp.className = slotClass;
  renderStatStack(tmp, s);
  return tmp.outerHTML;
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c] as string));
}

/**
 * Segment summary — middle section of the live practice view.
 *
 * Renders the current segment's name + a 4-column stat cluster
 * (Practice · Floor* · Expected · Deaths) with label/value/colored-diff
 * stacks, plus a "last completion" headline showing the most-recent episode
 * time, its rank among completions, and the per-completion decomposition
 * ('N deaths · X.Xs clean'). * = Floor column shown only when floor_diff_ms
 * is non-zero (a session improvement to highlight).
 */
import { formatTime } from "./format";
import { renderStatStack, type StatDiff, type StatStack } from "./stat-stack";
import type { LiveSegmentView } from "./types";

// Prediction-gate threshold: a segment needs at least this many clears AND
// this many deaths before the closed-form estimates are statistically
// meaningful. Mirrors em_suite_sampler._gate_passes (BE source of truth).
const GATE_MIN = 2;

export interface SegmentSummaryData {
  name: string;
  live: LiveSegmentView;
}

/** English ordinal for the last-completion rank. */
export function ordinal(n: number): string {
  const j = n % 10, k = n % 100;
  if (k >= 11 && k <= 13) return `${n}th`;
  if (j === 1) return `${n}st`;
  if (j === 2) return `${n}nd`;
  if (j === 3) return `${n}rd`;
  return `${n}th`;
}

function diffMs(deltaMs: number | null | undefined): StatDiff | null {
  if (deltaMs == null) return null;
  if (deltaMs === 0) return { text: "0", sign: "neutral" };
  const sign: StatDiff["sign"] = deltaMs < 0 ? "good" : "bad";
  const text = (deltaMs < 0 ? "-" : "+") + formatTime(Math.abs(deltaMs));
  return { text, sign };
}

function diffRate(delta: number | null | undefined): StatDiff | null {
  if (delta == null) return null;
  if (delta === 0) return { text: "0", sign: "neutral" };
  const sign: StatDiff["sign"] = delta < 0 ? "good" : "bad";
  return { text: (delta < 0 ? "" : "+") + (delta * 100).toFixed(0) + "%", sign };
}

function practiceStack(gainMs: number | null): StatStack {
  // Practice gain has no diff slot; the sign is conveyed by the value's prefix.
  const text = gainMs == null ? null : (gainMs > 0 ? "+" : "") + formatTime(gainMs);
  return { label: "Practice", value: text, diff: null };
}

export function renderSegmentSummary(host: HTMLElement, data: SegmentSummaryData): void {
  const v = data.live;

  if (!v.ready) {
    const needS = Math.max(0, GATE_MIN - v.n_successes);
    const needD = Math.max(0, GATE_MIN - v.n_deaths);
    const parts: string[] = [];
    if (needS) parts.push(`${needS} more clear${needS === 1 ? "" : "s"}`);
    if (needD) parts.push(`${needD} more death${needD === 1 ? "" : "s"}`);
    host.innerHTML = `
      <div class="sg-root">
        <div class="sg-name">${escapeHtml(data.name)}</div>
        <div class="sg-headline sg-empty">Not enough data yet — need ${parts.join(" and ") || "more attempts"}</div>
      </div>
    `;
    return;
  }

  const stacks: string[] = [];
  stacks.push(stackHtml("sg-practice", practiceStack(v.practice_gain_ms)));
  if ((v.floor_diff_ms ?? 0) !== 0 && v.floor_ms != null) {
    stacks.push(stackHtml("sg-floor", {
      label: "Floor",
      value: formatTime(v.floor_ms),
      diff: diffMs(v.floor_diff_ms),
    }));
  }
  stacks.push(stackHtml("sg-expected", {
    label: "Expected",
    value: formatTime(v.expected_episode_ms),
    diff: diffMs(v.expected_episode_diff_ms),
  }));
  stacks.push(stackHtml("sg-deaths", {
    label: "Deaths",
    value: (v.death_rate * 100).toFixed(0) + "%",
    diff: diffRate(v.death_rate_diff),
  }));

  const headline = v.last_episode_ms == null
    ? `<div class="sg-headline sg-empty">No completed runs yet</div>`
    : `<div class="sg-headline">
         <div class="sg-headline-label">last completion</div>
         <div class="sg-headline-value">${formatTime(v.last_episode_ms)}</div>
         <div class="sg-headline-rank">${v.last_rank != null ? ordinal(v.last_rank) + " best" : ""}</div>
         <div class="sg-headline-decomp">${v.last_deaths ?? 0} death${(v.last_deaths ?? 0) === 1 ? "" : "s"} · ${formatTime(v.last_clean_ms)} clean</div>
       </div>`;

  host.innerHTML = `
    <div class="sg-root">
      <div class="sg-header">
        <div class="sg-name">${escapeHtml(data.name)}</div>
        <div class="sg-stats">${stacks.join("")}</div>
      </div>
      ${headline}
    </div>
  `;
}

function stackHtml(slotClass: string, s: StatStack): string {
  // Mirror the route-bar.ts pattern: render into a detached div so the
  // slot-class wrapper (sg-practice / sg-floor / …) is preserved for CSS +
  // tests while renderStatStack stays the single source of truth.
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

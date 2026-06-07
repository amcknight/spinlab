import type { SegmentLike } from "./types";

// Shown on every model view when there is no active reference run (distinct
// from the "no segments / no data yet" message used when a run IS active but
// has zero traversed segments). Single source so the copy can't drift across
// the Model table, Simulator, and Segments views. See the reference-run-selector
// design (§A.3, §D, Notes): "Empty-state (no active run) must be handled on
// every page, not just Practice."
export const NO_ACTIVE_RUN_MESSAGE = "No reference run selected — pick or record one";

/**
 * Pick a model-view empty-state message. No active run gets the spec wording
 * (NO_ACTIVE_RUN_MESSAGE); a run that IS active but has zero traversed segments
 * gets the caller's generic "no segments / no data yet" copy.
 */
export function emptyStateMessage(hasActiveRun: boolean, noSegmentsCopy: string): string {
  return hasActiveRun ? noSegmentsCopy : NO_ACTIVE_RUN_MESSAGE;
}

export function shortEndpoint(type: string, ordinal: number): string {
  return type === "entrance" ? "start" : type === "goal" ? "goal" : "cp" + ordinal;
}

export function segmentName(s: SegmentLike): string {
  if (s.description) return s.description;
  return "L" + s.level_number + " " + shortEndpoint(s.start_type, s.start_ordinal) + " → " + shortEndpoint(s.end_type, s.end_ordinal);
}

// Time-unit divisors. We work in deciseconds (tenths of a second) so that
// rounding to one decimal happens ONCE on the whole duration — this keeps a
// value like 119.96s from rendering "1m60.0s" (rounds to 120.0s → "2m00.0s").
const DECISECONDS_PER_SECOND = 10;
const SECONDS_PER_MINUTE = 60;
const SECONDS_PER_HOUR = 3600;
const SECONDS_PER_DAY = 86400;

/** Two-digit zero-padded integer, for the carried-over h/m/s fields. */
function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/**
 * ms → human duration with smart units. Sub-minute stays "X.Xs"; above that it
 * carries into m/h/d, zero-padding the lower fields ("1m52.6s", "1h01m01.5s",
 * "1d01h01m01.5s"). One decimal of seconds throughout; negatives get a "-".
 */
export function formatTime(ms: number | null | undefined): string {
  if (ms == null) return "—";
  const neg = ms < 0;
  const totalDs = Math.round(Math.abs(ms) / 100);  // deciseconds, rounded once
  const tenth = totalDs % DECISECONDS_PER_SECOND;
  let totalSec = Math.floor(totalDs / DECISECONDS_PER_SECOND);
  const days = Math.floor(totalSec / SECONDS_PER_DAY);
  totalSec %= SECONDS_PER_DAY;
  const hours = Math.floor(totalSec / SECONDS_PER_HOUR);
  totalSec %= SECONDS_PER_HOUR;
  const minutes = Math.floor(totalSec / SECONDS_PER_MINUTE);
  const seconds = totalSec % SECONDS_PER_MINUTE;

  let out: string;
  if (days > 0) out = `${days}d${pad2(hours)}h${pad2(minutes)}m${pad2(seconds)}.${tenth}s`;
  else if (hours > 0) out = `${hours}h${pad2(minutes)}m${pad2(seconds)}.${tenth}s`;
  else if (minutes > 0) out = `${minutes}m${pad2(seconds)}.${tenth}s`;
  else out = `${seconds}.${tenth}s`;
  return (neg ? "-" : "") + out;
}

export function elapsedStr(startedAt: string | null | undefined): string {
  if (!startedAt) return "";
  const start = new Date(startedAt);
  if (!Number.isFinite(start.getTime())) return "0:00";
  const diff = Math.floor((Date.now() - start.getTime()) / 1000);
  const m = Math.floor(diff / 60);
  const s = diff % 60;
  return m + ":" + String(s).padStart(2, "0");
}

export function formatSavings(ms: number | null | undefined): string | null {
  if (ms == null) return null;
  const sign = ms >= 0 ? "+" : "-";
  const s = Math.abs(ms) / 1000;
  return sign + s.toFixed(1) + "s";
}

/** Compact "time ago" with a unit suffix. Months are skipped so `m` always
 *  means minutes: now / 47s / 34m / 7h / 6d / 8w / 7y. nowMs defaults to Date.now(). */
export function formatAgo(iso: string | null | undefined, nowMs: number = Date.now()): string {
  if (iso == null) return "";
  const secs = Math.max(0, Math.floor((nowMs - Date.parse(iso)) / 1000));
  if (secs < 10) return "now";
  if (secs < 60) return secs + "s";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return mins + "m";
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours + "h";
  const days = Math.floor(hours / 24);
  if (days < 7) return days + "d";
  const weeks = Math.floor(days / 7);
  if (weeks < 52) return weeks + "w";
  return Math.floor(days / 365) + "y";
}

/** Render a fraction (0.13) as a whole-number percent ("13%"). Empty for null. */
export function formatPct(frac: number | null | undefined): string {
  if (frac == null) return "";
  return Math.round(frac * 100) + "%";
}

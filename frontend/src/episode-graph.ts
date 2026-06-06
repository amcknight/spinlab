/**
 * Episode-time trend graph — the default occupant of the live view's graph slot.
 *
 * Plots one point per completed episode (episode time, incl. deaths+reload) as a
 * blue line that sinks toward a diagonal "floor" (the running-best clean clear).
 * Per-completion death counts sit under each point. Seconds Y-axis, lower = faster.
 * Pure render over the /segments/{id}/live payload — no fetch, no chart dep.
 * See the D-Live spec, "Graph #1".
 */
import { formatTime } from "./format";
import type { EpisodePoint, LiveSegmentView } from "./types";

// SVG geometry (viewBox units). Left gutter holds y-axis labels; bottom band
// holds the per-completion death counts.
const GEO = { left: 30, right: 392, top: 10, bottom: 104 } as const;
const VIEW_W = 400;
const VIEW_H = 124;
const DEATH_Y = 120;
// Target number of y-axis ticks. niceTimeTicks rounds to a clean time step, so
// the actual count varies; 4 is the aim point for a readable, uncrowded axis.
const AXIS_TICKS = 4;

/** Map a time (ms) to a y pixel: lower time = lower on the chart (larger y),
 *  higher time = top. NaN-safe when lo == hi. */
export function yForTime(
  v: number, lo: number, hi: number, top: number, bottom: number,
): number {
  const span = hi - lo || 1;
  const frac = (hi - v) / span;
  return top + frac * (bottom - top);
}

/** Build an SVG polyline points string for a series of times against a shared
 *  [lo, hi] scale. `null` entries are skipped (the floor line can have gaps
 *  before the first completed clean). NaN-safe. */
export function linePoints(
  values: (number | null)[],
  lo: number, hi: number,
  geo: { left: number; right: number; top: number; bottom: number },
): string {
  const n = values.length;
  const step = n > 1 ? (geo.right - geo.left) / (n - 1) : 0;
  const out: string[] = [];
  values.forEach((v, i) => {
    if (v == null) return;
    const x = (geo.left + i * step).toFixed(1);
    const y = yForTime(v, lo, hi, geo.top, geo.bottom).toFixed(1);
    out.push(`${x},${y}`);
  });
  return out.join(" ");
}

// Nice tick steps in ms, ascending: 0.1s, 0.25s, 0.5s, 1s, 2s, 5s, 10s, 15s,
// 30s, 1m, 2m, 5m, 10m. niceTimeTicks snaps to the smallest of these that is
// >= the raw step, so labels land on human-friendly round time values.
const NICE_TIME_STEPS_MS = [
  100, 250, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000,
];
// Float-comparison slop (ms) so the loop's final tick at exactly hiMs isn't
// dropped by accumulated addition error.
const TICK_EPSILON_MS = 1e-6;

/** Round-number y-axis tick values (ms) across [loMs, hiMs], aiming for
 *  ~targetCount ticks but snapping to a clean time step (see NICE_TIME_STEPS_MS). */
export function niceTimeTicks(loMs: number, hiMs: number, targetCount = 4): number[] {
  const range = hiMs - loMs;
  if (range <= 0) return [loMs];
  const rawStep = range / targetCount;
  const step = NICE_TIME_STEPS_MS.find(s => s >= rawStep) ?? NICE_TIME_STEPS_MS[NICE_TIME_STEPS_MS.length - 1]!;
  const first = Math.ceil(loMs / step) * step;
  const ticks: number[] = [];
  for (let t = first; t <= hiMs + TICK_EPSILON_MS; t += step) ticks.push(t);
  return ticks;
}

/** Round-number y-axis ticks across [lo, hi], formatted in seconds. */
export function axisTicks(
  lo: number, hi: number, count: number,
): { ms: number; label: string }[] {
  if (count < 1) return [];
  if (count === 1) return [{ ms: hi, label: formatTime(hi) }];
  return niceTimeTicks(lo, hi, count).map(ms => ({ ms, label: formatTime(ms) }));
}

/** x position + death count for each completed episode. */
export function deathLabels(
  points: EpisodePoint[],
  geo: { left: number; right: number },
): { x: number; deaths: number }[] {
  const n = points.length;
  const step = n > 1 ? (geo.right - geo.left) / (n - 1) : 0;
  return points.map((p, i) => ({ x: geo.left + i * step, deaths: p.deaths }));
}

function placeholder(host: HTMLElement, msg: string): void {
  host.innerHTML = `<div class="eg-empty">${msg}</div>`;
}

export function renderEpisodeGraph(host: HTMLElement, data: LiveSegmentView): void {
  host.innerHTML = "";
  if (!data.ready) {
    placeholder(host, "Not enough data yet");
    return;
  }
  const points = (data.series ?? []) as unknown as EpisodePoint[];
  if (points.length === 0 || data.floor_ms == null) {
    placeholder(host, "No completed runs yet");
    return;
  }

  const episodes = points.map(p => p.episode_ms);
  const lo = data.floor_ms;
  const hi = Math.max(...episodes);
  const episodePts = linePoints(episodes, lo, hi, GEO);
  const floorPts = linePoints(points.map(p => p.running_floor_ms), lo, hi, GEO);
  const floorY = yForTime(lo, lo, hi, GEO.top, GEO.bottom);

  const ticks = axisTicks(lo, hi, AXIS_TICKS)
    .map(t => `<text x="2" y="${(yForTime(t.ms, lo, hi, GEO.top, GEO.bottom) + 3).toFixed(1)}" class="eg-axis">${t.label}</text>`)
    .join("");
  const deaths = deathLabels(points, GEO)
    .filter(d => d.deaths > 0)  // a clean (0-death) completion gets no label
    .map(d => `<text x="${d.x.toFixed(1)}" y="${DEATH_Y}" class="eg-death" text-anchor="middle">${d.deaths}</text>`)
    .join("");
  const lastX = (GEO.left + (points.length > 1 ? GEO.right - GEO.left : 0)).toFixed(1);
  const lastY = yForTime(episodes[episodes.length - 1]!, lo, hi, GEO.top, GEO.bottom).toFixed(1);

  host.innerHTML = `
    <svg class="eg-svg" viewBox="0 0 ${VIEW_W} ${VIEW_H}" preserveAspectRatio="none">
      ${ticks}
      <polyline class="eg-floor" fill="none" points="${floorPts}"/>
      <text x="${GEO.right - 48}" y="${(floorY - 3).toFixed(1)}" class="eg-floor-label">floor ${formatTime(lo)}</text>
      <polyline class="eg-line" fill="none" points="${episodePts}"/>
      <circle class="eg-last" cx="${lastX}" cy="${lastY}" r="3.5"/>
      ${deaths}
    </svg>
  `;
}

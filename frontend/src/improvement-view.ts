/**
 * "Am I improving on this segment?" view for the practice card.
 *
 * Renders a verdict, a recent clear-time sparkline (inline SVG — no chart dep),
 * death-rate / consistency / gap-to-gold stats, and the PB. Fed per attempt by
 * GET /api/segments/{id}/progress. See the practice UI overhaul spec §A.
 */
import { fetchJSON } from "./api";
import { formatTime, formatSavings } from "./format";
import type { SegmentProgress } from "./types";

export function verdictLabel(verdict: string): string {
  switch (verdict) {
    case "faster": return "↓ Getting faster";
    case "slower": return "↑ Getting slower";
    case "holding": return "→ Holding steady";
    default: return "Not enough data yet";
  }
}

const VERDICT_CLASS: Record<string, string> = {
  faster: "iv-good", slower: "iv-bad", holding: "iv-neutral", not_ready: "iv-dim",
};

// The data gate: a segment needs ≥2 clears AND ≥2 deaths before the model runs.
// Mirrors em_suite_sampler._gate_passes; keep in sync if that threshold moves.
const GATE_MIN_OUTCOMES = 2;

/** Map clear-time values to an SVG polyline points string. Lower time = lower y
 * (drawn higher). Single/empty input is NaN-safe. */
export function sparklinePoints(values: number[], w: number, h: number): string {
  if (values.length === 0) return "";
  const lo = Math.min(...values), hi = Math.max(...values);
  const span = hi - lo || 1;
  const step = values.length > 1 ? w / (values.length - 1) : 0;
  return values
    .map((v, i) => {
      const x = (i * step).toFixed(1);
      const y = (h - ((v - lo) / span) * h).toFixed(1);
      return `${x},${y}`;
    })
    .join(" ");
}

export function renderImprovementView(host: HTMLElement, p: SegmentProgress): void {
  host.innerHTML = "";
  const cls = VERDICT_CLASS[p.verdict] ?? "iv-dim";

  if (!p.ready) {
    const needS = Math.max(0, GATE_MIN_OUTCOMES - p.n_successes);
    const needD = Math.max(0, GATE_MIN_OUTCOMES - p.n_deaths);
    const parts: string[] = [];
    if (needS) parts.push(`${needS} more clear${needS === 1 ? "" : "s"}`);
    if (needD) parts.push(`${needD} more death${needD === 1 ? "" : "s"}`);
    host.innerHTML =
      `<div class="iv-verdict iv-dim">Not enough data yet</div>` +
      `<div class="iv-sub">need ${parts.join(" and ") || "more attempts"} to model this segment</div>`;
    return;
  }

  // Pass nullable straight to formatTime (renders "—"); a ready segment can
  // still have a null EMA, and 0.0s would be a lie.
  const now = p.now_clear_ms;
  const baseline = p.baseline_clear_ms;
  const w = 320, hgt = 56;
  const pts = sparklinePoints(p.trend_ms, w, hgt);

  host.innerHTML = `
    <div class="iv-verdict ${cls}">${verdictLabel(p.verdict)}</div>
    <div class="iv-sub">recent <b>${formatTime(now)}</b> vs baseline ${formatTime(baseline)}</div>
    <svg class="iv-spark" viewBox="0 0 ${w} ${hgt}" preserveAspectRatio="none">
      <polyline fill="none" stroke="currentColor" stroke-width="2" points="${pts}"/>
    </svg>
    <div class="iv-stats">
      <span><label>Deaths</label>${(p.death_rate * 100).toFixed(0)}%</span>
      <span><label>Spread</label>${p.consistency_ms == null ? "—" : "±" + formatTime(p.consistency_ms)}</span>
      <span><label>PB</label>${formatTime(p.pb_ms)}</span>
      <span><label>vs gold</label>${p.gap_to_gold_ms == null ? "—" : (formatSavings(-p.gap_to_gold_ms) ?? "—")}</span>
    </div>
  `;
}

let _host: HTMLElement | null = null;

/** Fetch + render for a segment. Safe to call per SSE push. Errors render an
 * inline message rather than throwing (mirrors loadAndRenderEmSuitePanel). */
export async function loadAndRenderImprovementView(
  segmentId: string, host: HTMLElement,
): Promise<void> {
  _host = host;
  try {
    const data = await fetchJSON<SegmentProgress>(
      `/api/segments/${encodeURIComponent(segmentId)}/progress`);
    if (!data) {
      host.innerHTML = `<div class="iv-sub iv-dim">progress unavailable</div>`;
      return;
    }
    renderImprovementView(host, data);
  } catch (err) {
    host.innerHTML = `<div class="iv-sub iv-dim">progress unavailable: ${err}</div>`;
  }
}

export function destroyImprovementView(): void {
  if (_host) { _host.innerHTML = ""; _host = null; }
}

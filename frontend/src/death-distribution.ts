// Fixed bin count. Twenty is enough to see distribution shape in a
// segment detail card, few enough to read at a glance, and predictable
// (no Freedman-Diaconis surprises across segments).
export const BIN_COUNT = 20;

// One-second rounding for the upper edge so x-axis labels land on
// whole seconds without manual tick configuration.
const HI_ROUND_MS = 1000;

export interface Bin {
  // Left edge (inclusive) and right edge (exclusive) of this bin, in ms.
  // The topmost bin's right edge is inclusive (clamped).
  lo_ms: number;
  hi_ms: number;
  deaths: number;
  completions: number;
}

export interface BinSummary {
  bins: Bin[];
  lo: number;
  hi: number;
}

/**
 * Bucket death and completion samples into shared bins by time_ms.
 * Sample weights are ignored — bar heights are raw counts. Weighted
 * statistics (means) are surfaced via marker overlays, not bar height.
 *
 * The range always starts at 0; the upper edge is the max sample value
 * rounded up to the nearest second. Empty input returns BIN_COUNT
 * zero-counts with lo=hi=0.
 */
export function binSamples(
  deaths: [number, number][],
  completions: [number, number][],
): BinSummary {
  const empty = (): Bin[] => Array.from({ length: BIN_COUNT }, (_, i) => ({
    lo_ms: 0, hi_ms: 0, deaths: 0, completions: 0,
  }));

  if (deaths.length === 0 && completions.length === 0) {
    return { bins: empty(), lo: 0, hi: 0 };
  }

  let maxMs = 0;
  for (const [t] of deaths) if (t > maxMs) maxMs = t;
  for (const [t] of completions) if (t > maxMs) maxMs = t;
  const hi = Math.ceil(maxMs / HI_ROUND_MS) * HI_ROUND_MS;
  const lo = 0;
  const width = (hi - lo) / BIN_COUNT;

  const bins: Bin[] = Array.from({ length: BIN_COUNT }, (_, i) => ({
    lo_ms: lo + i * width,
    hi_ms: lo + (i + 1) * width,
    deaths: 0,
    completions: 0,
  }));

  const placeIdx = (t: number): number => {
    if (width === 0) return 0;
    let idx = Math.floor((t - lo) / width);
    if (idx >= BIN_COUNT) idx = BIN_COUNT - 1;
    if (idx < 0) idx = 0;
    return idx;
  };

  for (const [t] of deaths) bins[placeIdx(t)]!.deaths += 1;
  for (const [t] of completions) bins[placeIdx(t)]!.completions += 1;

  return { bins, lo, hi };
}

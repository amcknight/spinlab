import type { ModelSegment, Estimate, CurrentSegment, AppState } from "./types";

/** Extract the selected estimate for a model segment (total time series). */
export function selectedEstimate(seg: ModelSegment): Estimate | null {
  const output = seg.model_outputs[seg.selected_model];
  return output?.total ?? null;
}

/** Extract the selected estimate from the current practice segment. */
export function currentEstimate(seg: CurrentSegment): Estimate | null {
  const output = seg.model_outputs[seg.selected_model];
  return output?.total ?? null;
}

/** Format ms_per_attempt for display, or return null if unavailable. */
export function formatTrend(est: Estimate | null): string | null {
  if (!est || est.ms_per_attempt == null) return null;
  return est.ms_per_attempt.toFixed(1) + " ms/att";
}

/** Determine whether practice controls should allow starting. */
export function canStartPractice(state: AppState): boolean {
  return state.emu_connected && state.game_id !== null && state.mode === "idle";
}

/** Determine whether hyper play controls should allow starting. */
export function canStartHyperPlay(state: AppState): boolean {
  return state.emu_connected && state.game_id !== null && state.mode === "idle";
}

/** practice_gain_ms from the segment's selected model output, or null. */
export function selectedGain(seg: ModelSegment): number | null {
  const output = seg.model_outputs[seg.selected_model];
  return output?.practice_gain_ms ?? null;
}

/** Room = Expected - Floor (ms), or null when either is missing. */
export function roomMs(est: Estimate | null): number | null {
  if (!est || est.expected_ms == null || est.floor_ms == null) return null;
  return est.expected_ms - est.floor_ms;
}

/** Room% = (Expected - Floor) / Floor, or null when floor is 0/missing. */
export function roomPct(est: Estimate | null): number | null {
  if (!est || est.expected_ms == null || est.floor_ms == null || est.floor_ms === 0) {
    return null;
  }
  return (est.expected_ms - est.floor_ms) / est.floor_ms;
}

/** Trend% = gain / Expected = value per wall-clock second; null when undefined. */
export function trendPct(gain: number | null, est: Estimate | null): number | null {
  if (gain == null || !est || est.expected_ms == null || est.expected_ms === 0) {
    return null;
  }
  return gain / est.expected_ms;
}

/** Sort comparator: highest Room% first, segments with no Room% last. */
export function compareByRoomPctDesc(a: ModelSegment, b: ModelSegment): number {
  const ra = roomPct(selectedEstimate(a));
  const rb = roomPct(selectedEstimate(b));
  if (ra == null && rb == null) return 0;
  if (ra == null) return 1;
  if (rb == null) return -1;
  return rb - ra;
}

export type PracticeCardState = "live" | "frozen" | "hidden";

/** Decide the practice card's state. `live` = actively practicing/hyper-playing
 *  a segment; `frozen` = idle but a clean-stopped session persists and we have a
 *  remembered segment to re-render; `hidden` otherwise. */
export function practiceCardState(args: {
  mode: string;
  hasCurrentSegment: boolean;
  hasFrozenSession: boolean;
  hasLastPracticed: boolean;
  hasGameId: boolean;
}): PracticeCardState {
  const isLive = (args.mode === "practice" || args.mode === "hyper_play")
    && args.hasCurrentSegment;
  if (isLive) return "live";
  if (args.hasFrozenSession && args.hasLastPracticed && args.hasGameId) return "frozen";
  return "hidden";
}

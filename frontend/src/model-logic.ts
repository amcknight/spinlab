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
  return state.tcp_connected && state.game_id !== null && state.mode === "idle";
}

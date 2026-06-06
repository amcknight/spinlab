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

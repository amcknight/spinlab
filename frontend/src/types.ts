// -- Enums as string unions --

export type Mode =
  | "idle"
  | "reference"
  | "practice"
  | "replay"
  | "fill_gap"
  | "cold_fill";

export type EndpointType = "entrance" | "checkpoint" | "goal";

// -- API response shapes --

export interface Estimate {
  expected_ms: number | null;
  ms_per_attempt: number | null;
  floor_ms: number | null;
}

export interface ModelOutput {
  total: Estimate;
  clean: Estimate;
}

/** Shape of a segment in the /api/model response. */
export interface ModelSegment {
  segment_id: string;
  description: string;
  level_number: number;
  start_type: string;
  start_ordinal: number;
  end_type: string;
  end_ordinal: number;
  selected_model: string;
  model_outputs: Record<string, ModelOutput>;
  n_completed: number;
  n_attempts: number;
  gold_ms: number | null;
  clean_gold_ms: number | null;
}

export interface EstimatorInfo {
  name: string;
  display_name: string;
}

/** GET /api/model */
export interface ModelData {
  estimator: string | null;
  estimators: EstimatorInfo[];
  allocator_weights: Record<string, number> | null;
  segments: ModelSegment[];
}

export interface ParamDef {
  name: string;
  display_name: string;
  default: number;
  min: number;
  max: number;
  step: number;
  description: string;
  value: number;
}

/** GET /api/estimator-params */
export interface TuningData {
  estimator: string;
  params: ParamDef[];
}

export interface DraftState {
  run_id: string;
  segments_captured: number;
}

export interface ColdFillState {
  current: number;
  total: number;
  segment_label: string;
}

export interface SessionInfo {
  id: string;
  started_at: string;
  segments_attempted: number;
  segments_completed: number;
  saved_total_ms: number | null;
  saved_clean_ms: number | null;
}

/** Segment as it appears in current_segment (practice state). */
export interface CurrentSegment {
  id: string;
  game_id: string;
  level_number: number;
  start_type: string;
  start_ordinal: number;
  end_type: string;
  end_ordinal: number;
  description: string;
  attempt_count: number;
  model_outputs: Record<string, ModelOutput>;
  selected_model: string;
  state_path: string | null;
}

export interface RecentAttempt {
  id: number;
  segment_id: string;
  completed: number;
  time_ms: number | null;
  description: string;
  level_number: number;
  start_type: string;
  start_ordinal: number;
  end_type: string;
  end_ordinal: number;
}

/** GET /api/state and SSE event payload. */
export interface AppState {
  mode: Mode;
  tcp_connected: boolean;
  game_id: string | null;
  game_name: string | null;
  current_segment: CurrentSegment | null;
  recent: RecentAttempt[];
  session: SessionInfo | null;
  sections_captured: number;
  allocator_weights: Record<string, number> | null;
  estimator: string | null;
  capture_run_id: string | null;
  draft: DraftState | null;
  cold_fill: ColdFillState | null;
}

/** Segment as returned by /api/references/{id}/segments. */
export interface ReferenceSegment {
  id: string;
  game_id: string;
  level_number: number;
  start_type: string;
  start_ordinal: number;
  end_type: string;
  end_ordinal: number;
  description: string;
  active: number;
  ordinal: number | null;
  reference_id: string | null;
  state_path: string | null;
}

export interface Reference {
  id: string;
  game_id: string;
  name: string;
  created_at: string;
  active: number;
  draft: number;
  has_spinrec: boolean;
}

/** Any object with segment-naming fields (used by segmentName/shortSegName). */
export interface SegmentLike {
  description?: string;
  level_number: number;
  start_type: string;
  start_ordinal: number;
  end_type: string;
  end_ordinal: number;
}

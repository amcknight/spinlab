/** Any object with segment-naming fields (used by segmentName/shortSegName). */
export interface SegmentLike {
  description?: string;
  level_number: number;
  start_type: string;
  start_ordinal: number;
  end_type: string;
  end_ordinal: number;
}

/** Application state received from API. */
export interface AppState {
  tcp_connected: boolean;
  game_name?: string;
  mode: "idle" | "reference" | "practice" | "replay";
  draft?: {
    segments_captured: number;
  };
  sections_captured?: number;
  current_segment?: SegmentLike;
}

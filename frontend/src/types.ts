/**
 * Public type surface for the rest of the frontend.
 *
 * Everything in this file is either:
 *   1) re-exported from the auto-generated `api-types.ts` (which is produced
 *      from the FastAPI app's OpenAPI schema by `npm run gen-types`), or
 *   2) a frontend-only convenience type that doesn't appear in any API
 *      response.
 *
 * Don't add response/request shapes here by hand — edit
 * `python/spinlab/api_schemas.py` and let codegen pick it up.
 */
import type { components } from "./api-types";

type S = components["schemas"];

// ---- API response shapes (auto-generated) ---------------------------------

export type AppState = S["AppState"];
export type Mode = AppState["mode"];

export type Estimate = S["Estimate"];
export type ModelOutput = S["ModelOutput"];

export type CurrentSegment = S["CurrentSegment"];
export type RecentAttempt = S["RecentAttempt"];
export type SessionInfo = S["SessionInfo"];
export type ReplayState = S["ReplayState"];
export type PausedRunState = S["PausedRunState"];
export type ColdFillState = S["ColdFillState"];

export type ModelData = S["ModelData"];
export type ModelSegment = S["ModelSegment"];

export type ApiSegment = S["ApiSegment"];

export type Reference = S["Reference"];
export type CaptureSession = S["CaptureSession"];

export type SegmentHistory = S["SegmentHistory"];
export type SegmentAttempt = S["SegmentAttempt"];
export type EstimatorCurves = S["EstimatorCurves"];
export type EstimatorSeries = S["EstimatorSeries"];
export type ColdDistribution = S["ColdDistribution"];
export type ColdBin = S["ColdBin"];

export type EmSuiteMatrixResponse = S["EmSuiteMatrixResponse"];

// Practice Simulation Engine
export type PracticeEngineState = S["PracticeEngineState"];
export type PracticeEngineSegmentState = S["PracticeEngineSegmentState"];
export type PracticeEngineUngated = S["PracticeEngineUngated"];
export type PracticeEngineEvaluateRequest = S["PracticeEngineEvaluateRequest"];
export type PracticeEngineEvaluateResponse = S["PracticeEngineEvaluateResponse"];
export type PracticeEnginePerSegmentValue = S["PracticeEnginePerSegmentValue"];
export type PracticeEngineTotalTimeSummary = S["PracticeEngineTotalTimeSummary"];
export type SegmentProgress = S["SegmentProgressResponse"];

export type LiveSegmentView = S["LiveSegmentViewResponse"];
export type RouteSummary = S["RouteSummaryResponse"];

// ---- Request bodies (auto-generated) --------------------------------------

export type AttemptPatch = S["AttemptPatchRequest"];
export type AttemptPatchResponse = S["AttemptPatchResponse"];

// ---- Frontend-only conveniences -------------------------------------------

/** Any object with segment-naming fields, used by segmentName/shortSegName.
 *  Not part of any API response — intentionally structural so it accepts
 *  any segment-shaped object without coupling to a specific API type. */
export interface SegmentLike {
  description?: string | null;
  level_number: number;
  start_type: string;
  start_ordinal: number;
  end_type: string;
  end_ordinal: number;
}

export type EndpointType = "entrance" | "checkpoint" | "goal";

/** One completed-episode point in a LiveSegmentView.series. The API types
 *  `series` loosely (schema `list[dict]`), so this names the item shape the
 *  episode graph relies on. Keep in sync with live_view.py's series dicts. */
export interface EpisodePoint {
  episode_ms: number;
  deaths: number;
  clean_ms: number | null;
  running_floor_ms: number | null;
}

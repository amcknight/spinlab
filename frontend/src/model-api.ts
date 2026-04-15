import { fetchJSON, postJSON } from "./api";
import type { ModelData, TuningData } from "./types";

export async function fetchModelData(): Promise<ModelData | null> {
  return fetchJSON<ModelData>("/api/model");
}

export async function fetchTuningData(): Promise<TuningData | null> {
  return fetchJSON<TuningData>("/api/estimator-params");
}

export async function postEstimator(name: string): Promise<void> {
  await postJSON("/api/estimator", { name });
}

export async function postTuningParams(params: Record<string, number>): Promise<void> {
  await postJSON("/api/estimator-params", { params });
}

export async function postAllocatorWeights(weights: Record<string, number>): Promise<void> {
  await postJSON("/api/allocator-weights", weights);
}

export async function postPracticeStart(): Promise<void> {
  await postJSON("/api/practice/start");
}

export async function postPracticeStop(): Promise<void> {
  await postJSON("/api/practice/stop");
}

export async function postSpeedrunStart(): Promise<void> {
  await postJSON("/api/speedrun/start");
}

export async function postSpeedrunStop(): Promise<void> {
  await postJSON("/api/speedrun/stop");
}

// Uses raw fetch + silent .catch() to preserve original semantics: network
// failures are intentionally swallowed because the next SSE state update
// will reconcile UI state. fetchJSON/postJSON would surface errors via toast.
export async function patchAttemptInvalidated(id: number, invalidated: boolean): Promise<void> {
  await fetch(`/api/attempts/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ invalidated }),
  }).catch(() => {
    // Silently ignore network errors; next SSE update will reflect truth.
  });
}

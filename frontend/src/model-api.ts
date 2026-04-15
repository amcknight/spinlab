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

export async function patchAttemptInvalidated(id: number, invalidated: boolean): Promise<void> {
  await fetch(`/api/attempts/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ invalidated }),
  }).catch(() => {
    // Silently ignore network errors; next SSE update will reflect truth.
  });
}

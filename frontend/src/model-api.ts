import { fetchJSON, postJSON } from "./api";
import type { ModelData } from "./types";

export async function fetchModelData(): Promise<ModelData | null> {
  return fetchJSON<ModelData>("/api/model");
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

/** GrindOne: start practice pinned to one segment, repeating it every cycle. */
export async function postGrind(segmentId: string): Promise<void> {
  await postJSON("/api/practice/grind", { segment_id: segmentId });
}

export async function postHyperPlayStart(): Promise<void> {
  await postJSON("/api/hyperplay/start");
}

export async function postHyperPlayStop(): Promise<void> {
  await postJSON("/api/hyperplay/stop");
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

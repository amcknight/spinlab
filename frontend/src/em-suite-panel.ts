/**
 * EMA-suite panel orchestrator for the practice card.
 *
 * Owns the fetch + the three sub-renders (matrix, param-history, slope-
 * matrices) and the Chart.js lifecycle for the line-chart instances.
 * The practice card calls `loadAndRenderEmSuitePanel` on every state
 * update; this module dedupes fetches by segment id and tears down old
 * chart instances before redrawing so Chart.js doesn't leak on detached
 * canvases.
 *
 * Practice and Hyperplay share the same surface — `updatePracticeCard`
 * gates on `mode === "practice" || "hyper_play"` already, so this module
 * is reused for both.
 */
import type { Chart } from "chart.js";

import { fetchJSON } from "./api";
import { renderEmSuiteMatrix } from "./em-suite-matrix";
import { renderParamHistory } from "./em-suite-params";
import { renderSlopeMatrices } from "./em-suite-slopes";
import type { EmSuiteMatrixResponse } from "./types";

let _charts: Chart[] = [];
let _hostElement: HTMLElement | null = null;

function destroyCharts(): void {
  for (const c of _charts) {
    try {
      c.destroy();
    } catch {
      // ignore — chart may already be torn down by a re-render
    }
  }
  _charts = [];
}

/**
 * Fetch the EMA-suite matrix payload for a segment and render the full
 * three-section panel (matrix + param history + slope matrices) into
 * `host`. Safe to call repeatedly; destroys prior Chart instances first.
 */
export async function loadAndRenderEmSuitePanel(
  segmentId: string,
  host: HTMLElement,
): Promise<void> {
  _hostElement = host;
  let data: EmSuiteMatrixResponse | null = null;
  try {
    data = await fetchJSON<EmSuiteMatrixResponse>(
      `/api/segments/${encodeURIComponent(segmentId)}/em-suite-matrix`,
    );
  } catch (err) {
    destroyCharts();
    host.innerHTML =
      `<div class="em-suite-matrix__error">Matrix fetch failed: ${err}</div>`;
    return;
  }
  if (!data) {
    destroyCharts();
    host.innerHTML =
      `<div class="em-suite-matrix__error">Matrix unavailable</div>`;
    return;
  }

  // Tear down prior chart instances BEFORE clearing the DOM. Chart.js holds
  // references via its registry; clearing the canvas elements first orphans
  // those references and triggers warnings on re-create with the same id.
  destroyCharts();
  host.innerHTML = "";

  const matrixHost = document.createElement("div");
  matrixHost.className = "em-suite-matrix-host";
  host.appendChild(matrixHost);
  renderEmSuiteMatrix(matrixHost, data);

  const paramsHost = document.createElement("div");
  host.appendChild(paramsHost);
  _charts = renderParamHistory(paramsHost, data);

  const slopesHost = document.createElement("div");
  host.appendChild(slopesHost);
  renderSlopeMatrices(slopesHost, data);
}

/**
 * Tear down any active charts and clear the panel host. Call when leaving
 * Practice / Hyperplay mode so Chart instances don't outlive their
 * canvases and the panel doesn't show stale state on the next entry.
 */
export function destroyEmSuitePanel(): void {
  destroyCharts();
  if (_hostElement) {
    _hostElement.innerHTML = "";
    _hostElement = null;
  }
}

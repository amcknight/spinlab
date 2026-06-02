/**
 * Practice Simulation Engine — dashboard panel.
 *
 * Renders policy + objective controls and surfaces objective values,
 * per-segment value attribution, and total-time histogram. Read-only.
 */
import type {
  PracticeEngineState,
  PracticeEngineEvaluateRequest,
  PracticeEngineEvaluateResponse,
} from "./types";
import { segmentName } from "./format";

// seg_id → short display name, captured at render so the results table (which
// only carries seg_id) can label rows the same way the rest of the dashboard does.
let segNameById: Record<string, string> = {};

type PolicyName = "no_reset" | "target_paced";
type ObjectiveName =
  | "expected_wall_clock_per_attempt"
  | "expected_total_finished_time"
  | "q"
  | "quantile"
  | "p_pb_this_session";

interface BuildArgs {
  policy: PolicyName;
  cumSplits: Record<string, number>;
  slack: number;
  objective: ObjectiveName;
  objectiveCtx: Record<string, number>;
}

export function buildEvaluateRequest(args: BuildArgs): PracticeEngineEvaluateRequest {
  const req: PracticeEngineEvaluateRequest = {
    policy: args.policy,
    policy_kwargs: {},
    objective: args.objective,
    objective_ctx: args.objectiveCtx,
  };
  if (args.policy === "target_paced") {
    req.policy_kwargs = {
      cum_splits_ms: args.cumSplits,
      slack: args.slack,
    };
  }
  return req;
}

export async function fetchState(): Promise<PracticeEngineState> {
  const resp = await fetch("/api/practice-engine/state");
  if (!resp.ok) throw new Error(`/api/practice-engine/state ${resp.status}`);
  return resp.json();
}

export async function fetchEvaluate(
  body: PracticeEngineEvaluateRequest,
): Promise<PracticeEngineEvaluateResponse> {
  const resp = await fetch("/api/practice-engine/evaluate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`/api/practice-engine/evaluate ${resp.status}`);
  return resp.json();
}

export function renderPracticeEnginePanel(
  container: HTMLElement,
  state: PracticeEngineState,
): void {
  container.innerHTML = "";

  // Capture seg_id → short name for the results table (keyed only by seg_id).
  segNameById = {};
  for (const seg of state.gated_segments) segNameById[seg.seg_id] = segmentName(seg);
  for (const seg of state.ungated_segments) segNameById[seg.seg_id] = segmentName(seg);

  const header = document.createElement("h2");
  header.textContent = "Practice Simulator";
  container.appendChild(header);

  const help = document.createElement("details");
  help.className = "pe-help";
  help.innerHTML = `
    <summary>What is this? (how to use)</summary>
    <div class="pe-help-body">
      <p>Imagines thousands of full runs from your real per-segment data to answer
      two questions: <em>how your runs are likely to go</em>, and <em>which single
      segment is most worth practicing next</em>.</p>
      <ol>
        <li><strong>Policy</strong> — <code>no_reset</code>: simulate every run to the
        end. <code>target_paced</code>: quit a run once it falls behind your split.</li>
        <li><strong>Objective</strong> — what to measure/maximize (expected time,
        <code>q</code> = fraction under your target, per-session PB probability, …).</li>
        <li>Fill the inputs your objective needs (<code>target_ms</code> for
        <code>q</code>; <code>session_remaining_ms</code> for <code>p_pb</code>). For
        <code>target_paced</code>, set per-segment cumulative splits — or click
        <em>Fill cum-splits from gold</em>.</li>
        <li>Hit <strong>Recompute</strong>.</li>
      </ol>
      <p>The results table ranks segments by <strong>Value/sec</strong> — the
      objective improvement per second of practice. Practice the top row.
      <strong>Ungated</strong> segments lack enough data (need ≥2 successes and ≥2
      deaths) and are excluded.</p>
    </div>
  `;
  container.appendChild(help);

  const controls = document.createElement("div");
  controls.className = "pe-controls";
  controls.innerHTML = `
    <label>Policy:
      <select id="pe-policy">
        <option value="no_reset">no_reset</option>
        <option value="target_paced">target_paced</option>
      </select>
    </label>
    <label>Objective:
      <select id="pe-objective">
        <option value="expected_wall_clock_per_attempt">expected_wall_clock_per_attempt</option>
        <option value="expected_total_finished_time">expected_total_finished_time</option>
        <option value="q">q(target)</option>
        <option value="quantile">quantile(p)</option>
        <option value="p_pb_this_session">p_pb_this_session</option>
      </select>
    </label>
    <label>Slack:
      <input id="pe-slack" type="number" step="0.05" value="0" min="0" max="1" />
    </label>
    <label>target_ms:
      <input id="pe-target-ms" type="number" step="100" placeholder="e.g. 12000" />
    </label>
    <label>p (quantile):
      <input id="pe-p" type="number" step="0.05" min="0" max="1" placeholder="0.5" />
    </label>
    <label>session_remaining_ms:
      <input id="pe-h" type="number" step="60000" placeholder="e.g. 10440000" />
    </label>
    <button id="pe-recompute">Recompute</button>
  `;
  container.appendChild(controls);

  // Per-segment threshold input table (used by target_paced)
  // The "fill from gold" button pre-populates with cumulative golds.
  const segInputWrap = document.createElement("div");
  segInputWrap.innerHTML = `
    <button id="pe-fill-gold" type="button">Fill cum-splits from gold</button>
  `;
  container.appendChild(segInputWrap);

  const segInput = document.createElement("table");
  segInput.className = "pe-segments-input";
  segInput.innerHTML = `
    <thead><tr><th>Segment</th><th>Cumulative split (ms)</th><th>Gold (ms)</th></tr></thead>
    <tbody>
      ${state.gated_segments.map(seg => `
        <tr data-seg-id="${seg.seg_id}">
          <td>${segmentName(seg)}</td>
          <td><input class="pe-seg-split" type="number" step="100" data-seg-id="${seg.seg_id}" /></td>
          <td class="pe-seg-gold" data-gold-ms="${seg.gold_ms ?? ""}">${seg.gold_ms ?? "—"}</td>
        </tr>
      `).join("")}
    </tbody>
  `;
  container.appendChild(segInput);

  // Wire the fill-from-gold button: cumulative sum of per-segment golds.
  const fillBtn = segInputWrap.querySelector<HTMLButtonElement>("#pe-fill-gold");
  if (fillBtn) {
    fillBtn.addEventListener("click", () => {
      let cum = 0;
      state.gated_segments.forEach(seg => {
        if (seg.gold_ms !== null && seg.gold_ms !== undefined) {
          cum += seg.gold_ms;
          const input = segInput.querySelector<HTMLInputElement>(
            `.pe-seg-split[data-seg-id="${seg.seg_id}"]`,
          );
          if (input) input.value = String(cum);
        }
      });
    });
  }

  // Headline objective value
  const headline = document.createElement("div");
  headline.className = "pe-headline";
  headline.id = "pe-headline";
  headline.textContent = "(Click Recompute)";
  container.appendChild(headline);

  // Histogram canvas (placeholder for future chart.js wiring)
  const canvasWrap = document.createElement("div");
  canvasWrap.className = "pe-histogram-wrap";
  canvasWrap.innerHTML = `<canvas id="pe-histogram"></canvas>`;
  container.appendChild(canvasWrap);

  // Per-segment value table
  const valuesTable = document.createElement("table");
  valuesTable.className = "pe-values";
  valuesTable.innerHTML = `
    <thead><tr>
      <th>Segment</th><th>E[sample(0)]</th><th>E[sample(1)]</th>
      <th>Δ</th><th>Value</th><th>Value/sec</th>
    </tr></thead>
    <tbody id="pe-values-body"></tbody>
  `;
  container.appendChild(valuesTable);

  // Ungated segments
  if (state.ungated_segments.length > 0) {
    const ungated = document.createElement("div");
    ungated.className = "pe-ungated";
    ungated.innerHTML = `<h3>Ungated</h3><ul>${
      state.ungated_segments.map(u => `<li>${segmentName(u)}: ${u.reason}</li>`).join("")
    }</ul>`;
    container.appendChild(ungated);
  }
}

export function updatePanelResults(
  container: HTMLElement,
  response: PracticeEngineEvaluateResponse,
): void {
  const headline = container.querySelector<HTMLDivElement>("#pe-headline");
  if (headline) {
    headline.textContent = response.objective_value === null
      ? "(None — gate failed)"
      : `Objective: ${response.objective_value.toFixed(2)}`;
  }
  const body = container.querySelector<HTMLTableSectionElement>("#pe-values-body");
  if (body) {
    body.innerHTML = response.per_segment_values.map(psv => `
      <tr>
        <td>${segNameById[psv.seg_id] ?? psv.seg_id}</td>
        <td>${psv.e_sample_0_ms.toFixed(0)}</td>
        <td>${psv.e_sample_1_ms.toFixed(0)}</td>
        <td>${(psv.e_sample_0_ms - psv.e_sample_1_ms).toFixed(0)}</td>
        <td>${psv.value.toFixed(2)}</td>
        <td>${psv.value_per_second === null ? "—" : psv.value_per_second.toExponential(2)}</td>
      </tr>
    `).join("");
  }
}

export async function initPracticeEnginePanel(): Promise<void> {
  const container = document.getElementById("practice-engine-panel");
  if (!container) return;
  const state = await fetchState();
  renderPracticeEnginePanel(container, state);

  const recompute = container.querySelector<HTMLButtonElement>("#pe-recompute");
  if (!recompute) return;
  recompute.addEventListener("click", async () => {
    const policy = (container.querySelector<HTMLSelectElement>("#pe-policy"))?.value as PolicyName;
    const objective = (container.querySelector<HTMLSelectElement>("#pe-objective"))?.value as ObjectiveName;
    const slack = parseFloat((container.querySelector<HTMLInputElement>("#pe-slack"))?.value || "0");
    const cumSplits: Record<string, number> = {};
    container.querySelectorAll<HTMLInputElement>(".pe-seg-split").forEach(input => {
      const segId = input.dataset.segId;
      const value = parseFloat(input.value);
      if (segId && !isNaN(value)) {
        cumSplits[segId] = value;
      }
    });
    const objectiveCtx: Record<string, number> = {};
    const targetMs = parseFloat((container.querySelector<HTMLInputElement>("#pe-target-ms"))?.value || "");
    if (!isNaN(targetMs)) objectiveCtx.target_ms = targetMs;
    const p = parseFloat((container.querySelector<HTMLInputElement>("#pe-p"))?.value || "");
    if (!isNaN(p)) objectiveCtx.p = p;
    const h = parseFloat((container.querySelector<HTMLInputElement>("#pe-h"))?.value || "");
    if (!isNaN(h)) objectiveCtx.session_remaining_ms = h;
    const req = buildEvaluateRequest({
      policy, cumSplits, slack, objective, objectiveCtx,
    });
    const resp = await fetchEvaluate(req);
    updatePanelResults(container, resp);
  });
}

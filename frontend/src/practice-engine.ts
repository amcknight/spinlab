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
import { segmentName, formatTime, formatSavings } from "./format";

// seg_id → short display name, captured at render so the results table (which
// only carries seg_id) can label rows the same way the rest of the dashboard does.
let segNameById: Record<string, string> = {};

// Human labels + units for each objective, so the headline reads like a sentence
// instead of a raw number. Probabilities render as %, everything else as time.
const OBJECTIVE_LABELS: Record<ObjectiveName, string> = {
  expected_wall_clock_per_attempt: "Avg time per attempt",
  expected_total_finished_time: "Avg finished-run time",
  q: "Chance of finishing under target",
  quantile: "Finish-time at quantile",
  p_pb_this_session: "Chance of a PB this session",
};
const PROB_OBJECTIVES: ReadonlySet<string> = new Set(["q", "p_pb_this_session"]);

// objective_ctx keys each objective requires, with friendly names for the prompt.
// Without them the backend objective can't be computed (would 422), so the panel
// asks for the input instead of firing a doomed request.
const REQUIRED_CTX: Record<string, ReadonlyArray<[string, string]>> = {
  q: [["target_ms", "a target time"]],
  quantile: [["p", "a quantile (0–1)"]],
  p_pb_this_session: [["target_ms", "a target time"], ["session_remaining_ms", "session time left"]],
};

function formatObjectiveValue(name: string, value: number): string {
  return PROB_OBJECTIVES.has(name) ? (value * 100).toFixed(1) + "%" : formatTime(value);
}

/** Signed objective improvement from one practice attempt (baseline − swap). */
function formatObjectiveDelta(name: string, value: number): string {
  if (PROB_OBJECTIVES.has(name)) {
    const pp = value * 100;
    return (pp >= 0 ? "+" : "") + pp.toFixed(2) + "pp";
  }
  return formatSavings(value) ?? "—";
}

/** Show only the inputs the selected policy/objective actually consume. */
function applyControlVisibility(container: HTMLElement): void {
  const policy = container.querySelector<HTMLSelectElement>("#pe-policy")?.value;
  const objective = container.querySelector<HTMLSelectElement>("#pe-objective")?.value;
  const show = (sel: string, on: boolean) => {
    const el = container.querySelector<HTMLElement>(sel);
    if (el) el.style.display = on ? "" : "none";
  };
  const paced = policy === "target_paced";
  show("#pe-slack-label", paced);
  show("#pe-target-paced-section", paced);
  show("#pe-target-ms-label", objective === "q" || objective === "p_pb_this_session");
  show("#pe-p-label", objective === "quantile");
  show("#pe-h-label", objective === "p_pb_this_session");
}

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
      <p>Imagines thousands of full runs from your real per-segment data to rank
      <em>which segment is most worth practicing next</em> and show how your runs
      are likely to go.</p>
      <ul>
        <li><strong>Practice next</strong> lists segments best-first by the expected
        gain from one more practice attempt. Practice the top one.</li>
        <li>A segment needs <strong>≥2 clears and ≥2 deaths</strong> before it can be
        modelled; until then it shows greyed with what it still needs.</li>
        <li><strong>Advanced</strong> lets you change what's measured (objective) and
        the reset rule (policy). The default is average time per attempt.</li>
      </ul>
    </div>
  `;
  container.appendChild(help);

  const gatedCount = state.gated_segments.length;
  const total = gatedCount + state.ungated_segments.length;
  const status = document.createElement("div");
  status.className = "pe-status";
  status.innerHTML = total === 0
    ? "No segments tracked yet — make a reference run and practice."
    : `<strong>${gatedCount}</strong> of ${total} segment${total === 1 ? "" : "s"} `
      + `ready. The rest need ≥2 clears and ≥2 deaths each.`;
  container.appendChild(status);

  const headline = document.createElement("div");
  headline.className = "pe-headline";
  headline.id = "pe-headline";
  headline.textContent = "(computing…)";
  container.appendChild(headline);

  // Practice-next: ranked gated segments (filled by updatePanelResults), then
  // not-ready segments inline (greyed) — one block, no separate "Ungated" header.
  const rank = document.createElement("div");
  rank.className = "pe-ranklist";
  rank.innerHTML = `
    <div class="pe-ranklist-title">Practice next</div>
    <ol id="pe-rank-body"></ol>
    <ul id="pe-notready">
      ${state.ungated_segments.map(u =>
        `<li class="pe-nr"><span class="pe-nr-seg">${segmentName(u)}</span>` +
        `<span class="dim"> — ${u.reason}</span></li>`).join("")}
    </ul>
  `;
  container.appendChild(rank);

  const advanced = document.createElement("details");
  advanced.className = "pe-advanced";
  advanced.innerHTML = `<summary>Advanced</summary>`;
  const advBody = document.createElement("div");
  advBody.className = "pe-advanced-body";
  advanced.appendChild(advBody);
  container.appendChild(advanced);

  const controls = document.createElement("div");
  controls.className = "pe-controls";
  controls.innerHTML = `
    <label>Policy
      <select id="pe-policy">
        <option value="no_reset">no_reset</option>
        <option value="target_paced">target_paced</option>
      </select>
    </label>
    <label>Objective
      <select id="pe-objective">
        <option value="expected_wall_clock_per_attempt">avg time / attempt</option>
        <option value="expected_total_finished_time">avg finished-run time</option>
        <option value="q">chance under target</option>
        <option value="quantile">finish-time quantile</option>
        <option value="p_pb_this_session">PB chance this session</option>
      </select>
    </label>
    <label id="pe-slack-label" title="How far over your split a run may drift before it resets">Slack
      <input id="pe-slack" type="number" step="0.05" value="0" min="0" max="1" />
    </label>
    <label id="pe-target-ms-label">Target (ms)
      <input id="pe-target-ms" type="number" step="100" placeholder="e.g. 12000" />
    </label>
    <label id="pe-p-label">Quantile p
      <input id="pe-p" type="number" step="0.05" min="0" max="1" placeholder="0.5" />
    </label>
    <label id="pe-h-label">Session left (ms)
      <input id="pe-h" type="number" step="60000" placeholder="e.g. 10440000" />
    </label>
    <button id="pe-recompute" title="Recompute now (also runs automatically on change)">Recompute</button>
  `;
  advBody.appendChild(controls);

  const segInputWrap = document.createElement("div");
  segInputWrap.id = "pe-target-paced-section";
  segInputWrap.innerHTML = `
    <p class="pe-caption">Target pace per segment — only used by the
      <code>target_paced</code> policy. A run resets when it falls this far behind.</p>
    <button id="pe-fill-gold" type="button">Fill cum-splits from gold</button>
    <table class="pe-segments-input">
      <thead><tr><th>Segment</th><th>Cumulative split</th><th>Gold</th></tr></thead>
      <tbody>
        ${state.gated_segments.map(seg => `
          <tr data-seg-id="${seg.seg_id}">
            <td>${segmentName(seg)}</td>
            <td><input class="pe-seg-split" type="number" step="100" data-seg-id="${seg.seg_id}" /></td>
            <td class="pe-seg-gold dim" data-gold-ms="${seg.gold_ms ?? ""}">${formatTime(seg.gold_ms)}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
  advBody.appendChild(segInputWrap);

  const fillBtn = segInputWrap.querySelector<HTMLButtonElement>("#pe-fill-gold");
  if (fillBtn) {
    fillBtn.addEventListener("click", () => {
      let cum = 0;
      state.gated_segments.forEach(seg => {
        if (seg.gold_ms !== null && seg.gold_ms !== undefined) {
          cum += seg.gold_ms;
          const input = segInputWrap.querySelector<HTMLInputElement>(
            `.pe-seg-split[data-seg-id="${seg.seg_id}"]`,
          );
          if (input) input.value = String(cum);
        }
      });
    });
  }
}

export function updatePanelResults(
  container: HTMLElement,
  response: PracticeEngineEvaluateResponse,
): void {
  const objName = container.querySelector<HTMLSelectElement>("#pe-objective")?.value
    ?? "expected_wall_clock_per_attempt";
  const label = OBJECTIVE_LABELS[objName as ObjectiveName] ?? "Objective";
  const headline = container.querySelector<HTMLDivElement>("#pe-headline");
  if (headline) {
    headline.textContent = response.objective_value === null
      ? `${label}: — (not enough data)`
      : `${label}: ${formatObjectiveValue(objName, response.objective_value)}`;
  }
  const body = container.querySelector<HTMLOListElement>("#pe-rank-body");
  if (body) {
    // Best-first by value per second of practice (the ranking metric); nulls last.
    const ranked = [...response.per_segment_values].sort(
      (a, b) => (b.value_per_second ?? -Infinity) - (a.value_per_second ?? -Infinity),
    );
    body.innerHTML = ranked.map((psv, i) => `
      <li>
        <span class="pe-rank-n">${i + 1}</span>
        <span class="pe-rank-seg">${segNameById[psv.seg_id] ?? psv.seg_id}</span>
        <span class="pe-rank-gain" title="Expected gain from one more practice attempt">${formatObjectiveDelta(objName, psv.value)}</span>
        <span class="pe-rank-times dim">${formatTime(psv.e_sample_0_ms)} → ${formatTime(psv.e_sample_1_ms)}</span>
      </li>
    `).join("");
  }
}

/** Gather control values and run one evaluation, painting the results. Failures
 * (e.g. no game loaded) surface in the headline rather than throwing. */
async function runRecompute(container: HTMLElement): Promise<void> {
  const policy = container.querySelector<HTMLSelectElement>("#pe-policy")?.value as PolicyName;
  const objective = container.querySelector<HTMLSelectElement>("#pe-objective")?.value as ObjectiveName;
  const slack = parseFloat(container.querySelector<HTMLInputElement>("#pe-slack")?.value || "0");
  const cumSplits: Record<string, number> = {};
  container.querySelectorAll<HTMLInputElement>(".pe-seg-split").forEach(input => {
    const segId = input.dataset.segId;
    const value = parseFloat(input.value);
    if (segId && !isNaN(value)) cumSplits[segId] = value;
  });
  const objectiveCtx: Record<string, number> = {};
  const targetMs = parseFloat(container.querySelector<HTMLInputElement>("#pe-target-ms")?.value || "");
  if (!isNaN(targetMs)) objectiveCtx.target_ms = targetMs;
  const p = parseFloat(container.querySelector<HTMLInputElement>("#pe-p")?.value || "");
  if (!isNaN(p)) objectiveCtx.p = p;
  const h = parseFloat(container.querySelector<HTMLInputElement>("#pe-h")?.value || "");
  if (!isNaN(h)) objectiveCtx.session_remaining_ms = h;

  // Skip the request (and the backend 422) when the objective's inputs are blank.
  const missing = (REQUIRED_CTX[objective] ?? []).filter(([k]) => !(k in objectiveCtx));
  if (missing.length > 0) {
    const headline = container.querySelector<HTMLDivElement>("#pe-headline");
    if (headline) headline.textContent = `Enter ${missing.map(([, name]) => name).join(" and ")} to compute.`;
    const body = container.querySelector<HTMLOListElement>("#pe-rank-body");
    if (body) body.innerHTML = "";
    return;
  }

  const req = buildEvaluateRequest({ policy, cumSplits, slack, objective, objectiveCtx });
  const resp = await fetchEvaluate(req);
  updatePanelResults(container, resp);
}

let recomputeTimer: ReturnType<typeof setTimeout> | undefined;
function scheduleRecompute(container: HTMLElement): void {
  if (recomputeTimer) clearTimeout(recomputeTimer);
  recomputeTimer = setTimeout(() => { void runRecompute(container); }, 250);
}

export async function initPracticeEnginePanel(): Promise<void> {
  const container = document.getElementById("practice-engine-panel");
  if (!container) return;
  const state = await fetchState();
  renderPracticeEnginePanel(container, state);
  applyControlVisibility(container);

  // Recompute automatically: on load, and whenever a control changes — no
  // button-hunting. Policy/objective also re-evaluate which inputs are relevant.
  container.querySelector("#pe-recompute")?.addEventListener(
    "click", () => { void runRecompute(container); });
  container.querySelectorAll<HTMLElement>("#pe-policy, #pe-objective").forEach(el =>
    el.addEventListener("change", () => {
      applyControlVisibility(container);
      scheduleRecompute(container);
    }));
  container.querySelectorAll<HTMLElement>(
    "#pe-slack, #pe-target-ms, #pe-p, #pe-h, .pe-seg-split",
  ).forEach(el => el.addEventListener("input", () => scheduleRecompute(container)));
  container.querySelector("#pe-fill-gold")?.addEventListener(
    "click", () => scheduleRecompute(container));

  await runRecompute(container);
}

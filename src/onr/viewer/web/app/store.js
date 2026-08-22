// Central UI state + URL-hash router. No framework — app.js orchestrates
// polling and rendering against this module.

import { walkSteps } from "./format.js";

export const VIEWS = ["trajectory", "tree", "timeline", "overview", "workflow"];

export const state = {
  runtime: null,
  run: null,
  stepsPayload: null,
  steps: [],          // top-level steps
  flat: [],           // depth-first [{step, depth, parent}]
  byId: new Map(),    // step_id → step
  bySeq: new Map(),   // seq → step

  missionId: "",
  view: "trajectory",
  selectedStepId: "",
  detailTab: "",      // "" = auto-pick per step
  search: "",
  errorsOnly: false,

  errors: { runtime: null, steps: null, run: null },
  signature: "",
  loadedOnce: false,

  treeCollapsed: new Set(),  // step_ids collapsed in the tree view
  artifactRef: "",           // artifact open in the overview viewer
  dismissedWarnings: new Set(),
  workflowNode: "",          // expanded workflow node: "", a node id, or "all"
  phaseFilter: "",           // trajectory phase/component filter (deep links)
};

export function setStepsPayload(payload) {
  state.stepsPayload = payload;
  state.steps = payload && Array.isArray(payload.steps) ? payload.steps : [];
  state.flat = [...walkSteps(state.steps)];
  state.byId = new Map();
  state.bySeq = new Map();
  for (const { step } of state.flat) {
    state.byId.set(step.step_id, step);
    // seq is only unique per source (op-log vs agent records) — first wins.
    if (!state.bySeq.has(step.seq)) state.bySeq.set(step.seq, step);
  }
}

export function signatureOf(payload, run) {
  if (!payload) return "empty";
  const steps = payload.steps || [];
  const last = steps.length ? steps[steps.length - 1] : null;
  const countKind = { llm: 0, tool: 0, decision: 0, feedback: 0, error: 0 };
  const revisions = [];
  for (const { step } of walkSteps(steps)) {
    countKind[step.kind] = (countKind[step.kind] || 0) + 1;
    if (step.status === "error") countKind.error += 1;
    const draftLength = (step.tool_calls || []).reduce(
      (total, call) => total + (call.arguments_text || "").length, 0);
    revisions.push([
      step.step_id,
      step.revision || 0,
      step.completion_state || "complete",
      (step.reasoning || "").length,
      (step.content || "").length,
      draftLength,
    ].join(":"));
  }
  return [
    payload.mission_id,
    steps.length,
    countKind.llm, countKind.tool, countKind.decision, countKind.feedback, countKind.error,
    last ? last.finished_at : "",
    revisions.join(","),
    run ? run.status : "",
    run && run.aggregates ? run.aggregates.step_count : "",
  ].join("|");
}

export function selectedStep() {
  return state.selectedStepId ? state.byId.get(state.selectedStepId) || null : null;
}

/* ------------------------------ hash router ------------------------------ */

export function readHash() {
  const raw = location.hash.replace(/^#/, "");
  const params = new URLSearchParams(raw);
  const out = {};
  if (params.get("mission")) out.mission = params.get("mission");
  if (params.get("view") && VIEWS.includes(params.get("view"))) out.view = params.get("view");
  if (params.get("step")) out.step = params.get("step"); // seq number or step_id
  if (params.get("node")) out.node = params.get("node"); // workflow L1 expansion
  if (params.get("phase")) out.phase = params.get("phase"); // trajectory deep-link filter
  return out;
}

export function currentHash() {
  const params = new URLSearchParams();
  if (state.missionId) params.set("mission", state.missionId);
  params.set("view", state.view);
  if (state.view === "workflow" && state.workflowNode) params.set("node", state.workflowNode);
  if (state.view === "trajectory" && state.phaseFilter) params.set("phase", state.phaseFilter);
  const selected = selectedStep();
  if (selected) params.set("step", selected.step_id);
  return "#" + params.toString();
}

export function writeHash({ push = false } = {}) {
  const next = currentHash();
  if (location.hash === next) return;
  if (push) history.pushState(null, "", next);
  else history.replaceState(null, "", next);
}

// Resolve a hash `step` value: step_id first (unique, e.g. "hyper-agent:7"),
// then a bare seq number as a convenience (first match wins).
export function resolveHashStep(value) {
  if (!value) return null;
  if (state.byId.has(value)) return state.byId.get(value);
  const asNumber = Number(value);
  if (Number.isFinite(asNumber) && state.bySeq.has(asNumber)) return state.bySeq.get(asNumber);
  return null;
}

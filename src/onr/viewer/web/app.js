// Entry point: boots the store, wires the hash router, runs the 1.5 s poll
// loop (stale-while-revalidate — the UI always renders from local state and
// swaps in fresh data when it lands), and owns keyboard navigation.

import { h, icon } from "./app/dom.js";
import {
  state, setStepsPayload, signatureOf, readHash, writeHash, resolveHashStep, selectedStep, VIEWS,
} from "./app/store.js";
import { getRuntime, getRun, getSteps, mockUsed } from "./app/api.js";
import { renderHeader } from "./app/header.js";
import { renderTrajectory, visibleTrajectorySteps } from "./app/trajectory.js";
import { renderTree, visibleTreeSteps } from "./app/tree.js";
import { renderTimeline, visibleTimelineSteps } from "./app/timeline.js";
import { renderOverview } from "./app/overview.js";
import { renderWorkflow } from "./app/workflow.js";
import { renderDetail } from "./app/detail.js";
import { resetJsonViewState } from "./app/jsonview.js";
import { invalidateArtifactCache } from "./app/artifact.js";

const POLL_MS = 1500;

const headerRoot = document.getElementById("app-header");
const bannerRoot = document.getElementById("app-banner");
const viewRoot = document.getElementById("view-root");

const viewRenderers = {
  trajectory: renderTrajectory,
  tree: renderTree,
  timeline: renderTimeline,
  overview: renderOverview,
  workflow: renderWorkflow,
};

function visibleStepsForView() {
  if (state.view === "trajectory") return visibleTrajectorySteps();
  if (state.view === "tree") return visibleTreeSteps();
  if (state.view === "timeline") return visibleTimelineSteps();
  return [];
}

/* ------------------------------ rendering ------------------------------ */

let lastHeaderSig = "";
function headerSignature() {
  const runtime = state.runtime;
  return [
    state.signature,
    state.missionId,
    state.view,
    runtime ? runtime.active + ":" + (runtime.mission_ids || []).join(",") : "none",
    mockUsed.runtime, mockUsed.steps, mockUsed.run,
    state.errors.runtime ? "err" : "",
  ].join("|");
}

function renderHeaderIfChanged() {
  const sig = headerSignature();
  if (sig === lastHeaderSig) return;
  lastHeaderSig = sig;
  renderHeader(headerRoot, actions);
}

function renderBanner() {
  bannerRoot.replaceChildren();
  const warnings = new Set([
    ...((state.stepsPayload && state.stepsPayload.warnings) || []),
    ...((state.run && state.run.warnings) || []),
  ]);
  for (const text of warnings) {
    if (state.dismissedWarnings.has(text)) continue;
    bannerRoot.append(h("div", { class: "banner tone-running", role: "status" },
      icon("alert", 13),
      h("span", { class: "banner-text" }, text),
      h("button", {
        class: "banner-dismiss", type: "button", "aria-label": "Dismiss warning",
        onclick: () => { state.dismissedWarnings.add(text); renderBanner(); },
      }, icon("x", 11))));
  }
  const hardError = state.errors.steps || state.errors.run;
  if (hardError) {
    bannerRoot.append(h("div", { class: "banner tone-error", role: "alert" },
      icon("alert", 13),
      h("span", { class: "banner-text" }, "Live data failed: " + hardError + ". The last good data stays on screen."),
      h("button", { class: "banner-dismiss", type: "button", onclick: () => { state.errors.steps = null; state.errors.run = null; renderBanner(); } }, icon("x", 11))));
  }
}

function preserveScrollAndFocus(render) {
  const scrollEl = viewRoot.querySelector(".nav-scroll, .tl-scroll, .overview, .workflow");
  const scrollTop = scrollEl ? scrollEl.scrollTop : 0;
  const active = document.activeElement;
  const keepFocus = active && active.dataset && active.dataset.testid === "step-search";
  const caret = keepFocus ? active.selectionStart : 0;
  render();
  const nextScroll = viewRoot.querySelector(".nav-scroll, .tl-scroll, .overview, .workflow");
  if (nextScroll) nextScroll.scrollTop = scrollTop;
  if (keepFocus) {
    const input = viewRoot.querySelector('[data-testid="step-search"]');
    if (input) {
      input.focus();
      try { input.setSelectionRange(caret, caret); } catch (_) { /* search inputs accept this in practice */ }
    }
  }
}

const actions = {
  selectMission(missionId) {
    if (!missionId || missionId === state.missionId) return;
    state.missionId = missionId;
    state.selectedStepId = "";
    state.detailTab = "";
    state.artifactRef = "";
    state.phaseFilter = "";
    state.signature = "";
    state.run = null;
    setStepsPayload(null);
    resetJsonViewState();
    invalidateArtifactCache();
    state.treeCollapsed.clear();
    writeHash({ push: true });
    renderAll();
    refreshMission();
  },
  setView(view) {
    if (!VIEWS.includes(view) || view === state.view) return;
    state.view = view;
    writeHash({ push: true });
    renderAll();
  },
  setWorkflowNode(node) {
    if (node === state.workflowNode) return;
    state.workflowNode = node;
    writeHash({ push: true });
    renderAll();
  },
  clearPhaseFilter() {
    if (!state.phaseFilter) return;
    state.phaseFilter = "";
    writeHash({ push: true });
    renderAll();
  },
  selectStep(stepId, { push = true, scroll = false } = {}) {
    if (!state.byId.has(stepId)) return;
    state.selectedStepId = stepId;
    state.detailTab = ""; // auto-pick for the new step
    writeHash({ push });
    // cheap path: update row highlight in place, re-render only the detail pane
    viewRoot.querySelectorAll("[data-step-id]").forEach((el) => {
      const on = el.dataset.stepId === stepId;
      el.classList.toggle("selected", on);
      if (el.getAttribute("role") === "treeitem") el.setAttribute("aria-selected", String(on));
    });
    const pane = viewRoot.querySelector('[data-testid="detail-panel"]');
    if (pane) renderDetailInto(pane);
    if (scroll) {
      const row = viewRoot.querySelector(".selected[data-step-id]");
      if (row) row.scrollIntoView({ block: "nearest" });
    }
  },
  renderView(options = {}) {
    preserveScrollAndFocus(() => renderView(options));
  },
  renderDetail() {
    const pane = viewRoot.querySelector('[data-testid="detail-panel"]');
    if (pane) renderDetailInto(pane);
  },
};

function renderDetailInto(pane) {
  renderDetail(pane, selectedStep(), actions);
}

function renderView() {
  const render = viewRenderers[state.view] || renderTrajectory;
  render(viewRoot, actions);
}

function renderAll() {
  renderHeaderIfChanged();
  renderBanner();
  renderView();
}

/* ------------------------------ polling ------------------------------ */

async function refreshMission() {
  if (!state.missionId) return;
  const missionId = state.missionId;
  try {
    const [run, steps] = await Promise.all([getRun(missionId), getSteps(missionId)]);
    if (missionId !== state.missionId) return; // switched while fetching
    state.errors.run = null;
    state.errors.steps = null;
    state.run = run;
    const signature = signatureOf(steps, run);
    setStepsPayload(steps);
    state.loadedOnce = true;
    if (signature !== state.signature) {
      state.signature = signature;
      if (state.selectedStepId && !state.byId.has(state.selectedStepId)) {
        state.selectedStepId = "";
      }
      renderAll();
    }
  } catch (error) {
    if (missionId !== state.missionId) return;
    state.errors.run = error.message;
    state.errors.steps = error.message;
    state.loadedOnce = true;
    renderBanner();
  }
}

async function poll() {
  try {
    const runtime = await getRuntime();
    state.runtime = runtime;
    state.errors.runtime = null;
    if (!state.missionId) {
      const hash = readHash();
      const missions = Array.isArray(runtime.mission_ids) ? runtime.mission_ids : [];
      if (hash.mission) state.missionId = hash.mission;
      else if (missions.length) state.missionId = missions[0];
    }
    renderHeaderIfChanged();
    await refreshMission();
  } catch (error) {
    state.errors.runtime = error.message;
    renderHeaderIfChanged();
    renderBanner();
  } finally {
    setTimeout(poll, POLL_MS);
  }
}

/* ------------------------------ keyboard ------------------------------ */

document.addEventListener("keydown", (event) => {
  const target = event.target;
  const inField = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT");

  if (event.key === "/" && !inField) {
    const search = viewRoot.querySelector('[data-testid="step-search"]');
    if (search) { event.preventDefault(); search.focus(); }
    return;
  }
  if (event.key === "Escape" && inField) {
    target.blur();
    return;
  }
  if (inField || state.view === "overview") return;
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;

  const steps = visibleStepsForView();
  if (!steps.length) return;
  event.preventDefault();
  const currentIndex = steps.findIndex((step) => step.step_id === state.selectedStepId);
  const delta = event.key === "ArrowDown" ? 1 : -1;
  const nextIndex = currentIndex === -1
    ? (delta > 0 ? 0 : steps.length - 1)
    : Math.max(0, Math.min(steps.length - 1, currentIndex + delta));
  actions.selectStep(steps[nextIndex].step_id, { push: false, scroll: true });
});

/* ------------------------------ routing ------------------------------ */

function applyHash() {
  const hash = readHash();
  if (hash.mission && hash.mission !== state.missionId) {
    state.missionId = hash.mission;
    state.selectedStepId = "";
    state.signature = "";
    refreshMission();
  }
  if (hash.view && hash.view !== state.view) state.view = hash.view;
  state.workflowNode = state.view === "workflow" && hash.node ? hash.node : "";
  state.phaseFilter = hash.phase || "";
  if (hash.step) {
    const step = resolveHashStep(hash.step);
    if (step) {
      state.selectedStepId = step.step_id;
      state.detailTab = "";
    }
  }
  renderAll();
}

window.addEventListener("hashchange", applyHash);
window.addEventListener("popstate", applyHash);

/* ------------------------------ boot ------------------------------ */

const initialHash = readHash();
if (initialHash.view) state.view = initialHash.view;
if (initialHash.mission) state.missionId = initialHash.mission;
if (initialHash.node) state.workflowNode = initialHash.node;
if (initialHash.phase) state.phaseFilter = initialHash.phase;
renderAll();
poll().then(() => {
  // restore deep-linked step once the first payload lands
  if (initialHash.step && !state.selectedStepId) {
    const step = resolveHashStep(initialHash.step);
    if (step) {
      state.selectedStepId = step.step_id;
      renderAll();
      const row = viewRoot.querySelector(".selected[data-step-id]");
      if (row) row.scrollIntoView({ block: "nearest" });
    }
  }
});

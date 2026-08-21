// Trajectory view: chronological step navigator (left, grouped by phase with
// inline tool-call cards) + detail panel (right). This is the default view.

import { h, icon } from "./dom.js";
import { jsonView } from "./jsonview.js";
import { renderDetail, autoTab } from "./detail.js";
import {
  KIND_META, fmtDuration, stepErrored, stepSearchText, plural,
} from "./format.js";
import { state } from "./store.js";

function filteredSteps() {
  const query = state.search.trim().toLowerCase();
  const needle = state.phaseFilter; // deep-link filter (#view=trajectory&phase=…)
  return state.steps.filter((step) => {
    if (needle && step.phase !== needle && step.component !== needle) return false;
    if (state.errorsOnly && !stepErrored(step) && !(step.children || []).some(stepErrored)) return false;
    if (!query) return true;
    return stepSearchText(step).includes(query)
      || (step.children || []).some((child) => stepSearchText(child).includes(query));
  });
}

export function visibleTrajectorySteps() {
  return filteredSteps();
}

function toolInlineCard(step, call, index) {
  const failed = Boolean(call.error);
  const card = h("div", { class: "tool-inline" + (failed ? " failed" : "") });
  const head = h("button", {
    class: "tool-inline-head",
    type: "button",
    "aria-expanded": "false",
    onclick: () => {
      const open = card.classList.toggle("open");
      head.setAttribute("aria-expanded", String(open));
    },
  },
    icon("chevronRight", 10),
    icon("tool", 12),
    h("code", { class: "tool-name" }, call.name),
    h("span", { class: "tool-dur" }, fmtDuration(call.duration_ms)),
    failed ? h("span", { class: "inline-error" }, "error") : null);
  const body = h("div", { class: "tool-inline-body" },
    h("div", { class: "section-label" }, "Arguments"),
    jsonView(call.args ?? {}, { stateKey: `nav-tool-args:${step.step_id}:${index}` }),
    failed
      ? h("pre", { class: "tool-error" }, typeof call.error === "string" ? call.error : JSON.stringify(call.error, null, 2))
      : h("div", {}, h("div", { class: "section-label" }, "Result"),
          jsonView(call.result ?? null, { stateKey: `nav-tool-result:${step.step_id}:${index}` })));
  card.append(head, body);
  return card;
}

function stepRow(step, actions) {
  const meta = KIND_META[step.kind] || KIND_META.llm;
  const errored = stepErrored(step);
  const selected = state.selectedStepId === step.step_id;
  const row = h("button", {
    class: "step-row kind-" + step.kind + (selected ? " selected" : "") + (errored ? " errored" : ""),
    type: "button",
    "data-testid": "step-row",
    "data-step-id": step.step_id,
    "data-seq": step.seq,
    "aria-current": selected ? "true" : null,
    onclick: () => actions.selectStep(step.step_id),
  },
    h("span", { class: "row-seq" }, String(step.seq).padStart(2, "0")),
    h("span", { class: "row-icon" }, icon(meta.icon, 13)),
    h("span", { class: "row-main" },
      h("span", { class: "row-title" }, step.title || step.name || "Step " + step.seq),
      h("span", { class: "row-meta" },
        step.component, step.name && step.name !== step.component && step.name !== step.model ? " · " + step.name : "")),
    h("span", { class: "row-side" },
      h("span", { class: "row-dur" }, fmtDuration(step.duration_ms)),
      h("span", { class: "status-dot tone-" + (step.status === "error" ? "error" : step.status === "ok" ? "ok" : "unknown"), title: "status: " + step.status })));
  return row;
}

function phaseGroup(phase, steps, actions) {
  const durations = steps.reduce((n, s) => n + (s.duration_ms || 0), 0);
  const errors = steps.filter(stepErrored).length;
  const group = h("section", { class: "phase-group", "data-phase": phase },
    h("header", { class: "phase-header" },
      h("span", { class: "phase-name" }, phase),
      h("span", { class: "phase-meta" },
        plural(steps.length, "step"),
        durations ? " · " + fmtDuration(durations) : "",
        errors ? " · " : null,
        errors ? h("span", { class: "inline-error" }, plural(errors, "error")) : null)));

  const list = h("div", { class: "phase-steps" });
  for (const step of steps) {
    list.append(stepRow(step, actions));
    const calls = step.tool_calls || [];
    const childSteps = (step.children || []).filter((child) => child.kind !== "tool");
    if (calls.length || childSteps.length) {
      const sub = h("div", { class: "step-sub" });
      calls.forEach((call, index) => sub.append(toolInlineCard(step, call, index)));
      childSteps.forEach((child) => sub.append(stepRow(child, actions)));
      list.append(sub);
    }
  }
  group.append(list);
  return group;
}

export function renderTrajectory(root, actions) {
  root.replaceChildren();
  const split = h("div", { class: "split", "data-testid": "view-trajectory" });

  /* left: navigator */
  const navPane = h("div", { class: "nav-pane" });
  const search = h("input", {
    class: "search-input",
    "data-testid": "step-search",
    type: "search",
    placeholder: "Filter steps by name, component, kind…  ( / )",
    value: state.search,
    "aria-label": "Filter steps",
    oninput: (event) => { state.search = event.target.value; actions.renderView({ preserveFocus: true }); },
  });
  const errorsToggle = h("button", {
    class: "filter-toggle" + (state.errorsOnly ? " active" : ""),
    type: "button",
    "aria-pressed": String(state.errorsOnly),
    "data-testid": "errors-only",
    onclick: () => { state.errorsOnly = !state.errorsOnly; actions.renderView({ preserveFocus: true }); },
  }, icon("alert", 12), "Errors only");

  const phaseChip = state.phaseFilter
    ? h("span", { class: "filter-chip", "data-testid": "phase-filter", title: "Deep-linked from the Workflow view" },
        icon("workflow", 11),
        state.phaseFilter,
        h("button", {
          class: "filter-chip-clear", type: "button", "aria-label": "Clear phase filter",
          onclick: () => actions.clearPhaseFilter(),
        }, icon("x", 10)))
    : null;

  const visible = filteredSteps();
  const count = h("span", { class: "nav-count" },
    visible.length === state.steps.length
      ? plural(visible.length, "step")
      : `${visible.length} of ${state.steps.length}`);
  navPane.append(h("div", { class: "nav-toolbar" }, search, phaseChip, errorsToggle, count));

  const scroll = h("div", { class: "nav-scroll", "data-testid": "step-list" });
  if (!state.steps.length) {
    const empty = !state.missionId
      ? ["No mission selected", "The runtime did not report any missions. Start a run, or pick one when it appears."]
      : state.loadedOnce
        ? ["This run recorded no steps", "No debug records, operational log entries, or transport feedback exist for this mission yet."]
        : ["Loading run data…", "Reading the run projection."];
    scroll.append(h("div", { class: "empty-state tall" },
      icon("inbox", 24),
      h("p", { class: "empty-heading" }, empty[0]),
      h("p", { class: "empty-copy" }, empty[1])));
  } else if (!visible.length) {
    scroll.append(h("div", { class: "empty-state tall" },
      icon("search", 22),
      h("p", { class: "empty-heading" }, "No steps match"),
      h("p", { class: "empty-copy" }, "Try a broader term, or clear the errors-only filter.")));
  } else {
    const phases = state.stepsPayload && Array.isArray(state.stepsPayload.phases) ? state.stepsPayload.phases : [];
    const byPhase = new Map();
    for (const step of visible) {
      const key = step.phase || "ungrouped";
      if (!byPhase.has(key)) byPhase.set(key, []);
      byPhase.get(key).push(step);
    }
    const ordered = [
      ...phases.filter((phase) => byPhase.has(phase)),
      ...[...byPhase.keys()].filter((key) => !phases.includes(key)),
    ];
    for (const phase of ordered) scroll.append(phaseGroup(phase, byPhase.get(phase), actions));
  }

  // degraded-data notice: steps exist but no reasoning anywhere
  if (state.steps.length && !state.flat.some(({ step }) => step.reasoning)) {
    navPane.append(h("div", { class: "notice tone-running", "data-testid": "no-debug-notice" },
      icon("alert", 13),
      h("span", {}, "No debug records for this run — reasoning and tool payloads were not captured. Showing operational-log and transport evidence only.")));
  }
  navPane.append(scroll);

  /* right: detail */
  const detailPane = h("div", { class: "detail-pane", "data-testid": "detail-panel" });
  const step = state.selectedStepId ? state.byId.get(state.selectedStepId) : null;
  renderDetail(detailPane, step || null, actions);

  split.append(navPane, detailPane);
  root.append(split);
}

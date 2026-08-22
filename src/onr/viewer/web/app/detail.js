// Detail panel for the selected step: header metadata + the five tabs
// (Reasoning · Decision · Tool calls · Feedback · Raw JSON) with deliberate
// empty states when the pipeline didn't capture that kind of evidence.

import { h, icon } from "./dom.js";
import { jsonView } from "./jsonview.js";
import { artifactViewer } from "./artifact.js";
import {
  KIND_META, fmtDuration, fmtTimeMs, outcomeTone, stepErrored,
} from "./format.js";
import { state } from "./store.js";

const REASONING_COLLAPSE = 1200;

const TABS = [
  { id: "reasoning", label: "Reasoning", available: (s) => Boolean(s.reasoning || s.content) },
  { id: "decision", label: "Decision", available: (s) => Boolean(s.decision) },
  { id: "tools", label: "Tool calls", available: (s) => (s.tool_calls || []).length > 0 },
  { id: "feedback", label: "Feedback", available: (s) => (s.feedback || []).length > 0 },
  { id: "raw", label: "Raw JSON", available: () => true },
];

function tabCount(step, id) {
  if (id === "reasoning") return step.reasoning ? 1 : step.content ? 1 : 0;
  if (id === "decision") return step.decision ? 1 : 0;
  if (id === "tools") return (step.tool_calls || []).length;
  if (id === "feedback") return (step.feedback || []).length;
  return 0;
}

export function autoTab(step) {
  for (const tab of TABS) if (tab.available(step)) return tab.id;
  return "raw";
}

function emptyState(iconName, heading, copy) {
  return h("div", { class: "empty-state", "data-testid": "detail-empty" },
    icon(iconName, 22),
    h("p", { class: "empty-heading" }, heading),
    h("p", { class: "empty-copy" }, copy));
}

function metaGrid(step) {
  const rows = [
    ["Component", step.component],
    ["Phase", step.phase],
    ["Seq", "#" + step.seq],
    ["Started", fmtTimeMs(step.started_at)],
    ["Finished", fmtTimeMs(step.finished_at)],
    ["Updated", fmtTimeMs(step.updated_at)],
    ["Duration", fmtDuration(step.duration_ms)],
    ["Revision", step.revision],
  ];
  if (step.model) rows.push(["Model", step.model]);
  if (step.finish_reason) rows.push(["Finish", step.finish_reason]);
  if (step.name && step.name !== step.model) rows.push(["Name", step.name]);
  const grid = h("dl", { class: "meta-grid" });
  for (const [label, value] of rows) {
    grid.append(h("dt", {}, label), h("dd", {}, value === null || value === undefined || value === "" ? "—" : String(value)));
  }
  return grid;
}

function reasoningTab(step, rerender) {
  const wrap = h("div", { class: "tab-body" });
  if (step.completion_state === "live" || step.completion_state === "partial") {
    wrap.append(h("p", { class: "partial-note", "data-testid": "generation-note" },
      icon("alert", 12),
      step.completion_state === "live"
        ? " This response is still growing."
        : " This response ended before the stream completed."));
  }
  if (!step.reasoning && !step.content) {
    wrap.append(emptyState("llm", "No reasoning captured",
      "Reasoning appears here when the pipeline runs with debug recording enabled and the model returns reasoning content. This step produced none."));
    return wrap;
  }
  if (step.reasoning) {
    const paragraphs = step.reasoning.split(/\n{2,}/).map((p) => p.trim()).filter(Boolean);
    const wordCount = step.reasoning.split(/\s+/).length;
    const collapsed = step.reasoning.length > REASONING_COLLAPSE;
    const box = h("div", { class: "reasoning" + (collapsed ? " collapsed" : ""), "data-testid": "reasoning-body" });
    for (const para of paragraphs) box.append(h("p", {}, para));
    wrap.append(
      h("div", { class: "section-label" }, "Model reasoning"),
      box);
    if (collapsed) {
      const expand = h("button", {
        class: "reasoning-expand", type: "button",
        onclick: () => { box.classList.remove("collapsed"); expand.remove(); },
      }, "Show full reasoning (" + wordCount.toLocaleString("en-US") + " words)");
      wrap.append(expand);
    }
  }
  if (step.content) {
    if (!step.reasoning) {
      wrap.append(h("p", { class: "partial-note" },
        icon("alert", 12),
        " No reasoning was captured for this step — debug capture was partial. The response content below is all that was recorded."));
    }
    wrap.append(h("div", { class: "section-label" }, "Response content"), h("p", { class: "content-text" }, step.content));
  }
  if (step.truncated) {
    wrap.append(h("p", { class: "artifact-note tone-running" }, "This record was truncated by the server text limit."));
  }
  return wrap;
}

function decisionTab(step) {
  const wrap = h("div", { class: "tab-body" });
  const decision = step.decision;
  if (!decision) {
    wrap.append(emptyState("decision", "No decision recorded",
      "Decision evidence comes from the operational log. This step has no correlated decision record."));
    return wrap;
  }
  const tone = outcomeTone(decision.outcome || step.outcome);
  wrap.append(
    h("div", { class: "decision-summary" },
      h("div", { class: "decision-row" },
        h("span", { class: "decision-key" }, "event_kind"),
        h("code", {}, decision.event_kind || "—")),
      h("div", { class: "decision-row" },
        h("span", { class: "decision-key" }, "outcome"),
        h("span", { class: "status-pill tone-" + tone }, h("span", { class: "status-dot" }), decision.outcome || step.outcome || "—"))),
    decision.details && Object.keys(decision.details).length
      ? h("div", {}, h("div", { class: "section-label" }, "Details"), jsonView(decision.details, { stateKey: "decision:" + step.step_id }))
      : null);
  return wrap;
}

function toolCallCard(step, call, index) {
  const failed = Boolean(call.error);
  const partial = call.partial === true;
  const head = h("div", { class: "tool-card-head" },
    icon("tool", 13),
    h("code", { class: "tool-name" }, call.name),
    h("span", { class: "tool-dur" }, fmtDuration(call.duration_ms)),
    h("span", { class: "status-pill tone-" + (failed ? "error" : partial ? "running" : "ok") },
      h("span", { class: "status-dot" }), failed ? "error" : partial ? "draft" : "ok"));
  const argumentView = partial
    ? h("pre", { class: "draft-arguments", "data-testid": "draft-tool-arguments" }, call.arguments_text || "")
    : jsonView(call.args ?? {}, { stateKey: `tool-args:${step.step_id}:${index}` });
  const body = h("div", { class: "tool-card-body" },
    h("div", { class: "section-label" }, partial ? "Draft arguments (not executed)" : "Arguments"),
    argumentView,
    partial
      ? h("p", { class: "partial-note" }, "This incomplete argument text is display-only and is not an executed tool call.")
      : failed
      ? h("div", {}, h("div", { class: "section-label" }, "Error"),
          h("pre", { class: "tool-error" }, typeof call.error === "string" ? call.error : JSON.stringify(call.error, null, 2)))
      : h("div", {}, h("div", { class: "section-label" }, "Result"),
          jsonView(call.result ?? null, { stateKey: `tool-result:${step.step_id}:${index}` })));
  return h("article", { class: "tool-card" + (failed ? " failed" : ""), "data-testid": "tool-card" }, head, body);
}

function toolsTab(step) {
  const wrap = h("div", { class: "tab-body" });
  const calls = step.tool_calls || [];
  if (!calls.length) {
    wrap.append(emptyState("tool", "No tool calls",
      "This step did not invoke tools. Tool calls appear as cards with arguments, results, and errors."));
    return wrap;
  }
  for (const [index, call] of calls.entries()) wrap.append(toolCallCard(step, call, index));
  return wrap;
}

function feedbackTab(step) {
  const wrap = h("div", { class: "tab-body" });
  const entries = step.feedback || [];
  if (!entries.length) {
    wrap.append(emptyState("feedback", "No feedback entries",
      "Feedback arrives over transport topics and is attached here by correlation. None was recorded for this step."));
    return wrap;
  }
  for (const [index, entry] of entries.entries()) {
    wrap.append(h("article", { class: "feedback-card" },
      h("div", { class: "feedback-head" },
        icon("feedback", 13),
        h("code", { class: "tool-name" }, entry.kind || "feedback")),
      jsonView(entry.payload ?? {}, { stateKey: `feedback:${step.step_id}:${index}` })));
  }
  return wrap;
}

function rawTab(step) {
  return h("div", { class: "tab-body" },
    jsonView(step, { stateKey: "raw:" + step.step_id }));
}

function artifactsBlock(step, actions) {
  const artifacts = step.artifacts || [];
  if (!artifacts.length) return null;
  const wrap = h("div", { class: "detail-artifacts" },
    h("div", { class: "section-label" }, "Artifacts"));
  for (const artifact of artifacts) {
    const open = state.artifactRef === artifact.ref;
    const chip = h("button", {
      class: "artifact-chip" + (open ? " open" : ""),
      type: "button",
      onclick: () => {
        state.artifactRef = open ? "" : artifact.ref;
        actions.renderDetail();
      },
    }, icon("file", 12), artifact.label || artifact.ref);
    wrap.append(chip);
    if (open) wrap.append(artifactViewer(artifact.ref, artifact.label));
  }
  return wrap;
}

export function renderDetail(root, step, actions) {
  root.replaceChildren();
  if (!step) {
    root.append(emptyState("inbox", "No step selected", "Select a step from the list to inspect its reasoning, decisions, tool calls, and feedback."));
    return;
  }

  const meta = KIND_META[step.kind] || KIND_META.llm;
  const errored = stepErrored(step);
  const tab = TABS.some((t) => t.id === state.detailTab) ? state.detailTab : autoTab(step);
  const completion = step.completion_state || "complete";
  const completionTone = completion === "error" ? "error" : completion === "live" || completion === "partial" ? "running" : "ok";

  const header = h("div", { class: "detail-header" },
    h("div", { class: "detail-title-row" },
      h("span", { class: "kind-badge kind-" + step.kind }, icon(meta.icon, 13), meta.label),
      h("h2", { class: "detail-title" }, step.title || step.name || "Step " + step.seq),
      h("span", { class: "status-pill tone-" + (step.status === "error" ? "error" : step.status === "ok" ? "ok" : "unknown"), "data-testid": "detail-status" },
        h("span", { class: "status-dot" }), step.status),
      h("span", { class: "status-pill tone-" + completionTone, "data-testid": "completion-state" },
        h("span", { class: "status-dot" }), completion),
      step.outcome ? h("span", { class: "outcome-chip tone-" + outcomeTone(step.outcome) }, step.outcome) : null),
    metaGrid(step),
    artifactsBlock(step, actions));

  const tabBar = h("div", { class: "detail-tabs", role: "tablist", "aria-label": "Step detail" },
    TABS.map((t) => {
      const count = tabCount(step, t.id);
      return h("button", {
        class: "detail-tab" + (tab === t.id ? " active" : "") + (t.available(step) ? "" : " disabled-look"),
        role: "tab",
        "aria-selected": String(tab === t.id),
        "data-testid": "detail-tab-" + t.id,
        type: "button",
        onclick: () => { state.detailTab = t.id; actions.renderDetail(); },
      }, t.label, count ? h("span", { class: "tab-count" }, String(count)) : null);
    }));

  const body = h("div", { class: "detail-body", "data-testid": "detail-body" });
  if (tab === "reasoning") body.append(reasoningTab(step, actions.renderDetail));
  else if (tab === "decision") body.append(decisionTab(step));
  else if (tab === "tools") body.append(toolsTab(step));
  else if (tab === "feedback") body.append(feedbackTab(step));
  else body.append(rawTab(step));

  const panel = h("div", { class: "detail-inner" }, header, tabBar, body);
  root.append(panel);
  // Reset scroll only when a different step is shown — poll-driven re-renders
  // of the same step must not yank the reader's position.
  if (root._lastStepId !== step.step_id) root.scrollTop = 0;
  root._lastStepId = step.step_id;
}

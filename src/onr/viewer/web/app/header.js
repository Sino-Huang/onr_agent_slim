// Sticky header: brand, mission title + status pill, mission picker, runtime
// state, view tabs, aggregate badges.

import { h, icon } from "./dom.js";
import { fmtCount, fmtDuration } from "./format.js";
import { state, VIEWS } from "./store.js";
import { mockUsed } from "./api.js";

const VIEW_DEFS = [
  { id: "trajectory", label: "Trajectory", icon: "list" },
  { id: "tree", label: "Tree", icon: "tree" },
  { id: "timeline", label: "Timeline", icon: "timeline" },
  { id: "overview", label: "Overview", icon: "overview" },
  { id: "workflow", label: "Workflow", icon: "workflow" },
  { id: "world-model", label: "World Model", icon: "world" },
];

function statusPill() {
  const run = state.run;
  const status = run && run.status ? String(run.status) : (state.runtime && state.runtime.active ? "running" : "unknown");
  const tone = status === "complete" ? "ok" : status === "running" ? "running" : "unknown";
  return h("span", { class: "status-pill tone-" + tone, "data-testid": "run-status-pill" },
    h("span", { class: "status-dot" }), status);
}

function badge(id, iconName, label, value, tone) {
  return h("span", { class: "badge" + (tone ? " tone-" + tone : ""), "data-testid": id, title: label + ": " + value },
    icon(iconName, 12),
    h("span", { class: "badge-value" }, value),
    h("span", { class: "badge-label" }, label));
}

function badges() {
  const agg = state.run && state.run.aggregates ? state.run.aggregates : null;
  const value = (key, fmt = fmtCount) => (agg && Number.isFinite(agg[key]) ? fmt(agg[key]) : "—");
  const errorCount = agg && Number.isFinite(agg.error_count) ? agg.error_count : null;
  return h("div", { class: "badges", "aria-label": "Run aggregates" },
    badge("badge-steps", "list", "steps", value("step_count")),
    badge("badge-llm-calls", "llm", "llm calls", value("llm_call_count")),
    badge("badge-tool-calls", "tool", "tool calls", value("tool_call_count")),
    badge("badge-errors", "alert", "errors", errorCount === null ? "—" : fmtCount(errorCount), errorCount > 0 ? "error" : ""),
    badge("badge-duration", "clock", "duration", value("duration_ms", fmtDuration)),
    badge("badge-planner-attempts", "zap", "planner attempts", value("planner_attempts")),
    badge("badge-statechart-attempts", "decision", "fsm attempts", value("statechart_attempts")),
  );
}

function missionPicker(actions) {
  const missions = state.runtime && Array.isArray(state.runtime.mission_ids) ? state.runtime.mission_ids : [];
  if (!missions.length) return null;
  const select = h("select", {
    id: "missionSelect",
    "data-testid": "mission-picker",
    "aria-label": "Selected mission",
    onchange: (event) => actions.selectMission(event.target.value),
  });
  for (const id of missions) {
    select.append(h("option", { value: id, selected: id === state.missionId || null }, id));
  }
  if (state.missionId && !missions.includes(state.missionId)) {
    select.prepend(h("option", { value: state.missionId, selected: true }, state.missionId));
  }
  return h("label", { class: "mission-picker" }, icon("play", 11), select);
}

function runtimeStatus() {
  const runtime = state.runtime;
  const unavailable = state.errors.runtime && !runtime;
  const active = Boolean(runtime && runtime.active);
  const label = unavailable ? "Runtime unavailable" : active ? "Runtime live" : runtime ? "Runtime idle" : "Connecting…";
  return h("span", {
    class: "runtime-status" + (unavailable ? " tone-error" : active ? " tone-ok" : ""),
    "data-testid": "runtime-status", role: "status",
  }, h("span", { class: "status-dot" + (active ? " pulse" : "") }), label);
}

export function renderHeader(root, actions) {
  const mission = state.run && state.run.mission && typeof state.run.mission === "object" ? state.run.mission : {};
  const title = mission.title || state.missionId || "No mission selected";
  const anyMock = mockUsed.steps || mockUsed.run || mockUsed.runtime;

  const tabs = h("nav", { class: "view-tabs", role: "tablist", "aria-label": "Run views" },
    VIEW_DEFS.map((view) => h("button", {
      class: "tab" + (state.view === view.id ? " active" : ""),
      role: "tab",
      "aria-selected": String(state.view === view.id),
      "data-view": view.id,
      "data-testid": "tab-" + view.id,
      type: "button",
      onclick: () => actions.setView(view.id),
    }, icon(view.icon, 13), view.label)));

  root.replaceChildren(
    h("div", { class: "header-tier header-main" },
      h("div", { class: "brand" },
        h("span", { class: "brand-mark" }, "ONR"),
        h("span", { class: "brand-name" }, "Run Viewer")),
      h("div", { class: "mission-block" },
        h("div", { class: "mission-title-row" },
          h("h1", { class: "mission-title", "data-testid": "mission-title" }, title),
          statusPill(),
          anyMock ? h("span", { class: "mock-badge", "data-testid": "mock-badge", title: "Live endpoints did not answer; showing bundled demo data" }, "demo data") : null),
        state.missionId && mission.title
          ? h("div", { class: "mission-sub" }, state.missionId, mission.sector ? " · " + mission.sector : "")
          : null),
      h("div", { class: "header-right" },
        missionPicker(actions),
        runtimeStatus())),
    h("div", { class: "header-tier header-sub" },
      tabs,
      badges()),
  );
}

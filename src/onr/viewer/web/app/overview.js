// Overview view: mission file, final result, summaries, FSM statechart +
// execution record, environment snapshot, artifacts index with inline viewer.

import { h, icon } from "./dom.js";
import { jsonView } from "./jsonview.js";
import { artifactViewer } from "./artifact.js";
import { fmtTimeMs, fmtDate } from "./format.js";
import { state } from "./store.js";

function section(title, iconName, options = {}) {
  const sec = h("section", { class: "ov-section" + (options.wide ? " wide" : ""), "data-testid": "ov-" + options.id });
  sec.append(h("header", { class: "ov-head" }, icon(iconName, 14), h("h2", {}, title), options.aside || null));
  return sec;
}

function emptyNote(text) {
  return h("p", { class: "ov-empty" }, text);
}

function kvRows(pairs) {
  const dl = h("dl", { class: "kv-list" });
  for (const [key, value] of pairs) {
    if (value === null || value === undefined || value === "") continue;
    dl.append(h("dt", {}, key), h("dd", {}, typeof value === "string" ? value : String(value)));
  }
  return dl;
}

function missionSection(run) {
  const sec = section("Mission", "file", { id: "mission" });
  const mission = run && run.mission && typeof run.mission === "object" ? run.mission : {};
  if (!Object.keys(mission).length) {
    sec.append(emptyNote("No mission file was recorded for this run."));
    return sec;
  }
  const { title, objective, source_authority, issued_at, sector, constraints } = mission;
  sec.append(kvRows([
    ["Title", title],
    ["Mission id", run.mission_id],
    ["Authority", source_authority],
    ["Sector", sector],
    ["Issued", issued_at ? fmtDate(issued_at) + " " + fmtTimeMs(issued_at) : ""],
  ]));
  if (objective) sec.append(h("p", { class: "ov-objective" }, objective));
  if (Array.isArray(constraints) && constraints.length) {
    const list = h("ul", { class: "ov-constraints" });
    for (const item of constraints) list.append(h("li", {}, item));
    sec.append(h("div", { class: "section-label" }, "Constraints"), list);
  }
  const rest = Object.fromEntries(Object.entries(mission).filter(
    ([key]) => !["title", "objective", "source_authority", "issued_at", "sector", "constraints", "mission_id"].includes(key)));
  if (Object.keys(rest).length) {
    sec.append(h("div", { class: "section-label" }, "Full mission record"),
      jsonView(mission, { stateKey: "ov:mission" }));
  }
  return sec;
}

function finalResultSection(run) {
  const sec = section("Final result", "check", { id: "result" });
  if (!run || run.final_result === null || run.final_result === undefined) {
    sec.append(emptyNote(run && run.status === "running" ? "The run is still in progress — no final result yet." : "This run produced no final result record."));
    return sec;
  }
  if (typeof run.final_result === "object") {
    const result = run.final_result;
    const status = result.status || run.status;
    sec.append(h("div", { class: "result-banner tone-" + (status === "success" || status === "complete" ? "ok" : "unknown") },
      icon(status === "success" || status === "complete" ? "check" : "alert", 14),
      h("span", {}, String(status))));
    sec.append(jsonView(result, { stateKey: "ov:final" }));
  } else {
    sec.append(h("p", { class: "ov-objective" }, String(run.final_result)));
  }
  return sec;
}

function summariesSection(run) {
  const sec = section("Summaries", "list", { id: "summaries" });
  const summaries = run && Array.isArray(run.summaries) ? run.summaries : [];
  if (!summaries.length) {
    sec.append(emptyNote("No summaries were published for this run."));
    return sec;
  }
  for (const summary of summaries) {
    const text = summary.summary || summary.text || JSON.stringify(summary);
    sec.append(h("article", { class: "ov-summary" },
      h("div", { class: "ov-summary-meta" },
        summary.summary_id ? h("code", {}, summary.summary_id) : null,
        summary.input_start_sequence !== undefined
          ? h("span", {}, `seq ${summary.input_start_sequence}–${summary.input_end_sequence}`)
          : null),
      h("p", {}, text)));
  }
  return sec;
}

function fsmSection(run) {
  const sec = section("FSM execution", "decision", { id: "fsm", wide: true });
  const fsm = run && run.fsm;
  if (!fsm || (!fsm.statechart && !fsm.execution_record)) {
    sec.append(emptyNote("No statechart or execution record was produced for this run."));
    return sec;
  }
  const cols = h("div", { class: "ov-fsm-cols" });
  if (fsm.statechart) {
    cols.append(h("div", {},
      h("div", { class: "section-label" }, "Statechart"),
      jsonView(fsm.statechart, { stateKey: "ov:statechart" })));
  }
  const record = fsm.execution_record;
  if (record) {
    const right = h("div", {},
      h("div", { class: "section-label" }, "Execution record"),
      kvRows([
        ["Record", record.record_id],
        ["Statechart", record.statechart_id],
        ["Status", record.status],
        ["Started", record.started_at ? fmtTimeMs(record.started_at) : ""],
        ["Finished", record.finished_at ? fmtTimeMs(record.finished_at) : ""],
      ]));
    const transitions = Array.isArray(record.transitions) ? record.transitions : [];
    if (transitions.length) {
      const table = h("table", { class: "fsm-table", "data-testid": "fsm-transitions" },
        h("thead", {}, h("tr", {}, ["tick", "from", "to", "event", "note"].map((col) => h("th", {}, col)))));
      const tbody = h("tbody", {});
      for (const t of transitions) {
        tbody.append(h("tr", {},
          h("td", { class: "num" }, t.at_tick ?? t.tick ?? "—"),
          h("td", {}, t.from ?? "∅"),
          h("td", {}, t.to ?? "—"),
          h("td", {}, h("code", {}, t.event ?? "—")),
          h("td", { class: "dim" }, t.note || "")));
      }
      table.append(tbody);
      right.append(table);
    }
    cols.append(right);
  }
  sec.append(cols);
  return sec;
}

function environmentSection(run) {
  const sec = section("Environment", "zap", { id: "environment" });
  if (!run || !run.environment) {
    sec.append(emptyNote("No environment snapshot is available for this run."));
    return sec;
  }
  sec.append(jsonView(run.environment, { stateKey: "ov:environment" }));
  return sec;
}

function artifactsSection(run, actions) {
  const sec = section("Artifacts", "file", { id: "artifacts", wide: true });
  const index = run && Array.isArray(run.artifacts_index) ? run.artifacts_index : [];
  if (!index.length) {
    sec.append(emptyNote("No planner artifacts are indexed for this run."));
    return sec;
  }
  const list = h("div", { class: "artifact-list", "data-testid": "artifact-list" });
  for (const artifact of index) {
    const open = state.artifactRef === artifact.ref;
    list.append(h("button", {
      class: "artifact-row" + (open ? " open" : ""),
      type: "button",
      "data-testid": "artifact-row",
      "data-ref": artifact.ref,
      "aria-expanded": String(open),
      onclick: () => {
        state.artifactRef = open ? "" : artifact.ref;
        actions.renderView();
      },
    },
      icon("file", 13),
      h("span", { class: "artifact-label" }, artifact.label || artifact.ref),
      h("code", { class: "artifact-ref" }, artifact.ref),
      h("span", { class: "kind-chip" }, artifact.kind)));
    if (open) list.append(artifactViewer(artifact.ref, artifact.label));
  }
  sec.append(list);
  return sec;
}

export function renderOverview(root, actions) {
  root.replaceChildren();
  const wrap = h("div", { class: "overview", "data-testid": "view-overview" });
  if (!state.run) {
    wrap.append(h("div", { class: "empty-state tall" },
      icon("inbox", 24),
      h("p", { class: "empty-heading" },
        !state.missionId ? "No mission selected" : state.loadedOnce ? "No run overview" : "Loading run data…"),
      h("p", { class: "empty-copy" }, "The run overview aggregates mission, FSM, environment, and artifact evidence.")));
    root.append(wrap);
    return;
  }
  const grid = h("div", { class: "ov-grid" },
    missionSection(state.run),
    finalResultSection(state.run),
    summariesSection(state.run),
    environmentSection(state.run),
    fsmSection(state.run),
    artifactsSection(state.run, actions));
  wrap.append(grid);
  root.append(wrap);
}

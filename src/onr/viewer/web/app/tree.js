// Tree view: steps nested by children, collapse/expand, kind coloring,
// error highlighting. Detail panel stays on the right.

import { h, icon } from "./dom.js";
import { renderDetail } from "./detail.js";
import { KIND_META, fmtDuration, stepErrored, walkSteps } from "./format.js";
import { state } from "./store.js";

export function visibleTreeSteps() {
  // Selection order = visible nodes in the tree (respecting collapsed nodes).
  const out = [];
  const visit = (steps) => {
    for (const step of steps) {
      out.push(step);
      if (!state.treeCollapsed.has(step.step_id)) visit(step.children || []);
    }
  };
  visit(state.steps);
  return out;
}

function hasChildren(step) {
  return (step.children || []).length > 0;
}

function treeNode(step, actions) {
  const meta = KIND_META[step.kind] || KIND_META.llm;
  const errored = stepErrored(step);
  const collapsible = hasChildren(step);
  const collapsed = state.treeCollapsed.has(step.step_id);
  const selected = state.selectedStepId === step.step_id;

  const caret = collapsible
    ? h("button", {
        class: "tree-caret" + (collapsed ? "" : " open"),
        type: "button",
        "aria-label": collapsed ? "Expand node" : "Collapse node",
        onclick: (event) => {
          event.stopPropagation();
          if (collapsed) state.treeCollapsed.delete(step.step_id);
          else state.treeCollapsed.add(step.step_id);
          actions.renderView();
        },
      }, icon("chevronRight", 10))
    : h("span", { class: "tree-caret leaf" });

  const row = h("div", {
    class: "tree-row kind-" + step.kind + (selected ? " selected" : "") + (errored ? " errored" : ""),
    role: "treeitem",
    "aria-expanded": collapsible ? String(!collapsed) : null,
    "aria-selected": String(selected),
    "data-testid": "tree-node",
    "data-step-id": step.step_id,
    "data-seq": step.seq,
    tabindex: "-1",
    onclick: () => actions.selectStep(step.step_id),
  },
    caret,
    h("span", { class: "row-icon" }, icon(meta.icon, 13)),
    h("span", { class: "row-main" },
      h("span", { class: "row-title" }, step.title || step.name || "Step " + step.seq),
      h("span", { class: "row-meta" }, step.component, step.outcome ? " · " + step.outcome : "")),
    h("span", { class: "row-side" },
      h("span", { class: "row-dur" }, fmtDuration(step.duration_ms)),
      h("span", { class: "status-dot tone-" + (step.status === "error" ? "error" : step.status === "ok" ? "ok" : "unknown") })));

  const wrap = h("li", { class: "tree-item", role: "none" }, row);
  if (collapsible && !collapsed) {
    const kids = h("ul", { class: "tree-children", role: "group" });
    for (const child of step.children) kids.append(treeNode(child, actions));
    wrap.append(kids);
  }
  return wrap;
}

export function renderTree(root, actions) {
  root.replaceChildren();
  const split = h("div", { class: "split", "data-testid": "view-tree" });

  const navPane = h("div", { class: "nav-pane" });
  const total = state.flat.length;
  const toolbar = h("div", { class: "nav-toolbar" },
    h("span", { class: "nav-count" }, total + " nodes"),
    h("span", { class: "toolbar-spacer" }),
    h("button", {
      class: "filter-toggle", type: "button", "data-testid": "tree-expand-all",
      onclick: () => { state.treeCollapsed.clear(); actions.renderView(); },
    }, icon("expandAll", 12), "Expand all"),
    h("button", {
      class: "filter-toggle", type: "button", "data-testid": "tree-collapse-all",
      onclick: () => {
        for (const { step } of walkSteps(state.steps)) if (hasChildren(step)) state.treeCollapsed.add(step.step_id);
        actions.renderView();
      },
    }, icon("collapseAll", 12), "Collapse all"));
  navPane.append(toolbar);

  const scroll = h("div", { class: "nav-scroll tree-scroll", "data-testid": "tree-list" });
  if (!state.steps.length) {
    scroll.append(h("div", { class: "empty-state tall" },
      icon("inbox", 24),
      h("p", { class: "empty-heading" }, "Nothing to nest"),
      h("p", { class: "empty-copy" }, "This run recorded no steps, so there is no tree to show.")));
  } else {
    const list = h("ul", { class: "tree-root", role: "tree", "aria-label": "Step hierarchy" });
    for (const step of state.steps) list.append(treeNode(step, actions));
    scroll.append(list);
  }
  navPane.append(scroll);

  const detailPane = h("div", { class: "detail-pane", "data-testid": "detail-panel" });
  renderDetail(detailPane, state.selectedStepId ? state.byId.get(state.selectedStepId) || null : null, actions);

  split.append(navPane, detailPane);
  root.append(split);
}

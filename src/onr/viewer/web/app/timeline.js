// Timeline view: waterfall bars by start/finish, one lane per component,
// error markers, hover tooltip, click-to-select. Detail panel on the right.

import { h, icon } from "./dom.js";
import { renderDetail } from "./detail.js";
import { fmtDuration, fmtOffset, fmtTime, stepErrored, KIND_META } from "./format.js";
import { state } from "./store.js";

export function visibleTimelineSteps() {
  return [...state.flat].map((entry) => entry.step)
    .sort((a, b) => (Date.parse(a.started_at) || 0) - (Date.parse(b.started_at) || 0));
}

const NICE_STEPS = [100, 200, 500, 1000, 2000, 5000, 10_000, 15_000, 30_000, 60_000, 120_000, 300_000];

function tickStep(spanMs, targetTicks = 8) {
  for (const step of NICE_STEPS) if (spanMs / step <= targetTicks) return step;
  return NICE_STEPS[NICE_STEPS.length - 1];
}

function barClass(step) {
  return "tl-bar kind-" + step.kind + (stepErrored(step) ? " errored" : "")
    + (state.selectedStepId === step.step_id ? " selected" : "");
}

export function renderTimeline(root, actions) {
  root.replaceChildren();
  const split = h("div", { class: "split", "data-testid": "view-timeline" });

  const pane = h("div", { class: "nav-pane timeline-pane" });

  const timed = state.flat.filter(({ step }) => step.started_at && step.finished_at);
  if (!timed.length) {
    pane.append(h("div", { class: "empty-state tall" },
      icon("timeline", 24),
      h("p", { class: "empty-heading" }, "No timing data"),
      h("p", { class: "empty-copy" }, "Steps need start and finish timestamps to draw a waterfall.")));
    split.append(pane, h("div", { class: "detail-pane", "data-testid": "detail-panel" }));
    renderDetail(split.lastChild, null, actions);
    root.append(split);
    return;
  }

  const minStart = Math.min(...timed.map(({ step }) => Date.parse(step.started_at)));
  const maxEnd = Math.max(...timed.map(({ step }) => Date.parse(step.finished_at)));
  const span = Math.max(1, maxEnd - minStart);
  const pad = span * 0.02;
  const t0 = minStart - pad;
  const t1 = maxEnd + pad;
  const total = t1 - t0;
  const pct = (ms) => ((ms - t0) / total) * 100;

  // lanes in first-appearance order
  const lanes = [];
  const laneIndex = new Map();
  for (const { step } of timed) {
    if (!laneIndex.has(step.component)) {
      laneIndex.set(step.component, lanes.length);
      lanes.push({ component: step.component, items: [] });
    }
    lanes[laneIndex.get(step.component)].items.push(step);
  }

  const tooltip = h("div", { class: "tl-tooltip", hidden: true });

  const showTip = (event, step) => {
    tooltip.hidden = false;
    tooltip.replaceChildren(
      h("strong", {}, step.title || step.name),
      h("span", {}, `${step.component} · ${step.kind} · ${fmtDuration(step.duration_ms)}`),
      h("span", { class: "tl-tip-time" }, `${fmtTime(step.started_at)} → ${fmtTime(step.finished_at)} · ${step.status}`));
    const rect = pane.getBoundingClientRect();
    const x = Math.min(event.clientX - rect.left + 14, rect.width - 240);
    tooltip.style.left = x + "px";
    tooltip.style.top = Math.max(8, event.clientY - rect.top - 10) + "px";
  };

  /* axis */
  const stepMs = tickStep(span);
  const axis = h("div", { class: "tl-axis" });
  for (let at = 0; at <= span; at += stepMs) {
    const tick = h("span", {
      class: "tl-tick" + (at === 0 ? " first" : ""),
      style: { left: pct(minStart + at) + "%" },
    }, fmtOffset(at));
    axis.append(tick);
  }

  const grid = h("div", { class: "tl-grid" });
  for (let at = 0; at <= span; at += stepMs) {
    grid.append(h("span", { class: "tl-gridline", style: { left: pct(minStart + at) + "%" } }));
  }

  const lanesEl = h("div", { class: "tl-lanes" });
  for (const lane of lanes) {
    const row = h("div", { class: "tl-lane" });
    const label = h("div", { class: "tl-lane-label", title: lane.component },
      h("span", { class: "tl-lane-name" }, lane.component),
      h("span", { class: "tl-lane-count" }, String(lane.items.length)));
    const track = h("div", { class: "tl-track" }, grid.cloneNode(true));
    for (const step of lane.items) {
      const start = Date.parse(step.started_at);
      const end = Date.parse(step.finished_at);
      const left = pct(start);
      const width = Math.max(0.35, pct(end) - left);
      const bar = h("button", {
        class: barClass(step),
        type: "button",
        style: { left: left + "%", width: width + "%" },
        "data-step-id": step.step_id,
        "data-testid": "timeline-bar",
        "aria-label": `${step.title || step.name} — ${fmtDuration(step.duration_ms)} — ${step.status}`,
        onclick: () => actions.selectStep(step.step_id),
        onmousemove: (event) => showTip(event, step),
        onmouseleave: () => { tooltip.hidden = true; },
      }, stepErrored(step) ? h("span", { class: "tl-error-mark" }, "!") : null);
      track.append(bar);
    }
    row.append(label, track);
    lanesEl.append(row);
  }

  const legend = h("div", { class: "tl-legend" },
    Object.entries(KIND_META).map(([kind, meta]) =>
      h("span", { class: "tl-legend-item" }, h("i", { class: "tl-swatch kind-" + kind }), meta.label)),
    h("span", { class: "tl-legend-item" }, h("i", { class: "tl-swatch errored" }), "error"));

  pane.append(
    h("div", { class: "nav-toolbar" },
      h("span", { class: "nav-count" }, `${lanes.length} lanes · ${timed.length} timed steps`),
      h("span", { class: "toolbar-spacer" }),
      h("span", { class: "nav-count" }, fmtTime(new Date(minStart).toISOString()) + " → " + fmtTime(new Date(maxEnd).toISOString()))),
    h("div", { class: "tl-scroll" },
      h("div", { class: "tl-canvas" },
        h("div", { class: "tl-axis-row" }, h("div", { class: "tl-lane-label axis-spacer" }), axis),
        lanesEl,
        legend)),
    tooltip);

  const detailPane = h("div", { class: "detail-pane", "data-testid": "detail-panel" });
  renderDetail(detailPane, state.selectedStepId ? state.byId.get(state.selectedStepId) || null : null, actions);

  split.append(pane, detailPane);
  root.append(split);
}

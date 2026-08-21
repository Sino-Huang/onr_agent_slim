// Workflow view: hierarchical, progressively-disclosed architecture diagram
// of the ONR agentic pipeline. Declared topology — no API calls; renders
// identically with or without a mission selected.
//
//   L0 (default) — five big nodes: Mission → Planning Agent → Execution Agent
//       → Mission Result, with the Data & Memory Plane spanning below.
//   L1 — a node expands in place to its sub-graph; siblings stay visible,
//       dimmed. Expansion state lives in the hash (#view=workflow&node=hyper).
//   L2 — "View in run" chips deep-link into the Trajectory view
//       (#view=trajectory&phase=…), disabled when no mission is selected.
//
// Edge system: nodes expose N/S/E/W anchor ports; edges connect port→port
// with orthogonal elbow routing through the gutters (never through a node),
// arrowhead at the target port, and label chips at the path midpoint so text
// never sits on a line. Retry loops route around the outside of the chain.

import { h, icon } from "./dom.js";
import { state } from "./store.js";

/* ------------------------------ theme ------------------------------ */

const KIND_STYLE = {
  llm:      { stroke: "var(--kind-llm)",      fill: "var(--kind-llm-soft)",      chip: "var(--kind-llm)" },
  tool:     { stroke: "var(--kind-tool)",     fill: "var(--kind-tool-soft)",     chip: "var(--kind-tool)" },
  decision: { stroke: "var(--kind-decision)", fill: "var(--kind-decision-soft)", chip: "var(--kind-decision)" },
  feedback: { stroke: "var(--kind-feedback)", fill: "var(--kind-feedback-soft)", chip: "var(--kind-feedback)" },
  data:     { stroke: "var(--border-strong)", fill: "var(--node-data-soft)",     chip: "var(--text-2)" },
  io:       { stroke: "var(--accent)",        fill: "var(--accent-soft)",        chip: "var(--accent)" },
};

const EDGE_KINDS = {
  flow:  { color: "var(--text-2)",        dash: "",    marker: "wf-ar" },
  data:  { color: "var(--text-2)",        dash: "5 4", marker: "wf-ar" },
  retry: { color: "var(--kind-decision)", dash: "6 4", marker: "wf-ar-amber" },
  llm:   { color: "var(--kind-llm)",      dash: "",    marker: "wf-ar-violet" },
  done:  { color: "var(--kind-feedback)", dash: "",    marker: "wf-ar-green" },
};

function esc(text) {
  return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function lines(text) {
  return Array.isArray(text) ? text : [text];
}

/* ------------------------------ edge system ------------------------------ */

function box(x, y, w, h) {
  return { x, y, w, h };
}

// Anchor port on a node border. t (0..1) slides along the side.
function port(node, side, t = 0.5) {
  if (side === "N") return { x: node.x + node.w * t, y: node.y, dx: 0, dy: -1 };
  if (side === "S") return { x: node.x + node.w * t, y: node.y + node.h, dx: 0, dy: 1 };
  if (side === "W") return { x: node.x, y: node.y + node.h * t, dx: -1, dy: 0 };
  return { x: node.x + node.w, y: node.y + node.h * t, dx: 1, dy: 0 };
}

// Orthogonal port→port routing. Opposite-facing ports get a Z through the
// gutter; same-facing ports swing around the outside via a clearance line.
function routePoints(a, b, opts = {}) {
  const pad = opts.pad ?? 16;
  if (a.dx === 1 && b.dx === -1 || a.dx === -1 && b.dx === 1) {
    const mx = opts.mid ?? (a.x + b.x) / 2;
    return [[a.x, a.y], [mx, a.y], [mx, b.y], [b.x, b.y]];
  }
  if (a.dy === 1 && b.dy === -1 || a.dy === -1 && b.dy === 1) {
    const my = opts.mid ?? (a.y + b.y) / 2;
    return [[a.x, a.y], [a.x, my], [b.x, my], [b.x, b.y]];
  }
  if (a.dy !== 0 && a.dy === b.dy) {
    const cy = opts.clear ?? (a.dy === 1 ? Math.max(a.y, b.y) + pad : Math.min(a.y, b.y) - pad);
    return [[a.x, a.y], [a.x, cy], [b.x, cy], [b.x, b.y]];
  }
  if (a.dx !== 0 && a.dx === b.dx) {
    const cx = opts.clear ?? (a.dx === 1 ? Math.max(a.x, b.x) + pad : Math.min(a.x, b.x) - pad);
    return [[a.x, a.y], [cx, a.y], [cx, b.y], [b.x, b.y]];
  }
  // L-shaped single bend (horizontal exit + vertical entry, or vice versa).
  if (a.dx !== 0) return [[a.x, a.y], [b.x, a.y], [b.x, b.y]];
  return [[a.x, a.y], [a.x, b.y], [b.x, b.y]];
}

function simplify(points) {
  const deduped = [];
  for (const p of points) {
    const last = deduped[deduped.length - 1];
    if (!last || last[0] !== p[0] || last[1] !== p[1]) deduped.push(p);
  }
  const out = [];
  for (let i = 0; i < deduped.length; i++) {
    const prev = out[out.length - 1];
    const next = deduped[i + 1];
    if (prev && next) {
      const [px, py] = prev, [cx, cy] = deduped[i], [nx, ny] = next;
      if ((cx - px) * (ny - cy) === (cy - py) * (nx - cx)) continue; // collinear
    }
    out.push(deduped[i]);
  }
  return out;
}

function pathFromPoints(points, radius = 8) {
  let d = `M ${points[0][0]},${points[0][1]}`;
  for (let i = 1; i < points.length - 1; i++) {
    const [px, py] = points[i - 1], [cx, cy] = points[i], [nx, ny] = points[i + 1];
    const v1 = [cx - px, cy - py], v2 = [nx - cx, ny - cy];
    const l1 = Math.hypot(v1[0], v1[1]), l2 = Math.hypot(v2[0], v2[1]);
    const r = Math.min(radius, l1 / 2, l2 / 2);
    if (r > 0.5) {
      d += ` L ${cx - (v1[0] / l1) * r},${cy - (v1[1] / l1) * r}` +
           ` Q ${cx},${cy} ${cx + (v2[0] / l2) * r},${cy + (v2[1] / l2) * r}`;
    } else {
      d += ` L ${cx},${cy}`;
    }
  }
  const last = points[points.length - 1];
  return d + ` L ${last[0]},${last[1]}`;
}

function pathMidpoint(points) {
  const segs = [];
  let total = 0;
  for (let i = 1; i < points.length; i++) {
    const len = Math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]);
    segs.push(len);
    total += len;
  }
  let target = total / 2;
  for (let i = 0; i < segs.length; i++) {
    if (target <= segs[i] || i === segs.length - 1) {
      const t = segs[i] ? target / segs[i] : 0;
      return [
        points[i][0] + (points[i + 1][0] - points[i][0]) * t,
        points[i][1] + (points[i + 1][1] - points[i][1]) * t,
      ];
    }
    target -= segs[i];
  }
  return points[0];
}

// Edge from port a to port b. opts.mid/opts.clear pin the elbow position;
// labels render on a background chip at the path midpoint.
function edge(a, b, { label = "", kind = "flow", ...opts } = {}) {
  const style = EDGE_KINDS[kind];
  const points = simplify(routePoints(a, b, opts));
  let svg = `<path d="${pathFromPoints(points)}" fill="none" stroke="${style.color}" stroke-width="1.5"` +
    (style.dash ? ` stroke-dasharray="${style.dash}"` : "") +
    ` marker-end="url(#${style.marker})"/>`;
  if (label) {
    const [mx, my] = pathMidpoint(points);
    const w = label.length * 4.9 + 14;
    svg += `<rect x="${mx - w / 2}" y="${my - 8.5}" width="${w}" height="17" rx="4" class="wf-edge-chip"/>` +
      `<text x="${mx}" y="${my + 3.5}" text-anchor="middle" class="wf-edge-label">${esc(label)}</text>`;
  }
  return svg;
}

/* ------------------------------ node primitives ------------------------------ */

function node(b, { kind, num = "", title, caption = [], mono = false }) {
  const style = KIND_STYLE[kind];
  const titleLines = lines(title);
  const capLines = lines(caption);
  const chip = num
    ? `<circle cx="${b.x}" cy="${b.y}" r="11" fill="${style.chip}"/>` +
      `<text x="${b.x}" y="${b.y + 3.5}" text-anchor="middle" class="wf-num">${num}</text>`
    : "";
  const titleY = b.y + 22;
  const titles = titleLines.map((line, i) =>
    `<text x="${b.x + 12}" y="${titleY + i * 13}" class="wf-title${mono ? " mono" : ""}">${esc(line)}</text>`).join("");
  const capY = titleY + (titleLines.length - 1) * 13 + 17;
  const caps = capLines.map((line, i) =>
    `<text x="${b.x + 12}" y="${capY + i * 12}" class="wf-caption">${esc(line)}</text>`).join("");
  return `<g>` +
    `<rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="8" fill="${style.fill}" stroke="${style.stroke}" stroke-width="1.2"/>` +
    chip + titles + caps +
  `</g>`;
}

// Dashed-border terminal = a target outside this sub-graph.
function terminal(b, text) {
  const style = KIND_STYLE.io;
  return `<g>` +
    `<rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="8" fill="${style.fill}" stroke="${style.stroke}" stroke-width="1.2" stroke-dasharray="5 4"/>` +
    `<text x="${b.x + b.w / 2}" y="${b.y + b.h / 2 + 4}" text-anchor="middle" class="wf-terminal-text">${esc(text)}</text>` +
  `</g>`;
}

/* ------------------------------ L0: system at a glance ------------------------------ */

const L0_NODES = [
  { id: "mission",  title: "Mission",            caption: "the tasking that starts a run",              badge: "input",           kind: "io",       x: 40,   w: 200 },
  { id: "hyper",    title: "Planning Agent",     caption: "solver-verified plan + state machine",       badge: "self-repairing",  kind: "llm",      x: 330,  w: 260 },
  { id: "maneuver", title: "Execution Agent",    caption: "closed-loop control: reason → act → adapt",  badge: "feedback-driven", kind: "feedback", x: 680,  w: 260 },
  { id: "result",   title: "Mission Result",     caption: "final structured outcome",                   badge: "output",          kind: "io",       x: 1030, w: 170 },
];
const L0_DATA = { id: "data", title: "Data & Memory Plane", caption: "durable artifacts; every decision inspectable", badge: "auditable", kind: "data", x: 330, w: 610 };
const L0_IDS = [...L0_NODES.map((n) => n.id), L0_DATA.id];
const L0_Y = 70, L0_H = 108, L0_DATA_Y = 236, L0_DATA_H = 96;
const L0_BASE_H = 356;
const REGION_TOP = 380;

function l0Node(def, y, h, expanded, dimmed) {
  const style = KIND_STYLE[def.kind];
  const badgeW = def.badge.length * 5 + 18;
  const chevron = expanded
    ? `<path d="M -4 1.5 L 0 -3 L 4 1.5" class="wf-afford-chevron"/>`
    : `<path d="M -4 -1.5 L 0 3 L 4 -1.5" class="wf-afford-chevron"/>`;
  return `<g class="wf-l0node${expanded ? " active" : ""}${dimmed ? " dim" : ""}"` +
    ` data-wf="toggle-node" data-node="${def.id}" tabindex="0" role="button"` +
    ` aria-expanded="${String(expanded)}" aria-label="${esc(def.title)} — ${expanded ? "collapse" : "expand"}">` +
    `<rect class="card" x="${def.x}" y="${y}" width="${def.w}" height="${h}" rx="10" fill="${style.fill}" stroke="${style.stroke}" stroke-width="1.4"/>` +
    `<text x="${def.x + 18}" y="${y + 32}" class="wf-l0title">${esc(def.title)}</text>` +
    `<text x="${def.x + 18}" y="${y + 52}" class="wf-l0caption">${esc(def.caption)}</text>` +
    `<rect x="${def.x + 18}" y="${y + 64}" width="${badgeW}" height="18" rx="9" fill="${style.fill}" stroke="${style.stroke}" stroke-width="1"/>` +
    `<text x="${def.x + 18 + badgeW / 2}" y="${y + 76.5}" text-anchor="middle" class="wf-badge-text" fill="${style.stroke}">${esc(def.badge)}</text>` +
    `<text x="${def.x + def.w - 30}" y="${y + h - 14}" text-anchor="end" class="wf-afford">${expanded ? "collapse" : "expand"}</text>` +
    `<g transform="translate(${def.x + def.w - 18}, ${y + h - 18})">${chevron}</g>` +
  `</g>`;
}

function buildL0(expanded) {
  const dim = (id) => expanded && expanded !== "all" && expanded !== id;
  const b = {
    mission: box(40, L0_Y, 200, L0_H),
    hyper: box(330, L0_Y, 260, L0_H),
    maneuver: box(680, L0_Y, 260, L0_H),
    result: box(1030, L0_Y, 170, L0_H),
    data: box(330, L0_DATA_Y, 610, L0_DATA_H),
  };
  const edges =
    edge(port(b.mission, "E"), port(b.hyper, "W"), { label: "mission brief" }) +
    edge(port(b.hyper, "E"), port(b.maneuver, "W"), { label: "verified plan" }) +
    edge(port(b.maneuver, "E"), port(b.result, "W"), { label: "final result" }) +
    edge(port(b.hyper, "S", 0.3), port(b.data, "N", 0.25), { label: "publish / subscribe", kind: "data" }) +
    edge(port(b.maneuver, "S", 0.7), port(b.data, "N", 0.75), { label: "read / write artifacts", kind: "data" });
  const nodes =
    L0_NODES.map((def) => l0Node(def, L0_Y, L0_H, expanded === def.id || expanded === "all", dim(def.id))).join("") +
    l0Node(L0_DATA, L0_DATA_Y, L0_DATA_H, expanded === "data" || expanded === "all", dim("data"));
  return `<g class="${expanded && expanded !== "all" ? "wf-l0-dimmed" : ""}">${edges}</g>` + nodes;
}

/* ------------------------------ shared region pieces ------------------------------ */

function regionShell(oy, h, id, title, subtitle) {
  return {
    open: `<g class="wf-region" data-region="${id}">` +
      `<rect x="12" y="${oy}" width="1216" height="${h}" rx="10" class="wf-band"/>` +
      `<text x="28" y="${oy + 30}" class="wf-region-title">${esc(title)}</text>` +
      `<text x="28" y="${oy + 48}" class="wf-region-sub">${esc(subtitle)}</text>` +
      `<g class="wf-collapse" data-wf="collapse" tabindex="0" role="button" aria-label="Collapse">` +
        `<rect x="1120" y="${oy + 14}" width="96" height="24" rx="12"/>` +
        `<text x="1168" y="${oy + 29.5}" text-anchor="middle">collapse ✕</text>` +
      `</g>`,
    close: `</g>`,
  };
}

// L2 deep-link chip row ("View in run" → #view=trajectory&phase=…).
function runStrip(x, y, chips) {
  const enabled = Boolean(state.missionId);
  let svg = `<text x="${x}" y="${y + 15}" class="wf-strip-label">view in run:</text>`;
  let cx = x + 74;
  for (const chip of chips) {
    const w = chip.label.length * 5 + 30;
    svg += `<g class="wf-runchip${enabled ? "" : " disabled"}"` +
      (enabled ? ` data-wf="open-run" data-phase="${esc(chip.phase)}" tabindex="0" role="link"` : "") +
      ` aria-label="View ${esc(chip.label)} in current run">` +
      (enabled ? "" : `<title>Select a mission to open the run</title>`) +
      `<rect x="${cx}" y="${y}" width="${w}" height="22" rx="11"/>` +
      `<text x="${cx + 11}" y="${y + 15}">${esc(chip.label)}</text>` +
      `<path d="M -2.5 -3.5 L 2 0 L -2.5 3.5" transform="translate(${cx + w - 11}, ${y + 11})" class="wf-runchip-arrow"/>` +
    `</g>`;
    cx += w + 8;
  }
  return svg;
}

/* ------------------------------ L1 regions ------------------------------ */

const PHASES = [
  { num: "1", kind: "decision", title: "record_planning_intent", caption: ["PlanningIntent +", "PlannerChoice"], run: "planning-intent", chip: "1 · intent" },
  { num: "2", kind: "tool",     title: "load_planning_context",  caption: ["mission + environment", "context"],  run: "planning-context", chip: "2 · context" },
  { num: "3", kind: "tool",     title: ["write model.mzn", "/ data.dzn"], caption: "filesystem tools",           run: "planner-assets",  chip: "3–4 · assets" },
  { num: "4", kind: "tool",     title: "persist_planner_assets", caption: "freeze the planner draft",            run: "planner-assets" },
  { num: "5", kind: "tool",     title: "planner_executor",       caption: ["runs the solver —", "verified plan ∨ rejection"], run: "planner-execution", chip: "5 · solver" },
  { num: "6", kind: "decision", title: "submit_statechart_draft", caption: ["schema + machine-build", "validation"], run: "statechart-generation", chip: "6 · statechart" },
  { num: "7", kind: "feedback", title: "handoff_execution",      caption: ["activates FSM, invokes", "maneuver agent"], run: "maneuver-handoff", chip: "7 · handoff" },
];

function regionHyper(oy) {
  const h = 452;
  const shell = regionShell(oy, h, "hyper",
    "Planning Agent — phase pipeline",
    "DeepAgents · phase-gated tools · solver-verified plan + statechart");
  const chainY = oy + 180;
  const boxes = PHASES.map((_, i) => box(26 + i * 172, chainY, 156, 84));
  const llmBox = box(40, oy + 72, 230, 56);
  const solverBox = box(714, oy + 320, 156, 56);
  const termBox = box(1058, oy + 66, 156, 46);

  let svg = shell.open;
  // edges first (under nodes)
  svg += edge(port(llmBox, "S"), port(boxes[0], "N"), { label: "phase-gated tools", kind: "llm" });
  for (let i = 0; i < boxes.length - 1; i++) {
    svg += edge(port(boxes[i], "E"), port(boxes[i + 1], "W"), {});
  }
  svg += edge(port(boxes[4], "S", 0.25), port(solverBox, "N", 0.25), { label: "invoke" });
  svg += edge(port(solverBox, "N", 0.75), port(boxes[4], "S", 0.75), { label: "plan ∨ rejection", kind: "retry" });
  // retry loops route around the outside of the chain
  svg += edge(port(boxes[4], "N"), port(boxes[2], "N"),
    { label: "rejection + repair ↺ ≤ max_planner_attempts", kind: "retry", clear: oy + 124 });
  svg += edge(port(boxes[5], "N", 0.3), port(boxes[5], "N", 0.7),
    { label: "schema + machine-build validation ↺ ≤ max_statechart_attempts", kind: "retry", clear: oy + 141 });
  svg += edge(port(boxes[6], "N", 0.8), port(termBox, "S", 0.8), {});
  // nodes
  svg += node(llmBox, { kind: "llm", title: "DeepAgents LLM", caption: "reasons + calls phase-gated tools" });
  PHASES.forEach((phase, i) => {
    svg += node(boxes[i], { kind: phase.kind, num: phase.num, title: phase.title, caption: phase.caption, mono: true });
  });
  svg += node(solverBox, { kind: "tool", title: "MiniZinc solver", caption: "model.mzn + data.dzn" });
  svg += terminal(termBox, "→ execution agent");
  // L2 strip
  svg += runStrip(28, oy + 410, PHASES.filter((p) => p.chip).map((p) => ({ label: p.chip, phase: p.run })));
  return { h, svg: svg + shell.close };
}

function regionManeuver(oy) {
  const h = 288;
  const shell = regionShell(oy, h, "maneuver",
    "Execution Agent — closed control loop",
    "heartbeat-driven · reason → act → observe → adapt");
  const rowY = oy + 96;
  const heartbeat = box(40, rowY, 170, 84);
  const llm = box(260, rowY, 190, 84);
  const tools = box(500, rowY, 330, 84);
  const record = box(880, rowY, 250, 84);
  const termBox = box(770, oy + 18, 200, 44);

  let svg = shell.open;
  svg += edge(port(heartbeat, "E"), port(llm, "W"), {});
  svg += edge(port(llm, "E"), port(tools, "W"), {});
  svg += edge(port(tools, "E"), port(record, "W"), {});
  svg += edge(port(record, "S"), port(heartbeat, "S"), { label: "next heartbeat", kind: "data", clear: oy + 224 });
  svg += edge(port(record, "N", 0.35), port(termBox, "S", 0.5), { label: "completion → final result", kind: "done" });
  svg += node(heartbeat, { kind: "io", title: "heartbeat invocation", caption: ["runtime ticks the", "FSM loop"] });
  svg += node(llm, { kind: "llm", title: "Maneuver agent LLM", caption: ["reasons over FSM state", "+ belief store"] });
  svg += node(tools, { kind: "tool", title: "execution tools",
    caption: ["transition_fsm · navigate · takeoff · land", "search_area · pursue · investigate", "update_belief · communicate"] });
  svg += node(record, { kind: "feedback", title: ["execution record +", "maneuver-feedback"], caption: ["recorded + published", "each invocation"] });
  svg += terminal(termBox, "→ mission result");
  svg += runStrip(28, oy + 248, [
    { label: "heartbeat loop", phase: "heartbeat" },
    { label: "maneuver-control", phase: "maneuver-control" },
  ]);
  return { h, svg: svg + shell.close };
}

const DATA_NODES = [
  { kind: "io",   title: "transport event bus", caption: ["snapshots · env-data · plans", "fsm-status · statechart · feedback", "commands + receipts / outcomes"] },
  { kind: "data", title: "operational log",     caption: ["structured timeline", "of every step"] },
  { kind: "data", title: "FSM store",           caption: ["statechart +", "execution record"] },
  { kind: "data", title: "planner artifacts",   caption: ["model.mzn · data.dzn", "statechart attempts"] },
  { kind: "data", title: "debug recorders",     caption: ["raw LLM + agent", "invocations"] },
  { kind: "data", title: "environment state",   caption: "live world snapshot" },
];

function regionData(oy) {
  const h = 250;
  const shell = regionShell(oy, h, "data",
    "Data & Memory Plane — durable artifacts",
    "transport bus · operational log · FSM store · planner workspace · debug recorders · environment");
  const boxes = DATA_NODES.map((_, i) => box(23 + i * 202, oy + 72, 184, 78));
  const viewer = box(24, oy + 196, 280, 30);

  let svg = shell.open;
  svg += edge(port(boxes[0], "S"), port(viewer, "N", 0.4), { label: "read by this viewer", kind: "data" });
  DATA_NODES.forEach((def, i) => {
    svg += node(boxes[i], { kind: def.kind, title: def.title, caption: def.caption });
  });
  svg += `<g><rect x="${viewer.x}" y="${viewer.y}" width="${viewer.w}" height="${viewer.h}" rx="8"` +
    ` fill="var(--accent-soft)" stroke="var(--accent)" stroke-width="1.2" stroke-dasharray="4 3"/>` +
    `<text x="${viewer.x + viewer.w / 2}" y="${viewer.y + 19.5}" text-anchor="middle" class="wf-chip-text">run viewer — this app</text></g>`;
  return { h, svg: svg + shell.close };
}

function regionMission(oy) {
  const h = 200;
  const shell = regionShell(oy, h, "mission",
    "Mission — input",
    "examples/mission.json · CLI/runtime composes both agents");
  const file = box(28, oy + 84, 220, 56);
  const cli = box(330, oy + 84, 250, 56);
  let svg = shell.open;
  svg += edge(port(file, "E"), port(cli, "W"), { label: "composes agents" });
  svg += node(file, { kind: "io", title: "mission.json", caption: "objective + constraints", mono: true });
  svg += node(cli, { kind: "io", title: "CLI / runtime composer", caption: "wires agents + transport" });
  return { h, svg: svg + shell.close };
}

function regionResult(oy) {
  const h = 200;
  const shell = regionShell(oy, h, "result",
    "Mission Result — output",
    "returned by the planning agent's handoff when execution completes");
  const done = box(28, oy + 84, 260, 56);
  const termBox = box(330, oy + 84, 220, 56);
  let svg = shell.open;
  svg += edge(port(termBox, "E"), port(done, "W"), {});
  svg += terminal(termBox, "execution complete");
  svg += node(done, { kind: "io", title: "final structured result", caption: "status + evidence refs" });
  return { h, svg: svg + shell.close };
}

/* ------------------------------ svg assembly ------------------------------ */

const REGION_BUILDERS = {
  mission: regionMission,
  hyper: regionHyper,
  maneuver: regionManeuver,
  result: regionResult,
  data: regionData,
};
const REGION_ORDER = ["mission", "hyper", "maneuver", "result", "data"];

function buildSvg(expandedRaw) {
  const expanded = L0_IDS.includes(expandedRaw) || expandedRaw === "all" ? expandedRaw : "";
  const parts = [`<defs>
    <marker id="wf-ar" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="var(--text-2)"/></marker>
    <marker id="wf-ar-amber" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="var(--kind-decision)"/></marker>
    <marker id="wf-ar-green" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="var(--kind-feedback)"/></marker>
    <marker id="wf-ar-violet" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="var(--kind-llm)"/></marker>
  </defs>`];

  parts.push(buildL0(expanded));

  let height = L0_BASE_H;
  if (expanded) {
    let oy = REGION_TOP;
    const ids = expanded === "all" ? REGION_ORDER : [expanded];
    for (const id of ids) {
      const region = REGION_BUILDERS[id](oy);
      parts.push(region.svg);
      oy += region.h + 16;
    }
    height = oy + 4;
  }

  return `<svg viewBox="0 0 1240 ${height}" role="img" aria-label="ONR agentic pipeline workflow diagram">${parts.join("")}</svg>`;
}

/* ------------------------------ legend + value strip ------------------------------ */

function swatch(kind) {
  const style = KIND_STYLE[kind];
  return h("span", { class: "wf-swatch", style: { background: style.fill, borderColor: style.stroke } });
}

function legend() {
  const item = (sample, text) => h("span", { class: "wf-legend-item" }, sample, text);
  return h("div", { class: "wf-legend", "aria-label": "Diagram legend" },
    item(swatch("llm"), "LLM reasoning"),
    item(swatch("tool"), "tool / solver"),
    item(swatch("decision"), "decision / validation"),
    item(swatch("feedback"), "feedback / record"),
    item(swatch("data"), "artifact / store"),
    item(swatch("io"), "input / output boundary"),
    item(h("span", { class: "wf-line" }), "invokes / flows into"),
    item(h("span", { class: "wf-line dashed amber" }), "retry loop (↺)"),
    item(h("span", { class: "wf-line dashed" }), "artifact / event read-write"));
}

const VALUES = [
  { icon: "check",    title: "Autonomy with verification", text: "Plans are solver-checked and statecharts schema-validated before anything executes." },
  { icon: "feedback", title: "Self-repairing",             text: "Rejection → repair loops recover from bad plans and invalid state machines without human intervention." },
  { icon: "zap",      title: "Closed-loop",                text: "Execution feedback drives the next decision — reason, act, observe, adapt." },
  { icon: "file",     title: "Fully auditable",            text: "Every reasoning step, tool call, and artifact is persisted — this viewer reads that record." },
];

function valueStrip() {
  return h("div", { class: "wf-values", "data-testid": "wf-values" },
    VALUES.map((value) => h("article", { class: "wf-value" },
      h("span", { class: "wf-value-icon" }, icon(value.icon, 14)),
      h("h3", {}, value.title),
      h("p", {}, value.text))));
}

/* ------------------------------ view ------------------------------ */

function openInRun(phase) {
  if (!state.missionId || !phase) return;
  const params = new URLSearchParams();
  params.set("mission", state.missionId);
  params.set("view", "trajectory");
  params.set("phase", phase);
  location.hash = "#" + params.toString();
}

export function renderWorkflow(root, actions) {
  root.replaceChildren();
  const wrap = h("div", { class: "workflow", "data-testid": "view-workflow" });

  const expanded = state.workflowNode;
  const toolbar = h("div", { class: "wf-toolbar" },
    h("p", { class: "wf-note" }, expanded
      ? "Expanded in place — click the node again, or collapse, to return to the overview."
      : "System at a glance — click any node to expand its pipeline."),
    h("button", {
      class: "filter-toggle",
      type: "button",
      "data-testid": "wf-expand-all",
      onclick: () => actions.setWorkflowNode(expanded ? "" : "all"),
    }, icon(expanded ? "collapseAll" : "expandAll", 12), expanded ? "Collapse" : "Expand all"));

  const frame = h("div", { class: "wf-frame", "data-testid": "workflow-diagram", html: buildSvg(expanded) });

  const activate = (el) => {
    const action = el.getAttribute("data-wf");
    if (action === "toggle-node") {
      const id = el.getAttribute("data-node");
      actions.setWorkflowNode(state.workflowNode === id ? "" : id);
    } else if (action === "collapse") {
      actions.setWorkflowNode("");
    } else if (action === "open-run") {
      openInRun(el.getAttribute("data-phase"));
    }
  };
  frame.addEventListener("click", (event) => {
    const el = event.target.closest("[data-wf]");
    if (el && frame.contains(el)) activate(el);
  });
  frame.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const el = event.target.closest("[data-wf]");
    if (!el || !frame.contains(el)) return;
    event.preventDefault();
    activate(el);
  });

  wrap.append(h("div", { class: "wf-inner" }, toolbar, frame, legend(), valueStrip()));
  root.append(wrap);
}

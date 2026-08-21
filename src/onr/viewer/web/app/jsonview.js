// Collapsible JSON viewer with a formatted(tree) ⇄ raw toggle.
// Toggle/collapse state is kept in a module-level map keyed by `stateKey`,
// so poll-driven re-renders never lose the user's place.

import { h, icon } from "./dom.js";
import { escapeHtml } from "./format.js";
import { highlightCode } from "./highlight.js";

const LONG_STRING = 240;
const STATE = new Map(); // stateKey -> { mode, collapsed:Set, openStrings:Set }

function viewState(key) {
  if (!STATE.has(key)) STATE.set(key, { mode: "tree", collapsed: null, openStrings: new Set() });
  return STATE.get(key);
}

function defaultCollapsed(value, path, depth) {
  if (depth >= 3) return true;
  if (Array.isArray(value) && value.length > 24) return true;
  if (value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length > 24) return true;
  return false;
}

function preview(value) {
  if (Array.isArray(value)) return value.length ? `${value.length} item${value.length === 1 ? "" : "s"}` : "[]";
  const keys = Object.keys(value);
  if (!keys.length) return "{}";
  return keys.slice(0, 3).join(", ") + (keys.length > 3 ? ", …" : "");
}

function primitive(value, ctx) {
  if (value === null) return h("span", { class: "tok-bool" }, "null");
  if (typeof value === "number") return h("span", { class: "tok-num" }, String(value));
  if (typeof value === "boolean") return h("span", { class: "tok-bool" }, String(value));
  const text = String(value);
  if (text.length <= LONG_STRING) return h("span", { class: "tok-str" }, JSON.stringify(text));
  const openKey = ctx.path;
  const open = ctx.state.openStrings.has(openKey);
  const toggle = h("button", {
    class: "str-toggle",
    type: "button",
    onclick: (event) => {
      event.stopPropagation();
      if (open) ctx.state.openStrings.delete(openKey);
      else ctx.state.openStrings.add(openKey);
      ctx.rerender();
    },
  }, open ? "collapse" : `… +${(text.length - LONG_STRING).toLocaleString("en-US")} chars`);
  return h("span", { class: "tok-str" },
    JSON.stringify(open ? text : text.slice(0, LONG_STRING) + "…"), " ", toggle);
}

function node(value, key, ctx) {
  const path = ctx.path;
  const depth = ctx.depth;
  const isObj = value !== null && typeof value === "object";
  if (!isObj) {
    return h("div", { class: "jv-row jv-leaf" },
      key !== null ? h("span", { class: "jv-key" }, key) : null,
      key !== null ? h("span", { class: "jv-colon" }, ": ") : null,
      primitive(value, ctx));
  }

  const collapsed = ctx.state.collapsed === null
    ? defaultCollapsed(value, path, depth)
    : ctx.state.collapsed.has(path);
  const entries = Array.isArray(value)
    ? value.map((item, index) => [String(index), item])
    : Object.entries(value);

  const caret = h("button", {
    class: "jv-caret" + (collapsed ? "" : " open"),
    type: "button",
    "aria-label": collapsed ? "Expand" : "Collapse",
    onclick: (event) => {
      event.stopPropagation();
      if (ctx.state.collapsed === null) {
        // Materialize the default collapse set so one toggle doesn't disturb others.
        ctx.state.collapsed = new Set();
        collectDefaultCollapsed(ctx.root, "", 0, ctx.state.collapsed);
      }
      if (collapsed) ctx.state.collapsed.delete(path);
      else ctx.state.collapsed.add(path);
      ctx.rerender();
    },
    html: `<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 5.5l6.5 6.5L9 18.5"/></svg>`,
  });

  const head = h("div", { class: "jv-row jv-node" },
    caret,
    key !== null ? h("span", { class: "jv-key" }, key) : null,
    key !== null ? h("span", { class: "jv-colon" }, ": ") : null,
    collapsed
      ? h("button", { class: "jv-preview", type: "button", onclick: caret.onclick },
          Array.isArray(value) ? "[" : "{", h("span", { class: "jv-preview-body" }, preview(value)), Array.isArray(value) ? "]" : "}")
      : h("span", { class: "jv-brace" }, Array.isArray(value) ? "[" : "{"));

  const wrap = h("div", { class: "jv-block" }, head);
  if (!collapsed) {
    const children = h("div", { class: "jv-children" });
    for (const [childKey, childValue] of entries) {
      children.append(node(childValue, Array.isArray(value) ? null : childKey, {
        ...ctx, path: path ? path + "." + childKey : childKey, depth: depth + 1,
      }));
    }
    wrap.append(children, h("div", { class: "jv-row jv-close" }, h("span", { class: "jv-brace" }, Array.isArray(value) ? "]" : "}")));
  }
  return wrap;
}

function collectDefaultCollapsed(value, path, depth, set) {
  if (value === null || typeof value !== "object") return;
  if (defaultCollapsed(value, path, depth)) {
    set.add(path);
    return; // children of a collapsed node are irrelevant
  }
  const entries = Array.isArray(value) ? value.map((v, i) => [String(i), v]) : Object.entries(value);
  for (const [k, v] of entries) collectDefaultCollapsed(v, path ? path + "." + k : k, depth + 1, set);
}

export function jsonView(value, opts = {}) {
  const stateKey = opts.stateKey || "anon";
  const state = viewState(stateKey);
  const root = h("div", { class: "jsonview" });

  const rerender = () => {
    root.replaceChildren();
    root.append(buildBody());
  };

  const buildBody = () => {
    const body = h("div", { class: "jv-body" });
    if (state.mode === "raw") {
      const raw = typeof value === "string" ? value : JSON.stringify(value, null, 2);
      body.append(h("pre", { class: "jv-raw", html: highlightCode(raw ?? "null", "json") }));
    } else {
      body.append(node(value, null, { path: "", depth: 0, state, rerender, root: value }));
    }
    return body;
  };

  const modeBtn = (mode, label) => h("button", {
    class: "jv-mode" + (state.mode === mode ? " active" : ""),
    type: "button",
    "aria-pressed": String(state.mode === mode),
    onclick: () => { state.mode = mode; rerender(); },
  }, label);

  const copyBtn = h("button", {
    class: "jv-copy", type: "button", title: "Copy JSON",
    onclick: async (event) => {
      const raw = typeof value === "string" ? value : JSON.stringify(value, null, 2);
      try {
        await navigator.clipboard.writeText(raw ?? "null");
        event.currentTarget.classList.add("done");
        setTimeout(() => copyBtn.classList.remove("done"), 900);
      } catch (_) { /* clipboard unavailable (permissions) — ignore */ }
    },
    html: `<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5.5 14.5V6a2 2 0 0 1 2-2h8.5"/></svg>`,
  });

  root.append(
    h("div", { class: "jv-toolbar" },
      h("span", { class: "jv-modes" }, modeBtn("tree", "Formatted"), modeBtn("raw", "Raw")),
      copyBtn),
    buildBody(),
  );
  return root;
}

export function resetJsonViewState(prefix) {
  for (const key of [...STATE.keys()]) {
    if (!prefix || key.startsWith(prefix)) STATE.delete(key);
  }
}

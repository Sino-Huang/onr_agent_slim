// Live physical world-model frame and state.

import { h, icon } from "./dom.js";
import { jsonView } from "./jsonview.js";
import { state } from "./store.js";

function statusTone(payload) {
  if (payload.connected) return "ok";
  if (payload.available) return "running";
  return "unknown";
}

export function renderWorldModel(root) {
  const payload = state.worldModel || {
    available: false,
    connected: false,
    status: "connecting",
    sequence: 0,
    state: {},
  };
  const frame = payload.available
    ? h("img", {
        class: "wm-frame-image",
        src: "/api/world-model/frame?sequence=" + encodeURIComponent(payload.sequence),
        alt: "Current physical world-model frame",
        "data-testid": "world-model-frame",
      })
    : h("div", { class: "wm-empty" },
        icon("world", 30),
        h("strong", {}, payload.status === "disabled" ? "World-model stream disabled" : "Waiting for the physical runtime"),
        h("span", {}, payload.error || "No world_update frame has arrived yet."));

  const status = h("span", {
    class: "status-pill tone-" + statusTone(payload),
    "data-testid": "world-model-status",
  }, h("span", { class: "status-dot" + (payload.connected ? " pulse" : "") }), payload.status);

  root.replaceChildren(h("section", {
    class: "world-model-view",
    "data-testid": "view-world-model",
  },
  h("header", { class: "wm-head" },
    h("div", {},
      h("h2", {}, "Physical world model"),
      h("p", {}, "Live frame emitted by the mission's in-memory MultiGrid environment.")),
    status),
  h("div", { class: "wm-layout" },
    h("div", { class: "wm-frame" }, frame,
      payload.available
        ? h("div", { class: "wm-frame-meta" },
            h("span", {}, "Frame ", h("code", {}, String(payload.sequence))),
            payload.generation_timestamp_s
              ? h("span", {}, new Date(payload.generation_timestamp_s * 1000).toLocaleTimeString())
              : null)
        : null),
    h("aside", { class: "wm-state" },
      h("div", { class: "wm-state-head" }, icon("list", 13), h("h3", {}, "World-model state")),
      jsonView(payload.state || {}, { stateKey: "world-model:state" }))));
}

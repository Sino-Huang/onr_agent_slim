// Inline artifact viewer: fetches /api/artifact (or the mock) and renders the
// content with syntax highlighting and line numbers. Shared by the overview
// artifacts index and artifact chips in the detail panel.

import { h, icon } from "./dom.js";
import { codeBlockHtml, langForRef } from "./highlight.js";
import { getArtifact } from "./api.js";
import { state } from "./store.js";

const CACHE = new Map(); // ref -> {status:'loading'} | {status:'ok', text, json, fromMock} | {status:'error', message}

export function invalidateArtifactCache() {
  CACHE.clear();
}

export function artifactViewer(ref, label) {
  const wrap = h("div", { class: "artifact-viewer", "data-testid": "artifact-viewer", "data-ref": ref });
  const cached = CACHE.get(ref);

  const header = h("div", { class: "artifact-header" },
    icon("file", 13),
    h("span", { class: "artifact-name" }, label || ref),
    h("code", { class: "artifact-ref" }, ref));

  const body = h("div", { class: "artifact-body" });
  wrap.append(header, body);

  const render = () => {
    const entry = CACHE.get(ref);
    body.replaceChildren();
    if (!entry || entry.status === "loading") {
      body.append(h("p", { class: "artifact-note" }, "Loading artifact…"));
    } else if (entry.status === "error") {
      body.append(h("p", { class: "artifact-note tone-error" }, entry.message));
    } else {
      body.append(
        h("pre", { class: "code lang-" + langForRef(ref), html: codeBlockHtml(entry.text, langForRef(ref)) }),
        entry.fromMock ? h("p", { class: "artifact-note" }, "Served from bundled demo data.") : null,
      );
    }
  };

  if (!cached) {
    CACHE.set(ref, { status: "loading" });
    getArtifact(state.missionId, ref)
      .then((result) => CACHE.set(ref, { status: "ok", ...result }))
      .catch((error) => CACHE.set(ref, { status: "error", message: error.status === 404 ? "Artifact not found on the server." : "Could not load artifact: " + error.message }))
      .finally(render);
  }
  render();
  return wrap;
}

// Formatting helpers + step metadata shared by all views.

export function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

export function fmtDuration(ms) {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "—";
  if (ms < 1000) return Math.round(ms) + "ms";
  if (ms < 10_000) return (ms / 1000).toFixed(1) + "s";
  if (ms < 60_000) return Math.round(ms / 1000) + "s";
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return minutes + "m " + String(seconds).padStart(2, "0") + "s";
}

export function fmtCount(n) {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  return n.toLocaleString("en-US");
}

// Clock time as recorded (UTC), no timezone surprises when comparing logs.
export function fmtTime(isoValue) {
  if (!isoValue) return "—";
  const parsed = Date.parse(isoValue);
  if (!Number.isFinite(parsed)) return String(isoValue);
  const d = new Date(parsed);
  const p = (n, w = 2) => String(n).padStart(w, "0");
  return (
    p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + ":" + p(d.getUTCSeconds())
  );
}

export function fmtTimeMs(isoValue) {
  if (!isoValue) return "—";
  const parsed = Date.parse(isoValue);
  if (!Number.isFinite(parsed)) return String(isoValue);
  const d = new Date(parsed);
  return fmtTime(isoValue) + "." + String(d.getUTCMilliseconds()).padStart(3, "0");
}

export function fmtDate(isoValue) {
  if (!isoValue) return "";
  const parsed = Date.parse(isoValue);
  if (!Number.isFinite(parsed)) return "";
  return new Date(parsed).toISOString().slice(0, 10);
}

// Offset from run start for timeline ticks: "+34.2s"
export function fmtOffset(ms) {
  if (ms < 1000) return "+" + Math.round(ms) + "ms";
  if (ms < 60_000) return "+" + (ms / 1000).toFixed(ms < 10_000 ? 1 : 0) + "s";
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return "+" + minutes + "m" + String(seconds).padStart(2, "0") + "s";
}

export function plural(n, word, pluralWord) {
  return n + " " + (n === 1 ? word : pluralWord || word + "s");
}

export function shorten(value, limit = 90) {
  const text = String(value ?? "");
  return text.length > limit ? text.slice(0, limit - 1) + "…" : text;
}

export const KIND_META = {
  llm: { label: "LLM", icon: "llm" },
  tool: { label: "Tool", icon: "tool" },
  decision: { label: "Decision", icon: "decision" },
  feedback: { label: "Feedback", icon: "feedback" },
};

export const STATUS_META = {
  ok: { label: "ok" },
  error: { label: "error" },
  unknown: { label: "unknown" },
};

// Outcomes are free-form strings from the pipeline; bucket them for color.
export function outcomeTone(outcome) {
  if (!outcome) return "unknown";
  const value = String(outcome).toLowerCase();
  if (["accepted", "complete", "completed", "ok", "ready", "success", "succeeded", "proceed", "received"].includes(value)) return "ok";
  if (["error", "failed", "failure", "rejected", "tool-error", "abort"].includes(value)) return "error";
  if (["actionable", "running", "in_progress", "started"].includes(value)) return "running";
  return "neutral";
}

export function* walkSteps(steps, depth = 0, parent = null) {
  for (const step of steps || []) {
    yield { step, depth, parent };
    yield* walkSteps(step.children || [], depth + 1, step);
  }
}

export function stepSearchText(step) {
  return [
    step.title, step.name, step.component, step.role, step.phase,
    step.kind, step.status, step.outcome,
    ...(step.tool_calls || []).map((t) => t.name),
    step.decision ? step.decision.event_kind : "",
    ...(step.feedback || []).map((f) => f.kind),
  ]
    .filter(Boolean)
    .join("\n")
    .toLowerCase();
}

export function hasToolError(step) {
  return (step.tool_calls || []).some((call) => call.error);
}

// A step "has an error" for display if its own status says so or one of its
// tool calls failed (even when the step recovered afterwards).
export function stepErrored(step) {
  return step.status === "error" || hasToolError(step);
}

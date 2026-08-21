// Minimal syntax highlighting for artifact viewers (json / mzn / dzn).
// Tokenizes raw source first, then escapes per token — safe against
// regex-over-escaped-HTML pitfalls.

import { escapeHtml } from "./format.js";

const RULES = {
  json: [
    { cls: "tok-key", re: /"(?:[^"\\]|\\.)*"(?=\s*:)/y },
    { cls: "tok-str", re: /"(?:[^"\\]|\\.)*"/y },
    { cls: "tok-bool", re: /\b(?:true|false|null)\b/y },
    { cls: "tok-num", re: /-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/y },
    { cls: null, re: /[\s\S]/y },
  ],
  mzn: [
    { cls: "tok-comment", re: /%[^\n]*/y },
    { cls: "tok-str", re: /"(?:[^"\\]|\\.)*"/y },
    {
      cls: "tok-kw",
      re: /\b(?:constraint|solve|maximize|minimize|satisfy|function|var|int|float|bool|string|set|of|array|output|if|then|else|endif|let|in|forall|exists|where|not|true|false)\b/y,
    },
    { cls: "tok-num", re: /-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/y },
    { cls: "tok-fn", re: /\b(?:show|round|ceil|sqrt|int2float|bool2int|sum|exists|forall|array2d|abs|min|max)\b(?=\s*\()/y },
    { cls: null, re: /[\s\S]/y },
  ],
};
RULES.dzn = RULES.mzn;

export function langForRef(ref) {
  const lower = String(ref || "").toLowerCase();
  if (lower.endsWith(".mzn")) return "mzn";
  if (lower.endsWith(".dzn")) return "dzn";
  if (lower.endsWith(".json")) return "json";
  return "text";
}

export function highlightCode(source, lang) {
  const rules = RULES[lang];
  if (!rules) return escapeHtml(source);
  let html = "";
  let pos = 0;
  const text = String(source);
  outer: while (pos < text.length) {
    for (const rule of rules) {
      rule.re.lastIndex = pos;
      const match = rule.re.exec(text);
      if (match && match.index === pos && match[0].length > 0) {
        const escaped = escapeHtml(match[0]);
        html += rule.cls ? `<span class="${rule.cls}">${escaped}</span>` : escaped;
        pos += match[0].length;
        continue outer;
      }
    }
    // Should be unreachable thanks to the catch-all rule; advance defensively.
    html += escapeHtml(text[pos]);
    pos += 1;
  }
  return html;
}

// Wrap highlighted code in numbered lines for the artifact viewer.
export function codeBlockHtml(source, lang) {
  const lines = highlightCode(source, lang).split("\n");
  return lines.map((line) => `<span class="code-line">${line || " "}</span>`).join("");
}

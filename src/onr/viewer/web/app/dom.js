// Tiny DOM helper + inline SVG icon set (stroke icons, currentColor).
// Zero dependencies; everything renders through these primitives.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "html") el.innerHTML = value; // trusted, internal markup only
    else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
    else if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, String(value));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(20)) {
    if (child === null || child === undefined || child === false || child === "") continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
}

const STROKE = 'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"';

const ICONS = {
  llm: `<path d="M12 3l1.9 5.6L19.5 10l-5.6 1.9L12 17.5l-1.9-5.6L4.5 10l5.6-1.4z"/><path d="M18.5 15.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z"/>`,
  tool: `<path d="M14.6 6.1a4.2 4.2 0 0 0-5.6 5.3L3.6 16.8a1.5 1.5 0 0 0 0 2.1l1.5 1.5a1.5 1.5 0 0 0 2.1 0l5.4-5.4a4.2 4.2 0 0 0 5.3-5.6l-2.7 2.7-2.4-.7-.7-2.4z"/>`,
  decision: `<path d="M12 3.2l7.8 8.8-7.8 8.8L4.2 12z"/><path d="M12 8.5v3.5l2.4 2.4"/>`,
  feedback: `<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 3.5V8h-4.5"/>`,
  chevronRight: `<path d="M9 5.5l6.5 6.5L9 18.5"/>`,
  chevronDown: `<path d="M5.5 9l6.5 6.5L18.5 9"/>`,
  search: `<circle cx="11" cy="11" r="6.5"/><path d="M20.5 20.5L15.9 15.9"/>`,
  clock: `<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2.5"/>`,
  alert: `<path d="M12 4L21 20H3z"/><path d="M12 10.5v4"/><path d="M12 17.4v.1"/>`,
  check: `<path d="M4.5 12.5l5 5L19.5 7"/>`,
  x: `<path d="M6 6l12 12M18 6L6 18"/>`,
  copy: `<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5.5 14.5V6a2 2 0 0 1 2-2h8.5"/>`,
  file: `<path d="M13.5 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8.5z"/><path d="M13.5 3v5.5H19"/>`,
  tree: `<circle cx="6" cy="5.5" r="2.2"/><circle cx="6" cy="18.5" r="2.2"/><circle cx="18" cy="12" r="2.2"/><path d="M6 7.7v8.6M7.8 6.6l8 4.2M7.8 17.4l8-4.2"/>`,
  list: `<path d="M8.5 6h12M8.5 12h12M8.5 18h12"/><circle cx="4" cy="6" r="1" fill="currentColor" stroke="none"/><circle cx="4" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="4" cy="18" r="1" fill="currentColor" stroke="none"/>`,
  timeline: `<path d="M3 20.5h18"/><path d="M4.5 16.5h6M8.5 11.5h8M5.5 6.5h5"/>`,
  overview: `<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/>`,
  workflow: `<rect x="2.5" y="3.5" width="7" height="6" rx="1.5"/><circle cx="18" cy="6.5" r="3"/><rect x="14.5" y="14.5" width="7" height="6" rx="1.5"/><path d="M9.5 6.5H15M18 9.5v5M14.5 17.5H9.5V9.5"/>`,
  world: `<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>`,
  inbox: `<path d="M3.5 13l2.7-7.5h11.6L20.5 13v5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/><path d="M3.5 13h5l1.5 2.5h4L15.5 13h5"/>`,
  zap: `<path d="M13 2.5L4.5 13.5H11l-1 8 8.5-11H12z"/>`,
  link: `<path d="M10 14a4.5 4.5 0 0 0 6.4.4l3-3a4.5 4.5 0 1 0-6.4-6.4l-1.7 1.7"/><path d="M14 10a4.5 4.5 0 0 0-6.4-.4l-3 3a4.5 4.5 0 1 0 6.4 6.4l1.7-1.7"/>`,
  arrowUp: `<path d="M12 19V5M5.5 11.5L12 5l6.5 6.5"/>`,
  arrowDown: `<path d="M12 5v14M5.5 12.5L12 19l6.5-6.5"/>`,
  expandAll: `<path d="M7 4.5L12 9.5l5-5"/><path d="M7 19.5l5-5 5 5"/>`,
  collapseAll: `<path d="M7 9.5l5 5 5-5"/><path d="M7 14.5l5 5 5-5"/>`,
  play: `<path d="M7 4.5l12 7.5-12 7.5z"/>`,
  dot: `<circle cx="12" cy="12" r="5" fill="currentColor" stroke="none"/>`,
};

export function icon(name, size = 14) {
  const svg = `<svg viewBox="0 0 24 24" width="${size}" height="${size}" ${STROKE} aria-hidden="true">${ICONS[name] || ICONS.dot}</svg>`;
  return h("span", { class: "icon", html: svg });
}

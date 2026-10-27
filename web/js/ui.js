// DOM helpers. Data from the notebook is always inserted as text, never as HTML.

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k === "dataset") Object.assign(el.dataset, v);
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k === "href") {
        const safe = safeHref(v);
        if (safe) el.setAttribute("href", safe);
      } else if (k === "src") {
        const safe = safeUrl(v);
        if (safe) el.setAttribute("src", safe);
      } else if (v === true) el.setAttribute(k, "");
      else el.setAttribute(k, String(v));
    }
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false || c === "") continue;
    el.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function safeUrl(u) {
  if (typeof u !== "string") return null;
  const s = u.trim();
  return /^https?:\/\//i.test(s) ? s : null;
}

function safeHref(u) {
  if (typeof u !== "string") return null;
  if (u.startsWith("#") || u.startsWith("/")) return u;
  return safeUrl(u);
}

/** External link that never sends a referrer; plain text when the URL is unusable. */
export function extLink(url, text) {
  const safe = safeUrl(url);
  if (!safe) return document.createTextNode(text || url || "");
  return h("a", { href: safe, target: "_blank", rel: "noopener noreferrer", referrerpolicy: "no-referrer" },
    text || shortUrl(safe));
}

export function shortUrl(u) {
  try {
    const url = new URL(u);
    const path = url.pathname.length > 28 ? url.pathname.slice(0, 26) + "…" : url.pathname;
    return url.hostname.replace(/^www\./, "") + (path === "/" ? "" : path);
  } catch {
    return u;
  }
}

export function initials(name) {
  const parts = (name || "").replace(/\(.*?\)/g, "").split(/\s+/).filter((p) => p && /[A-Za-zÀ-ž]/.test(p[0]));
  if (!parts.length) return "?";
  const first = parts[0][0];
  const last = parts.length > 1 ? parts[parts.length - 1][0] : "";
  return (first + last).toUpperCase();
}

export function ordinal(d) {
  const sfx = { 1: "st", 2: "nd", 3: "rd" }[d] || "th";
  return `${d}${sfx}`;
}

export function degreeText(d) {
  if (d === null || d === undefined) return "No recorded path";
  if (d === 0) return "Lab member";
  const sfx = { 1: "st", 2: "nd", 3: "rd" }[d] || "th";
  return `${d}${sfx} degree`;
}

export const GROUP_OF = {
  mentor: "supervision", postdoc: "supervision", committee: "supervision",
  coauthor: "research", grant: "research", collaborator: "research",
  training: "association", labmate: "association", irta_colleague: "association", met: "association", event: "association",
};
export const GROUP_LABEL = { research: "Research", supervision: "Supervision", association: "Association" };
export const GROUP_PRIORITY = ["supervision", "research", "association"];

export function dominantGroup(types) {
  for (const g of GROUP_PRIORITY) if ((types || []).some((t) => GROUP_OF[t] === g)) return g;
  return "association";
}

export const DOCTORATE_TEXT = {
  confirmed: "Doctorate confirmed",
  "not-yet-earned": "Doctorate not yet earned",
  unknown: "Doctorate recorded as unknown",
  "not-classified": "Doctorate not classified",
};

export const HIDDEN_TEXT = {
  doctorate: "no confirmed doctorate recorded",
  placement: "current institution or position not identified",
  degree: "outside the degree range shown",
  beyond: "further out than the degrees this notebook covers",
  archived: "archived",
};

/** The lab member a free-text attribution refers to: exact name or a longer form of it. */
export function rootForName(roots, name) {
  const n = (s) => (s || "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase().trim();
  const target = n(name);
  if (!target) return null;
  return roots.find((r) => n(r.name) === target) ||
    roots.find((r) => target.startsWith(n(r.name) + " ")) || null;
}

export function chip(text, cls = "") {
  return h("span", { class: "chip " + cls }, text);
}

export function missing(text = "Not recorded") {
  return h("span", { class: "missing" }, text);
}

let noteCount = 0;

/**
 * A small superscript "i" that shows more on hover, keyboard focus or a tap: the sources, dates, rules and caveats
 * that would crowd the page if they were always shown. A click or tap keeps it open until the next click elsewhere.
 */
export function noteMark(...content) {
  const id = `note-${++noteCount}`;
  const btn = h("button", { type: "button", class: "note-btn", "aria-label": "Details", "aria-expanded": "false",
    "aria-controls": id }, "i");
  const mark = h("span", { class: "note-mark" }, btn, h("span", { class: "note-pop", id }, content));
  let timer = 0;
  const show = () => { clearTimeout(timer); setNote(mark, true, mark.classList.contains("pinned")); };
  mark.addEventListener("mouseenter", show);
  mark.addEventListener("mouseleave", () => {
    if (!mark.classList.contains("pinned")) timer = setTimeout(() => setNote(mark, false), 250);
  });
  mark.addEventListener("focusin", show);
  mark.addEventListener("focusout", (e) => { if (!mark.contains(e.relatedTarget)) setNote(mark, false); });
  mark.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !mark.classList.contains("open")) return;
    e.stopPropagation();                      // close the note, not the profile behind it
    btn.focus();
    setNote(mark, false);
  });
  btn.addEventListener("click", () => setNote(mark, !mark.classList.contains("pinned"), true));
  return mark;
}

function setNote(mark, open, pinned = false) {
  mark.classList.toggle("open", open);
  mark.classList.toggle("pinned", open && pinned);
  mark.firstChild.setAttribute("aria-expanded", String(open));
  if (open) placeNote(mark);
}

/**
 * Keep an open note inside the panel it sits in, the drawer or the page beside it: each scrolls, so a note that
 * runs past its edge is cut off.
 */
function placeNote(mark) {
  const pop = mark.lastChild;
  pop.style.left = "";
  const holder = mark.closest(".drawer, .view");
  const left = (holder ? holder.getBoundingClientRect().left : 0) + 8;
  const right = left - 16 + (holder ? holder.clientWidth : document.documentElement.clientWidth);
  const r = pop.getBoundingClientRect();
  if (r.right > right) pop.style.left = `${Math.max(right - r.right, left - r.left)}px`;
}

document.addEventListener("click", (e) => {
  for (const m of document.querySelectorAll(".note-mark.open")) if (!m.contains(e.target)) setNote(m, false);
});

/** Text with <mark> around [start, end) highlight ranges. */
export function marked(text, ranges) {
  const frag = document.createDocumentFragment();
  let pos = 0;
  for (const [s, e] of ranges || []) {
    if (s > pos) frag.appendChild(document.createTextNode(text.slice(pos, s)));
    frag.appendChild(h("mark", null, text.slice(s, e)));
    pos = Math.max(pos, e);
  }
  if (pos < text.length) frag.appendChild(document.createTextNode(text.slice(pos)));
  return frag;
}

export function face(person, cls = "mini-face") {
  const url = safeUrl(person.photoUrl);
  const fallback = () => h("span", { class: cls + " fallback", "aria-hidden": "true" }, initials(person.name));
  if (!url) return fallback();
  const img = h("img", { class: cls, src: url, alt: "", loading: "lazy", referrerpolicy: "no-referrer" });
  img.addEventListener("error", () => img.replaceWith(fallback()), { once: true });
  return img;
}

export function fmtTs(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

export function toast(message, kind = "") {
  const root = document.getElementById("toast-root");
  const el = h("div", { class: "toast " + kind, role: kind === "error" ? "alert" : "status" }, message);
  root.appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 7000 : 3500);
}

/** Modal dialog. Returns {el, close}. `body` is a node; `actions` are buttons. */
export function modal({ title, lede, body, actions = [], onClose }) {
  const root = document.getElementById("modal-root");
  const prevFocus = document.activeElement;
  const titleId = "m-" + Math.random().toString(36).slice(2);
  const dialog = h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-labelledby": titleId },
    h("header", null, h("h2", { id: titleId }, title), lede ? h("p", null, lede) : null),
    h("div", { class: "mbody" }, body),
    h("footer", null, actions));
  const backdrop = h("div", { class: "modal-backdrop" }, dialog);
  function close() {
    backdrop.remove();
    document.removeEventListener("keydown", onKey, true);
    if (onClose) onClose();
    if (prevFocus && prevFocus.focus) prevFocus.focus();
  }
  function onKey(e) {
    if (e.key === "Escape") {
      e.stopPropagation();
      close();
    } else if (e.key === "Tab") {
      const f = dialog.querySelectorAll("button, input, select, textarea, a[href], [tabindex]:not([tabindex='-1'])");
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
      else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
    }
  }
  backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) close(); });
  document.addEventListener("keydown", onKey, true);
  root.appendChild(backdrop);
  const firstInput = dialog.querySelector("input, select, textarea") || dialog.querySelector("button");
  if (firstInput) firstInput.focus();
  return { el: dialog, close };
}

const FIELD_LABELS = {
  name: "Name", role: "Current position", institution: "Current institution", labName: "Lab", email: "Email",
  website: "Website", profileSourceUrl: "Profile page", profileCheckedAt: "Profile checked date",
  researchSummary: "Research summary", researchSourceUrl: "Research summary source", photoUrl: "Photo image URL",
  photoSourceUrl: "Photo source page", doctorateStatus: "Doctorate status", doctorateEvidence: "Doctorate evidence",
  doctorateSourceUrl: "Doctorate source", doctorateCheckedAt: "Doctorate checked on", date: "Date", context: "How you met",
  metBy: "Who met them", sourceUrl: "Source link", evidence: "Evidence", relationshipNarrative: "Narrative",
};

function readable(detail) {
  const m = /^([A-Za-z]+)(\.[^:]*)?:\s*(.*)$/.exec(detail);
  return m && FIELD_LABELS[m[1]] ? `${FIELD_LABELS[m[1]]}: ${m[3]}` : detail;
}

export function errorList(err) {
  const details = err && err.details;
  const items = (Array.isArray(details) ? details : []).map((d) => readable(String(d)));
  return h("div", { class: "form-errors", role: "alert" }, err.message || String(err),
    items.length ? h("ul", null, items.map((d) => h("li", null, d))) : null);
}

export function field(label, control, { hint, wide, id } = {}) {
  const target = control.input || control;
  const cid = id || target.id || "f-" + Math.random().toString(36).slice(2);
  target.id = cid;
  return h("div", { class: "field" + (wide ? " wide" : "") }, h("label", { for: cid }, label), control,
    hint ? h("span", { class: "hint" }, hint) : null);
}

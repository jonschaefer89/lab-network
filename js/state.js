// App state mirrored in the URL hash, so back/forward and reloads keep your place.
//   #/<view>?sel=<personId>&ego=<personId>&q=<query>&inst=<institution>&grp=<institution>&deg=2&...

export const VIEWS = ["map", "people", "affiliations", "search", "paths", "conferences", "notes", "data"];
export const DEFAULT_FILTERS = { deg: "2", doct: "confirmed", ident: "1", types: "", prov: "", status: "" };

export const state = {
  view: "map",
  sel: null,
  ego: null,
  q: "",
  from: "lab",
  to: "",
  mode: "any",
  tab: "",
  hl: "",
  inst: "",   // institution open on the Affiliations page
  grp: "",    // institution whose people are outlined on the map
  conf: "",   // conference open on the Conferences page
  filters: { ...DEFAULT_FILTERS },
};

const bus = new EventTarget();
export const on = (type, fn) => bus.addEventListener(type, (e) => fn(e.detail));
export const emit = (type, detail) => bus.dispatchEvent(new CustomEvent(type, { detail }));

/** Use the workspace's own defaults (a built network may not filter on doctorates). */
export function setDefaults(d) {
  if (!d) return;
  for (const k of ["deg", "doct", "ident"]) if (d[k] !== undefined) DEFAULT_FILTERS[k] = String(d[k]);
  Object.assign(state.filters, DEFAULT_FILTERS);
}

export function filterParams(extra = {}) {
  const f = state.filters;
  return {
    maxDegree: f.deg, doctorate: f.doct, identified: f.ident,
    types: f.types || undefined, provenance: f.prov || undefined, status: f.status || undefined, ...extra,
  };
}

export function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, query] = raw.split("?");
  const view = VIEWS.includes(path) ? path : "map";
  const p = new URLSearchParams(query || "");
  state.view = view;
  state.sel = p.get("sel") || null;
  state.ego = p.get("ego") || null;
  state.q = p.get("q") || "";
  state.from = p.get("from") || "lab";
  state.to = p.get("to") || "";
  state.mode = p.get("mode") === "directed" ? "directed" : "any";
  state.tab = p.get("tab") || "";
  state.hl = p.get("hl") || "";
  state.inst = p.get("inst") || "";
  state.grp = p.get("grp") || "";
  state.conf = p.get("conf") || "";
  for (const k of Object.keys(DEFAULT_FILTERS)) {
    state.filters[k] = p.has(k) ? p.get(k) : DEFAULT_FILTERS[k];
  }
}

export function hashFor(patch = {}) {
  const s = { ...state, filters: { ...state.filters, ...(patch.filters || {}) }, ...patch };
  delete s.filters_;
  const p = new URLSearchParams();
  if (s.sel) p.set("sel", s.sel);
  if (s.view === "map" && s.ego) p.set("ego", s.ego);
  if (s.q && (s.view === "search" || s.hl)) p.set("q", s.q);
  if (s.view === "map" && s.hl) p.set("hl", s.hl);
  if (s.view === "map" && s.grp && !s.hl) p.set("grp", s.grp);
  if ((s.view === "affiliations" || (s.view === "map" && s.grp)) && s.inst) p.set("inst", s.inst);
  if (s.view === "paths") {
    if (s.from && s.from !== "lab") p.set("from", s.from);
    if (s.to) p.set("to", s.to);
    if (s.mode === "directed") p.set("mode", "directed");
  }
  if (s.view === "data" && s.tab) p.set("tab", s.tab);
  if (s.view === "conferences" && s.conf) p.set("conf", s.conf);
  for (const [k, v] of Object.entries(s.filters)) {
    if (v !== DEFAULT_FILTERS[k]) p.set(k, v);
  }
  const q = p.toString();
  return `#/${s.view}${q ? "?" + q : ""}`;
}

export function go(patch = {}, { replace = false } = {}) {
  const target = hashFor(patch);
  if (target === location.hash) {
    emit("route");
    return;
  }
  if (replace) {
    history.replaceState(null, "", target);
    emit("route");
  } else {
    location.hash = target;
  }
}

// Boot, routing and shared app services.

import * as api from "./api.js";
import { emit, go, on, parseHash, setDefaults, state } from "./state.js";
import { append, h, toast } from "./ui.js";
import { DataView } from "./views/data.js";
import { contactForm, noteForm } from "./views/forms.js";
import { MapView } from "./views/mapview.js";
import { NotesView } from "./views/notes.js";
import { PathsView } from "./views/paths.js";
import { PeopleView } from "./views/people.js";
import { Drawer } from "./views/profile.js";
import { SearchView } from "./views/search.js";
import { AffiliationsView } from "./views/affiliations.js";
import { ConferencesView } from "./views/conferences.js";

const ACTOR_KEY = "labnetwork.actor";

function storageGet(k) {
  try { return localStorage.getItem(k); } catch { return null; }
}
function storageSet(k, v) {
  try { localStorage.setItem(k, v); } catch { /* private mode: keep it for this visit only */ }
}

const app = {
  boot: null,
  revision: 0,
  actor: "",
  _people: null,
  _names: new Map(),
  nameOf(id) {
    if (id === "lab") return "the lab";
    return this._names.get(id) || id;
  },
  async peopleIndex() {
    if (!this._people) {
      this._people = api.get("/api/people", { maxDegree: "all", doctorate: "any", identified: "0" }).then((d) => {
        for (const r of d.rows) this._names.set(r.id, r.name);
        return d.rows;
      });
    }
    return this._people;
  },
  async changed(message) {
    if (message) toast(message);
    this.revision += 1;
    this._people = null;
    try {
      this.boot = await api.get("/api/state");
    } catch { /* keep the old summary */ }
    await this.peopleIndex();
    updateStatus();
    emit("changed");
  },
};

function updateStatus() {
  const el = document.getElementById("status-line");
  const b = app.boot;
  if (!b) return;
  el.textContent = "";
  const c = b.counts;
  if (b.sharedCopy) {        // the lab's read-only copy: when it was made, and no change history to count
    append(el, [`Read-only copy as of ${b.sharedCopy.madeAt}. `,
      `${c.contacts.toLocaleString()} people, ${c.edges.toLocaleString()} relationship records, ${c.meetings} notes.`]);
    return;
  }
  append(el, [b.packet.ok ? "Packet verified. " : h("a", { href: "#/data?tab=health" }, "Packet check failed. "),
    `${c.contacts.toLocaleString()} people, ${c.edges.toLocaleString()} relationship records, ${c.meetings} notes. `,
    c.events ? `${c.events} saved change${c.events === 1 ? "" : "s"}. ` : "No changes yet. ",
    c.openConflicts ? h("a", { href: "#/data?tab=conflicts" }, `${c.openConflicts} open conflicts.`) : ""]);
}

async function init() {
  try {
    app.boot = await api.get("/api/state");
  } catch (err) {
    document.getElementById("main").append(h("p", { class: "notice notice-error" }, "Lab Network could not start: " + err.message));
    return;
  }
  const b = app.boot;
  if (b.sharedCopy) document.documentElement.classList.add("read-only");
  setDefaults(b.defaults);
  document.getElementById("lab-name").textContent = b.lab.name || "";
  document.title = b.lab.name ? `Lab Network: ${b.lab.name}` : "Lab Network";
  // Who is using the app: used as the author of notes and edits.
  const sel = document.getElementById("actor-select");
  const saved = storageGet(ACTOR_KEY) || storageGet("fieldnotes.actor");   // the app's earlier name
  append(sel, [h("option", { value: "" }, "Choose…"), b.lab.roots.map((r) => h("option", { value: r.name }, r.name)),
    h("option", { value: "__other" }, "Someone else")]);
  const known = b.lab.roots.some((r) => r.name === saved);
  if (saved && known) sel.value = saved;
  else if (saved) {
    sel.append(h("option", { value: saved }, saved));
    sel.value = saved;
  }
  app.actor = sel.value === "__other" ? "" : sel.value;
  api.setActor(app.actor);
  sel.addEventListener("change", () => {
    let v = sel.value;
    if (v === "__other") {
      v = (window.prompt("Your name, as it should appear on notes and edits:") || "").trim();
      if (v) {
        if (![...sel.options].some((o) => o.value === v)) sel.append(h("option", { value: v }, v));
        sel.value = v;
      } else sel.value = "";
    }
    app.actor = sel.value;
    api.setActor(app.actor);
    storageSet(ACTOR_KEY, app.actor);
  });

  await app.peopleIndex();
  updateStatus();

  const views = {
    map: new MapView(document.getElementById("view-map"), app),
    people: new PeopleView(document.getElementById("view-people"), app),
    affiliations: new AffiliationsView(document.getElementById("view-affiliations"), app),
    search: new SearchView(document.getElementById("view-search"), app),
    paths: new PathsView(document.getElementById("view-paths"), app),
    conferences: new ConferencesView(document.getElementById("view-conferences"), app),
    notes: new NotesView(document.getElementById("view-notes"), app),
    data: new DataView(document.getElementById("view-data"), app),
  };
  const drawer = new Drawer(document.getElementById("drawer"), app);
  window.labNetwork = { app, views, drawer, state };

  async function route() {
    parseHash();
    for (const [name, v] of Object.entries(views)) v.el.hidden = name !== state.view;
    document.querySelectorAll(".viewnav a").forEach((a) => {
      if (a.dataset.view === state.view) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    try {
      await Promise.all([views[state.view].show(), drawer.sync()]);
    } catch (err) {
      toast(err.message, "error");
    }
  }
  window.addEventListener("hashchange", route);
  on("route", route);

  document.getElementById("search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = document.getElementById("search-input").value.trim();
    // Search filters the map first; the map's search panel links to the detailed results.
    if (q) go({ view: "map", q, hl: "1", grp: "", ego: null, sel: null });
  });
  document.getElementById("btn-add-contact").addEventListener("click", () => contactForm(app));
  document.getElementById("btn-add-note").addEventListener("click", () => noteForm(app, { contactId: state.sel || "" }));
  document.querySelectorAll(".viewnav a").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    const view = a.dataset.view;
    go({ view, ego: view === "map" ? state.ego : null, tab: "" });
  }));
  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if (e.key === "/" && !typing) {
      e.preventDefault();
      document.getElementById("search-input").focus();
    } else if (e.key === "Escape" && !typing && state.sel && !document.querySelector(".modal-backdrop")) {
      go({ sel: null });
    }
  });
  if (!location.hash) history.replaceState(null, "", "#/map");
  await route();
}

init();

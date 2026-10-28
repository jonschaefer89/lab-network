// Map view: filter bar, canvas map, map key, zoom controls and breadcrumbs.

import * as api from "../api.js";
import { NetworkMap } from "../map.js";
import { emit, filterParams, go, on, state } from "../state.js";
import { append, DOCTORATE_TEXT, GROUP_LABEL, GROUP_OF, HIDDEN_TEXT, clear, degreeText, h, noteMark, ordinal } from "../ui.js";

const GROUP_TYPES = {
  supervision: ["mentor", "postdoc", "committee"],
  research: ["coauthor", "grant", "collaborator"],
  association: ["training", "labmate", "irta_colleague", "met", "event"],
};

export class MapView {
  constructor(el, app) {
    this.el = el;
    this.app = app;
    this.loadedKey = null;
    this.build();
    on("person-loaded", (p) => this.onPersonLoaded(p));
    on("changed", () => { this.loadedKey = null; if (state.view === "map") this.show(); });
  }

  build() {
    const types = this.app.boot.types;
    this.degBtns = {};
    // A workspace can cap the degrees shown at the depth its connections were collected to.
    const limit = this.app.boot.degreeLimit;
    const options = [["1", "1st degree"], ["2", "1st and 2nd"]].filter(([v]) => !limit || Number(v) <= limit);
    if (!limit) options.push(["all", "All"]);
    else if (limit >= 3) options.push([String(limit), `1st to ${ordinal(limit)}`]);
    const seg = h("div", { class: "segmented", role: "group", "aria-label": "Degrees shown",
      title: limit ? `This notebook collected connections to the ${ordinal(limit)} degree, so nothing further out is shown.` : null },
      options.map(([v, label]) => {
        const b = h("button", { type: "button", "aria-pressed": "false", onclick: () => this.setFilter({ deg: v }) }, label);
        this.degBtns[v] = b;
        return b;
      }));
    this.doctBox = h("input", { type: "checkbox", onchange: (e) => this.setFilter({ doct: e.target.checked ? "confirmed" : "any" }) });
    this.identBox = h("input", { type: "checkbox", onchange: (e) => this.setFilter({ ident: e.target.checked ? "1" : "0" }) });
    this.counts = h("span", { class: "counts", "aria-live": "polite" });
    this.tiesMenu = this.buildTiesMenu(types);
    const tools = h("div", { class: "maptools", id: "maptools" },
      h("div", { class: "group" }, h("span", { class: "label" }, "Show"), seg),
      h("div", { class: "group" },
        h("label", { class: "check", title: "Lab members are always shown" }, this.doctBox, "Doctorate confirmed"),
        h("label", { class: "check", title: "Lab members are always shown" }, this.identBox, "Current institution and position known")),
      this.tiesMenu,
      this.counts);

    this.canvas = h("canvas", { tabindex: "0", "aria-label": "Network map. Use the People view for a list version." });
    this.crumbs = h("div", { class: "crumbs", "aria-label": "Map location" });
    this.hint = h("div", { class: "maphint", hidden: true });
    this.searchBar = h("div", { class: "searchbar", hidden: true, role: "region", "aria-label": "Search results on the map", "aria-live": "polite" });
    this.keyPanel = this.buildKey();
    const ctl = h("div", { class: "mapctl" },
      h("button", { type: "button", title: "Zoom in", "aria-label": "Zoom in", onclick: () => this.map.zoomBy(1.3) }, "+"),
      h("button", { type: "button", title: "Zoom out", "aria-label": "Zoom out", onclick: () => this.map.zoomBy(1 / 1.3) }, "−"),
      h("button", { type: "button", title: "Fit to screen", "aria-label": "Fit map to screen", onclick: () => this.map.fit() }, "⤢"),
      h("button", { type: "button", class: "home", title: "Back to the lab map (H)", onclick: () => this.home() }, "Lab"));
    const stage = h("div", { class: "mapstage" }, this.canvas, this.crumbs, this.searchBar, this.hint, this.keyPanel, ctl);
    if (window.innerHeight < 980 || window.innerWidth < 1280) this.keyPanel.open = false;
    this.filtersToggle = h("button", { type: "button", class: "btn btn-small filters-toggle", "aria-controls": "maptools",
      "aria-expanded": "false", onclick: () => {
        const open = tools.classList.toggle("open");
        this.filtersToggle.setAttribute("aria-expanded", String(open));
        this.filtersToggle.textContent = open ? "Hide filters" : "Filters";
      } }, "Filters");
    this.el.append(h("div", { class: "filters-bar" }, this.filtersToggle), tools, stage);
    this.tooltip = document.getElementById("tooltip");
    this.map = new NetworkMap(this.canvas, {
      onSelect: (id) => go({ sel: id }),
      onBackground: () => { if (state.sel) go({ sel: null }); },
      onHover: (node, ev) => this.showTip(node, ev),
    });
    this.canvas.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && state.sel) go({ sel: null });
      if (e.key.toLowerCase() === "h") this.home();
    });
  }

  buildTiesMenu(types) {
    this.typeBoxes = {};
    const groups = Object.entries(GROUP_TYPES).map(([g, ts]) => h("fieldset", { class: "tiesgroup" },
      h("legend", null, GROUP_LABEL[g]),
      ts.map((t) => {
        const info = types.find((x) => x.type === t) || { label: t, count: 0 };
        const box = h("input", { type: "checkbox", onchange: () => this.applyTypeBoxes() });
        this.typeBoxes[t] = box;
        return h("label", { class: "check" }, box, `${info.label} (${info.count})`);
      })));
    this.provBoxes = {};
    const prov = h("fieldset", { class: "tiesgroup" }, h("legend", null, "Evidence"),
      [["public", "Public record"], ["reported", "Reported"], ["firsthand", "Firsthand note"]].map(([v, label]) => {
        const box = h("input", { type: "checkbox", onchange: () => this.applyTypeBoxes() });
        this.provBoxes[v] = box;
        return h("label", { class: "check" }, box, label);
      }));
    this.proposedBox = h("input", { type: "checkbox", onchange: () => this.applyTypeBoxes() });
    const status = h("fieldset", { class: "tiesgroup" }, h("legend", null, "Review status"),
      h("label", { class: "check" }, this.proposedBox, "Also count proposed records"));
    this.tiesSummary = h("span", null, "All relationship types");
    return h("details", { class: "ties-menu" },
      h("summary", { class: "btn btn-small" }, "Relationships: ", this.tiesSummary),
      h("div", { class: "ties-pop" },
        h("p", { class: "dim" }, "These choices decide which relationship records count as connections.",
          noteMark("Degrees and paths are recalculated from these choices.")),
        groups, prov, status,
        h("button", { type: "button", class: "btn btn-small", onclick: () => this.setFilter({ types: "", prov: "", status: "" }) },
          "Count every reviewed record")));
  }

  buildKey() {
    this.keyBtns = {};
    const row = (g, desc) => {
      const b = h("button", { type: "button", "aria-pressed": "true", onclick: () => this.toggleGroup(g) },
        h("span", { class: "swatch sw-" + g }), h("span", null, GROUP_LABEL[g], h("small", null, desc)));
      this.keyBtns[g] = b;
      return h("div", { class: "keyrow" }, b);
    };
    this.allTiesBox = h("input", { type: "checkbox", onchange: (e) => { this.map.showAllTies = e.target.checked; this.map.requestDraw(); } });
    return h("details", { class: "mapkey", open: true },
      h("summary", null, "Map key"),
      h("div", { class: "body" },
        h("div", null, h("h3", null, "Rings"),
          h("p", { class: "dim" }, "The lab is at the centre. Each ring out is one more step of recorded relationships.",
            noteMark("People sit in the sector of the lab member they are connected through."))),
        h("div", null, h("h3", null, "Line colour: relationship kind (click to count or ignore)", noteMark("When two " +
            "people have records of more than one kind, the line takes the first kind in this list. Their profile lists " +
            "every record.")),
          row("supervision", "Mentor, postdoc, committee"),
          row("research", "Coauthor, shared grant, collaborator"),
          row("association", "Training program, lab or center colleague, NIH IRTA colleague, met, same event")),
        h("div", null, h("h3", null, "Arrows"),
          h("p", { class: "dim" }, "On supervision lines, arrows point from the supervisor to the person supervised.",
            noteMark("The supervisor is the mentor, postdoc supervisor or committee member."))),
        h("div", null, h("h3", null, "Line style: evidence"),
          h("div", { class: "keyrow" }, h("span", { class: "styleline" }), h("span", null, "Public record")),
          h("div", { class: "keyrow" }, h("span", { class: "styleline dashed" }), h("span", null, "Reported (no public source)")),
          h("div", { class: "keyrow" }, h("span", { class: "styleline dotted" }), h("span", null, "Firsthand note only"))),
        h("div", null, h("h3", null, "People"),
          h("div", { class: "keyrow" }, h("span", { class: "nodekey" }), h("span", null, "Shown by the current filters")),
          h("div", { class: "keyrow" }, h("span", { class: "nodekey ghost" }),
            h("span", null, "Hidden by a filter", noteMark("On the lab map they appear only when they link someone " +
              "shown to the lab; on a person map they are that person's ties outside the current filters.")))),
        h("label", { class: "check" }, this.allTiesBox, "Draw every tie, not only the connecting lines")));
  }

  syncControls() {
    const f = state.filters;
    for (const [v, b] of Object.entries(this.degBtns)) b.setAttribute("aria-pressed", String(f.deg === v));
    this.doctBox.checked = f.doct === "confirmed";
    this.identBox.checked = f.ident !== "0";
    const active = f.types ? new Set(f.types.split(",")) : null;
    for (const [t, box] of Object.entries(this.typeBoxes)) box.checked = !active || active.has(t);
    const prov = f.prov ? new Set(f.prov.split(",")) : null;
    for (const [v, box] of Object.entries(this.provBoxes)) box.checked = !prov || prov.has(v);
    this.proposedBox.checked = (f.status || "").includes("proposed");
    for (const [g, b] of Object.entries(this.keyBtns)) {
      const on = GROUP_TYPES[g].some((t) => !active || active.has(t));
      b.setAttribute("aria-pressed", String(on));
    }
    const off = active ? Object.keys(this.typeBoxes).filter((t) => !active.has(t)).length : 0;
    this.tiesSummary.textContent = !active && !prov && !f.status ? "all reviewed records" :
      `${Object.keys(this.typeBoxes).length - off} of ${Object.keys(this.typeBoxes).length} types` +
      (prov ? ", some evidence excluded" : "") + (f.status ? ", proposed included" : "");
  }

  applyTypeBoxes() {
    const all = Object.keys(this.typeBoxes);
    const on = all.filter((t) => this.typeBoxes[t].checked);
    const provAll = Object.keys(this.provBoxes);
    const provOn = provAll.filter((v) => this.provBoxes[v].checked);
    this.setFilter({
      types: on.length === all.length ? "" : on.join(",") || "none",
      prov: provOn.length === provAll.length ? "" : provOn.join(",") || "none",
      status: this.proposedBox.checked ? "reviewed,proposed" : "",
    });
  }

  toggleGroup(g) {
    const active = state.filters.types ? new Set(state.filters.types.split(",")) : new Set(Object.keys(this.typeBoxes));
    const groupOn = GROUP_TYPES[g].some((t) => active.has(t));
    for (const t of GROUP_TYPES[g]) groupOn ? active.delete(t) : active.add(t);
    const all = Object.keys(this.typeBoxes);
    const list = all.filter((t) => active.has(t));
    this.setFilter({ types: list.length === all.length ? "" : list.join(",") || "none" });
  }

  setFilter(patch) {
    go({ filters: { ...state.filters, ...patch } }, { replace: true });
  }

  home() {
    go({ view: "map", ego: null, sel: null, hl: "", grp: "" });
    this.map.fit();
  }

  loadKey() {
    return JSON.stringify([state.ego, filterParams(), this.app.revision]);
  }

  async show() {
    this.syncControls();
    const k = this.loadKey();
    if (k !== this.loadedKey) {
      this.loadedKey = k;
      try {
        if (state.ego) {
          const data = await api.get(`/api/person/${encodeURIComponent(state.ego)}/neighborhood`, filterParams());
          if (k !== this.loadedKey) return;
          this.map.setEgo(data, { keepView: false });
          this.egoData = data;
          this.counts.textContent = `${data.nodes.filter((n) => n.role_in_view === "neighbor").length} direct ties` +
            (data.distance === null ? "; no recorded path to the lab" : "");
        } else {
          const data = await api.get("/api/network", filterParams());
          if (k !== this.loadedKey) return;
          this.network = data;
          this.map.setLab(data, { keepView: this.map.mode === "lab" && this.map.fitted });
          this.renderCounts(data.counts);
        }
      } catch (err) {
        this.loadedKey = null;
        this.hint.hidden = false;
        this.hint.textContent = "The map could not load: " + err.message;
        return;
      }
    }
    this.renderCrumbs();
    const empty = this.map.nodes.length === 0;
    this.hint.hidden = !empty;
    if (empty) this.hint.textContent = "No one matches these filters. Widen the degree range or turn off a people filter.";
    await this.applyHighlight();
    this.map.setSelection(state.sel, this.routesFor(state.sel));
    if (state.sel && this.lastPerson && this.lastPerson.id === state.sel && !this.map.byId.has(state.sel)) {
      this.hint.hidden = false;
    }
    if (state.ego && !state.sel) this.map.setSelection(state.ego, this.egoData ? this.egoData.routes : []);
  }

  renderCounts(c) {
    clear(this.counts);
    const deg = c.byDegree || {};
    const parts = [];
    if (deg["0"]) parts.push(`${deg["0"]} lab members`);
    const word = { 1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth" };
    for (const d of ["1", "2", "3", "4", "5"]) if (deg[d]) parts.push(`${deg[d]} ${word[d]}-degree`);
    if (deg.none) parts.push(`${deg.none} with no recorded path`);
    this.counts.append(`${c.shown} people shown (${parts.join(", ")}). `);
    if (c.degreeLimit && c.hiddenBy && c.hiddenBy.beyond) {
      this.counts.append(`${c.hiddenBy.beyond} people further out than ${ordinal(c.degreeLimit)} degree are left out.`,
        noteMark("Their connections were not collected, so their ring would be incomplete. They still appear in name " +
          "searches and in the relationship lists of the people they are tied to."), " ");
    }
    if (c.collection && c.collection.firstDegree) {
      const by = c.collection.byStatus || {};
      const other = Object.entries(by).filter(([k]) => k !== "collected")
        .map(([k, n]) => `${n} ${COLLECTION_TEXT[k] || k}`).join("; ");
      this.counts.append(`Connections collected for ${by.collected || 0} of ${c.collection.firstDegree} first-degree people.`);
      if (other) this.counts.append(noteMark(`Not collected: ${other}.`));
      this.counts.append(" ");
    }
    if (c.hiddenWithinDegree > 0) {
      const why = [];
      if (c.hiddenWithinBy.doctorate) why.push(`${c.hiddenWithinBy.doctorate} without a confirmed doctorate`);
      if (c.hiddenWithinBy.placement) why.push(`${c.hiddenWithinBy.placement} with no identified institution or position`);
      this.counts.append(`${c.hiddenWithinDegree} more in range are hidden by the people filters.`,
        noteMark(why.join("; ") + "." + (why.length > 1 ? " Some people are hidden for both reasons." : "")), " ",
        h("button", { type: "button", class: "btn btn-small btn-quiet", onclick: () => this.setFilter({ doct: "any", ident: "0" }) },
          "Show them"));
    }
  }

  renderCrumbs() {
    clear(this.crumbs);
    if (!state.ego) {
      append(this.crumbs, [h("span", { class: "current" }, "Lab map")]);
      return;
    }
    const name = this.app.nameOf(state.ego);
    append(this.crumbs, [
      h("button", { type: "button", onclick: () => go({ ego: null }) }, "Lab map"),
      h("span", { class: "sep", "aria-hidden": "true" }, "›"),
      h("span", { class: "current" }, name),
      state.sel && state.sel !== state.ego ? h("button", { type: "button", onclick: () => go({ ego: state.sel }) },
        `Centre on ${this.app.nameOf(state.sel)}`) : null]);
  }

  async applyHighlight() {
    if (state.grp && !state.hl) return this.applyGroup();
    if (!state.hl || !state.q) {
      this.map.setHighlight(null);
      this.searchBar.hidden = true;
      return;
    }
    const box = document.getElementById("search-input");
    if (box && document.activeElement !== box && box.value !== state.q) box.value = state.q;
    const key = JSON.stringify([state.q, filterParams(), this.app.revision]);
    let res = this.searchCache && this.searchCache.key === key ? this.searchCache.res : null;
    try {
      if (!res) {
        res = await api.get("/api/search", { q: state.q, ...filterParams() });
        this.searchCache = { key, res };
      }
    } catch (err) {
      this.map.setHighlight(null);
      this.renderSearchBar(null, err.message);
      return;
    }
    const ids = new Set(res.peopleSummary.map((p) => p.personId));
    this.map.setHighlight(ids);
    this.renderSearchBar(res, null, ids);
    // A search that matches exactly one person's name opens that person; other matches stay outlined.
    const named = res.people.filter((x) => x.field === "name");
    if (named.length === 1 && this.autoOpened !== key && !state.sel) {
      this.autoOpened = key;
      go({ sel: named[0].personId }, { replace: true });
    }
  }

  /** Outline one institution's people (from the Affiliations page). */
  async applyGroup() {
    let res;
    try {
      res = await api.get("/api/affiliations", { ...filterParams(), inst: state.grp });
    } catch (err) {
      this.map.setHighlight(null);
      this.renderSearchBar(null, err.message);
      return;
    }
    const ids = new Set(res.ids);
    this.map.setHighlight(ids);
    const onMap = [...ids].filter((id) => this.map.byId.has(id) && !this.map.byId.get(id).ghost).length;
    const bar = clear(this.searchBar);
    bar.hidden = false;
    bar.append(
      h("p", { class: "sb-main" }, h("strong", null, res.institution.name), `: ${ids.size} ${ids.size === 1 ? "person" : "people"} within two degrees.`),
      h("p", { class: "dim" }, onMap === ids.size ? "All of them are outlined on this map. Everyone else is faded." :
        `${onMap} are outlined on this map; ${ids.size - onMap} more are outside the degrees shown.`),
      h("p", { class: "dim" }, "Sharing this institution is not a relationship; the lines show recorded relationships only."),
      h("div", { class: "sb-actions" },
        h("button", { type: "button", class: "btn btn-small btn-primary", onclick: () => go({ view: "affiliations", inst: state.grp, grp: "" }) },
          "Back to Affiliations"),
        h("button", { type: "button", class: "btn btn-small btn-quiet", onclick: () => go({ grp: "" }) }, "Clear")));
  }

  renderSearchBar(res, error, ids) {
    const bar = clear(this.searchBar);
    bar.hidden = false;
    const close = h("button", { type: "button", class: "btn btn-small btn-quiet", onclick: () => {
      const box = document.getElementById("search-input");
      if (box) box.value = "";
      go({ hl: "", q: "" });
    } }, "Clear search");
    if (error) {
      append(bar, [h("p", null, "The search could not run: " + error), h("div", { class: "sb-actions" }, close)]);
      return;
    }
    const c = res.counts;
    const kinds = [[c.profiles, "research profile", "research profiles"], [c.papers, "paper", "papers"],
      [c.grants, "grant", "grants"], [c.notes, "note", "notes"], [c.people, "name or affiliation", "names or affiliations"]]
      .filter(([n]) => n).map(([n, one, many]) => `${n} ${n === 1 ? one : many}`);
    const n = ids.size;
    const onMap = [...ids].filter((id) => this.map.byId.has(id) && !this.map.byId.get(id).ghost).length;
    const detail = h("button", { type: "button", class: "btn btn-small btn-primary", onclick: () => go({ view: "search", q: state.q, sel: null }) },
      "Show detailed results");
    append(bar, [
      h("p", { class: "sb-main" }, h("strong", null, `“${res.query}”`),
        n ? `: ${n} ${n === 1 ? "person is" : "people are"} linked to ${kinds.join(", ")}.` : ": no matches in the notebook."),
      n ? h("p", { class: "dim" }, onMap === n ? "All of them are outlined on this map. Everyone else is faded." :
        `${onMap} are outlined on this map. ${n - onMap} more are hidden by the current filters.`) : null,
      res.aliasesUsed.length ? h("p", { class: "dim" },
        `Also searched ${res.aliasesUsed.length === 1 ? "“" + res.aliasesUsed[0] + "”" : res.aliasesUsed.length + " spelled-out forms"}.`,
        noteMark(res.aliasesUsed.length > 1 ? res.aliasesUsed.join("; ") + ". " : "", "Put the term in quotes to search it alone.")) : null,
      h("div", { class: "sb-actions" }, detail, close)]);
  }

  routesFor(id) {
    if (!id) return [];
    const p = this.lastPerson;
    if (p && p.id === id && p.connection && p.connection.paths) return p.connection.paths.map((x) => x.people);
    return [];
  }

  onPersonLoaded(p) {
    this.lastPerson = p;
    if (state.view === "map" && state.sel === p.id) {
      this.map.setSelection(p.id, this.routesFor(p.id));
      if (this.map.byId.has(p.id)) {
        const n = this.map.byId.get(p.id);
        const sx = n.x * this.map.k + this.map.tx, sy = n.y * this.map.k + this.map.ty;
        if (sx < 40 || sy < 40 || sx > this.map.width - 40 || sy > this.map.height - 40) this.map.centerOn(p.id);
      }
    }
  }

  showTip(node, ev) {
    const tip = this.tooltip;
    if (!node || !ev) {
      tip.hidden = true;
      return;
    }
    clear(tip);
    const role = [node.role, node.institution].filter(Boolean).join(", ");
    tip.append(h("strong", null, node.name), role ? h("div", { class: "muted" }, role) : h("div", { class: "muted" }, "Position not recorded"));
    tip.append(h("div", null, degreeText(node.degree)));
    if (node.ghost || (node.hiddenBy && node.hiddenBy.length)) {
      tip.append(h("div", { class: "muted" }, "Hidden by filters: " + node.hiddenBy.map((r) => HIDDEN_TEXT[r] || r).join("; ")));
    } else if (node.doctorate && node.doctorate !== "confirmed" && !node.isLab) {
      tip.append(h("div", { class: "muted" }, DOCTORATE_TEXT[node.doctorate]));
    }
    const l = this.map.mode === "lab" && node.parent ? this.map.linkByKey.get([node.id, node.parent].sort().join("|")) : null;
    if (l) tip.append(h("div", { class: "muted" }, `Connected through ${this.app.nameOf(node.parent)}: ` + l.types.map(typeLabel).join(", ")));
    tip.hidden = false;
    const x = Math.min(ev.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
    const y = Math.min(ev.clientY + 14, window.innerHeight - tip.offsetHeight - 8);
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }
}

const COLLECTION_TEXT = {
  "not-found": "not found in OpenAlex",
  ambiguous: "with several matching OpenAlex authors",
  "not-looked-up": "not looked up (no doctorate or research relationship recorded)",
  unanchored: "whose OpenAlex works connect to nothing recorded about them",
  stopped: "not reached before the spending limit",
  undone: "from an undone collection",
  none: "added after the last collection",
};

function typeLabel(t) {
  return {
    mentor: "mentor", postdoc: "postdoc supervision", committee: "committee", training: "training program",
    coauthor: "coauthors", grant: "shared grant", collaborator: "collaborators", labmate: "lab or center colleagues",
    irta_colleague: "NIH IRTA colleagues", met: "met", event: "same event",
  }[t] || t;
}

export { GROUP_TYPES, GROUP_OF };

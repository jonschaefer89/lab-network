// People view: the full list, with the same filters as the map. Also the accessible alternative to the map.

import * as api from "../api.js";
import { filterParams, go, on, state } from "../state.js";
import { DOCTORATE_TEXT, HIDDEN_TEXT, clear, debounce, degreeText, face, h, missing, noteMark, ordinal } from "../ui.js";

const PAGE = 100;

export class PeopleView {
  constructor(el, app) {
    this.el = el;
    this.app = app;
    this.rows = null;
    this.limit = PAGE;
    this.sort = "degree";
    this.text = "";
    this.includeHidden = false;
    on("changed", () => { this.rows = null; if (state.view === "people") this.show(); });
    this.build();
  }

  build() {
    this.filterInput = h("input", { type: "search", placeholder: "Filter by name, institution or position",
      "aria-label": "Filter the list", oninput: debounce((e) => { this.text = e.target.value; this.limit = PAGE; this.renderRows(); }, 150) });
    this.sortSel = h("select", { "aria-label": "Sort by", onchange: (e) => { this.sort = e.target.value; this.renderRows(); } },
      [["degree", "Closest to the lab"], ["name", "Name"], ["institution", "Institution"], ["papers", "Most papers"],
        ["grants", "Most grants"], ["notes", "Most notes"]].map(([v, l]) => h("option", { value: v }, l)));
    this.hiddenBox = h("input", { type: "checkbox", onchange: (e) => { this.includeHidden = e.target.checked; this.renderRows(); } });
    this.summary = h("p", { class: "dim", "aria-live": "polite" });
    this.tbody = h("tbody");
    this.more = h("div");
    this.el.append(h("div", { class: "page-inner" },
      h("h1", { class: "page-title" }, "People"),
      h("p", { class: "page-lede" }, "Everyone in the notebook, nearest to the lab first.", noteMark("The degree and people " +
        "filters match the map; change them on the map or include hidden people here.")),
      h("div", { class: "toolbar" }, this.filterInput, h("label", { class: "check" }, "Sort ", this.sortSel),
        h("label", { class: "check" }, this.hiddenBox, "Include people hidden by the filters")),
      this.summary,
      h("table", { class: "table" },
        h("thead", null, h("tr", null, ["Person", "Position and institution", "Degree", "Connected through", "Doctorate", "Records"]
          .map((t) => h("th", { scope: "col" }, t)))),
        this.tbody),
      this.more));
  }

  async show() {
    if (!this.rows || this.rowsKey !== JSON.stringify(filterParams())) {
      this.rowsKey = JSON.stringify(filterParams());
      clear(this.tbody).append(h("tr", null, h("td", { colspan: "6" }, "Loading…")));
      const data = await api.get("/api/people", filterParams());
      this.rows = data.rows;
    }
    this.renderRows();
  }

  renderRows() {
    if (!this.rows) return;
    const q = this.text.trim().toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "");
    const n = (s) => (s || "").toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "");
    const beyond = (r) => (r.hiddenBy || []).includes("beyond");
    let rows = this.rows.filter((r) => (this.includeHidden || r.shown) && !beyond(r) &&
      (!q || n(r.name).includes(q) || n(r.institution).includes(q) || n(r.role).includes(q) || r.id.includes(q)));
    const by = {
      degree: (a, b) => (a.degree ?? 99) - (b.degree ?? 99) || a.name.localeCompare(b.name),
      name: (a, b) => a.name.localeCompare(b.name),
      institution: (a, b) => (a.institution || "~").localeCompare(b.institution || "~") || a.name.localeCompare(b.name),
      papers: (a, b) => paperCount(b) - paperCount(a) || a.name.localeCompare(b.name),
      grants: (a, b) => grantCount(b) - grantCount(a) || a.name.localeCompare(b.name),
      notes: (a, b) => b.notes - a.notes || a.name.localeCompare(b.name),
    }[this.sort];
    rows = rows.sort(by);
    const hiddenCount = this.rows.filter((r) => !r.shown && !beyond(r)).length;
    const beyondCount = this.rows.filter(beyond).length;
    const limit = this.app.boot && this.app.boot.degreeLimit;
    const beyondShown = beyondCount && limit;
    clear(this.summary).append(`${rows.length} people listed` + (this.includeHidden ? "" : `; ${hiddenCount} hidden by the filters`) +
      (beyondShown ? `; ${beyondCount} further out than ${ordinal(limit)} degree are not listed` : "") + ".");
    if (beyondShown) this.summary.append(noteMark("Their connections were not collected."));
    clear(this.tbody);
    for (const r of rows.slice(0, this.limit)) this.tbody.append(this.row(r));
    clear(this.more);
    if (rows.length > this.limit) {
      this.more.append(h("button", { type: "button", class: "btn", onclick: () => { this.limit += PAGE; this.renderRows(); } },
        `Show ${Math.min(PAGE, rows.length - this.limit)} more`));
    }
  }

  row(r) {
    return h("tr", null,
      h("td", null, h("div", { class: "person-cell" }, face(r),
        h("div", null, h("button", { type: "button", class: "linklike", onclick: () => go({ sel: r.id }) }, r.name),
          r.origin === "workspace" ? h("div", { class: "dim" }, "Added in this workspace") : null,
          !r.shown ? h("div", { class: "dim" }, "Hidden: " + r.hiddenBy.map((x) => HIDDEN_TEXT[x] || x).join("; ")) : null))),
      h("td", null, r.role || missing("Position not recorded"), h("br"), r.institution ? h("span", { class: "dim" }, r.institution) : missing("Institution not recorded")),
      h("td", { class: "num" }, degreeText(r.degree)),
      h("td", null, r.degree === 0 ? h("span", { class: "dim" }, "Lab member") : r.via.length ? r.via.join(", ") + (r.viaCount > r.via.length ? ` and ${r.viaCount - r.via.length} more` : "") : h("span", { class: "dim" }, "No recorded path")),
      h("td", null, r.isLab ? h("span", { class: "dim" }, "Lab member") : DOCTORATE_TEXT[r.doctorate] || r.doctorate),
      h("td", { class: "num" }, r.works !== null && r.works !== undefined ?
        `${r.works} works in OpenAlex, ${grantCount(r)} awards and grants, ${r.notes} notes` :
        `${r.papers} papers, ${r.grants} grants, ${r.notes} notes`));
  }
}

// The research library's OpenAlex count when there is one; otherwise the papers recorded by hand.
function paperCount(r) {
  return r.works !== null && r.works !== undefined ? r.works : r.papers;
}

function grantCount(r) {
  return Math.max(r.grants || 0, r.awards || 0);
}

// Affiliations: people within two degrees grouped by institution, then department and lab.

import * as api from "../api.js";
import { filterParams, go, on, state } from "../state.js";
import { GROUP_LABEL, append, clear, debounce, degreeText, h, missing, noteMark } from "../ui.js";

const SORTS = [["people", "Most people"], ["ties", "Most relationships"], ["name", "Name"]];

export class AffiliationsView {
  constructor(el, app) {
    this.el = el;
    this.app = app;
    this.data = null;
    this.key = null;
    this.text = "";
    this.sort = "people";
    on("changed", () => { this.key = null; if (state.view === "affiliations") this.show(); });
    this.build();
  }

  build() {
    this.filterInput = h("input", { type: "search", placeholder: "Filter institutions", "aria-label": "Filter institutions",
      oninput: debounce((e) => { this.text = e.target.value; this.renderList(); }, 120) });
    this.sortSel = h("select", { "aria-label": "Sort institutions", onchange: (e) => { this.sort = e.target.value; this.renderList(); } },
      SORTS.map(([v, l]) => h("option", { value: v }, l)));
    this.summary = h("p", { class: "dim", "aria-live": "polite" });
    this.list = h("ul", { class: "aff-list", "aria-label": "Institutions" });
    this.detail = h("div", { class: "aff-detail" });
    this.el.append(h("div", { class: "page-inner page-wide" },
      h("h1", { class: "page-title" }, "Affiliations"),
      h("p", { class: "page-lede" }, "People within two degrees of the lab, grouped by institution, then department and lab. " +
        "Working at the same institution is not counted as a relationship."),
      h("div", { class: "toolbar" }, this.filterInput, h("label", { class: "check" }, "Sort ", this.sortSel)),
      this.summary,
      h("div", { class: "aff-grid" }, h("div", { class: "aff-col" }, this.list), this.detail)));
  }

  async show() {
    const key = JSON.stringify([filterParams(), this.app.revision]);
    if (key !== this.key) {
      this.key = key;
      clear(this.list).append(h("li", { class: "dim" }, "Loading…"));
      try {
        this.data = await api.get("/api/affiliations", filterParams());
      } catch (err) {
        clear(this.list).append(h("li", { class: "notice notice-error" }, err.message));
        return;
      }
      if (key !== this.key) return;
    }
    this.renderList();
    this.renderDetail();
  }

  sorted() {
    const n = (s) => (s || "").toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "");
    const q = n(this.text.trim());
    const rows = this.data.institutions.filter((i) => !q || n(i.name).includes(q));
    const by = {
      people: (a, b) => b.count - a.count || a.name.localeCompare(b.name),
      ties: (a, b) => (b.within.records + sum(b.connected)) - (a.within.records + sum(a.connected)) || b.count - a.count,
      name: (a, b) => a.name.localeCompare(b.name),
    }[this.sort];
    return rows.sort(by);
  }

  renderList() {
    if (!this.data) return;
    const rows = this.sorted();
    const d = this.data;
    clear(this.summary).append(`${d.counts.people} people at ${d.counts.institutions} institutions` +
      (d.noInstitution.count ? `; ${d.noInstitution.count} have no institution recorded` : "") + ".",
    noteMark("The people filters on the map apply here too."));
    clear(this.list);
    if (!state.inst && rows.length) state.inst = rows[0].key;
    const max = Math.max(1, ...d.institutions.map((i) => i.count));
    for (const inst of rows.slice(0, 300)) {
      const d1 = inst.byDegree["1"] || 0, d2 = inst.byDegree["2"] || 0, d0 = inst.byDegree["0"] || 0;
      // Widths go through the style property: the page's content security policy blocks style attributes.
      const near = h("span", { class: "seg seg-0" }), far = h("span", { class: "seg seg-2" });
      near.style.width = `${(100 * (d0 + d1)) / max}%`;
      far.style.width = `${(100 * d2) / max}%`;
      const bar = h("span", { class: "aff-bar", "aria-hidden": "true", title: "Dark: lab and first degree. Light: second degree." },
        near, far);
      const btn = h("button", { type: "button", class: "aff-row", "aria-pressed": String(inst.key === state.inst),
        onclick: () => go({ inst: inst.key }, { replace: true }) },
        h("span", { class: "aff-name" }, inst.name),
        h("span", { class: "aff-count" }, String(inst.count)),
        bar,
        h("span", { class: "aff-sub dim" }, degreeParts(inst.byDegree)));
      this.list.append(h("li", null, btn));
    }
    if (rows.length > 300) this.list.append(h("li", { class: "dim" }, `${rows.length - 300} more; filter to find them.`));
    if (!rows.length) this.list.append(h("li", { class: "dim" }, "No institution matches that filter."));
    if (d.noInstitution.count) {
      this.list.append(h("li", null, h("button", { type: "button", class: "aff-row", "aria-pressed": String(state.inst === "__none"),
        onclick: () => go({ inst: "__none" }, { replace: true }) },
        h("span", { class: "aff-name" }, missing("Institution not recorded")),
        h("span", { class: "aff-count" }, String(d.noInstitution.count)))));
    }
  }

  renderDetail() {
    const box = clear(this.detail);
    if (!this.data) return;
    const P = this.data.people;
    if (state.inst === "__none") {
      box.append(h("h2", null, "Institution not recorded", noteMark("Add an institution on a person's page to place them.")),
        personList(this.data.noInstitution.people, P));
      return;
    }
    const inst = this.data.institutions.find((i) => i.key === state.inst) || this.data.institutions[0];
    if (!inst) {
      box.append(h("p", { class: "dim" }, "No one within two degrees matches the current filters."));
      return;
    }
    const roots = (this.app.boot.lab.roots || []);
    const nameOf = (id) => (roots.find((r) => r.id === id) || {}).name || this.app.nameOf(id);
    // The append helper skips empty parts; the DOM's own append would print "null".
    append(box, [
      h("h2", { class: "aff-title" }, inst.name),
      h("p", null, `${inst.count} ${inst.count === 1 ? "person" : "people"}: ${degreeParts(inst.byDegree)}.`),
      h("div", { class: "aff-actions" },
        h("button", { type: "button", class: "btn btn-small btn-primary",
          onclick: () => go({ view: "map", grp: inst.key, inst: inst.key, hl: "", q: "", ego: null, sel: null }) },
        "Show them on the map")),
      inst.via.length ? h("p", null, h("strong", null, "Connected through: "),
        inst.via.map((v, i) => [i ? ", " : "", `${nameOf(v.id)} (${v.count})`]),
        noteMark("A person reached through several lab members counts for each of them.")) : null,
      withinLine(inst)]);
    box.append(h("h3", { class: "aff-h" }, "Departments and labs (inferred)", noteMark("Departments are read from position " +
      "titles and labs from lab records and rosters, so both are inferences to check.")));
    if (!inst.departments.length) box.append(h("p", { class: "dim" }, "No department or lab could be read for anyone here."));
    inst.departments.forEach((d, i) => box.append(deptBlock(d, P, i < 3)));
    if (inst.unplaced.length) {
      box.append(h("details", { class: "aff-dept" },
        h("summary", null, h("span", { class: "aff-dept-name" }, "No department or lab recorded"), ` (${inst.unplaced.length})`),
        personList(inst.unplaced, P)));
    }
    box.append(h("h3", { class: "aff-h" }, "Connected institutions", noteMark("Relationship records between people here " +
      "and people at each institution. Records, not shared affiliations.")));
    if (!inst.connected.length) {
      box.append(h("p", { class: "dim" }, "No relationship records link people here to people at other institutions."));
    } else {
      const ul = h("ul", { class: "aff-conn" });
      for (const c of inst.connected) {
        ul.append(h("li", null, h("button", { type: "button", class: "linklike", onclick: () => go({ inst: c.key }, { replace: true }) }, c.name),
          ` ${c.records} ${c.records === 1 ? "record" : "records"}`, h("span", { class: "dim" }, groupParts(c.byGroup))));
      }
      box.append(ul);
      if (inst.connectedCount > inst.connected.length) {
        box.append(h("p", { class: "dim" }, `${inst.connectedCount - inst.connected.length} more institutions have fewer records.`));
      }
    }
  }
}

function withinLine(inst) {
  const w = inst.within;
  if (!w.records) return h("p", { class: "dim" }, "No relationship records between people here.");
  return h("p", null, `${w.records} relationship ${w.records === 1 ? "record" : "records"} among ${w.people} of them`,
    h("span", { class: "dim" }, groupParts(w.byGroup)), ".");
}

function deptBlock(d, P, open) {
  const block = h("details", { class: "aff-dept", open: open || null },
    h("summary", null, h("span", { class: "aff-dept-name" }, d.name), ` (${d.count})`));
  for (const lab of d.labs) {
    block.append(h("div", { class: "aff-lab" },
      h("h4", null, lab.name, ` (${lab.count})`, noteMark("Lab membership: " + lab.basis + ".")),
      personList(lab.people, P)));
  }
  if (d.people.length) {
    block.append(h("div", { class: "aff-lab" }, d.labs.length ? h("h4", null, `No lab recorded (${d.people.length})`) : null,
      personList(d.people, P)));
  }
  return block;
}

function personList(ids, P) {
  const ul = h("ul", { class: "aff-people" });
  const render = (id) => {
    const p = P[id];
    if (!p) return;
    ul.append(h("li", null,
      h("button", { type: "button", class: "linklike aff-person", onclick: () => go({ sel: id }) }, p.name),
      h("span", { class: "aff-deg" }, degreeText(p.degree)),
      h("span", { class: "dim aff-role" }, p.role || "Position not recorded"),
      p.formerLabs && p.formerLabs.length ? h("span", { class: "dim aff-role" }, "Formerly in " + p.formerLabs.join(", ")) : null));
  };
  ids.slice(0, 40).forEach(render);
  if (ids.length > 40) {
    const more = h("button", { type: "button", class: "btn btn-small btn-quiet" }, `Show ${ids.length - 40} more`);
    more.addEventListener("click", () => { ids.slice(40).forEach(render); more.remove(); });
    return h("div", null, ul, more);
  }
  return ul;
}

function degreeParts(byDegree) {
  const parts = [];
  if (byDegree["0"]) parts.push(`${byDegree["0"]} in the lab`);
  if (byDegree["1"]) parts.push(`${byDegree["1"]} first-degree`);
  if (byDegree["2"]) parts.push(`${byDegree["2"]} second-degree`);
  return parts.join(", ");
}

function groupParts(byGroup) {
  const parts = ["supervision", "research", "association"].filter((g) => byGroup[g])
    .map((g) => `${GROUP_LABEL[g].toLowerCase()} ${byGroup[g]}`);
  return parts.length ? ` (${parts.join(", ")})` : "";
}

function sum(conn) {
  return conn.reduce((a, c) => a + c.records, 0);
}

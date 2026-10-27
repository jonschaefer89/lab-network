// Conferences view: the lab's conference list, and for each conference who in the network is on its programs.

import * as api from "../api.js";
import { filterParams, go, on, state } from "../state.js";
import { chip, clear, debounce, degreeText, extLink, face, h, modal, noteMark, ordinal, toast } from "../ui.js";

const PAGE = 100;

export class ConferencesView {
  constructor(el, app) {
    this.el = el;
    this.app = app;
    this.key = null;
    this.text = "";
    this.minYears = 1;
    this.year = "";
    this.sort = "years";
    this.coauthors = false;
    this.limit = PAGE;
    on("changed", () => { this.key = null; if (state.view === "conferences") this.show(); });
  }

  async show() {
    const key = JSON.stringify([state.conf, filterParams()]);
    if (key === this.key) return;
    this.key = key;
    clear(this.el);
    const inner = h("div", { class: "page-inner" });
    this.el.append(inner);
    inner.append(h("h1", { class: "page-title" }, "Conferences"),
      h("p", { class: "page-lede" }, "Who in your network is on the programs of the conferences your lab follows.",
        noteMark(h("span", { class: "note-line" }, "A program lists who presents, chairs or discusses a session, not " +
          "everyone who attends, so someone missing from a program may still have been there."),
        h("span", { class: "note-line" }, "Names on a program are matched to people in the network by name, and by " +
          "institution when the program gives one. A name several people share is left unmatched."))));
    this.listBox = h("div");
    this.detail = h("div");
    inner.append(this.listBox, this.detail);
    let list;
    try {
      list = await api.get("/api/conferences");
    } catch (err) {
      this.listBox.append(h("p", { class: "notice notice-error" }, err.message));
      return;
    }
    if (key !== this.key) return;
    this.renderList(list);
    const sel = state.conf || (list.series.find((x) => x.programs.length) || {}).id;
    if (sel && list.series.some((x) => x.id === sel)) await this.showSeries(sel, key);
  }

  renderList(list) {
    clear(this.listBox);
    const panel = h("section", { class: "panel" },
      h("h2", null, "Your conferences", noteMark("The conferences the lab attends or is considering. Only these are " +
        "listed here, searched, and shown on people's pages. Taking one off the list keeps its imported programs, so " +
        "adding it again brings them back.")));
    if (!list.series.length) {
      panel.append(h("p", null, "No conferences on the list yet. Add the ones your lab attends or is thinking of " +
        "attending, such as SRP or AACAP."));
    } else {
      const ul = h("ul", { class: "conf-list" });
      for (const s of list.series) {
        const years = s.programs.map((p) => p.year).sort();
        ul.append(h("li", { class: "conf-item" + (s.id === (state.conf || "") ? " selected" : "") },
          h("div", { class: "conf-main" },
            h("button", { type: "button", class: "linklike conf-name", onclick: () => go({ view: "conferences", conf: s.id }) }, s.short),
            s.name && s.name !== s.short ? h("span", { class: "dim" }, ` ${s.name}`) : null,
            s.url ? [" · ", extLink(s.url, "website")] : null),
          h("div", { class: "dim" }, years.length ? `Programs imported: ${years.join(", ")}. ` +
            `${s.inNetwork} ${s.inNetwork === 1 ? "person" : "people"} in your network on them.` : "No programs imported yet.",
          years.length ? null : noteMark("An assistant can build a program file from the conference's public program " +
            "and import it with the conference command; see AGENTS.md.")),
          h("button", { type: "button", class: "btn btn-small btn-quiet needs-write", onclick: () => this.confirmRemove(s) }, "Remove")));
      }
      panel.append(ul);
    }
    if (list.offList) {
      panel.append(h("p", { class: "dim" }, `${list.offList} imported ${list.offList === 1 ? "program belongs" : "programs belong"} ` +
        "to conferences that are not on the list."));
    }
    panel.append(this.addForm());
    this.listBox.append(panel);
  }

  addForm() {
    const short = h("input", { type: "text", placeholder: "Short name, such as SRP", "aria-label": "Short name", required: true });
    const name = h("input", { type: "text", placeholder: "Full name (optional)", "aria-label": "Full name" });
    const url = h("input", { type: "url", placeholder: "Website (optional)", "aria-label": "Website" });
    const add = async (e) => {
      e.preventDefault();
      if (!short.value.trim()) { short.focus(); return; }
      try {
        const r = await api.send("POST", "/api/conferences/series", { short: short.value, name: name.value, url: url.value });
        toast(`${r.short} is on the list.`);
        this.key = null;
        go({ view: "conferences", conf: r.id });
        this.show();
      } catch (err) {
        toast(err.message, "error");
      }
    };
    return h("form", { class: "toolbar conf-add needs-write", onsubmit: add }, short, name, url,
      h("button", { type: "submit", class: "btn" }, "Add to the list"));
  }

  confirmRemove(s) {
    const m = modal({
      title: `Take ${s.short} off the list?`,
      lede: s.programs.length ? `Its ${s.programs.length} imported ${s.programs.length === 1 ? "program is" : "programs are"} ` +
        "kept, and come back if you add it again." : "It has no imported programs.",
      body: null,
      actions: [h("button", { type: "button", class: "btn", onclick: () => m.close() }, "Cancel"),
        h("button", { type: "button", class: "btn btn-primary", onclick: async () => {
          try {
            await api.send("POST", `/api/conferences/series/${encodeURIComponent(s.id)}/remove`, {});
            m.close();
            toast(`${s.short} is off the list.`);
            this.key = null;
            if (state.conf === s.id) go({ view: "conferences", conf: "" });
            else this.show();
          } catch (err) {
            toast(err.message, "error");
          }
        } }, "Take it off")],
    });
  }

  // ------------------------------------------------------------------ one conference

  async showSeries(sid, key) {
    clear(this.detail).append(h("p", { class: "dim" }, "Loading…"));
    let b;
    try {
      b = await api.get("/api/conferences", { series: sid, ...filterParams() });
    } catch (err) {
      clear(this.detail).append(h("p", { class: "notice notice-error" }, err.message));
      return;
    }
    if (key !== this.key) return;
    this.b = b;
    this.limit = PAGE;
    clear(this.detail);
    const sec = h("section", { class: "panel" });
    this.detail.append(sec);
    sec.append(h("h2", null, `${b.short}: who is on the program`));
    if (!b.programs.length) {
      sec.append(h("p", null, "No programs imported for this conference yet."));
      return;
    }
    sec.append(h("div", { class: "conf-years" }, b.programs.map((p) => h("span", { class: "conf-year" },
      h("strong", null, String(p.year)), ` ${p.names.toLocaleString()} names, ${p.inNetwork} in your network`,
      p.ambiguous ? ` (${p.ambiguous} unclear)` : "",
      noteMark([p.name, p.location, p.dates].filter(Boolean).join(", ") + ".",
        p.url ? [" Program: ", extLink(p.url)] : "", p.note ? ` ${p.note}` : "")))));
    const presenting = b.people.filter((p) => p.presentingYears.length);
    const regulars = presenting.filter((p) => p.presentingYears.length >= 2).length;
    const coOnly = b.people.length - presenting.length;
    sec.append(h("p", null, `${presenting.length} ${presenting.length === 1 ? "person" : "people"} in your network ` +
      `presented, chaired or discussed at ${b.short}` + (b.years.length > 1 ? `; ${regulars} in two or more years.` : ".") +
      (coOnly ? ` ${coOnly} more ${coOnly === 1 ? "is" : "are"} listed only as a co-author.` : ""),
      b.beyondLimit && b.beyondLimit.people ? noteMark(`${b.beyondLimit.people} more on these programs ` +
        `${b.beyondLimit.people === 1 ? "is" : "are"} further out than the ${ordinal(b.beyondLimit.limit)} ` +
        "degree, whose connections were not collected, and are left out.") : null));
    // Filters
    const text = h("input", { type: "search", placeholder: "Filter by name, institution or talk", "aria-label": "Filter",
      value: this.text, oninput: debounce((e) => { this.text = e.target.value; this.limit = PAGE; this.renderRows(); }, 150) });
    const yearSel = h("select", { "aria-label": "Year", onchange: (e) => { this.year = e.target.value; this.renderRows(); } },
      h("option", { value: "" }, "All years"), b.years.slice().reverse().map((y) => h("option", { value: String(y) }, String(y))));
    yearSel.value = b.years.map(String).includes(this.year) ? this.year : "";
    this.year = yearSel.value;
    const minSel = h("select", { "aria-label": "Years on the program", onchange: (e) => { this.minYears = Number(e.target.value); this.renderRows(); } },
      [1, 2, 3, 4, 5].filter((n) => n <= Math.max(1, b.years.length)).map((n) =>
        h("option", { value: String(n) }, n === 1 ? "Any number of years" : `${n} or more years`)));
    minSel.value = String(Math.min(this.minYears, Math.max(1, b.years.length)));
    const sortSel = h("select", { "aria-label": "Order", onchange: (e) => { this.sort = e.target.value; this.renderRows(); } },
      [["years", "Most years first"], ["closest", "Closest to the lab first"], ["name", "Name"]].map(([v, l]) => h("option", { value: v }, l)));
    sortSel.value = this.sort;
    const topic = h("input", { type: "search", class: "conf-topic", placeholder: "Topic, such as ADHD", "aria-label": "Topic" });
    const coBox = h("input", { type: "checkbox", checked: this.coauthors || null,
      onchange: (e) => { this.coauthors = e.target.checked; this.renderRows(); } });
    const topicGo = (e) => {
      e.preventDefault();
      if (topic.value.trim()) go({ view: "search", q: `${topic.value.trim()} AND ${b.short}` });
    };
    sec.append(h("div", { class: "toolbar" }, text, h("label", { class: "check" }, "Year ", yearSel),
      h("label", { class: "check" }, "On ", minSel), h("label", { class: "check" }, "Order ", sortSel),
      h("label", { class: "check" }, coBox, " Include co-authors", noteMark("Abstracts list every co-author, whether " +
        "or not they came to the meeting. Without this, a year counts only when the person presented, chaired, " +
        "discussed, moderated or had a poster."))),
    h("form", { class: "toolbar", onsubmit: topicGo }, h("label", { class: "check" }, `Who on ${b.short} programs works on `, topic),
      h("button", { type: "submit", class: "btn btn-small" }, "Search"),
      noteMark(`Searches “topic AND ${b.short}”: people with matching research who are also on a ${b.short} program.`)));
    this.summary = h("p", { class: "dim", "aria-live": "polite" });
    this.tbody = h("tbody");
    this.more = h("div");
    sec.append(this.summary, h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", null, h("tr", null, ["Person", "Degree", "Years", "Roles", "Most recent talk", "Also on"]
        .map((t) => h("th", { scope: "col" }, t)))), this.tbody)), this.more);
    this.renderRows();
    if (b.outside.length) this.detail.append(this.outsidePanel(b));
  }

  renderRows() {
    const b = this.b;
    const fold = (s) => (s || "").toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "");
    const q = fold(this.text.trim());
    const yearsOf = (p) => (this.coauthors ? p.years : p.presentingYears);
    let rows = b.people.filter((p) => yearsOf(p).length >= Math.max(1, this.minYears) &&
      (!this.year || yearsOf(p).includes(Number(this.year))));
    if (q) rows = rows.filter((p) => fold([p.name, p.institution, p.role, p.latest && p.latest.title].join(" ")).includes(q));
    const deg = (p) => p.degree ?? 99;
    if (this.sort === "closest") rows.sort((a, c) => deg(a) - deg(c) || c.years.length - a.years.length || a.name.localeCompare(c.name));
    else if (this.sort === "name") rows.sort((a, c) => a.name.localeCompare(c.name));
    else rows.sort((a, c) => yearsOf(c).length - yearsOf(a).length || c.appearances - a.appearances || deg(a) - deg(c));
    this.summary.textContent = `${rows.length.toLocaleString()} ${rows.length === 1 ? "person" : "people"} shown.`;
    clear(this.tbody);
    for (const p of rows.slice(0, this.limit)) {
      const roles = Object.entries(p.roles).sort((a, c) => c[1] - a[1]).map(([r, n]) => (n > 1 ? `${r} ×${n}` : r)).join(", ");
      this.tbody.append(h("tr", { class: p.shown ? "" : "outside" },
        h("td", null, h("div", { class: "cell-person" }, face(p, "mini-face"),
          h("div", null, h("button", { type: "button", class: "linklike", onclick: () => go({ sel: p.id }) }, p.name),
            h("div", { class: "dim" }, [p.role, p.institution].filter(Boolean).join(", ") || "Position not recorded")))),
        h("td", null, chip(degreeText(p.degree), p.degree === 0 ? "chip-strong" : ""),
          p.via && p.via.length && p.degree > 0 ? h("div", { class: "dim" }, "through " + p.via.slice(0, 2).join(", ")) : null),
        h("td", null, this.b.years.map((y) => {
          const on = p.presentingYears.includes(y) ? "on" : p.years.includes(y) ? "co" : "";
          return h("span", { class: "conf-dot " + on, title: `${y}: ${on === "on" ? "presenting, chairing or discussing" :
            on === "co" ? "co-author only" : "not on the program"}` }, String(y).slice(2));
        })),
        h("td", null, roles),
        h("td", null, p.latest ? [p.latest.sourceUrl ? extLink(p.latest.sourceUrl, p.latest.title) : p.latest.title,
          h("span", { class: "dim" }, ` (${p.latest.year})`)] : h("span", { class: "dim" }, "No title on the program")),
        h("td", null, p.otherConferences.length ? p.otherConferences.join(", ") : h("span", { class: "dim" }, "None"))));
    }
    clear(this.more);
    if (rows.length > this.limit) {
      this.more.append(h("button", { type: "button", class: "btn", onclick: () => { this.limit += PAGE; this.renderRows(); } },
        `Show ${Math.min(PAGE, rows.length - this.limit)} more`));
    }
  }

  outsidePanel(b) {
    const sec = h("section", { class: "panel" },
      h("h2", null, `On ${b.short} programs, not in your network`, noteMark("Names on two or more years' programs, or with " +
        "three or more appearances, that match no one in the network. Look one up to see who in the network has " +
        "published with them.")),
      h("p", { class: "dim" }, `${b.outside.length} of ${b.outsideCount.toLocaleString()} names not in the network, most years first.`));
    const tbody = h("tbody");
    const render = (list) => list.forEach((p) => tbody.append(h("tr", null,
      h("td", null, p.name, p.ambiguous ? h("div", { class: "dim" }, "Shares a name with someone in the network") : null),
      h("td", null, p.institutions.slice(0, 2).join("; ") || h("span", { class: "dim" }, "Not printed")),
      h("td", null, p.presentingYears.join(", ")),
      h("td", null, String(p.appearances)),
      h("td", null, h("button", { type: "button", class: "btn btn-small", onclick: () => lookUp(this.app, p.name) }, "Look up")))));
    render(b.outside.slice(0, 25));
    sec.append(h("table", { class: "table" }, h("thead", null, h("tr", null,
      ["Name as printed", "Institution", "Years", "Appearances", ""].map((t) => h("th", { scope: "col" }, t)))), tbody));
    if (b.outside.length > 25) {
      const more = h("button", { type: "button", class: "btn btn-small btn-quiet" }, `Show ${b.outside.length - 25} more`);
      more.addEventListener("click", () => { render(b.outside.slice(25)); more.remove(); });
      sec.append(more);
    }
    return sec;
  }
}

/** Open the Paths page and look the name up in OpenAlex there. */
function lookUp(app, name) {
  go({ view: "paths", sel: null });
  setTimeout(() => window.labNetwork.views.paths.lookup(name), 0);
}

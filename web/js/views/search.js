// Search view: matches grouped by person (default), or by kind of record (profile, grant, paper, note, name).

import * as api from "../api.js";
import { filterParams, go, on, state } from "../state.js";
import { chip, clear, degreeText, extLink, face, h, marked, noteMark, ordinal } from "../ui.js";

const GROUP_KEY = "labnetwork.searchGroup";
const LAB_KEY = "labnetwork.searchLab";
const PAGE = 20;       // people shown before "Show more people"
const PER_KIND = 3;    // grants or papers shown per person before "Show more"

function load(key, fallback) {
  try {
    return localStorage.getItem(key) || fallback;
  } catch {
    return fallback;
  }
}

function save(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch { /* storage can be blocked; the choice then lasts for this visit */ }
}

export class SearchView {
  constructor(el, app) {
    this.el = el;
    this.app = app;
    this.key = null;
    this.result = null;
    this.group = load(GROUP_KEY, "person") === "kind" ? "kind" : "person";
    this.sort = "closest";
    this.showLab = load(LAB_KEY, "0") === "1";
    on("changed", () => { this.key = null; if (state.view === "search") this.show(); });
  }

  async show() {
    const key = JSON.stringify([state.q, filterParams()]);
    if (key === this.key) return;
    this.key = key;
    const box = document.getElementById("search-input");
    if (box && box.value !== state.q) box.value = state.q;
    clear(this.el);
    const inner = h("div", { class: "page-inner" });
    this.el.append(inner);
    if (!state.q) {
      inner.append(h("h1", { class: "page-title" }, "Search"),
        h("p", { class: "page-lede" }, "Type a name or a research topic in the search box above.", noteMark("Results list " +
          "each person with their matching research profile, grants, papers and notes.")));
      return;
    }
    inner.append(h("p", { class: "dim" }, "Searching…"));
    let r;
    try {
      [r] = await Promise.all([api.get("/api/search", { q: state.q, ...filterParams() }), this.loadIndex()]);
    } catch (err) {
      clear(inner).append(h("p", { class: "notice notice-error" }, err.message));
      return;
    }
    if (key !== this.key) return;
    this.result = r;
    clear(inner).append(this.header(r));
    const c = r.counts;
    if (!(c.profiles + c.papers + c.grants + c.notes + c.people + (c.events || 0))) {
      inner.append(h("div", { class: "panel" }, h("h2", null, "No matches"),
        h("p", null, `Nothing in the notebook mentions “${r.query}”.`, noteMark(`The search covers ${scopeShort}. ` +
          "No match here does not mean no one in your network works on it; profiles record only part of each person's work."))));
      return;
    }
    this.body = h("div");
    inner.append(this.body);
    this.renderBody();
  }

  /** Who connects each person to the lab, from the people list the app already holds. */
  async loadIndex() {
    const rows = await this.app.peopleIndex();
    this.index = new Map(rows.map((row) => [row.id, row]));
  }

  renderBody() {
    clear(this.body);
    for (const [v, b] of Object.entries(this.groupBtns)) b.setAttribute("aria-pressed", String(this.group === v));
    this.sortLabel.hidden = this.group !== "person";
    this.labLabel.hidden = this.group !== "person";
    if (this.group === "person") this.renderPeople(this.result);
    else this.renderKinds(this.result);
  }

  header(r) {
    const c = r.counts;
    const n = r.peopleSummary.length;
    const kinds = [[c.profiles, "research profile", "research profiles"], [c.grants, "grant", "grants"],
      [c.papers, "paper", "papers"], [c.notes, "note", "notes"], [c.events || 0, "conference appearance", "conference appearances"],
      [c.people, "name or affiliation", "names or affiliations"]]
      .filter(([k]) => k).map(([k, one, many]) => `${k} ${k === 1 ? one : many}`);
    this.groupBtns = {};
    const seg = h("div", { class: "segmented", role: "group", "aria-label": "Group results" },
      [["person", "By person"], ["kind", "By kind of record"]].map(([v, label]) => {
        const b = h("button", { type: "button", "aria-pressed": "false", onclick: () => {
          this.group = v;
          save(GROUP_KEY, v);
          this.renderBody();
        } }, label);
        this.groupBtns[v] = b;
        return b;
      }));
    const sortSel = h("select", { "aria-label": "Order people", onchange: (e) => { this.sort = e.target.value; this.renderBody(); } },
      [["closest", "Closest to the lab first"], ["most", "Most matches first"]]
        .map(([v, label]) => h("option", { value: v, selected: this.sort === v || null }, label)));
    this.sortLabel = h("label", { class: "check" }, "Order ", sortSel);
    const labBox = h("input", { type: "checkbox", checked: this.showLab || null, onchange: (e) => {
      this.showLab = e.target.checked;
      save(LAB_KEY, this.showLab ? "1" : "0");
      this.renderBody();
    } });
    this.labLabel = h("label", { class: "check" }, labBox, " Include lab members",
      noteMark("Lab members are left out of the list unless their name matches. Tick this to list them first, as before."));
    const mapBtn = h("button", { type: "button", class: "btn btn-small", onclick: () => go({ view: "map", hl: "1", q: r.query, ego: null }) },
      "Show these people on the map");
    const beyond = r.beyondLimit && r.beyondLimit.people;
    const and = r.andTerms && r.andTerms.length > 1;
    const termList = and ? r.andTerms.map((t) => `“${t}”`).join(" and ") : "";
    return h("div", { class: "results-head" },
      h("h1", null, `Results for “${r.query}”`, noteMark("What is searched: " + r.scope)),
      and ? h("p", null, n ? `${n} ${n === 1 ? "person matches" : "people match"} ${r.andTerms.length > 2 ? "all of" : "both"} ${termList}` +
        (c.allTerms ? `; ${c.allTerms} ${c.allTerms === 1 ? "record matches" : "records match"} every term on its own.` : ".") :
        `No one matches every term: ${termList}.`,
        noteMark(h("span", { class: "note-line" }, "With AND, a person is listed when each term matches something linked to " +
          "them: a profile, paper, grant, note, name or affiliation. One paper does not have to mention both."),
        h("span", { class: "note-line" }, "Papers, grants and notes that match every term are marked “All terms” and listed first."),
        h("span", { class: "note-line" }, "AND must be in capitals. Put a phrase in quotes to search it exactly."))) : null,
      h("p", null, n ? `${n} ${n === 1 ? "person is" : "people are"} linked to ${kinds.join(", ")}.` : "No one is linked to a match.",
        n ? noteMark(h("span", { class: "note-line" }, "By person, each person is listed with their matches: research " +
          "profile, grants, papers, then notes."),
        h("span", { class: "note-line" }, "A grant or paper is either on their own profile or named in a relationship " +
          "record between them and someone else, as each line says."),
        h("span", { class: "note-line" }, "A match is a recorded association, not proof of expertise.")) : null),
      r.aliasesUsed.length ? h("p", { class: "dim" }, "Also searched: " + r.aliasesUsed.join("; ") + ".",
        noteMark("Put the term in quotes to search it alone.")) : null,
      beyond ? h("p", { class: "dim" }, (beyond === 1 ? "1 more person " : `${beyond} more people `) +
        `further out than ${ordinal(r.beyondLimit.limit)} degree also ${beyond === 1 ? "matches and is" : "match and are"} ` +
        "left out.", noteMark("Their connections were not collected. Search a name to look someone up.")) : null,
      h("div", { class: "sr-controls" }, seg, this.sortLabel, this.labLabel, mapBtn));
  }

  // ------------------------------------------------------------------ by person

  renderPeople(r) {
    const people = new Map(r.peopleSummary.map((p) => [p.personId, { p, profiles: [], grants: [], papers: [], notes: [], events: [], names: [] }]));
    const add = (pid, kind, x) => {
      const e = people.get(pid);
      if (e) e[kind].push(x);
    };
    for (const x of r.profiles) add(x.personId, "profiles", x);
    for (const x of r.grants) for (const a of x.associations) add(a.personId, "grants", { item: x, a });
    for (const x of r.papers) for (const a of x.associations) add(a.personId, "papers", { item: x, a });
    for (const x of r.notes) add(x.personId, "notes", x);
    for (const x of r.events || []) add(x.personId, "events", x);
    for (const x of r.people) add(x.personId, "names", x);
    const total = (e) => e.profiles.length + e.grants.length + e.papers.length + e.notes.length + e.events.length + e.names.length;
    const outside = (e) => (e.p.shown ? 0 : 1);
    const deg = (e) => e.p.degree ?? 99;
    const byName = (a, b) => a.p.name.localeCompare(b.p.name);
    const all = [...people.values()].filter((e) => total(e));
    const lab = (e) => e.p.degree === 0 && !e.names.length;
    const list = this.showLab ? all : all.filter((e) => !lab(e));
    const hiddenLab = all.length - list.length;
    list.sort(this.sort === "most"
      ? (a, b) => total(b) - total(a) || outside(a) - outside(b) || deg(a) - deg(b) || byName(a, b)
      : (a, b) => outside(a) - outside(b) || deg(a) - deg(b) || total(b) - total(a) || byName(a, b));
    if (hiddenLab) {
      this.body.append(h("p", { class: "dim" }, `${hiddenLab} lab ${hiddenLab === 1 ? "member also matches" : "members also match"}. `,
        h("button", { type: "button", class: "linklike", onclick: () => {
          this.showLab = true;
          save(LAB_KEY, "1");
          this.renderBody();
        } }, "Show them")));
    }
    const wrap = h("div", { class: "sr-people" });
    this.body.append(wrap);
    let shown = 0;
    const more = h("button", { type: "button", class: "btn sr-more" });
    const page = () => {
      list.slice(shown, shown + PAGE).forEach((e) => wrap.append(this.personCard(e)));
      shown = Math.min(list.length, shown + PAGE);
      more.hidden = shown >= list.length;
      const next = Math.min(PAGE, list.length - shown);
      more.textContent = `Show ${next} more ${next === 1 ? "person" : "people"}`;
    };
    more.addEventListener("click", page);
    page();
    this.body.append(more);
  }

  personCard(e) {
    const p = e.p;
    const row = this.index && this.index.get(p.personId);
    const who = [p.role, p.institution].filter(Boolean).join(", ");
    const via = row && p.degree > 0 && row.via.length ? "Connected through " + row.via.slice(0, 2).join(" and ") +
      (row.viaCount > 2 ? ` and ${row.viaCount - 2} more` : "") : "";
    const card = h("article", { class: "sr-person" + (p.shown ? "" : " outside") },
      h("header", { class: "sr-head" },
        face({ name: p.name, photoUrl: p.photoUrl }, "sr-face"),
        h("div", { class: "sr-who" },
          h("button", { type: "button", class: "linklike sr-name", onclick: () => go({ sel: p.personId }) }, p.name),
          h("div", { class: "sr-role" }, who || "Position not recorded"),
          via ? h("div", { class: "sr-via" }, via) : null),
        h("div", { class: "sr-tags" }, chip(degreeText(p.degree), p.degree === 0 ? "chip-strong" : ""),
          p.shown ? null : chip("Outside the map filters", "chip-outline"))));
    if (e.profiles.length) card.append(this.block("Research profile", e.profiles.map((x) => this.profileLine(x))));
    if (e.grants.length) {
      const grants = [...e.grants].sort((a, b) => allFirst(a.item, b.item));
      card.append(this.block(`Grants (${e.grants.length})`, grants.map((g) => this.grantLine(g, p.personId)), PER_KIND, ["grant", "grants"]));
    }
    if (e.papers.length) {
      const papers = [...e.papers].sort((a, b) => allFirst(a.item, b.item) || yearOf(b.item.year) - yearOf(a.item.year));
      // A very broad search can match more papers than the results carry; say so rather than undercount.
      const total = Math.max(p.papers || 0, papers.length);
      card.append(this.block(total > papers.length ? `Papers (${papers.length} of ${total} shown)` : `Papers (${papers.length})`,
        papers.map((x) => this.paperLine(x, p.personId)), PER_KIND, ["paper", "papers"]));
    }
    if (e.events.length) card.append(this.block("Conference programs", [...e.events].sort(allFirst).map((x) => this.eventLine(x))));
    if (e.notes.length) card.append(this.block("Notes", [...e.notes].sort(allFirst).map((x) => this.noteLine(x))));
    if (e.names.length) card.append(this.block("Name or affiliation", e.names.map((x) => this.nameLine(x))));
    return card;
  }

  block(title, items, limit = Infinity, [one, many] = ["", ""]) {
    const ul = h("ul", { class: "sr-list" });
    items.slice(0, limit).forEach((li) => ul.append(li));
    const sec = h("section", { class: "sr-sec" }, h("h3", null, title), ul);
    if (items.length > limit) {
      const left = items.length - limit;
      const more = h("button", { type: "button", class: "btn btn-small btn-quiet" }, `Show ${left} more ${left === 1 ? one : many}`);
      more.addEventListener("click", () => { items.slice(limit).forEach((li) => ul.append(li)); more.remove(); });
      sec.append(more);
    }
    return sec;
  }

  profileLine(x) {
    const tag = x.fieldLabel === "Notebook tag";
    return h("li", null,
      h("p", { class: "sr-snip" }, allChip(x), tag ? "Notebook tag: " : null, marked(x.text, x.highlights), sourceNote(x)));
  }

  grantLine({ item, a }, pid) {
    return h("li", null,
      h("p", { class: "sr-title" }, allChip(item), titleOf(item)),
      item.snippetField !== "Title" ? h("p", { class: "sr-snip" }, `${item.snippetField}: `, marked(item.text, item.highlights)) : null,
      h("div", { class: "sr-meta" },
        item.grantNumber ? h("span", null, item.grantNumber) : null,
        item.funder ? h("span", null, item.funder) : null,
        h("span", null, grantBasis(a)),
        item.url ? extLink(item.url, "award record") : null),
      this.alsoLinked(item, pid));
  }

  paperLine({ item, a }, pid) {
    return h("li", null,
      h("p", { class: "sr-title" }, allChip(item), titleOf(item)),
      item.snippetField !== "Title" ? h("p", { class: "sr-snip" }, `${item.snippetField}: `, marked(item.text, item.highlights)) : null,
      h("div", { class: "sr-meta" },
        item.year ? h("span", null, item.year) : null,
        item.venue ? h("span", null, item.venue) : null,
        h("span", null, a.basis === "record" ? "named with them in a coauthor record" :
          a.basis === "openalex" ? "in their OpenAlex record" : "on their profile"),
        item.url ? extLink(item.url, "paper link") : null),
      this.alsoLinked(item, pid));
  }

  noteLine(x) {
    return h("li", null,
      h("p", { class: "sr-snip" }, allChip(x), x.kind === "meeting" ? `Meeting note, ${x.fieldLabel.toLowerCase()}: ` : "Contact note: ",
        marked(x.text, x.highlights)),
      h("div", { class: "sr-meta" }, x.author ? h("span", null, `Recorded by ${x.author}`) : null,
        x.metBy ? h("span", null, `Met by ${x.metBy}`) : null, x.date ? h("span", null, x.date) : null));
  }

  eventLine(x) {
    return h("li", null,
      h("p", { class: "sr-snip" }, allChip(x), marked(x.text, x.highlights)),
      h("div", { class: "sr-meta" }, x.when ? h("span", null, x.when) : null, x.location ? h("span", null, x.location) : null,
        h("span", null, eventBasis(x)), x.sourceUrl ? extLink(x.sourceUrl, "program") : null));
  }

  nameLine(x) {
    return h("li", null, h("p", { class: "sr-snip" }, `${x.fieldLabel}: `, marked(x.text, x.highlights)));
  }

  /** The other people in the notebook linked to the same grant or paper. */
  alsoLinked(item, pid) {
    const others = item.associations.filter((o) => o.personId !== pid);
    if (!others.length) return null;
    const names = others.slice(0, 4).map((o, i) => [i ? ", " : "",
      h("button", { type: "button", class: "linklike sr-also-name", onclick: () => go({ sel: o.personId }) }, o.name)]);
    return h("div", { class: "sr-also" }, "Also linked: ", names, others.length > 4 ? ` and ${others.length - 4} more` : "");
  }

  // ------------------------------------------------------------------ by kind of record

  renderKinds(r) {
    const c = r.counts;
    this.body.append(h("div", { class: "cat-tabs" },
      [["profiles", "Research profiles", c.profiles], ["grants", "Grants", c.grants], ["papers", "Papers", c.papers],
        ["notes", "Notes", c.notes], ["events", "Conferences", c.events || 0], ["people", "Names", c.people]]
        .map(([id, label, n]) => h("a", { href: `#sr-${id}`, onclick: (e) => {
          e.preventDefault();
          document.getElementById(`sr-${id}`).scrollIntoView({ behavior: "smooth" });
        } }, `${label} (${n})`))));
    this.body.append(this.section("profiles", "Research profiles", "kind-profile",
      "Matches in source-backed research summaries and in notebook tags. A summary that mentions a topic is not proof of expertise.",
      r.profiles, (x) => this.profileItem(x)));
    this.body.append(this.section("grants", "Grants", "kind-grant",
      "Matches in grants listed on someone's profile (title, funder, award number) and in shared-grant relationship records. " +
      "Roles are shown as recorded; an award date range is not a person's tenure on it.",
      r.grants, (x) => this.grantItem(x)));
    this.body.append(this.section("papers", "Papers", "kind-paper",
      "Matches in papers listed on someone's profile (title, venue, author list, selection note) and in coauthor " +
      "relationship records. Each paper lists the profiles it is on and the coauthors that relationship records name; " +
      "neither is a full author list.",
      r.papers, (x) => this.paperItem(x)));
    this.body.append(this.section("notes", "Notes", "kind-note",
      "Matches in contact notes and meeting notes written by the lab. These are private context, not public evidence.",
      r.notes, (x) => this.noteItem(x)));
    this.body.append(this.section("events", "Conference programs", "kind-note",
      "Matches in imported conference programs: the meeting, the person's role, the talk and the session. Names on a " +
      "program are matched to people in the network by name, and by institution when the program gives one.",
      r.events || [], (x) => this.eventItem(x)));
    this.body.append(this.section("people", "Names and affiliations", "kind-note",
      "Matches in names, IDs, institutions, positions and lab names.", r.people, (x) => this.personItem(x)));
  }

  section(id, title, kind, explain, items, render) {
    const sec = h("section", { class: "result-group", id: `sr-${id}` },
      h("h2", null, h("span", { class: "kind-mark " + kind, "aria-hidden": "true" }),
        h("span", null, `${title} (${items.length})`, noteMark(explain))));
    if (!items.length) {
      sec.append(h("p", { class: "dim" }, "No matches of this kind."));
      return sec;
    }
    const list = h("div");
    const first = items.slice(0, 25);
    first.forEach((x) => list.append(render(x)));
    sec.append(list);
    if (items.length > first.length) {
      const more = h("button", { type: "button", class: "btn btn-small" }, `Show ${items.length - first.length} more`);
      more.addEventListener("click", () => { items.slice(25).forEach((x) => list.append(render(x))); more.remove(); });
      sec.append(more);
    }
    return sec;
  }

  pill(p) {
    return h("button", { type: "button", class: "person-pill" + (p.shown ? "" : " outside"), onclick: () => go({ sel: p.personId }),
      title: p.shown ? "" : "Outside the current map filters" }, p.name, h("span", { class: "dim" }, degreeText(p.degree)));
  }

  profileItem(x) {
    return h("div", { class: "result" },
      h("p", { class: "rtitle" }, x.fieldLabel),
      h("p", { class: "rsnippet" }, marked(x.text, x.highlights), sourceNote(x)),
      h("div", { class: "people" }, this.pill(x)));
  }

  paperItem(x) {
    return h("div", { class: "result" },
      h("p", { class: "rtitle" }, allChip(x), titleOf(x)),
      x.snippetField !== "Title" ? h("p", { class: "rsnippet" }, `${x.snippetField}: `, marked(x.text, x.highlights)) : null,
      h("div", { class: "rmeta" }, x.year ? h("span", null, x.year) : null, x.venue ? h("span", null, x.venue) : null,
        h("span", null, "Matched in " + x.matchedFields.join(", ").toLowerCase()),
        x.url ? extLink(x.url, "paper link") : null),
      this.linked(x, "On the profiles of ", "Coauthors named in relationship records "));
  }

  linked(x, profileText, recordText, extra, libraryText = "In the OpenAlex records of ") {
    const onProfile = x.associations.filter((a) => a.basis === "profile" || !a.basis);
    const viaRecord = x.associations.filter((a) => a.basis === "record");
    const viaLibrary = x.associations.filter((a) => ["openalex", "nih", "nsf"].includes(a.basis));
    const item = (a) => (extra ? h("span", { class: "chips" }, this.pill(a), extra(a)) : this.pill(a));
    return [
      onProfile.length ? h("div", { class: "people" }, h("span", { class: "dim" }, profileText), onProfile.map(item)) : null,
      viaRecord.length ? h("div", { class: "people" }, h("span", { class: "dim" }, recordText), viaRecord.map((a) => this.pill(a))) : null,
      viaLibrary.length ? h("div", { class: "people" }, h("span", { class: "dim" }, libraryText), viaLibrary.map(item)) : null];
  }

  grantItem(x) {
    return h("div", { class: "result" },
      h("p", { class: "rtitle" }, allChip(x), titleOf(x)),
      x.snippetField !== "Title" ? h("p", { class: "rsnippet" }, `${x.snippetField}: `, marked(x.text, x.highlights)) : null,
      h("div", { class: "rmeta" }, x.grantNumber ? h("span", null, x.grantNumber) : null, x.funder ? h("span", null, x.funder) : null,
        x.url ? extLink(x.url, "award record") : null),
      this.linked(x, "On the profiles of ", "Named in shared-grant records ",
        (a) => h("span", { class: "dim" }, a.role || "role not recorded"), "NIH or NSF awards to "));
  }

  noteItem(x) {
    return h("div", { class: "result" },
      h("p", { class: "rtitle" }, x.kind === "meeting" ? `Meeting note: ${x.fieldLabel.toLowerCase()}` : "Contact note"),
      h("p", { class: "rsnippet" }, marked(x.text, x.highlights)),
      h("div", { class: "rmeta" }, x.author ? h("span", null, `Recorded by ${x.author}`) : null, x.metBy ? h("span", null, `Met by ${x.metBy}`) : null),
      h("div", { class: "people" }, this.pill(x)));
  }

  eventItem(x) {
    return h("div", { class: "result" },
      h("p", { class: "rtitle" }, allChip(x), x.conference),
      h("p", { class: "rsnippet" }, marked(x.text, x.highlights)),
      h("div", { class: "rmeta" }, h("span", null, eventBasis(x)), x.sourceUrl ? extLink(x.sourceUrl, "program") : null),
      h("div", { class: "people" }, this.pill(x)));
  }

  personItem(x) {
    return h("div", { class: "result" },
      h("p", { class: "rtitle" }, x.fieldLabel),
      h("p", { class: "rsnippet" }, marked(x.text, x.highlights)),
      h("div", { class: "people" }, this.pill(x)));
  }
}

/** Where a research-profile match comes from: its source and check date, or what a tag or topic is. */
function sourceNote(x) {
  if (x.fieldLabel === "Notebook tag") return noteMark(x.evidenceNote);
  return noteMark(x.sourceUrl ? ["Source: ", extLink(x.sourceUrl)] : "Source not recorded",
    x.checkedAt ? `, checked ${x.checkedAt}` : "", ".", x.evidenceNote ? ` ${x.evidenceNote}` : "");
}

/** With AND, a record that matches every term on its own gets a mark and is listed first. */
function allChip(x) {
  return x.allTerms ? [chip("All terms", "chip-strong"), " "] : null;
}

function allFirst(a, b) {
  return (b.allTerms ? 1 : 0) - (a.allTerms ? 1 : 0);
}

/** How a program name was matched to the person, with the name as the program prints it. */
function eventBasis(x) {
  const as = `listed as ${x.programName}${x.programInstitution ? `, ${x.programInstitution}` : ""}`;
  return x.basis === "name and institution" ? `${as}; name and institution match` :
    x.basis === "initials and institution" ? `${as}; initials and institution match` : `${as}; matched by name only`;
}

function grantBasis(a) {
  if (a.basis === "record") return "named with them in a shared-grant record";
  if (a.basis === "nih") return "NIH award" + (a.role ? `, ${a.role}` : "");
  if (a.basis === "nsf") return "NSF award, PI";
  return "on their profile, role " + (a.role || "not recorded");
}

function titleOf(item) {
  return item.titleHighlights && item.titleHighlights.length ? marked(item.title, item.titleHighlights) : item.title;
}

function yearOf(y) {
  const m = String(y || "").match(/\d{4}/);
  return m ? Number(m[0]) : 0;
}

const scopeShort = "research summaries, tags, paper and grant records, notes, conference programs, names and affiliations";

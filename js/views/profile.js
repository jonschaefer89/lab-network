// Person drawer: identity, connection to the lab, relationships, research, notes, record details.

import * as api from "../api.js";
import { emit, filterParams, go, on, state } from "../state.js";
import {
  DOCTORATE_TEXT, GROUP_LABEL, GROUP_OF, HIDDEN_TEXT, chip, clear, degreeText, extLink, face, fmtTs, h, initials,
  missing, noteMark, safeUrl, toast,
} from "../ui.js";
import { contactForm, noteForm, notTheirsForm, openalexOnRecord, theirsForm, tieForm, editTieForm } from "./forms.js";

export class Drawer {
  constructor(el, app) {
    this.el = el;
    this.app = app;
    this.current = null;
    on("changed", () => { if (this.current) this.load(this.current, { quiet: true }); });
  }

  async sync() {
    if (!state.sel) {
      this.el.hidden = true;
      this.current = null;
      return;
    }
    const key = JSON.stringify([state.sel, filterParams()]);
    if (key === this.key && !this.el.hidden) return;
    this.key = key;
    await this.load(state.sel);
  }

  async load(id, { quiet = false } = {}) {
    this.current = id;
    this.el.hidden = false;
    if (!quiet) {
      clear(this.el).append(h("div", { class: "profile" }, h("p", { class: "dim" }, "Loading…")));
    }
    try {
      const p = await api.get(`/api/person/${encodeURIComponent(id)}`, filterParams());
      if (this.current !== id) return;
      this.person = p;
      this.render(p);
      emit("person-loaded", p);
    } catch (err) {
      clear(this.el).append(h("div", { class: "profile" }, closeBtn(),
        h("p", { class: "notice notice-error" }, err.message)));
    }
  }

  render(p) {
    const c = p.record;
    const f = p.flags;
    const wrap = h("div", { class: "profile" }, closeBtn());
    // Header
    const photo = face({ name: c.name, photoUrl: c.photoUrl }, "portrait");
    if (photo.classList.contains("fallback")) photo.className = "portrait-fallback";
    const roleLine = c.role ? h("p", { class: "prole" }, c.role) : h("p", { class: "prole" }, missing("Position not recorded"));
    const instLine = c.institution ? h("p", { class: "pinst" }, c.institution) : h("p", { class: "pinst" }, missing("Institution not recorded"));
    const photoDoubt = c.photoUrl && c.photoConfidence && c.photoConfidence !== "confirmed";
    const chips = h("div", { class: "chips" },
      chip(degreeText(f.degree), f.degree === 0 ? "chip-strong" : ""),
      f.isLab ? null : chip(DOCTORATE_TEXT[f.doctorate] || f.doctorate, f.doctorate === "confirmed" ? "" : "chip-outline"),
      c.labName || c.lab ? chip(c.labName || c.lab, "chip-outline") : null,
      p.origin === "workspace" ? chip("Added in this workspace", "chip-outline") : null,
      p.editedFields.length ? chip(`Edited: ${p.editedFields.length} field${p.editedFields.length > 1 ? "s" : ""}`, "chip-outline") : null,
      photoDoubt ? chip(`Photo ${c.photoConfidence}`, "chip-outline") : null,
      p.conflicts.some((x) => x.status === "open") ? chip("Open conflict", "chip-warn") : null);
    // The photo's source is behind a mark on the portrait; a photo not confirmed as theirs also gets a chip.
    const credit = c.photoUrl ? noteMark("Photo: ",
      c.photoSourceUrl ? extLink(c.photoSourceUrl, "source page") : "source page not recorded",
      c.photoAttribution ? ". " + c.photoAttribution : "") : null;
    wrap.append(h("div", { class: "phead" }, h("div", { class: "pphoto" }, photo, credit),
      h("div", null, h("h1", { class: "pname" }, c.name), roleLine, instLine)), chips);
    if (!f.shown) {
      wrap.append(h("p", { class: "notice notice-quiet" },
        "Not on the map with the current filters: " + f.hiddenBy.map((r) => r === "degree" && f.degree === null ?
          "no recorded path to the lab" : HIDDEN_TEXT[r] || r).join("; ") + "."));
    }
    wrap.append(h("div", { class: "pactions" },
      h("button", { class: "btn btn-small btn-primary", type: "button", onclick: () => noteForm(this.app, { contactId: p.id }) }, "Add note"),
      h("button", { class: "btn btn-small", type: "button", onclick: () => go({ view: "map", ego: p.id, sel: p.id }) }, "Centre map on them"),
      h("button", { class: "btn btn-small", type: "button", onclick: () => go({ view: "paths", from: "lab", to: p.id, sel: p.id }) }, "Find a path"),
      h("button", { class: "btn btn-small", type: "button", onclick: () => contactForm(this.app, p) }, "Edit details"),
      h("button", { class: "btn btn-small", type: "button", onclick: () => tieForm(this.app, { from: p.id }) }, "Add relationship")));

    wrap.append(this.connectionSection(p));
    wrap.append(this.tiesSection(p));
    wrap.append(this.researchSection(p));
    wrap.append(this.conferencesSection(p));
    wrap.append(this.notesSection(p));
    wrap.append(this.recordSection(p));
    clear(this.el).append(wrap);
  }

  connectionSection(p) {
    const conn = p.connection;
    const sec = h("section", { class: "psection" }, h("h2", null, "How they connect to the lab"));
    const why = conn.status === "no-path" ? conn.coverageNote : conn.distance === 0 ? "" :
      "Paths use the fewest recorded relationships. Longer routes are left out. " +
      "Shared institutions or topics are never counted as connections.";
    sec.append(h("p", { class: "headline" }, conn.headline, why ? noteMark(why) : null));
    if (conn.status === "no-path" || conn.distance === 0) return sec;
    for (const path of conn.paths) sec.append(routeList(path, this.app));
    if (conn.truncated) sec.append(h("p", { class: "dim" }, `Showing ${conn.paths.length} of ${conn.pathCount} equally short paths.`));
    return sec;
  }

  tiesSection(p) {
    const order = p.ties.length ? noteMark("Each person appears once, under the first kind that applies: " +
      "supervision, then research, then association. Their other records are listed with them. Lists run newest " +
      "first with undated ones last; research lists start with the most shared records.") : null;
    const sec = h("section", { class: "psection" },
      h("h2", null, h("span", null, "Relationships", order), h("span", { class: "count" }, `${p.ties.length} people`)));
    if (!p.ties.length) {
      sec.append(h("p", { class: "dim" }, "No relationship records for this person."));
      const col = collectionLine(p);
      if (col) sec.append(col);
      return sec;
    }
    const col = collectionLine(p);
    if (col) sec.append(col);
    // The server sorts ties by group, then section, then date or record count.
    const groups = new Map();
    for (const t of p.ties) {
      const g = t.group || GROUP_OF[t.records[0].type] || "association";
      if (!groups.has(g)) groups.set(g, new Map());
      const secs = groups.get(g);
      const label = t.sectionLabel || GROUP_LABEL[g];
      if (!secs.has(label)) secs.set(label, []);
      secs.get(label).push(t);
    }
    for (const [g, secs] of groups) {
      const n = [...secs.values()].reduce((sum, list) => sum + list.length, 0);
      sec.append(h("h3", { class: "tiegroup" }, `${GROUP_LABEL[g] || g} (${n})`));
      for (const [label, list] of secs) {
        sec.append(h("h4", { class: "tiesection" }, `${label} (${list.length})`));
        const ul = h("ul", { class: "ties" });
        const render = (t) => ul.append(tieItem(t, p, this.app));
        list.slice(0, 8).forEach(render);
        sec.append(ul);
        if (list.length > 8) {
          const more = h("button", { type: "button", class: "btn btn-small btn-quiet" }, `Show ${list.length - 8} more`);
          more.addEventListener("click", () => { list.slice(8).forEach(render); more.remove(); });
          sec.append(more);
        }
      }
    }
    return sec;
  }

  researchSection(p) {
    const c = p.record;
    const lib = p.library;
    const sec = h("section", { class: "psection" }, h("h2", null, "Research"));
    if (c.researchSummary) {
      sec.append(h("p", null, c.researchSummary, noteMark("From ",
        c.researchSourceUrl ? extLink(c.researchSourceUrl) : "an unrecorded source",
        c.researchCheckedAt ? `, checked ${c.researchCheckedAt}` : "", ". Not a complete list of interests.")));
    } else {
      sec.append(h("p", null, missing("Research summary not recorded")));
    }
    const record = publicationRecord(lib);
    if (record) sec.append(record);
    const records = identityRecords(p, this.app);
    if (records) sec.append(records);
    // Matched in OpenAlex, but the library has not fetched their paper list yet.
    let waiting = null;
    if (lib && lib.openalex.status === "linked" && !lib.openalex.worksAt) {
      const live = h("span", { class: "note-live" });
      waiting = { live, el: h("p", { class: "dim" }, "Paper list not fetched yet.", noteMark("The research library ",
        "fetches paper lists within OpenAlex's free daily allowance, nearest people first, and each weekly update ",
        "fetches more.", live)) };
      sec.append(waiting.el);
    }
    // Papers and awards from the research library. The live check puts anything newer at the top, marked New.
    const libWorks = lib ? lib.works : [];
    const recent = libraryList("Recent papers", libWorks, libraryWorkItem, worksNote(lib));
    sec.append(recent.el);
    const papers = [...(c.papers || [])].sort((a, b) => yearOf(b.year) - yearOf(a.year));
    sec.append(h("h3", { class: "tiegroup" }, `${libWorks.length ? "Papers in the notebook" : "Papers"} (${papers.length})`));
    // The library leaves out the people expand added until their doctorate and position are known.
    const skipped = p.librarySkipped ? "The research library looks up the people expand adds to the second degree " +
      "once their doctorate is confirmed and their institution and position are known. " : "";
    if (!papers.length) {
      sec.append(h("p", { class: "dim" }, libWorks.length ? "None selected by hand." :
        ["No papers recorded.", noteMark(skipped, "An empty list is not evidence of no publications.")]));
    } else sec.append(listWithMore(papers, 5, paperItem, "works"));
    // Awards from NIH and NSF, then grants recorded by hand (an award already listed by hand is shown once).
    const grants = [...(c.grants || [])].sort((a, b) => (b.projectStartDate || b.period || "").localeCompare(a.projectStartDate || a.period || ""));
    const handNumbers = new Set(grants.map((g) => awardCore(g.grantNumber || g.projectId)));
    const awards = (lib ? lib.awards : []).filter((a) => !handNumbers.has(awardCore(a.number)));
    const funded = libraryList("NIH and NSF awards", awards, awardItem, lib ? awardsNote(lib) : "");
    sec.append(funded.el);
    sec.append(h("h3", { class: "tiegroup" }, `${awards.length ? "Grants in the notebook" : "Grants"} (${grants.length})`));
    if (!grants.length) sec.append(h("p", { class: "dim" }, awards.length ? "None recorded by hand." :
      ["No grants recorded.", noteMark(skipped, "An empty list does not mean no funding.")]));
    else sec.append(listWithMore(grants, 5, grantItem, "works"));
    if (lib) this.checkLatest(p.id, recent, funded, handNumbers, waiting);
    return sec;
  }

  /** Ask OpenAlex and NIH for anything newer than the library's last update for this person. */
  async checkLatest(id, recent, funded, handNumbers, waiting) {
    const first = recent.el.hidden ? funded : recent;
    let r;
    try {
      r = await api.get(`/api/person/${encodeURIComponent(id)}/latest`);
    } catch (err) {
      if (this.current === id) first.tell("The check for anything newer could not run: " + err.message);
      return;
    }
    if (this.current !== id) return;
    const works = r.works || [];
    const awards = (r.awards || []).filter((a) => !handNumbers.has(awardCore(a.number)));
    // Without a stored paper list, what the check found is the list itself, not new papers.
    const unstored = !!(r.sources && r.sources.openalex.checked && !r.sources.openalex.since);
    if (unstored) {
      recent.fill(works);
      if (waiting && works.length) waiting.el.remove();
    } else recent.addNew(works);
    funded.addNew(awards);
    if (r.sources) {
      const text = !unstored ? liveText(r.sources.openalex, works.length, "OpenAlex") : works.length ?
        "Fetched from OpenAlex just now. The library stores this list at its next update." :
        "Checked OpenAlex just now: no papers for this list.";
      if (waiting && waiting.el.isConnected) waiting.live.append(h("span", { class: "note-line" }, text));
      else recent.tell(text);
      funded.tell(liveText(r.sources.nih, awards.length, "NIH RePORTER"));
    } else {                                  // not checked: the demo lab, or the live check is off
      for (const note of r.notes || []) first.tell(note);
    }
  }

  conferencesSection(p) {
    const c = p.conferences || { appearances: [], summary: [], listed: 0 };
    const sec = h("section", { class: "psection" },
      h("h2", null, h("span", null, "Conferences", noteMark("From the imported programs of the conferences on the lab's " +
        "list. A program lists who presents, chairs or discusses, not everyone who attends. The name on the program is " +
        "matched to this person by name, and by institution when the program gives one; each line says how.")),
      h("span", { class: "count" }, `${c.appearances.length} ${c.appearances.length === 1 ? "appearance" : "appearances"}`)));
    if (!c.listed) {
      sec.append(h("p", { class: "dim" }, "The lab follows no conferences yet. ",
        h("a", { href: "#/conferences" }, "Add them on the Conferences page.")));
      return sec;
    }
    if (!c.appearances.length) {
      sec.append(h("p", { class: "dim" }, "Not on any imported program."));
      return sec;
    }
    const ul = h("ul", { class: "works" });
    for (const x of c.summary) {
      ul.append(h("li", null,
        h("div", { class: "wtitle" }, h("a", { href: `#/conferences?conf=${encodeURIComponent(x.series)}` }, x.short),
          x.name && x.name !== x.short ? h("span", { class: "dim" }, ` ${x.name}`) : null),
        h("div", { class: "wmeta" }, x.presentingYears.length ? `Presented, chaired or discussed in ` +
          `${x.presentingYears.join(", ")}: ${x.presentingYears.length} of ${x.imported.length} imported ` +
          `${x.imported.length === 1 ? "year" : "years"} (${x.imported.join(", ")}).` :
          `Listed only as a co-author (${x.years.join(", ")}).`,
          ` ${x.appearances} ${x.appearances === 1 ? "appearance" : "appearances"}.`)));
    }
    sec.append(ul, h("h3", { class: "tiegroup" }, "Talks and sessions"), listWithMore(c.appearances, 5, conferenceItem, "works"));
    return sec;
  }

  notesSection(p) {
    const c = p.record;
    const sec = h("section", { class: "psection" },
      h("h2", null, "Notes", h("span", { class: "count" }, `${p.meetings.length} meeting notes`)));
    for (const m of p.meetings) sec.append(noteCard(m, this.app));
    if (!p.meetings.length) sec.append(h("p", { class: "dim" }, "No meeting notes yet."));
    sec.append(h("button", { type: "button", class: "btn btn-small", onclick: () => noteForm(this.app, { contactId: p.id }) }, "Add a note"));
    if (c.notes) {
      sec.append(h("h3", { class: "tiegroup" }, "Notebook note",
        noteMark("Retained professional context from the notebook. Not public evidence.")), h("p", null, c.notes));
    }
    return sec;
  }

  recordSection(p) {
    const c = p.record;
    const det = h("details", { class: "psection" }, h("summary", null, h("h2", { class: "inline" }, "Record details and sources")));
    const kv = h("dl", { class: "kv" });
    const row = (k, v) => kv.append(h("dt", null, k), h("dd", null, v === "" || v === undefined || v === null ? missing() : v));
    row("Stable ID", h("code", null, p.id));
    if (!this.app.boot.sharedCopy) {     // a shared copy is rebuilt from the notebook, so every record looks original
      row("Origin", p.origin === "packet" ? ["Original packet", noteMark("An unchanged copy of the packet is kept separately.")] :
        "Added in this workspace");
    }
    row("Profile source", c.profileSourceUrl ? [extLink(c.profileSourceUrl), c.profileCheckedAt ? `, checked ${c.profileCheckedAt}` : "",
      c.profileConfidence ? ` (${c.profileConfidence})` : ""] : "");
    row("Website", c.website ? extLink(c.website) : "");
    row("Email", c.email || "");
    row("Doctorate", [DOCTORATE_TEXT[p.flags.doctorate] || p.flags.doctorate,
      c.doctorateType ? ` (${c.doctorateType})` : "", c.doctorateEvidence ? `. ${c.doctorateEvidence}` : "",
      c.doctorateSourceUrl ? [" ", extLink(c.doctorateSourceUrl, "source")] : ""]);
    row("Lab", c.labName || c.lab ? [c.labName || c.lab, c.labStatus ? ` (${c.labStatus})` : "",
      c.labSourceUrl ? [" ", extLink(c.labSourceUrl, "source")] : ""] : "");
    row("Primary affiliation", c.primaryInstitution ? [c.primaryInstitution, c.primaryInstitutionSourceUrl ? [" ", extLink(c.primaryInstitutionSourceUrl, "source")] : ""] : "");
    row("Tags", (c.tags || []).length ? c.tags.join(", ") : "");
    row("Created", c.createdAt || "");
    const hc = p.historicalCoverage;
    row("Collection history", [`seed ${hc.seed ? "yes" : "no"}, stored networkDegree ${hc.networkDegree ?? "none"}, ` +
      `expansion ${hc.expansionStatus || "not recorded"}.`, noteMark("These describe how the record was collected; the " +
      "degree shown above is calculated from the relationships.")]);
    if (p.aliases.length) row("Also imported as", p.aliases.join(", "));
    det.append(kv);
    if ((c.sources || []).length) {
      det.append(h("h3", { class: "tiegroup" }, "Source ledger"),
        h("ul", { class: "works" }, c.sources.map((s) => h("li", null, s.url ? extLink(s.url) : missing("No URL"), s.note ? ` ${s.note}` : ""))));
    }
    if (p.missing.length) {
      det.append(h("h3", { class: "tiegroup" }, "Missing information"),
        h("ul", { class: "works" }, p.missing.map((m) => h("li", null, `${m.label}: ${m.note}`))));
    }
    if (p.conflicts.length) {
      det.append(h("h3", { class: "tiegroup" }, "Import conflicts"),
        h("ul", { class: "works" }, p.conflicts.map((cf) => h("li", null,
          `${cf.field}: kept “${short(cf.current)}”; incoming “${short(cf.incoming)}” (${cf.status})`,
          cf.status === "open" ? [" ", h("a", { href: "#/data?tab=conflicts" }, "Resolve")] : ""))));
    }
    det.append(h("h3", { class: "tiegroup" }, "Edit history"));
    if (!p.history.length) {
      det.append(h("p", { class: "dim" }, "No edits.", noteMark("Every value shown comes from the original packet.")));
    }
    else det.append(historyList(p.history, p.packetValues));
    return det;
  }
}

/** Whether this person's connections were collected, from the latest collection run that covered them. */
function collectionLine(p) {
  const c = p.collection;
  if (!c) return null;
  const st = c.standard || {};
  if (c.status === "collected") {
    const lab = c.degree === 0 || p.flags.degree === 0;
    const scope = lab ? "Their OpenAlex coauthors" : `Their coauthors on works since ${st.sinceYear || "the start year"}`;
    const total = typeof c.coauthors === "number" ?
      `, of ${count(c.coauthors, "OpenAlex coauthor", "OpenAlex coauthors")}${lab ? "" : ` since ${st.sinceYear}`}` : "";
    const rule = lab ? " Each was linked, or added if not yet in the notebook." :
      ` Coauthors already in the notebook were linked; others were added when they shared ${st.minShared || 2} or more ` +
      `of these works${st.perPerson ? `, up to ${st.perPerson} per person` : ""}.`;
    const records = (c.openalexIds || []).length > 1 ? "records" : "record";
    return h("p", { class: "notice notice-quiet tienote" },
      `Coauthors collected: ${count(c.added, "person", "people")} added and ${count(c.linked, "person", "people")} ` +
      `already in the notebook linked${total}.`,
      noteMark(`${scope} were collected on ${c.date}, from papers with ${st.maxAuthors || 30} or fewer authors.`, rule,
        c.orcidWorks ? ` ${count(c.orcidWorks, "paper", "papers")} came from their ORCID record.` : "",
        c.unanchored ? ` ${count(c.unanchored, "work", "works")} on their OpenAlex ${records} ` +
          `${c.unanchored === 1 ? "was" : "were"} left out: nothing ties ${c.unanchored === 1 ? "it" : "them"} to ` +
          "their other work (a coauthor already in the network, a paper recorded with a lab member, or a shared " +
          "institution), and OpenAlex sometimes files researchers with the same name under one record." : "",
        c.truncated ? " Only their 10,000 newest works were read." : "",
        c.basis ? ` OpenAlex identity: ${c.basis}.` : ""));
  }
  return h("p", { class: "notice notice-quiet tienote" }, `Connections not collected: ${c.text}.`,
    noteMark(`Recorded by the collection run on ${c.date}.`));
}

function count(n, one, many) {
  return `${n || 0} ${(n || 0) === 1 ? one : many}`;
}

function closeBtn() {
  return h("button", { type: "button", class: "btn btn-quiet btn-small profile-close", "aria-label": "Close person details",
    onclick: () => go({ sel: null }) }, "Close");
}

export function routeList(path, app) {
  const ol = h("ol", { class: "route" });
  path.people.forEach((pid, i) => {
    const isLab = i === 0;
    ol.append(h("li", { class: "stop" + (isLab ? " lab" : "") },
      h("button", { type: "button", class: "linklike", onclick: () => go({ sel: pid }) }, app.nameOf(pid))));
    const st = path.steps[i];
    if (st) {
      const g = GROUP_OF[st.records[0] && st.records[0].type] || "association";
      const leg = h("li", { class: "leg leg-" + g });
      const lines = sameLines(st.records);
      lines.slice(0, 2).forEach((recs) => leg.append(recordLine(recs)));
      if (lines.length > 2) {
        const rest = lines.slice(2);
        const n = rest.reduce((sum, recs) => sum + recs.length, 0);
        const more = h("details", null, h("summary", null, `${n} more ${n === 1 ? "record" : "records"} between them`));
        rest.forEach((recs) => more.append(recordLine(recs)));
        leg.append(more);
      }
      ol.append(leg);
    }
  });
  return ol;
}

/** Records that read the same (sentence, qualifiers and kind of evidence) share one line of a route. */
function sameLines(records) {
  const lines = new Map();
  for (const r of records) {
    const key = JSON.stringify([r.sentence, r.qualifiers, r.provenanceLabel]);
    if (!lines.has(key)) lines.set(key, []);
    lines.get(key).push(r);
  }
  return [...lines.values()];
}

/** One line of a route: what the records say and their kind of evidence. Their sources are behind the mark. */
function recordLine(recs) {
  const r = recs[0];
  const what = [...r.qualifiers, recs.length > 1 ? `${recs.length} records` : ""].filter(Boolean);
  const source = (x) => x.sourceUrl ? extLink(x.sourceUrl, x.sourceTitle) :
    x.sourceTitle ? `${x.sourceTitle} (no link recorded)` : "No source link recorded";
  const note = recs.length > 1 ? ["Sources:", recs.map((x) => h("span", { class: "note-line" }, source(x)))] :
    r.sourceUrl || r.sourceTitle ? ["Source: ", source(r)] : "No source link recorded.";
  return h("p", null, r.sentence, what.length ? ` (${what.join("; ")})` : "", ". ",
    chip(r.provenanceLabel, "chip-outline"), noteMark(note));
}

function tieItem(t, p, app) {
  const other = t.person;
  // One phrase per role, in precedence order, with record counts and dates where recorded.
  const byRole = new Map();
  for (const r of t.records) {
    if (!byRole.has(r.fromPerspective)) byRole.set(r.fromPerspective, { n: 0, when: new Set() });
    const x = byRole.get(r.fromPerspective);
    x.n += 1;
    if (r.timing) x.when.add(r.timing);
  }
  const phrases = [...byRole].map(([role, x]) => [role + (x.n > 1 ? ` (${x.n} records)` : ""),
    x.when.size ? h("span", { class: "tie-when" }, ", " + [...x.when].slice(0, 2).join(", ")) : ""]);
  const li = h("li", { class: "tie" },
    h("div", { class: "tie-head" },
      h("button", { type: "button", class: "tie-name", onclick: () => go({ sel: other.id }) }, other.name),
      h("span", { class: "tie-deg" }, degreeText(other.degree))),
    h("p", { class: "tie-summary" }, phrases.flatMap((ph, i) => (i ? ["; ", ...ph] : ph)),
      other.institution ? h("span", { class: "dim tie-inst" }, other.institution) : ""));
  const det = h("details", null, h("summary", null, `Evidence (${t.records.length})`));
  for (const r of t.records) det.append(recordBlock(r, app));
  li.append(det);
  return li;
}

function recordBlock(r, app) {
  const g = r.group;
  const block = h("div", { class: `record g-${g}` + (r.included ? "" : " excluded") },
    h("div", { class: "sentence" }, r.sentence + "."),
    r.narrative ? h("div", { class: "evidence" }, r.narrative) : null,
    h("div", { class: "evidence" }, r.evidence),
    h("div", { class: "meta" },
      h("span", null, r.typeLabel + (r.direction === "one-way" ? ` (${r.fromName} → ${r.toName})` : "")),
      r.label ? h("span", null, r.label) : null,
      r.timing ? h("span", null, r.timing) : null,
      r.relationshipStatus ? h("span", null, r.relationshipStatus) : h("span", null, "status not recorded"),
      h("span", null, r.provenanceLabel),
      h("span", null, `confidence ${r.confidence}`),
      h("span", null, `review ${r.reviewStatus}`),
      r.sourceUrl ? extLink(r.sourceUrl, r.sourceTitle || "source") : h("span", null, r.sourceTitle || "no source link"),
      r.checkedAt ? h("span", null, `checked ${r.checkedAt}`) : null,
      r.origin === "workspace" ? h("span", null, "added in this workspace") : null),
    r.included ? null : h("div", { class: "dim" }, "Not counted by the current relationship filter."),
    h("div", { class: "meta" }, h("code", null, r.id),
      h("button", { type: "button", class: "btn btn-small btn-quiet", onclick: () => editTieForm(app, r) }, "Edit record")));
  if (r.relationshipStatusNote) block.insertBefore(h("div", { class: "evidence" }, r.relationshipStatusNote), block.lastChild);
  return block;
}

function publicationRecord(lib) {
  if (!lib) return null;
  const o = lib.openalex;
  const s = o.summary;
  if (o.status !== "linked") {
    const [why, more] = { "not-found": ["Not found in OpenAlex."],
      ambiguous: ["Several OpenAlex authors match their name; none was chosen."],
      pending: ["Not looked up in OpenAlex yet.", "The weekly update will look them up."],
      unmatched: ["No OpenAlex author matched yet.", "Each weekly update looks again."] }[o.status] || ["No OpenAlex record."];
    return h("p", { class: "dim" }, why, more ? noteMark(more) : null);
  }
  if (!s) return h("p", { class: "dim" }, "Linked to OpenAlex; the summary arrives with the next update.");
  const topics = (s.topics || []).slice(0, 4).map(([t, n]) => `${t} (${n})`).join("; ");
  const since = (lib.standard || {}).sinceYear || 2016;
  const several = (o.urls || []).length > 1;
  return h("div", { class: "pub-record" },
    h("p", null, `${s.worksCount} works in OpenAlex, ${s.worksSince} since ${since}`,
      s.hIndex !== null && s.hIndex !== undefined ? `; h-index ${s.hIndex}` : "", ". ",
      topics ? `Main topics: ${topics}.` : "",
      noteMark("OpenAlex identity: ", o.basis || "linked", ".", o.summaryAt ? ` Updated ${o.summaryAt}.` : "",
        several ? [` Works, works since ${since} and topic counts are added across their ${o.urls.length} OpenAlex ` +
          "records. The h-index is the largest record's; across all their works it may be higher. ", recordLinks(o), "."] :
        o.url ? [" ", extLink(o.url, "OpenAlex profile")] : "")));
}

/**
 * A person's OpenAlex records: the ones the research library links to them, the ones it held back after the lookup by
 * ORCID iD, and the ones marked as not theirs. A held-back record can be confirmed as theirs, which adds it to their
 * sources until the next library run links it; each record can be marked as not theirs, and a mark withdrawn.
 */
function identityRecords(p, app) {
  const o = p.library ? p.library.openalex : null;
  const linked = o && o.status === "linked" ? (o.ids || []).map((id, i) => ({ id, url: (o.urls || [])[i] })) : [];
  const marked = p.openalexNotTheirs || [];
  const markedIds = new Set(marked.map((r) => r.id));
  const held = ((o && o.held) || []).filter((r) => !markedIds.has(r.id));     // a marked record is listed as marked
  const onRecord = new Set(openalexOnRecord(p));
  if (!linked.length && !held.length && !marked.length) return null;
  const btn = (label, onclick) => h("button", { type: "button", class: "btn btn-small btn-quiet needs-write", onclick }, label);
  const title = (r, extra) => h("div", { class: "wtitle" }, extLink(r.url || `https://openalex.org/${r.id}`, r.id),
    r.name ? ` ${r.name}` : "", extra || "");
  const rows = [
    ...linked.map((r, i) => h("li", null, title(r, i === 0 && linked.length > 1 ? " (main record)" : ""),
      h("div", { class: "wmeta" }, "Linked to them. ", btn("Not theirs", () => notTheirsForm(app, p, r))))),
    ...held.map((r) => h("li", null, title(r),
      h("div", { class: "wmeta" }, `Held back after the lookup by ORCID iD: ${r.reason}. `,
        onRecord.has(r.id) ? "Added to their record; the next library run links it. " :
          [btn("Theirs", () => theirsForm(app, p, r)), " "],
        btn("Not theirs", () => notTheirsForm(app, p, r))))),
    ...marked.map((r) => {
      const text = `Marked as not theirs by ${r.actor || "someone"}${r.date ? ` on ${r.date}` : ""}` +
        `${r.note ? `: ${r.note}` : ""}`;
      return h("li", null, title(r), h("div", { class: "wmeta" }, /[.!?]$/.test(text) ? `${text} ` : `${text}. `,
        btn("Undo", () => undoNotTheirs(app, p, r))));
    }),
  ];
  const counts = [[linked.length, "linked"], [held.length, "held back"], [marked.length, "marked as not theirs"]]
    .filter(([n]) => n).map(([n, what]) => `${n} ${what}`);
  return h("details", { class: "id-records" }, h("summary", null, `OpenAlex records: ${counts.join(", ")}`),
    h("ul", { class: "works" }, rows),
    h("p", { class: "dim" }, "The research library, refresh and expand never link a record marked as not theirs to ",
      "them, even when their record links it. Choose Theirs to add a held-back record's URL to their sources; the next ",
      "library run links it."));
}

async function undoNotTheirs(app, p, r) {
  try {
    await api.send("POST", `/api/contacts/${encodeURIComponent(p.id)}/not-theirs`, { openalexId: r.id, undo: true });
    app.changed("Mark withdrawn; the next library run may link the record again");
  } catch (err) {
    toast(err.message, "error");
  }
}

/** Numbered links to a person's OpenAlex records, the main record first: "OpenAlex records 1, 2". */
function recordLinks(o) {
  const ids = o.ids || [];
  return ["OpenAlex records ", o.urls.map((url, i) => {
    const a = extLink(url, String(i + 1));
    if (a.setAttribute && ids[i]) a.setAttribute("title", `OpenAlex record ${ids[i]}`);
    return [i ? ", " : "", a];
  })];
}

function libraryWorkItem(w, isNew = false) {
  return h("li", null,
    h("div", { class: "wtitle" }, isNew ? h("span", { class: "chip chip-new", title: "Found just now; the next weekly update adds it to the library" }, "New") : null, isNew ? " " : null,
      w.url ? extLink(w.url, w.title) : w.title),
    h("div", { class: "wmeta" }, [w.date || w.year, w.byline].filter(Boolean).join(". ")));
}

function awardItem(a, isNew = false) {
  return h("li", null,
    h("div", { class: "wtitle" }, isNew ? h("span", { class: "chip chip-new", title: "Found just now; the next weekly update adds it to the library" }, "New") : null, isNew ? " " : null,
      a.url ? extLink(a.url, a.title) : a.title),
    h("div", { class: "wmeta" }, [`${a.funder} ${a.number}`, a.ic, a.org, a.start || a.end ? `${a.start || "?"} to ${a.end || "?"}` : ""]
      .filter(Boolean).join(", ")),
    a.part ? h("div", { class: "wmeta" }, "Core or project: " + a.part) : null,
    a.role ? h("div", { class: "wmeta" }, "Role: " + a.role) : null);
}

/**
 * A list from the research library: a heading with a count and a details mark, then the first five items. The live
 * check calls `addNew` with anything newer, which goes at the top marked New, and `tell` with what it checked.
 */
function libraryList(title, stored, render, details) {
  const count = h("span");
  const live = h("span", { class: "note-live" });
  const ul = h("ul", { class: "works" });
  const el = h("div", null, h("h3", { class: "tiegroup" }, title, " (", count, ")", noteMark(details, live)), ul);
  let more = null;
  let total = 0;
  const show = (items) => {                   // the first five, and a button for the rest
    total = items.length;
    count.textContent = String(total);
    items.slice(0, 5).forEach((x) => ul.append(render(x)));
    if (items.length > 5) {
      more = h("button", { type: "button", class: "btn btn-small btn-quiet" }, `Show all ${items.length}`);
      more.addEventListener("click", () => { items.slice(5).forEach((x) => ul.append(render(x))); more.remove(); });
      el.append(more);
    }
    el.hidden = !items.length;
  };
  show(stored);
  return {
    el,
    /** Anything newer than the library's list: at the top, marked New. */
    addNew(items) {
      if (!items.length) return;
      ul.prepend(...items.map((x) => render(x, true)));
      total += items.length;
      count.textContent = stored.length ? `${total}, ${items.length} new` : `${items.length} new`;
      if (more) more.textContent = `Show all ${total}`;
      el.hidden = false;
    },
    /** A list fetched just now for someone the library has none for yet. */
    fill(items) {
      if (!total) show(items);
    },
    tell(text) {
      if (text) live.append(h("span", { class: "note-line" }, text));
    },
  };
}

function worksNote(lib) {
  if (!lib) return "";
  const std = lib.standard || { sinceYear: 2016, maxAuthors: 30, perPerson: 50 };
  const urls = lib.openalex.urls || [];
  return [`From OpenAlex: works since ${std.sinceYear} with ${std.maxAuthors} or fewer authors, newest first, ` +
    `up to ${std.perPerson}.`, lib.openalex.worksAt ? ` Updated ${lib.openalex.worksAt}.` : "",
    urls.length > 1 ? [` Read from their ${urls.length} OpenAlex records; a paper under more than one is listed ` +
      "once. Full lists: ", recordLinks(lib.openalex), "."] :
    lib.openalex.url ? [" Full list: ", extLink(lib.openalex.url, "OpenAlex profile"), "."] : ""];
}

/** What the live check found in one source, for the details of the list it adds to. */
function liveText(src, n, name) {
  if (!src.checked) return src.note;
  if (!n) return `Checked ${name} again just now: nothing newer${src.since ? ` than the update on ${src.since}` : ""}.`;
  return `New: found in ${name} just now${src.since ? `, after the update on ${src.since}` : ""}. ` +
    `The next weekly update adds ${n === 1 ? "it" : "them"} to the library.`;
}

function awardsNote(lib) {
  const parts = [];
  if (lib.nih && lib.nih.status === "matched") parts.push(`NIH RePORTER: ${lib.nih.basis}`);
  if (lib.nsf && lib.nsf.status === "checked") parts.push("NSF: awards with their name, made to an institution on their record");
  return parts.join(". ") + (parts.length ? "." : "") + " Lists awards where they are a principal investigator, not co-investigator roles.";
}

function awardCore(num) {
  const m = String(num || "").toUpperCase().replace(/[^A-Z0-9]/g, "").match(/[A-Z]\d{2}[A-Z]{2}\d{6}/);
  return m ? m[0] : String(num || "").toUpperCase();
}

function paperItem(w) {
  return h("li", null,
    h("div", { class: "wtitle" }, w.url ? extLink(w.url, w.title) : w.title),
    h("div", { class: "wmeta" }, [w.year, w.venue, w.type || w.publicationType, w.publicationStatus].filter(Boolean).join(", ")),
    w.authors ? h("div", { class: "wmeta" }, w.authors.length > 180 ? w.authors.slice(0, 180) + "…" : w.authors) : null,
    w.selectionNote ? h("div", { class: "dim" }, w.selectionNote) : null,
    w.authorshipEvidence ? h("div", { class: "dim" }, "Authorship evidence: " + w.authorshipEvidence) : null,
    w.verificationStatus ? h("div", { class: "dim" }, "Verification: " + w.verificationStatus) : null);
}

function grantItem(g) {
  return h("li", null,
    h("div", { class: "wtitle" }, g.url ? extLink(g.url, g.title) : g.title),
    h("div", { class: "wmeta" }, [g.grantNumber || g.projectId, g.funder, g.period].filter(Boolean).join(", ")),
    g.role ? h("div", { class: "wmeta" }, "Role: " + g.role) : h("div", { class: "wmeta" }, missing("Role not recorded")),
    g.status ? h("div", { class: "dim" }, g.status) : null,
    g.verificationMethod ? h("div", { class: "dim" }, g.verificationMethod) : null);
}

function conferenceItem(x) {
  const as = `Listed as ${x.programName}${x.programInstitution ? `, ${x.programInstitution}` : ""}; ` +
    (x.basis === "name" ? "matched by name only" : `${x.basis} match`);
  return h("li", null,
    h("div", { class: "wtitle" }, [x.role, x.title].filter(Boolean).join(": ") || "On the program"),
    h("div", { class: "wmeta" }, [x.conference, x.session, x.when, x.location].filter(Boolean).join(", "),
      x.sourceUrl ? [" · ", extLink(x.sourceUrl, "program")] : null),
    h("div", { class: "dim" }, as));
}

function listWithMore(items, n, render, cls) {
  const frag = document.createDocumentFragment();
  const ul = h("ul", { class: cls });
  items.slice(0, n).forEach((x) => ul.append(render(x)));
  frag.append(ul);
  if (items.length > n) {
    const more = h("button", { type: "button", class: "btn btn-small btn-quiet" }, `Show all ${items.length}`);
    more.addEventListener("click", () => { items.slice(n).forEach((x) => ul.append(render(x))); more.remove(); });
    frag.append(more);
  }
  return frag;
}

export function noteCard(m, app, { showPerson = false } = {}) {
  const followDone = m.followUpStatus === "done";
  const card = h("article", { class: "note-card" },
    h("h3", null, showPerson ? h("button", { type: "button", class: "linklike", onclick: () => go({ sel: m.contactId }) }, m.contactName) : "Met by " + m.metBy),
    h("div", { class: "who" }, showPerson ? `Met by ${m.metBy}. ` : "", m.date ? `Date ${m.date}. ` : "Encounter date not recorded. ",
      m.recordedBy ? `Recorded by ${m.recordedBy}.` : "Recorder not recorded."),
    h("dl", null,
      h("dt", null, "How we met"), h("dd", null, m.context || missing()),
      h("dt", null, "What we discussed"), h("dd", null, m.learning || missing()),
      h("dt", null, "Follow-up"), h("dd", null, m.followUp ? [m.followUp, followDone ? " (done)" : ""] : missing("None recorded"))),
    h("div", { class: "note-actions needs-write" },
      h("button", { type: "button", class: "btn btn-small", onclick: () => noteForm(app, { note: m }) }, "Edit note"),
      m.followUp ? h("button", { type: "button", class: "btn btn-small btn-quiet", onclick: async () => {
        try {
          await api.send("PATCH", `/api/notes/${encodeURIComponent(m.id)}`, { set: { followUpStatus: followDone ? "open" : "done" } });
          app.changed(followDone ? "Follow-up reopened" : "Follow-up marked done");
        } catch (err) {
          toast(err.message, "error");
        }
      } }, followDone ? "Reopen follow-up" : "Mark follow-up done") : null),
    m.editedFields && m.editedFields.length ? h("div", { class: "dim" }, `Edited fields: ${m.editedFields.join(", ")}.`,
      noteMark("Earlier values are in the change history.")) : null);
  return card;
}

function historyList(history, packetValues) {
  const ul = h("ul", { class: "history" });
  for (const ev of [...history].reverse()) {
    const what = VERBS[ev.op.split(".")[1]] || ev.op;
    const desc = (ev.changes || []).map((c) => c.added !== undefined ? `${c.field}: ${c.added} added` :
      `${c.field}: “${short(c.from)}” to “${short(c.to)}”`).join("; ");
    ul.append(h("li", null, `${fmtTs(ev.ts)} ${ev.actor || "Someone"} ${what}${desc ? ": " + desc : ""}` +
      (ev.batch ? ` (import ${ev.batch})` : "")));
  }
  const fields = Object.keys(packetValues || {});
  if (fields.length) {
    ul.append(h("li", null, "Original packet values: " + fields.map((f) => `${f} “${short(packetValues[f])}”`).join("; ")));
  }
  return ul;
}

const VERBS = { create: "created this record", update: "edited", append: "added", archive: "archived",
  restore: "restored", alias: "linked an imported ID", meta: "changed follow-up status" };

function short(v) {
  if (v === null || v === undefined || v === "") return "empty";
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > 80 ? s.slice(0, 78) + "…" : s;
}

function yearOf(y) {
  const m = String(y || "").match(/\d{4}/);
  return m ? Number(m[0]) : 0;
}

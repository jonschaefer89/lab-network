// Dialogs for adding and editing contacts, notes and relationships, plus a person picker.

import * as api from "../api.js";
import { go } from "../state.js";
import { clear, errorList, field, h, modal, rootForName, toast } from "../ui.js";

// The local calendar date, as the server's today() gives it (toISOString alone gives the UTC date).
const today = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);

function norm(s) {
  return (s || "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase();
}

/** In the lab's shared read-only copy, say where changes go instead of opening a form. */
function readOnly(app) {
  const shared = app.boot && app.boot.sharedCopy;
  if (!shared) return false;
  const ok = h("button", { type: "button", class: "btn btn-primary" }, "OK");
  const m = modal({ title: "This copy is read-only", lede: shared.howToAdd ||
    "Changes are made in the lab's own copy of Lab Network, which updates this one.", body: null, actions: [ok] });
  ok.addEventListener("click", () => m.close());
  return true;
}

/** Text input with a dropdown of matching people. `onPick(row)` fires on selection. */
export function personPicker(app, { value = "", placeholder = "Type a name", onPick, allowLab = false, id, onOutside } = {}) {
  const input = h("input", { type: "text", placeholder, autocomplete: "off", role: "combobox", "aria-autocomplete": "list",
    "aria-expanded": "false", id });
  const list = h("ul", { class: "picker-list", role: "listbox", hidden: true });
  const wrap = h("div", { class: "picker" }, input, list);
  let rows = [];
  let active = -1;
  wrap.value = value;
  if (value === "lab") input.value = "The lab (any of its members)";
  else if (value) input.value = app.nameOf(value);
  const choose = (row) => {
    wrap.value = row.id;
    input.value = row.label || row.name;
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    if (onPick) onPick(row);
  };
  const render = async () => {
    const all = await app.peopleIndex();
    const q = norm(input.value.trim());
    rows = [];
    if (allowLab && (!q || "the lab".includes(q) || "lab".startsWith(q))) rows.push({ id: "lab", name: "The lab", label: "The lab (any of its members)", sub: "Start from every lab member" });
    for (const r of all) {
      if (!q || norm(r.name).includes(q) || norm(r.institution).includes(q) || r.id.includes(q)) {
        rows.push({ id: r.id, name: r.name, sub: [r.role, r.institution].filter(Boolean).join(", ") || "Position and institution not recorded" });
        if (rows.length >= 12) break;
      }
    }
    // Someone the notebook does not hold can still be looked up, when the page offers it.
    const typed = input.value.trim();
    if (onOutside && typed.length >= 3 && rows.length < 3) {
      rows.push({ id: "", outside: typed, name: `Look up “${typed}” in OpenAlex`,
        sub: "Not in the network: find who in it has published with them" });
    }
    clear(list);
    rows.forEach((r, i) => list.append(h("li", { role: "option", "aria-selected": String(i === active),
      onmousedown: (e) => { e.preventDefault(); r.outside ? pickOutside(r) : choose(r); } }, r.label || r.name, h("small", null, r.sub))));
    list.hidden = !rows.length;
    input.setAttribute("aria-expanded", String(!list.hidden));
  };
  const pickOutside = (row) => {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    onOutside(row.outside);
  };
  input.addEventListener("input", () => { wrap.value = ""; active = -1; render(); });
  input.addEventListener("focus", () => { if (!wrap.value) render(); });
  input.addEventListener("click", () => { if (list.hidden) render(); });
  input.addEventListener("blur", () => setTimeout(() => { list.hidden = true; }, 120));
  input.addEventListener("keydown", (e) => {
    if (list.hidden) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      active = Math.max(0, Math.min(rows.length - 1, active + (e.key === "ArrowDown" ? 1 : -1)));
      [...list.children].forEach((li, i) => li.setAttribute("aria-selected", String(i === active)));
    } else if (e.key === "Enter" && active >= 0) {
      e.preventDefault();
      rows[active].outside ? pickOutside(rows[active]) : choose(rows[active]);
    }
  });
  wrap.input = input;
  return wrap;
}

function input(type, value = "", attrs = {}) {
  return h("input", { type, value: value || "", ...attrs });
}

function select(options, value) {
  const s = h("select", null, options.map(([v, label]) => h("option", { value: v }, label)));
  s.value = value || options[0][0];
  return s;
}

function area(value = "", rows = 3) {
  const t = h("textarea", { rows: String(rows) });
  t.value = value || "";
  return t;
}

async function submitWith(dialog, errorsEl, fn) {
  const buttons = dialog.el.querySelectorAll("footer button");
  buttons.forEach((b) => { b.disabled = true; });
  clear(errorsEl);
  try {
    await fn();
    return true;
  } catch (err) {
    errorsEl.append(errorList(err));
    errorsEl.scrollIntoView({ block: "nearest" });
    return false;
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
}

// ---------------------------------------------------------------------- contacts

const CONTACT_FIELDS = ["name", "role", "institution", "labName", "email", "website", "profileSourceUrl",
  "profileConfidence", "researchSummary", "researchSourceUrl", "photoUrl", "photoSourceUrl", "photoAttribution",
  "photoConfidence", "doctorateStatus", "doctorateType", "doctorateEvidence", "doctorateSourceUrl", "doctorateCheckedAt",
  "tags", "notes"];

export function contactForm(app, person = null) {
  if (readOnly(app)) return;
  const c = person ? person.record : {};
  const f = {
    name: input("text", c.name, { required: true }),
    role: input("text", c.role),
    institution: input("text", c.institution),
    labName: input("text", c.labName || ""),
    email: input("email", c.email),
    website: input("url", c.website, { placeholder: "https://" }),
    profileSourceUrl: input("url", c.profileSourceUrl, { placeholder: "https://" }),
    profileConfidence: select([["", "Not set"], ["confirmed", "Confirmed"], ["uncertain", "Uncertain"], ["unverified", "Unverified"]], c.profileConfidence || ""),
    researchSummary: area(c.researchSummary, 3),
    researchSourceUrl: input("url", c.researchSourceUrl, { placeholder: "https://" }),
    photoUrl: input("url", c.photoUrl, { placeholder: "https:// link to the image file" }),
    photoSourceUrl: input("url", c.photoSourceUrl, { placeholder: "https:// page that shows who this is" }),
    photoAttribution: input("text", c.photoAttribution),
    photoConfidence: select([["unverified", "Unverified"], ["uncertain", "Uncertain"], ["confirmed", "Confirmed"]], c.photoConfidence || "unverified"),
    doctorateStatus: select([["", "Not classified"], ["confirmed", "Confirmed"], ["not-yet-earned", "Not yet earned"], ["unknown", "Unknown"]], c.doctorateStatus || ""),
    doctorateType: input("text", c.doctorateType, { placeholder: "PhD, MD, PsyD…" }),
    doctorateEvidence: area(c.doctorateEvidence, 2),
    doctorateSourceUrl: input("url", c.doctorateSourceUrl, { placeholder: "https://" }),
    doctorateCheckedAt: input("date", c.doctorateCheckedAt),
    tags: input("text", (c.tags || []).join(", ")),
    notes: area(c.notes, 3),
  };
  const errorsEl = h("div");
  const body = h("div", null, errorsEl,
    person && person.origin === "packet" ? h("p", { class: "notice notice-quiet" },
      "This person comes from the original packet. Your edits are stored separately and the packet values stay in the history.") : null,
    h("div", { class: "form-grid" },
      field("Name", f.name, { wide: true }),
      field("Current position", f.role),
      field("Current institution", f.institution),
      field("Lab", f.labName),
      field("Email", f.email),
      field("Website", f.website),
      field("Profile page", f.profileSourceUrl, { hint: "The page that confirms who this is" }),
      field("Profile confidence", f.profileConfidence),
      field("Research summary", f.researchSummary, { wide: true }),
      field("Research summary source", f.researchSourceUrl, { wide: true, hint: "Required when there is a research summary" }),
      h("div", { class: "fieldset-title" }, "Photo"),
      field("Photo image URL", f.photoUrl),
      field("Photo source page", f.photoSourceUrl, { hint: "Required for a confirmed photo" }),
      field("Photo credit", f.photoAttribution),
      field("Photo confidence", f.photoConfidence),
      h("div", { class: "fieldset-title" }, "Doctorate"),
      field("Doctorate status", f.doctorateStatus, { hint: "Confirmed or not yet earned needs evidence, a source and a date" }),
      field("Degree", f.doctorateType),
      field("Evidence", f.doctorateEvidence, { wide: true }),
      field("Source", f.doctorateSourceUrl),
      field("Checked on", f.doctorateCheckedAt),
      field("Tags", f.tags, { wide: true, hint: "Separate with commas" }),
      field("Notes", f.notes, { wide: true, hint: "Private professional context" })));
  const values = () => {
    const out = {};
    for (const k of CONTACT_FIELDS) {
      const el = f[k];
      out[k] = k === "tags" ? el.value.split(",").map((t) => t.trim()).filter(Boolean) : el.value.trim();
    }
    return out;
  };
  const original = (() => {
    const out = {};
    for (const k of CONTACT_FIELDS) out[k] = k === "tags" ? (c.tags || []) : (c[k] ?? "");
    return out;
  })();
  let confirmNotDuplicate = false;
  const save = h("button", { type: "button", class: "btn btn-primary" }, person ? "Save changes" : "Add contact");
  const dlg = modal({
    title: person ? `Edit ${c.name}` : "Add a contact",
    lede: person ? "Only changed fields are saved." : "Add someone the lab knows. You can add a meeting note next.",
    body,
    actions: [h("button", { type: "button", class: "btn", onclick: () => dlg.close() }, "Cancel"), save],
  });
  save.addEventListener("click", () => submitWith(dlg, errorsEl, async () => {
    const v = values();
    const today_ = today();
    if (v.researchSummary !== original.researchSummary || v.researchSourceUrl !== original.researchSourceUrl) v.researchCheckedAt = v.researchSummary ? today_ : "";
    if (v.profileSourceUrl !== original.profileSourceUrl) v.profileCheckedAt = v.profileSourceUrl ? today_ : "";
    if (v.photoUrl !== original.photoUrl || v.photoSourceUrl !== original.photoSourceUrl) v.photoCheckedAt = v.photoUrl ? today_ : "";
    if (person) {
      const set = {}, expected = {};
      for (const [k, val] of Object.entries(v)) {
        const orig = k in original ? original[k] : (c[k] ?? "");
        if (JSON.stringify(val) !== JSON.stringify(orig)) {
          set[k] = val === "" ? null : val;
          if (k in original) expected[k] = c[k] === undefined ? null : c[k];
        }
      }
      for (const k of Object.keys(expected)) if (expected[k] === null) delete expected[k];
      if (!Object.keys(set).length) { dlg.close(); return; }
      await api.send("PATCH", `/api/contacts/${encodeURIComponent(person.id)}`, { set, expected });
      dlg.close();
      app.changed("Changes saved");
    } else {
      const fields = {};
      for (const [k, val] of Object.entries(v)) if (val !== "" && !(Array.isArray(val) && !val.length)) fields[k] = val;
      if (!fields.photoUrl) delete fields.photoConfidence;
      try {
        const res = await api.send("POST", "/api/contacts", { fields, confirmNotDuplicate });
        dlg.close();
        app.changed("Contact added");
        go({ sel: res.id });
      } catch (err) {
        if (err.status === 409 && err.details && err.details.possibleDuplicates) {
          confirmNotDuplicate = true;
          save.textContent = "Add as a different person";
          const e = new Error("This may be someone already in the notebook. Open them, or press the button again to add a new person.");
          e.details = err.details.possibleDuplicates.map((d) => `${d.name}${d.institution ? ", " + d.institution : ""} (${d.reasons.join(", ")}) — ID ${d.id}`);
          throw e;
        }
        throw err;
      }
    }
  }));
}

// ---------------------------------------------------------------------- notes

export async function noteForm(app, { contactId = "", note = null } = {}) {
  if (readOnly(app)) return;
  const boot = app.boot;
  const roots = boot.lab.roots;
  const editing = !!note;
  const picker = personPicker(app, { value: note ? note.contactId : contactId, placeholder: "Who did you meet?" });
  const metOptions = [...roots.map((r) => [r.id, r.name]), ["other", "Someone else"]];
  const actorRoot = rootForName(roots, app.actor);
  const noteRoot = note ? rootForName(roots, note.metBy) : null;
  // An attribution that is not exactly a lab member's name stays as written (for example a fuller name).
  const exactRoot = noteRoot && noteRoot.name === note.metBy ? noteRoot : null;
  const metSel = select(metOptions, note ? (exactRoot ? exactRoot.id : "other") : (actorRoot ? actorRoot.id : roots[0] ? roots[0].id : "other"));
  const metOther = input("text", note && !exactRoot ? note.metBy : "", { placeholder: "Their name" });
  const date = input("date", note ? note.date : "");
  const context = area(note ? note.context : "", 2);
  const learning = area(note ? note.learning : "", 3);
  const followUp = area(note ? note.followUp : "", 2);
  const recordedBy = input("text", note ? note.recordedBy : app.actor || "");
  const tieBox = h("input", { type: "checkbox", checked: !editing });
  const tieRow = h("label", { class: "check" }, tieBox, "Also record that they met, as a relationship on the map");
  const syncOther = () => {
    metOther.parentElement.hidden = metSel.value !== "other";
    tieRow.hidden = editing || metSel.value === "other";
  };
  const errorsEl = h("div");
  const body = h("div", null, errorsEl, h("div", { class: "form-grid" },
    field("Person", picker, { wide: true }),
    field("Who met them", metSel),
    field("Their name", metOther),
    field("Date", date, { hint: "Leave empty if you are not sure" }),
    field("Recorded by", recordedBy),
    field("How you met", context, { wide: true, hint: "Where, at what event, through whom" }),
    field("What you discussed", learning, { wide: true }),
    field("Follow-up", followUp, { wide: true, hint: "Anything to send, ask or schedule" }),
    h("div", { class: "field wide" }, tieRow)));
  const save = h("button", { type: "button", class: "btn btn-primary" }, editing ? "Save note" : "Add note");
  const dlg = modal({
    title: editing ? "Edit note" : "Add a meeting note",
    lede: editing ? "Earlier wording stays in the change history." : "Whom you met, how, what you discussed and what to do next.",
    body,
    actions: [h("button", { type: "button", class: "btn", onclick: () => dlg.close() }, "Cancel"), save],
  });
  metSel.addEventListener("change", syncOther);
  syncOther();
  save.addEventListener("click", () => submitWith(dlg, errorsEl, async () => {
    const cid = picker.value;
    if (!cid) throw new Error("Choose the person from the list (type part of their name).");
    const metName = metSel.value === "other" ? metOther.value.trim() : roots.find((r) => r.id === metSel.value).name;
    const fields = { contactId: cid, metBy: metName, date: date.value, context: context.value.trim(),
      learning: learning.value.trim(), followUp: followUp.value.trim(), recordedBy: recordedBy.value.trim() };
    if (editing) {
      const set = {}, expected = {};
      for (const [k, v] of Object.entries(fields)) {
        if ((note[k] ?? "") !== v) { set[k] = v; expected[k] = note[k] ?? ""; }
      }
      if (!Object.keys(set).length) { dlg.close(); return; }
      await api.send("PATCH", `/api/notes/${encodeURIComponent(note.id)}`, { set, expected });
      dlg.close();
      app.changed("Note saved");
    } else {
      const payload = { fields };
      if (tieBox.checked && metSel.value !== "other") payload.tie = { from: metSel.value };
      await api.send("POST", "/api/notes", payload);
      dlg.close();
      app.changed("Note added");
      go({ sel: cid });
    }
  }));
}

// ---------------------------------------------------------------------- relationships

const TYPE_OPTIONS = [
  ["coauthor", "Coauthors (mutual)"],
  ["collaborator", "Collaborators (mutual)"],
  ["grant", "Shared grant (mutual)"],
  ["mentor", "Mentor: first person mentored the second"],
  ["postdoc", "Postdoc: first person was a postdoc with the second"],
  ["committee", "Committee: first person served on the second's committee"],
  ["training", "Training program connection"],
  ["labmate", "Lab or center colleagues (mutual)"],
  ["irta_colleague", "NIH IRTA colleagues (mutual)"],
  ["met", "Met (mutual)"],
  ["event", "Same event (mutual)"],
];

export function tieForm(app, { from = "", to = "" } = {}) {
  if (readOnly(app)) return;
  const pFrom = personPicker(app, { value: from, placeholder: "First person" });
  const pTo = personPicker(app, { value: to, placeholder: "Second person" });
  const type = select(TYPE_OPTIONS, "collaborator");
  const evidence = area("", 3);
  const prov = select([["firsthand", "Firsthand: you saw or took part in it"], ["reported", "Reported: someone told you"],
    ["public", "Public record: a page or document shows it"]], "firsthand");
  const sourceTitle = input("text", "");
  const sourceUrl = input("url", "", { placeholder: "https:// (required for a public record)" });
  const conf = select([["confirmed", "Confirmed"], ["likely", "Likely"], ["unverified", "Unverified"]], "confirmed");
  const status = select([["reviewed", "Reviewed: count it on the map"], ["proposed", "Proposed: keep it, do not count it yet"]], "reviewed");
  const label = input("text", "", { placeholder: "For example: Dissertation committee chair" });
  const start = input("text", "", { placeholder: "2019" });
  const end = input("text", "", { placeholder: "2023" });
  const relStatus = select([["", "Not stated"], ["current", "Current"], ["historical", "Historical"], ["unknown", "Unknown"]], "");
  const errorsEl = h("div");
  const body = h("div", null, errorsEl, h("div", { class: "form-grid" },
    field("First person", pFrom), field("Second person", pTo),
    field("Relationship", type, { wide: true, hint: "Direction matters only for mentor, postdoc and committee" }),
    field("Evidence", evidence, { wide: true, hint: "What shows these two people are connected. Sharing an institution is not enough." }),
    field("Kind of evidence", prov), field("Confidence", conf),
    field("Source title", sourceTitle), field("Source link", sourceUrl),
    field("Label", label, { wide: true }),
    field("Start", start), field("End", end), field("Status", relStatus), field("Review status", status)));
  const save = h("button", { type: "button", class: "btn btn-primary" }, "Add relationship");
  let confirmNotDuplicate = false;
  const dlg = modal({ title: "Add a relationship", lede: "Recorded with its evidence, so it can be checked later.", body,
    actions: [h("button", { type: "button", class: "btn", onclick: () => dlg.close() }, "Cancel"), save] });
  save.addEventListener("click", () => submitWith(dlg, errorsEl, async () => {
    if (!pFrom.value || !pTo.value) throw new Error("Choose both people from the lists.");
    const fields = { from: pFrom.value, to: pTo.value, type: type.value, evidence: evidence.value.trim(), provenance: prov.value,
      confidence: conf.value, status: status.value, sourceTitle: sourceTitle.value.trim(), sourceUrl: sourceUrl.value.trim(),
      relationshipLabel: label.value.trim(), relationshipStart: start.value.trim(), relationshipEnd: end.value.trim(),
      relationshipStatus: relStatus.value };
    try {
      await api.send("POST", "/api/ties", { fields, confirmNotDuplicate });
    } catch (err) {
      if (err.status === 409) {
        confirmNotDuplicate = true;
        save.textContent = "Add it anyway";
        throw new Error("A record with the same people, type, source and evidence already exists. Press the button again only if this is a separate fact.");
      }
      throw err;
    }
    dlg.close();
    app.changed("Relationship added");
  }));
}

// ---------------------------------------------------------------------- identities

/** Mark an OpenAlex author record ({id, name}) as not this person's. */
export function notTheirsForm(app, person, rec) {
  if (readOnly(app)) return;
  const note = area("", 2);
  note.placeholder = "For example: her husband's record";
  const errorsEl = h("div");
  const save = h("button", { type: "button", class: "btn btn-primary" }, "Mark as not theirs");
  const dlg = modal({
    title: "Not this person's OpenAlex record",
    lede: `OpenAlex record ${rec.id}${rec.name ? ` (${rec.name})` : ""} will never be linked to ${person.record.name}, ` +
      "even if their record links it. If the research library holds it for them now, it leaves their profile at once, " +
      "and the next library run reads their summary and papers again without it.",
    body: h("div", null, errorsEl, field("Why (optional)", note, { wide: true, hint: "Kept with the mark in the change log" })),
    actions: [h("button", { type: "button", class: "btn", onclick: () => dlg.close() }, "Cancel"), save],
  });
  save.addEventListener("click", () => submitWith(dlg, errorsEl, async () => {
    await api.send("POST", `/api/contacts/${encodeURIComponent(person.id)}/not-theirs`,
      { openalexId: rec.id, name: rec.name || "", note: note.value.trim() });
    dlg.close();
    app.changed("Marked as not theirs");
  }));
}

/** The OpenAlex author records a person's sources link, in order, without the ones marked as not theirs. */
export function openalexOnRecord(person) {
  const marked = new Set((person.openalexNotTheirs || []).map((m) => m.id));
  const ids = [];
  for (const s of person.record.sources || []) {
    for (const [, id] of String(s.url || "").matchAll(/openalex\.org\/(A\d+)/g)) {
      if (!ids.includes(id) && !marked.has(id)) ids.push(id);
    }
  }
  return ids;
}

/**
 * Confirm that an OpenAlex author record held back after the lookup by ORCID iD ({id, name, reason}) is this person's:
 * its URL goes in their sources, and the next library run links it. Once a person's sources link an OpenAlex record,
 * the library links them to those records alone (with the ones that share their ORCID iD), so when their sources link
 * none yet, the records the library linked to them go in first, the main record first.
 */
export function theirsForm(app, person, rec) {
  if (readOnly(app)) return;
  const o = person.library.openalex;
  const date = today();
  const keep = openalexOnRecord(person).length ? [] :
    (o.status === "linked" ? o.ids || [] : []).filter((id) => id !== rec.id);
  const note = `Another OpenAlex record for the same person, confirmed on the profile page on ${date}: ` +
    `${rec.name ? `${rec.name}, ` : ""}held back after the lookup by ORCID iD${rec.reason ? ` (${rec.reason})` : ""}.`;
  const sources = [
    ...keep.map((id, i) => ({ url: `https://openalex.org/${id}`,
      note: `OpenAlex record the research library linked to them (${o.basis || "linked"}), added on the profile page ` +
        `on ${date} so that it stays ${i ? "linked" : "their main record"} when ${rec.id} is added.` })),
    { url: `https://openalex.org/${rec.id}`, note },
  ];
  const errorsEl = h("div");
  const save = h("button", { type: "button", class: "btn btn-primary" }, "Add to their record");
  const dlg = modal({
    title: "Another OpenAlex record for this person",
    lede: `OpenAlex record ${rec.id}${rec.name ? ` (${rec.name})` : ""} goes in ${person.record.name}'s sources, and ` +
      "the next library run links it to them and reads their summary and papers again. A source cannot be taken off " +
      "a record, so if the record turns out not to be theirs, mark it as not theirs.",
    body: h("div", null, errorsEl,
      keep.length ? h("p", { class: "notice notice-quiet" }, "Their sources link no OpenAlex record yet. Once they " +
        "link one, the library uses only those links, so the records it linked to them go in first: ",
        keep.map((id, i) => [i ? ", " : "", id, i ? "" : " (main record)"]), ".") : null,
      h("p", { class: "dim" }, `Source note: ${note}`)),
    actions: [h("button", { type: "button", class: "btn", onclick: () => dlg.close() }, "Cancel"), save],
  });
  const added = new Set();           // if one request fails, pressing the button again sends only the rest
  save.addEventListener("click", () => submitWith(dlg, errorsEl, async () => {
    for (const s of sources) {
      if (added.has(s.url)) continue;
      await api.send("POST", `/api/contacts/${encodeURIComponent(person.id)}/sources`, s);
      added.add(s.url);
    }
    dlg.close();
    app.changed("Added to their record; the next library run links it");
  }));
}

export function editTieForm(app, r) {
  if (readOnly(app)) return;
  const evidence = area(r.evidence, 4);
  const sourceTitle = input("text", r.sourceTitle);
  const sourceUrl = input("url", r.sourceUrl);
  const prov = select([["public", "Public record"], ["reported", "Reported"], ["firsthand", "Firsthand"]], r.provenance);
  const conf = select([["confirmed", "Confirmed"], ["likely", "Likely"], ["unverified", "Unverified"]], r.confidence);
  const status = select([["reviewed", "Reviewed: counted"], ["proposed", "Proposed: not counted"], ["rejected", "Rejected: kept, never counted"]], r.reviewStatus);
  const label = input("text", r.label);
  const start = input("text", r.relationshipStart || "");
  const end = input("text", r.relationshipEnd || "");
  const relStatus = select([["", "Not stated"], ["current", "Current"], ["historical", "Historical"], ["unknown", "Unknown"]], r.relationshipStatus);
  const errorsEl = h("div");
  const body = h("div", null, errorsEl,
    h("p", { class: "dim" }, r.sentence + ". People and type cannot change; add a new record if they are wrong, and mark this one rejected."),
    h("div", { class: "form-grid" },
      field("Evidence", evidence, { wide: true }), field("Kind of evidence", prov), field("Confidence", conf),
      field("Source title", sourceTitle), field("Source link", sourceUrl), field("Label", label, { wide: true }),
      field("Start", start), field("End", end), field("Status", relStatus), field("Review status", status)));
  const save = h("button", { type: "button", class: "btn btn-primary" }, "Save record");
  const dlg = modal({ title: "Edit relationship record", lede: `ID ${r.id}. Earlier values stay in the change history.`, body,
    actions: [h("button", { type: "button", class: "btn", onclick: () => dlg.close() }, "Cancel"), save] });
  save.addEventListener("click", () => submitWith(dlg, errorsEl, async () => {
    const now = { evidence: evidence.value.trim(), sourceTitle: sourceTitle.value.trim(), sourceUrl: sourceUrl.value.trim(),
      provenance: prov.value, confidence: conf.value, status: status.value, relationshipLabel: label.value.trim(),
      relationshipStatus: relStatus.value };
    const before = { evidence: r.evidence, sourceTitle: r.sourceTitle, sourceUrl: r.sourceUrl, provenance: r.provenance,
      confidence: r.confidence, status: r.reviewStatus, relationshipLabel: r.label, relationshipStatus: r.relationshipStatus };
    const set = {}, expected = {};
    for (const [k, v] of Object.entries(now)) if ((before[k] || "") !== v) { set[k] = v === "" ? null : v; expected[k] = before[k]; }
    if (start.value.trim() !== (r.relationshipStart || "")) set.relationshipStart = start.value.trim();
    if (end.value.trim() !== (r.relationshipEnd || "")) set.relationshipEnd = end.value.trim();
    if (!Object.keys(set).length) { dlg.close(); return; }
    for (const k of Object.keys(expected)) if (expected[k] === undefined || expected[k] === "") delete expected[k];
    await api.send("PATCH", `/api/ties/${encodeURIComponent(r.id)}`, { set, expected });
    dlg.close();
    app.changed("Relationship record saved");
  }));
}

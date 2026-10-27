// Data view: import with preview, export, conflicts, change history and data health.

import * as api from "../api.js";
import { go, on, state } from "../state.js";
import { append, chip, clear, extLink, fmtTs, h, noteMark, toast } from "../ui.js";

const TABS = [["import", "Import"], ["inbox", "Slack inbox"], ["export", "Export"], ["conflicts", "Conflicts"],
  ["updates", "Updates"], ["history", "Change history"], ["health", "Data health"]];
// The lab's shared read-only copy shows only what reading needs; it carries no change history.
const SHARED_TABS = ["updates", "health"];

const STATUS_TEXT = {
  new: "New", update: "Adds missing values", conflict: "Conflicts with current values", unchanged: "Already here, no change",
  duplicate: "Duplicate of an existing record", "possible-duplicate": "Possibly someone already here",
  "id-collision": "ID already used by a different record", rejected: "Rejected",
};

export class DataView {
  constructor(el, app) {
    this.el = el;
    this.app = app;
    this.preview = null;
    on("changed", () => { if (state.view === "data" && this.tab !== "import") this.show(); });
  }

  async show() {
    const shared = !!(this.app.boot && this.app.boot.sharedCopy);
    const tabs = shared ? TABS.filter(([t]) => SHARED_TABS.includes(t)) : TABS;
    this.tab = tabs.some(([t]) => t === state.tab) ? state.tab : tabs[0][0];
    clear(this.el);
    this._people = null;                 // the people list may have changed since the last visit
    let waiting = 0;
    try {
      const v = shared ? null : await api.get("/api/intake");
      waiting = v ? (v.counts.held || 0) + (v.counts.pending || 0) : 0;
    } catch (err) {
      waiting = 0;
    }
    const inner = h("div", { class: "page-inner" },
      h("h1", { class: "page-title" }, "Data", noteMark("The original packet is never changed. Every edit and import is " +
        "stored as a separate, dated change, so you can export everything and rebuild it elsewhere.")),
      h("div", { class: "subnav", role: "group", "aria-label": "Data sections" },
        tabs.map(([t, label]) => h("button", { type: "button", "aria-pressed": String(t === this.tab),
          onclick: () => go({ view: "data", tab: t }) }, t === "inbox" && waiting ? `${label} (${waiting})` : label))));
    this.body = h("div");
    inner.append(this.body);
    this.el.append(inner);
    await this[this.tab]();
  }

  // ---------------------------------------------------------------- import

  async import() {
    const file = h("input", { type: "file", accept: ".csv,.json,text/csv,application/json", "aria-label": "File to import" });
    const out = h("div", { "aria-live": "polite" });
    file.addEventListener("change", async () => {
      const f = file.files[0];
      if (!f) return;
      clear(out).append(h("p", { class: "dim" }, `Reading ${f.name}…`));
      try {
        const content = await f.text();
        this.preview = await api.send("POST", "/api/import/preview", { filename: f.name, content });
        this.renderPreview(out);
      } catch (err) {
        clear(out).append(h("p", { class: "notice notice-error" }, err.message));
      }
    });
    this.body.append(
      h("div", { class: "panel" }, h("h2", null, "Import a file"),
        h("p", null, "Choose a Lab Network export (.json), a notebook in the packet format (.json), or a contacts, notes or relationships " +
          "spreadsheet saved as CSV.", noteMark("Nothing changes until you review the preview and press Import.")),
        file,
        h("p", { class: "dim" }, "CSV templates: ",
          h("a", { href: "/api/templates/contacts.csv", download: "contacts.csv" }, "contacts"), ", ",
          h("a", { href: "/api/templates/notes.csv", download: "notes.csv" }, "notes"), ", ",
          h("a", { href: "/api/templates/relationships.csv", download: "relationships.csv" }, "relationships"), ".",
          noteMark("Rows with a stable ID update that record; rows without one are matched by name, email and profile URL."))),
      out);
  }

  renderPreview(out) {
    const p = this.preview;
    clear(out);
    const panel = h("div", { class: "panel" }, h("h2", null, `Preview: ${p.file.name}`, noteMark(
      h("span", { class: "note-line" }, p.explain),
      h("span", { class: "note-line" }, "Conflicting values will be kept side by side as open conflicts; your current " +
        "values stay until you choose."))),
      p.alreadyImported.length ? h("p", { class: "notice" }, `This exact file was imported before (${p.alreadyImported.map((b) => fmtTs(b.ts)).join(", ")}). ` +
        "Records already here are shown as unchanged.") : null,
      p.meta && p.meta.ignoredColumns && p.meta.ignoredColumns.length ? h("p", { class: "notice notice-quiet" },
        "Columns not used: " + p.meta.ignoredColumns.join(", ") + ".") : null);
    for (const [coll, counts] of Object.entries(p.summary)) {
      panel.append(h("div", { class: "preview-summary" }, h("strong", null, collName(coll) + ":"),
        Object.entries(counts).map(([s, n]) => chip(`${STATUS_TEXT[s] || s}: ${n}`, "status-" + s))));
    }
    const decisions = {};
    const needs = new Set(p.needsDecision);
    const table = h("table", { class: "table" }, h("thead", null, h("tr", null,
      ["Row", "Record", "Result", "Details"].map((t) => h("th", { scope: "col" }, t)))));
    const tb = h("tbody");
    const interesting = p.items.filter((it) => it.status !== "unchanged");
    for (const it of interesting.slice(0, 400)) tb.append(this.itemRow(it, decisions));
    table.append(tb);
    const unchanged = p.items.length - interesting.length;
    const go_ = h("button", { type: "button", class: "btn btn-primary" }, "Import these changes");
    const status = h("p", { class: "dim" });
    const update = () => {
      const missing = [...needs].filter((k) => !decisions[k]);
      go_.disabled = missing.length > 0;
      status.textContent = missing.length ? `${missing.length} row(s) need your decision before importing.` : "";
    };
    this.onDecision = update;
    go_.addEventListener("click", async () => {
      go_.disabled = true;
      try {
        const res = await api.send("POST", "/api/import/apply", { batchId: p.batchId, decisions });
        this.preview = null;
        clear(out).append(h("div", { class: "panel" }, h("h2", null, "Import finished"),
          h("p", null, summarise(res.applied)),
          res.applied.conflictsOpened ? h("p", null, h("a", { href: "#/data?tab=conflicts" }, "Review the open conflicts")) : null,
          (res.applied.rejectedAtApply || []).length ? h("p", { class: "notice" }, res.applied.rejectedAtApply.map((r) => `${r.key}: ${r.reason}`).join("; ")) : null));
        this.app.changed("Imported");
      } catch (err) {
        go_.disabled = false;
        status.textContent = err.message;
        toast(err.message, "error");
      }
    });
    append(panel, [unchanged ? h("p", { class: "dim" }, `${unchanged} rows match records already here and are not listed.`) : null,
      interesting.length ? table : h("p", null, "Every row is already in the notebook.",
        noteMark("Importing records the attempt and changes nothing.")),
      interesting.length > 400 ? h("p", { class: "dim" }, `Showing the first 400 of ${interesting.length} rows.`) : null,
      h("div", { class: "toolbar" }, go_, h("button", { type: "button", class: "btn", onclick: () => { this.preview = null; clear(out); } }, "Cancel"), status)]);
    out.append(panel);
    update();
  }

  itemRow(it, decisions) {
    const details = h("div");
    if (it.errors && it.errors.length) details.append(h("ul", null, it.errors.map((e) => h("li", null, e))));
    if (it.changes && it.changes.length) {
      details.append(h("ul", null, it.changes.map((c) => h("li", null,
        c.action === "append" ? `${c.field}: ${c.count} new item(s) added` :
          c.action === "fill" ? `${c.field}: empty here, will be set to “${short(c.incoming)}”` :
            c.action === "already-open" ? `${c.field}: same conflict already open` :
              `${c.field}: keeps “${short(c.current)}”, incoming “${short(c.incoming)}” becomes an open conflict`))));
    }
    if (it.conflicts && Object.keys(it.conflicts).length) {
      details.append(h("ul", null, Object.entries(it.conflicts).map(([f, v]) => h("li", null,
        `${f}: keeps “${short(v.current)}”, incoming “${short(v.incoming)}” becomes an open conflict`))));
    }
    if (it.duplicateOf) details.append(h("p", { class: "dim" }, `Matches existing record ${it.duplicateOf}; it will not be added again.`));
    if (it.status === "possible-duplicate") {
      const name = `d-${it.key}`;
      const opts = [];
      for (const m of it.matches || []) {
        if (m.id) opts.push([`merge:${m.id}`, `Same person as ${m.name}${m.institution ? ", " + m.institution : ""} (${m.reasons.join(", ")})`]);
        else opts.push(["", m.reasons.join(", ")]);
      }
      opts.push(["create", "A different person: add as new"], ["skip", "Skip this row"]);
      details.append(h("fieldset", { class: "decision" }, h("legend", null, "Who is this?"),
        opts.filter(([v]) => v).map(([v, label]) => h("label", { class: "check" },
          h("input", { type: "radio", name, value: v, onchange: () => { decisions[it.key] = v; this.onDecision && this.onDecision(); } }), label)),
        opts.filter(([v]) => !v).map(([, label]) => h("p", { class: "dim" }, label))));
    } else if (["new", "update", "conflict"].includes(it.status)) {
      details.append(h("label", { class: "check" }, h("input", { type: "checkbox", onchange: (e) => {
        if (e.target.checked) decisions[it.key] = "skip"; else delete decisions[it.key];
      } }), "Skip this row"));
    }
    const label = it.name || (it.record && (it.record.name || it.record.context || `${it.record.from || ""} ${it.record.type || ""} ${it.record.to || ""}`)) ||
      (it.op ? `${it.op} ${it.id || ""}` : it.id || "");
    return h("tr", null,
      h("td", { class: "num" }, it.row ?? ""),
      h("td", null, h("div", null, label), it.id && it.id !== label ? h("div", { class: "dim" }, it.id) : null),
      h("td", null, chip(STATUS_TEXT[it.status] || it.status, "status-" + it.status)),
      h("td", null, details));
  }

  // ---------------------------------------------------------------- export

  async export() {
    const link = (fmt, label, desc) => h("li", null, h("a", { class: "btn", href: `/api/export?format=${fmt}`, download: "" }, label),
      noteMark(desc));
    this.body.append(h("div", { class: "panel" }, h("h2", null, "Export"),
      h("ul", { class: "works" },
        link("bundle", "Everything (bundle, JSON)", "Current records, archived records, every change with its author and date, and open conflicts. Import it into another copy to reproduce this one exactly."),
        link("notebook", "Notebook (packet format, JSON)", "Current records only, in the same structure as the original packet."),
        link("contacts-csv", "Contacts (CSV)", "One row per person with the computed degree."),
        link("notes-csv", "Notes (CSV)", "One row per meeting note."),
        link("ties-csv", "Relationships (CSV)", "One row per relationship record with its evidence.")),
      h("p", null, h("button", { type: "button", class: "btn btn-small", onclick: async () => {
        const res = await fetch("/api/export?format=bundle&save=1");
        if (res.ok) toast("A bundle was saved in the workspace exports folder");
        else toast("The bundle could not be saved", "error");
      } }, "Also save a bundle in the workspace exports folder"))));
  }

  // ---------------------------------------------------------------- conflicts

  async conflicts() {
    const data = await api.get("/api/conflicts", { status: "open" });
    const panel = h("div", { class: "panel" }, h("h2", null, `Open conflicts (${data.conflicts.length})`,
      noteMark("An import offered a value that differs from the one you hold. Both are kept until you choose. " +
        "Choosing keeps a record of the other value.")));
    if (!data.conflicts.length) panel.append(h("p", { class: "dim" }, "No open conflicts."));
    for (const cf of data.conflicts) {
      const resolve = async (resolution) => {
        try {
          await api.send("POST", `/api/conflicts/${encodeURIComponent(cf.conflictId)}/resolve`, { resolution });
          this.app.changed(resolution === "use-incoming" ? "Incoming value applied" : "Current value kept");
        } catch (err) {
          toast(err.message, "error");
        }
      };
      panel.append(h("div", { class: "result" },
        h("p", { class: "rtitle" }, cf.recordName || cf.recordId, h("span", { class: "dim" }, ` ${cf.collection.slice(0, -1)} field “${cf.field}”`)),
        h("dl", { class: "kv" }, h("dt", null, "Current value"), h("dd", null, short(cf.currentNow ?? cf.current, 400)),
          h("dt", null, "Incoming value"), h("dd", null, short(cf.incoming, 400)),
          h("dt", null, "From"), h("dd", null, `${(cf.source && cf.source.file) || "import"}, ${fmtTs(cf.openedAt)}`)),
        h("div", { class: "toolbar" },
          h("button", { type: "button", class: "btn btn-small", onclick: () => resolve("keep-current") }, "Keep current value"),
          h("button", { type: "button", class: "btn btn-small", onclick: () => resolve("use-incoming") }, "Use incoming value"),
          cf.collection === "contacts" ? h("button", { type: "button", class: "btn btn-small btn-quiet", onclick: () => go({ sel: cf.recordId }) }, "Open person") : null)));
    }
    this.body.append(panel);
  }

  // ---------------------------------------------------------------- Slack inbox

  async inbox() {
    const v = await api.get("/api/intake");
    if (!v.channel) {
      this.body.append(h("div", { class: "panel" }, h("h2", null, "Slack inbox"),
        h("p", null, "Lab members can add people, relationships and meeting notes with a form in a private Slack channel.",
          noteMark("Lab Network reads the channel and adds what they send, under their names. It never posts to Slack.")),
        h("p", null, "To set it up, follow docs/slack-intake.md in the Lab Network folder: create the channel and the form, " +
          "save the bot token, then run:"),
        h("pre", { class: "cmd" }, "python3 labnetwork.py intake <workspace> --setup --channel '#lab-network'")));
      return;
    }
    const waiting = v.items.filter((i) => i.status === "held" || i.status === "pending");
    const added = v.items.filter((i) => i.status === "added");
    this.body.append(this.inboxStatus(v), this.inboxWaiting(waiting), this.inboxAdded(added), inboxMembers(v));
  }

  inboxStatus(v) {
    const auto = h("input", { type: "checkbox", id: "intake-auto" });
    auto.checked = v.mode === "automatic";
    auto.addEventListener("change", async () => {
      try {
        await api.send("POST", "/api/intake/settings", { mode: auto.checked ? "automatic" : "review" });
        toast(auto.checked ? "Submissions will be added automatically" : "Submissions will wait here for you");
      } catch (err) {
        toast(err.message, "error");
      }
      this.show();
    });
    const check = h("button", { type: "button", class: "btn btn-small" }, "Check Slack now");
    check.addEventListener("click", async () => {
      check.disabled = true;
      try {
        const r = await api.send("POST", "/api/intake/check", {});
        const c = r.checked || {};
        toast(`Read ${c.read || 0} new message${c.read === 1 ? "" : "s"}: ${c.added || 0} added, ` +
          `${(c.held || 0) + (c.pending || 0)} waiting.`);
      } catch (err) {
        toast(err.message, "error");
      }
      this.show();
    });
    return h("div", { class: "panel" }, h("h2", null, "Slack inbox"),
      h("p", null, `Lab members add people, relationships and meeting notes with a form in #${v.channelName || v.channel}.`,
        noteMark(h("span", { class: "note-line" }, "Lab Network reads the channel and records each submission under the " +
          "lab member's name. It never posts to Slack."),
        v.formApp ? h("span", { class: "note-line" }, `Forms are read only from the app you trusted (${v.formApp}). ` +
          "To trust a different one, run the intake command with --forget-form.") : null)),
      v.tokenProblem ? h("p", { class: "notice notice-error" }, "It cannot check Slack: " + v.tokenProblem + ".") : null,
      v.lastError ? h("p", { class: "notice notice-error" }, "The last check failed: " + v.lastError) : null,
      h("p", null, h("label", { class: "check", for: "intake-auto" }, auto, " Add lab members' submissions automatically"),
        noteMark("When this is off, each submission waits here until you add or dismiss it. When it is on, a submission " +
          "waits only if its person might be someone already in the notebook, or if it is a plain message instead of " +
          "the form. You can undo any addition below.")),
      h("div", { class: "toolbar" }, h("span", null, check, noteMark(`Last checked ${v.lastCheck ? fmtTs(v.lastCheck) :
        "never"}. While the app is open it checks every ${v.pollMinutes} minutes.`))));
  }

  inboxWaiting(items) {
    const panel = h("div", { class: "panel" }, h("h2", null, `Waiting for a decision (${items.length})`));
    if (!items.length) panel.append(h("p", { class: "dim" }, "Nothing is waiting."));
    for (const i of items) panel.append(this.inboxCard(i));
    return panel;
  }

  inboxCard(i) {
    const card = h("div", { class: "intake-item" },
      h("p", { class: "intake-head" }, h("strong", null, i.memberName || i.submitter || "A lab member"),
        ` sent this ${fmtTs(i.postedAt)}.`, i.permalink ? [" ", extLink(i.permalink, "Open in Slack")] : null));
    card.append(i.kind === "form" ? fieldList(i.fields || {}) : h("p", { class: "intake-text" }, i.text || ""));
    if (i.reason) card.append(h("p", { class: "dim" }, `Waiting because ${i.reason}.`));
    if (i.untrusted) {
      // A form from an app no one has vouched for yet: any channel member can make an app post a message.
      const app = i.app ? `${i.app.name ? `“${i.app.name}”` : "an app"} (Slack app ${i.app.botId || "unknown"})` : "an app";
      const trust = h("button", { type: "button", class: "btn btn-small btn-primary" }, "Trust this form");
      const no = h("button", { type: "button", class: "btn btn-small btn-quiet" }, "Dismiss");
      trust.addEventListener("click", () => this.decide(i.ts, "trust", ""));
      no.addEventListener("click", () => this.decide(i.ts, "dismiss", ""));
      card.append(h("p", null, `It was posted by ${app}. Trust it only if this is the form you made for the lab; ` +
        "after that, forms from any other app are ignored."), h("div", { class: "toolbar" }, trust, no));
      return card;
    }
    const choice = this.personChoice(i);
    const what = `${(i.fields || {}).name || "the message"} from ${i.memberName || i.submitter || "a lab member"}`;
    const add = h("button", { type: "button", class: "btn btn-small btn-primary", "aria-label": `Add ${what}` }, "Add");
    const dismiss = h("button", { type: "button", class: "btn btn-small btn-quiet", "aria-label": `Dismiss ${what}` }, "Dismiss");
    add.addEventListener("click", () => this.decide(i.ts, "add", choice.value()));
    dismiss.addEventListener("click", () => this.decide(i.ts, "dismiss", ""));
    card.append(choice.el, h("div", { class: "toolbar" }, add, dismiss));
    return card;
  }

  personChoice(i) {
    const key = i.ts.replace(".", "-");
    if (i.kind === "form") {
      const opts = (i.candidates || []).map((c) => [c.id, c.name + (c.institution ? `, ${c.institution}` : "")]);
      opts.push(["new", `Someone new: ${(i.fields || {}).name || "no name given"}`]);
      const radios = opts.map(([value, label]) => {
        const r = h("input", { type: "radio", name: `who-${key}`, value });
        if (value === i.suggestion) r.checked = true;
        return h("label", { class: "check intake-choice" }, r, " ", label);
      });
      const el = h("fieldset", { class: "intake-person" }, h("legend", null, "Who is this?"), radios);
      return { el, value: () => (el.querySelector("input:checked") || {}).value || "" };
    }
    const list = h("datalist", { id: `people-${key}` });
    const input = h("input", { type: "text", list: list.id, placeholder: "Start typing a name", "aria-label": "Who the note is about" });
    const byName = {};
    this.peopleList().then((rows) => {
      // Each option names one person: the institution tells namesakes apart, and the ID when even that is shared.
      const label = (p) => p.name + (p.institution ? `, ${p.institution}` : "");
      const seen = {};
      for (const p of rows) seen[label(p)] = (seen[label(p)] || 0) + 1;
      for (const p of rows) {
        const text = seen[label(p)] > 1 ? `${label(p)} (${p.id})` : label(p);
        byName[text] = p.id;
        list.append(h("option", { value: text }));
      }
    });
    return { el: h("div", { class: "intake-person" }, h("label", null, "Who is this note about? ", input), list),
      value: () => byName[input.value.trim()] || "" };
  }

  peopleList() {
    if (!this._people) {
      this._people = api.get("/api/people", { maxDegree: "all", doctorate: "any", identified: "0" })
        .then((r) => (r.rows || []).map((p) => ({ id: p.id, name: p.name, institution: p.institution || "" })))
        .catch(() => []);
    }
    return this._people;
  }

  async decide(ts, action, person) {
    if (action === "add" && !person) {
      toast("Choose who this is first.", "error");
      return;
    }
    const done = { add: "Added", dismiss: "Dismissed", undo: "Undone", trust: "Forms from this app will be read" };
    try {
      // Choosing "Someone new" from the list just shown is a decision to add a separate person.
      await api.send("POST", `/api/intake/items/${ts}`, { action, person, confirmNew: person === "new" });
      toast(done[action]);
    } catch (err) {
      toast(err.message, "error");
    }
    this.show();
  }

  inboxAdded(items) {
    const panel = h("div", { class: "panel" }, h("h2", null, `Added (${items.length})`));
    if (!items.length) {
      panel.append(h("p", { class: "dim" }, "Nothing added yet."));
      return panel;
    }
    panel.append(h("ul", { class: "intake-added" }, items.slice(0, 60).map((i) => {
      const undo = h("button", { type: "button", class: "btn btn-small btn-quiet",
        "aria-label": `Undo the submission about ${i.personName || "someone"} from ${i.memberName || i.submitter}` }, "Undo");
      undo.addEventListener("click", () => this.decide(i.ts, "undo", ""));
      const who = i.personName ? h("button", { type: "button", class: "linklike", onclick: () => go({ view: "people", sel: i.person }) },
        i.personName) : "someone";
      const how = i.decidedBy === "added automatically" ? "added automatically" :
        (i.decidedBy && i.decidedBy !== "someone" ? `added by ${i.decidedBy}` : "added by hand");
      return h("li", null, h("span", null, `${fmtTs(i.decidedAt || i.postedAt)}: `, who,
        `, from ${i.memberName || i.submitter}; ${how}; ${i.createdCount} record${i.createdCount === 1 ? "" : "s"}. `), undo);
    })));
    return panel;
  }

  // ---------------------------------------------------------------- updates

  async updates() {
    const data = await api.get("/api/digests");
    this.body.append(libraryPanel(data.library || {}),
      h("div", { class: "panel" }, h("h2", null, "Expand and refresh", noteMark("Expand and refresh add people, " +
        "relationships and profile papers through the import rules: nothing is overwritten, and a changed affiliation " +
        "becomes a conflict for you to review.")),
        h("p", null, data.lastRefresh ? `Last refresh: ${data.lastRefresh}.` : "No refresh recorded.")));
    for (const d of data.digests) {
      let label;
      if (d.kind === "library") {
        const papers = Object.values(d.newWorks || {}).reduce((n, l) => n + l.length, 0);
        const awards = Object.values(d.newAwards || {}).reduce((n, l) => n + l.length, 0);
        label = `${d.date}: library update, ${papers} new paper${papers === 1 ? "" : "s"} and ` +
          `${awards} new award${awards === 1 ? "" : "s"}`;
      } else if (d.kind === "expand") {
        label = `${d.date}: network expansion`;
      } else {
        label = `${d.date}: refresh, ` + [`${(d.papers || []).length} papers`, `${(d.newPeople || []).length} new people`,
          `${(d.affiliation || []).length} possible moves`, `${(d.grants || []).length} awards`].join(", ");
      }
      this.body.append(h("details", { class: "panel" }, h("summary", null, label), h("pre", { class: "digest" }, d.markdown || "")));
    }
  }

  // ---------------------------------------------------------------- history

  async history() {
    const data = await api.get("/api/history", { limit: 300 });
    const panel = h("div", { class: "panel" }, h("h2", null, `Change history (${data.total} changes)`,
      noteMark("Newest first. The full log is changes.jsonl in the workspace folder.")));
    if (!data.events.length) {
      panel.append(h("p", { class: "dim" }, "No changes yet.", noteMark("Everything shown comes from the original packet.")));
    }
    else panel.append(h("ul", { class: "history" }, data.events.map((ev) => h("li", null,
      `${fmtTs(ev.ts)} ${ev.actor || "Someone"}: ${ev.op} `, ev.op.startsWith("contact.") ? h("button", { type: "button", class: "linklike", onclick: () => go({ sel: ev.id }) }, ev.label || ev.id) : (ev.label || ""),
      ev.batch ? h("span", { class: "dim" }, ` (import ${ev.batch})`) : null))));
    this.body.append(panel);
  }

  // ---------------------------------------------------------------- health

  async health() {
    const v = await api.get("/api/validation");
    const rep = v.packet;
    const boot = this.app.boot;
    const checks = h("ul", { class: "checks" }, rep.checks.map((c) => h("li", null,
      h("span", { class: c.ok ? "pass" : "fail" }, c.ok ? "Pass" : "Fail"), h("span", null, `${c.check}.`,
        !c.detail ? null : c.ok ? noteMark(c.detail) : [" ", h("span", { class: "dim" }, c.detail)]))));
    const counts = h("table", { class: "table" }, h("thead", null, h("tr", null, ["Count", "Loaded", "Manifest"].map((t) => h("th", null, t)))),
      h("tbody", null, Object.entries(rep.counts).filter(([, x]) => typeof x !== "object").map(([k, x]) => h("tr", null,
        h("td", null, k.replace(/_/g, " ")), h("td", { class: "num" }, String(x)),
        h("td", { class: "num" }, rep.expectedCounts && k in rep.expectedCounts ? String(rep.expectedCounts[k]) : "not stated")))));
    const roots = h("ul", { class: "works" }, rep.roots.map((r) => {
      const others = r.sameFirstNameCandidates.length - 1;
      return h("li", null, `${r.id}: ${r.found ? r.name : "missing"}`, others > 0 ? noteMark(`Resolved by ID; ${others} ` +
        `other ${others === 1 ? "person shares" : "people share"} the first name.`) : null);
    }));
    const c = boot.counts;
    const cov = boot.coverage;
    const seeds = boot.appliedSeeds || [];
    const covNote = cov && (cov.notes || seeds.length) ? noteMark(
      cov.notes ? h("span", { class: "note-line" }, cov.notes) : null,
      seeds.length ? h("span", { class: "note-line" }, "Migration provenance (applied seed releases): " + seeds.join(", ")) : null) : null;
    append(this.body, [
      h("div", { class: "panel" }, h("h2", null, "Original packet", noteMark("notebook.json SHA-256 ", h("code", null, rep.sha256),
        ` (${rep.bytes} bytes). It is checked on every start and never written to.`)),
        h("p", null, `${rep.datasetStatus || "No status in manifest"}${rep.savedAt ? `, saved ${rep.savedAt}` : ""}.`),
        checks),
      h("div", { class: "panel" }, h("h2", null, "Counts"), counts),
      h("div", { class: "panel" }, h("h2", null, "Lab members"), roots),
      h("div", { class: "panel" }, h("h2", null, "Current notebook"),
        h("p", null, v.current.ok ? "The current notebook, with all edits applied, passes the schema and data rules." :
          `The current notebook has ${v.current.schemaErrors.length + v.current.ruleIssues.length} problems.`),
        v.current.ok ? null : h("ul", null, [...v.current.schemaErrors.map((e) => `${e.path}: ${e.message}`),
          ...v.current.ruleIssues.map((i) => `${i.collection} ${i.id} ${i.field}: ${i.message}`)].slice(0, 50).map((t) => h("li", null, t))),
        h("p", { class: "dim" }, `${c.events} changes in this workspace; ${c.createdInWorkspace.contacts} people, ` +
          `${c.createdInWorkspace.edges} relationships and ${c.createdInWorkspace.meetings} notes added; ${c.editedRecords} records edited; ` +
          `${c.openConflicts} open conflicts; ${c.importBatches} imports.`),
        boot.workspace.quarantined.length ? h("p", { class: "notice" }, "An interrupted write was set aside at start-up: " + boot.workspace.quarantined.join(", ")) : null),
      cov ? h("div", { class: "panel" }, h("h2", null, "Coverage"),
        h("p", null, cov.scope, covNote),
        h("p", { class: "dim" }, `Status ${cov.status}; ${cov.sourcesReviewed} sources reviewed, ${cov.pendingSources} pending, ` +
          `${cov.unresolvedIdentities} unresolved names held separately.`)) : null,
      rep.limitations && rep.limitations.length ? h("div", { class: "panel" }, h("h2", null, "Known limits of the data"),
        h("ul", null, rep.limitations.map((t) => h("li", null, t)))) : null]);
  }
}

function collName(c) {
  return { contacts: "People", edges: "Relationships", meetings: "Notes", events: "Changes" }[c] || c;
}

function summarise(a) {
  const parts = [];
  const labels = [["created", "record added", "records added"], ["updated", "record updated", "records updated"],
    ["merged", "row merged into an existing person", "rows merged into existing people"],
    ["eventsApplied", "change applied", "changes applied"], ["conflictsOpened", "conflict opened", "conflicts opened"],
    ["unchanged", "row unchanged", "rows unchanged"], ["alreadyPresent", "change already present", "changes already present"],
    ["duplicates", "duplicate not added", "duplicates not added"], ["skipped", "row skipped", "rows skipped"],
    ["rejected", "row rejected", "rows rejected"]];
  for (const [k, one, many] of labels) {
    if (a[k]) parts.push(`${a[k]} ${a[k] === 1 ? one : many}`);
  }
  return parts.length ? parts.join(", ") + "." : "No changes.";
}

function short(v, n = 90) {
  if (v === null || v === undefined || v === "") return "empty";
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

/** The research library's state, and the commands that build it, update it and schedule the weekly update. */
function libraryPanel(lib) {
  const panel = h("div", { class: "panel" }, h("h2", null, "Research library"));
  const run = lib.lastRun;
  const cov = (run && run.coverage) || null;
  const n = (x) => Number(x || 0).toLocaleString();
  const totals = `${n(lib.papers)} papers, ${n(lib.nihAwards)} NIH and ${n(lib.nsfAwards)} NSF awards.`;
  if (lib.demo) {
    panel.append(h("p", null, "This is the fictional demo lab. Library updates, the weekly schedule and the live check " +
      "on profile pages are off for it.", noteMark("Its research library comes with the demo.")), h("p", null, totals));
    return panel;
  }
  const cmds = lib.commands || {};
  if (lib.sharedCopy) {
    panel.append(h("p", null, run ? `Last ${run.mode === "update" ? "updated" : "built"} ${String(run.date).slice(0, 10)}. ` : "",
      totals, noteMark("The lab's own copy of Lab Network keeps the library up to date, and this copy shows it as of " +
        "the day it was made.")));
    return panel;
  }
  if (!run) {
    panel.append(h("p", null, "The research library has not been built.", noteMark("It keeps each researcher's " +
      "OpenAlex publication record, recent papers, and NIH and NSF awards."), " Build it from a terminal; it sends names " +
      "to OpenAlex, NIH RePORTER and NSF:"), command(cmds.build),
    h("p", null, "Then turn on the weekly update. The command shows what it will install; run it again with --yes to " +
      "install it:"), command(cmds.weekly));
    return panel;
  }
  panel.append(h("p", null, `Last ${run.mode === "update" ? "updated" : "built"} ${String(run.date).slice(0, 10)}.`,
    lib.liveCheck ? noteMark("When you open a profile page, Lab Network also checks OpenAlex and NIH for anything newer.") : null,
    " " + (cov ? `${n(cov.openalexLinked)} of ${n(cov.people)} people matched to OpenAlex authors, with paper lists for ` +
      n(cov.openalexFetched) + (cov.openalexPending ? ` (${n(cov.openalexPending)} still to fetch)` : "") +
      (cov.openalexUnmatched ? `; ${n(cov.openalexUnmatched)} not matched` : "") + ". " : "") + totals));
  if (run.openalexStopped) {
    const [why, more] = /could not be reached/.test(run.openalexStopped) ? ["OpenAlex could not be reached during the last run.", ""] :
      /limit/.test(run.openalexStopped) ? ["The last run stopped at its OpenAlex spending limit.",
        "The limit keeps each run within OpenAlex's free daily allowance. "] :
        ["The last run stopped before OpenAlex's free daily allowance ran out.", ""];
    panel.append(h("p", { class: "dim" }, why, noteMark(more + "The next run continues where it stopped.")));
  }
  const on = String(lib.schedule || "").startsWith("On");
  if (on) {
    panel.append(h("p", null, `Weekly update: ${lib.schedule.replace(/^On: /, "")}.`, noteMark("It adds new papers and " +
      "awards and writes a digest, listed below."), " To turn it off:"), command(cmds.weeklyOff));
  } else {
    panel.append(h("p", null, "Weekly update: not scheduled" + (lib.schedule === "unknown" ? " (this computer's " +
      "scheduler could not be checked)" : "") + ". To check for new papers and awards every week, run this command. It " +
      "shows what it will install; run it again with --yes to install it. Change the day and time to suit you:"),
    command(cmds.weekly));
  }
  panel.append(h("p", null, "To update now:"), command(cmds.update));
  if (!lib.liveCheck) panel.append(h("p", { class: "dim" }, "The live check on profile pages is off."));
  return panel;
}

function command(text) {
  const copy = h("button", { type: "button", class: "btn btn-small btn-quiet" }, "Copy");
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied");
    } catch (err) {
      toast("Select the command and copy it.");
    }
  });
  return h("div", { class: "cmdrow" }, h("pre", { class: "cmd" }, text || ""), copy);
}

function fieldList(f) {
  const rows = [["Name", f.name], ["Institution", f.institution], ["Position", f.position], ["Relationship", f.relationship],
    ["When", f.when], ["Where", f.where], ["Notes", f.notes], ["Follow-up", f.followUp], ["Link", f.link]].filter(([, v]) => v);
  return h("dl", { class: "intake-fields" }, rows.flatMap(([k, v]) => [h("dt", null, k),
    h("dd", null, k === "Link" && /^https?:\/\//.test(v) ? extLink(v) : v)]));
}

function inboxMembers(v) {
  return h("div", { class: "panel" }, h("h2", null, `Lab members who can submit (${v.members.length})`,
    noteMark("Messages from anyone else in the channel are ignored. The list comes from the channel's members, " +
      "matched to the lab by name. To refresh it, stop the app and run the setup command again.")),
    v.members.length ? h("ul", null, v.members.map((m) => h("li", null, m.name +
      (m.slackName && m.slackName !== m.name ? ` (Slack: ${m.slackName}, ${m.slackId})` : ` (Slack ${m.slackId})`)))) :
      h("p", { class: "dim" }, "No one yet. Run the setup command again after lab members join the channel."));
}

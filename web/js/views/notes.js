// Notes view: every meeting note, with follow-ups that can be marked done.

import * as api from "../api.js";
import { on, state } from "../state.js";
import { clear, debounce, h, noteMark, rootForName } from "../ui.js";
import { noteForm } from "./forms.js";
import { noteCard } from "./profile.js";

export class NotesView {
  constructor(el, app) {
    this.el = el;
    this.app = app;
    this.notes = null;
    this.metBy = "";
    this.follow = "";
    this.text = "";
    on("changed", () => { this.notes = null; if (state.view === "notes") this.show(); });
    this.build();
  }

  build() {
    const roots = this.app.boot.lab.roots;
    this.metSel = h("select", { "aria-label": "Met by", onchange: (e) => { this.metBy = e.target.value; this.render(); } },
      h("option", { value: "" }, "Anyone in the lab"), roots.map((r) => h("option", { value: r.id }, r.name)),
      h("option", { value: "__other" }, "Someone else"));
    this.followSel = h("select", { "aria-label": "Follow-up", onchange: (e) => { this.follow = e.target.value; this.render(); } },
      h("option", { value: "" }, "All notes"), h("option", { value: "open" }, "Open follow-ups"), h("option", { value: "done" }, "Done follow-ups"));
    this.filter = h("input", { type: "search", placeholder: "Filter notes", "aria-label": "Filter notes",
      oninput: debounce((e) => { this.text = e.target.value; this.render(); }, 150) });
    this.list = h("div");
    this.summary = h("p", { class: "dim" });
    this.el.append(h("div", { class: "page-inner" },
      h("h1", { class: "page-title" }, "Notes"),
      h("p", { class: "page-lede" }, "Whom the lab met, how, what you discussed and what to do next.", noteMark("Notes are " +
        "firsthand or reported context; they keep their author and are never treated as public evidence.")),
      h("div", { class: "toolbar" }, this.filter, this.metSel, this.followSel,
        h("button", { type: "button", class: "btn btn-primary", onclick: () => noteForm(this.app, {}) }, "Add note")),
      this.summary, this.list));
  }

  async show() {
    if (!this.notes) {
      const data = await api.get("/api/notes");
      this.notes = data.notes;
    }
    this.render();
  }

  render() {
    if (!this.notes) return;
    const roots = this.app.boot.lab.roots;
    const q = this.text.trim().toLowerCase();
    const rows = this.notes.filter((m) => {
      const root = rootForName(roots, m.metBy);
      if (this.metBy === "__other" ? root : this.metBy && (!root || root.id !== this.metBy)) return false;
      if (this.follow && m.followUpStatus !== this.follow) return false;
      if (q && ![m.contactName, m.context, m.learning, m.followUp, m.metBy, m.recordedBy].join(" ").toLowerCase().includes(q)) return false;
      return true;
    });
    this.summary.textContent = `${rows.length} of ${this.notes.length} notes.`;
    clear(this.list);
    if (!rows.length) {
      this.list.append(h("div", { class: "panel" }, h("h2", null, this.notes.length ? "No notes match" : "No notes yet"),
        h("p", null, this.notes.length ? "Change the filters above." : "Add a note after you meet someone: who, how you met, what you discussed and what to follow up.")));
      return;
    }
    for (const m of rows) this.list.append(noteCard(m, this.app, { showPerson: true }));
  }
}
